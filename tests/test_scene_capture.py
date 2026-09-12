"""Image ownership, encoding and real Metal scene capture regressions."""

import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from _cocoa import _metal
from scene import Group, ImageData, Label, Layer, ParticleEmitter, Path as ShapePath, Rect, Scene, ShaderNode, Sprite, action, gpu


HAS_PILLOW = importlib.util.find_spec("PIL") is not None
HAS_NUMPY = importlib.util.find_spec("numpy") is not None


def pixel(image, x, y):
    offset = (y * image.width + x) * 4
    return tuple(image.rgba[offset:offset + 4])


class ImageDataTests(unittest.TestCase):
    def test_image_owns_immutable_packed_bytes(self):
        source = bytearray((255, 0, 0, 255, 0, 255, 0, 128))
        image = ImageData(1, 2, source)
        source[:] = b"\0" * 8
        self.assertEqual(image.size, (1, 2))
        self.assertEqual(pixel(image, 0, 0), (255, 0, 0, 255))
        self.assertEqual(pixel(image, 0, 1), (0, 255, 0, 128))
        with self.assertRaises(AttributeError):
            image.width = 4

    def test_dimensions_and_storage_are_validated(self):
        for width, height, data in ((0, 1, b""), (1, -1, b""), (2, 2, b"1234")):
            with self.subTest(size=(width, height)), self.assertRaises(ValueError):
                ImageData(width, height, data)
        with self.assertRaises(TypeError):
            ImageData(1.5, 1, b"1234")
        for args in ((0, 1, b""), (1, 1, b""), (sys.maxsize, 2, b"")):
            with self.assertRaises(ValueError):
                _metal.encode_png(*args)

    def test_png_encoding_needs_no_numpy_or_pillow(self):
        source = """
import importlib.abc
import sys
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('numpy', 'PIL'):
            raise AssertionError('Unexpected optional import: ' + fullname)
sys.meta_path.insert(0, BlockOptional())
from scene import ImageData
image = ImageData(1, 1, bytes((255, 0, 0, 128)))
assert image.to_png().startswith(b'\\x89PNG\\r\\n\\x1a\\n')
assert 'numpy' not in sys.modules and 'PIL' not in sys.modules
"""
        result = subprocess.run([sys.executable, "-I", "-c", source],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(HAS_PILLOW, "Pillow is optional; install it to verify decoded PNG pixels.")
    def test_png_and_pillow_preserve_colors_alpha_and_row_order(self):
        from PIL import Image

        image = ImageData(2, 2, bytes((255, 0, 0, 255, 0, 255, 0, 128,
                                     0, 0, 255, 64, 12, 34, 56, 0)))
        with Image.open(io.BytesIO(image.to_png())) as decoded:
            self.assertEqual(decoded.size, image.size)
            self.assertEqual(decoded.convert("RGBA").tobytes(), image.rgba)
        with image.to_pil() as converted:
            self.assertEqual(converted.tobytes(), image.rgba)
            converted.putpixel((0, 0), (0, 0, 0, 0))
        self.assertEqual(pixel(image, 0, 0), (255, 0, 0, 255))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image.png"
            image.save(path)
            with Image.open(path) as decoded:
                self.assertEqual(decoded.convert("RGBA").tobytes(), image.rgba)
            invalid = path.with_suffix(".jpg")
            with self.assertRaises(ValueError):
                image.save(invalid)
            self.assertFalse(invalid.exists())

    @unittest.skipUnless(HAS_NUMPY, "NumPy is optional.")
    def test_numpy_shape_copy_and_view_lifetimes(self):
        image = ImageData(2, 1, bytes((1, 2, 3, 4, 5, 6, 7, 8)))
        copied = image.to_numpy()
        view = image.to_numpy(copy=False)
        self.assertEqual(copied.shape, (1, 2, 4))
        self.assertEqual(str(copied.dtype), "uint8")
        copied[0, 0] = 0
        self.assertEqual(view[0, 0].tolist(), [1, 2, 3, 4])
        self.assertFalse(view.flags.writeable)
        del image
        self.assertEqual(view[0, 1].tolist(), [5, 6, 7, 8])

    def test_capture_requires_a_running_scene(self):
        for node in (Scene(), Group()):
            with self.assertRaisesRegex(RuntimeError, "running scene"):
                node.capture(rect=(0, 0, 10, 10))
            node.close()


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1",
                     "Set COCOA_PY_UI_TESTS=1 for desktop Metal capture tests.")
class SceneCaptureTests(unittest.TestCase):
    def setUp(self):
        self.window = gpu.Window("cocoa-py image capture validation")
        self.addCleanup(self.window.close)
        self.scene = Scene()
        self.scene._init(self.window, "#112233")
        self.addCleanup(self.scene._close)

    def capture(self, rect=(0, 0, 80, 40), size=(80, 40), **kwargs):
        return self.scene.capture(rect=rect, size=size, **kwargs)

    def test_scene_crop_scale_channels_and_top_left_origin(self):
        self.scene.add(Rect(20, 20, x=10, y=10, fill="#ff0000"),
                       Rect(20, 20, x=70, y=30, fill="#0000ff"))
        image = self.capture()
        self.assertEqual(pixel(image, 10, 10), (255, 0, 0, 255))
        self.assertEqual(pixel(image, 70, 30), (0, 0, 255, 255))
        self.assertEqual(pixel(image, 40, 20), (17, 34, 51, 255))
        cropped = self.capture(rect=(60, 20, 20, 20), size=(30, 50))
        self.assertEqual(cropped.size, (30, 50))
        self.assertEqual(pixel(cropped, 15, 25), (0, 0, 255, 255))

    def test_default_scene_size_uses_display_scale(self):
        image = self.scene.capture()
        self.assertEqual(image.size, tuple(round(v * self.window.scale) for v in self.window.size))
        self.assertEqual(pixel(image, 0, 0), (17, 34, 51, 255))

    def test_camera_is_applied_to_scene_capture(self):
        self.scene.camera.position = (self.scene.width / 2 + 20, self.scene.height / 2)
        self.scene.add(Rect(10, 10, x=30, y=10, fill="#ff0000"))
        image = self.capture()
        self.assertEqual(pixel(image, 10, 10), (255, 0, 0, 255))
        self.assertEqual(pixel(image, 30, 10), (17, 34, 51, 255))

    def test_node_capture_excludes_ancestors_camera_and_siblings(self):
        parent = Group(x=180, y=160, scale=1.5, rotation=23, opacity=0.25)
        board = Group(x=30, y=40, scale=2, rotation=-11, opacity=0.5)
        ink = Rect(20, 20, x=10, y=10, fill="#ff0000")
        board.add(ink)
        parent.add(board)
        self.scene.add(parent, Rect(1000, 1000, fill="#0000ff", z=99))
        self.scene.camera.zoom = 1.3
        self.scene._render()
        original = (board._world_transform, ink._world_transform, ink._world_opacity)
        image = board.capture(rect=(0, 0, 40, 30), size=(40, 30))
        self.assertEqual(pixel(image, 10, 10), (255, 0, 0, 128))
        self.assertEqual(pixel(image, 35, 25), (0, 0, 0, 0))
        self.assertEqual((board._world_transform, ink._world_transform, ink._world_opacity), original)
        self.scene._render()
        self.assertEqual((board._world_transform, ink._world_transform, ink._world_opacity), original)

    def test_translucent_background_and_vector_paths(self):
        path = ShapePath(fill=None, stroke="#ff000080", stroke_width=10,
                         cap="round", join="round").move_to(10, 20).line_to(70, 20)
        self.scene.add(path)
        image = self.capture(background=(0, 0, 1, 0.5))
        self.assertEqual(pixel(image, 40, 2), (0, 0, 255, 128))
        center = pixel(image, 40, 20)
        self.assertAlmostEqual(center[0], 170, delta=2)
        self.assertAlmostEqual(center[2], 85, delta=2)
        self.assertAlmostEqual(center[3], 192, delta=1)

    def test_capture_does_not_tick_or_change_input_coordinates(self):
        label = Label("Capture", x=60, y=60)
        self.scene.add(label)
        label.run_action(action.move_by(100, 0, 1, easing=action.linear))
        self.scene._frame(0.1)
        self.scene._touch_owners[123] = label
        before = (self.scene.frame, self.scene.elapsed, self.scene.dt, label.position,
                  label._world_transform, label._rendered_size, self.scene.camera.position)
        self.capture(rect=(20, 30, 100, 80), size=(50, 40))
        after = (self.scene.frame, self.scene.elapsed, self.scene.dt, label.position,
                 label._world_transform, label._rendered_size, self.scene.camera.position)
        self.assertEqual(after, before)
        self.assertIs(self.scene._touch_owners[123], label)
        self.scene._frame(0.1)
        self.assertAlmostEqual(label.x, 80)

    def test_repeated_capture_releases_temporary_gpu_resources(self):
        self.scene.add(Rect(30, 20, x=25, y=15, fill="#ff0000"))
        self.capture(size=(47, 31))
        before = gpu.resource_counts()
        for _ in range(12):
            image = self.capture(size=(47, 31))
        after = gpu.resource_counts()
        for key in ("buffers", "textures", "active_frames", "active_blit_passes", "offscreen_command_buffers"):
            self.assertEqual(after[key], before[key], key)
        self.scene._close()
        self.window.close()
        self.assertEqual(image.size, (47, 31))
        self.assertTrue(image.to_png().startswith(b"\x89PNG"))

    def test_capture_failure_restores_renderer_and_world_state(self):
        rectangle = Rect(20, 20, x=30, y=20)
        self.scene.add(rectangle)
        self.scene._render()
        renderer = self.scene._renderer
        before = (rectangle._world_transform, renderer.screen_scale, renderer._active_frame_slot)
        resources = gpu.resource_counts()
        with patch.object(renderer, "render_packed", side_effect=RuntimeError("injected render failure")):
            with self.assertRaisesRegex(RuntimeError, "injected render failure"):
                self.capture(rect=(20, 10, 20, 20))
        self.assertEqual((rectangle._world_transform, renderer.screen_scale, renderer._active_frame_slot), before)
        self.assertEqual(gpu.resource_counts()["textures"], resources["textures"])
        self.assertFalse(renderer._capturing)
        self.assertEqual(self.capture().size, (80, 40))

    def test_invalid_parameters_and_active_passes_raise_python_exceptions(self):
        for rect in ((0, 0, 0, 1), (0, 0, float("nan"), 1), (1, 2, 3)):
            with self.assertRaises(ValueError):
                self.capture(rect=rect)
        for size in ((0, 1), (1.5, 2), (16385, 1), (16384, 16384)):
            with self.assertRaises(ValueError):
                self.capture(size=size)
        board = Group()
        self.scene.add(board)
        with self.assertRaisesRegex(ValueError, "requires rect"):
            board.capture()
        texture = gpu.Texture.render_target(8, 8)
        self.addCleanup(texture.close)
        with self.window.frame(target_texture=texture):
            with self.assertRaisesRegex(RuntimeError, "between"):
                self.capture()
        self.assertEqual(self.capture().size, (80, 40))

    def test_texture_readback_handles_odd_rows_alpha_formats_and_lifetime(self):
        for format in ("bgra8", "rgba8"):
            with self.subTest(format=format):
                texture = gpu.Texture.render_target(7, 3, format=format)
                try:
                    with self.window.frame(clear_color=(0.25, 0.125, 0, 0.5), target_texture=texture):
                        pass
                    raw = texture.to_image(self.window, unpremultiply=False)
                    straight = texture.to_image(self.window)
                    self.assertEqual(pixel(raw, 6, 2), (64, 32, 0, 128))
                    self.assertEqual(pixel(straight, 6, 2), (128, 64, 0, 128))
                finally:
                    texture.close()
                with self.assertRaises(RuntimeError):
                    texture.to_image(self.window)
                self.assertEqual(len(straight.rgba), 7 * 3 * 4)
        unsupported = gpu.Texture.render_target(8, 8, format="r8")
        self.addCleanup(unsupported.close)
        with self.assertRaisesRegex(RuntimeError, "bgra8 and rgba8"):
            unsupported.to_image(self.window)

    def test_particle_capture_keeps_time_and_maps_crop_coordinates(self):
        particles = ParticleEmitter(rate=0, max_particles=1, speed=0, gravity=(0, 0),
                                    lifetime=10, size=(5, 5), size_over_life=(1, 1),
                                    opacity_over_life=(1, 1), colors=["#00ff00"], x=40, y=20)
        self.scene.add(particles)
        self.scene._render()
        particles.emit(1)
        self.scene._frame(0.1)
        before = (particles._time, particles._cursor, particles._world_transform)
        image = self.capture(rect=(30, 10, 20, 20), size=(40, 40), background="#000000")
        self.assertEqual(pixel(image, 20, 20), (0, 255, 0, 255))
        local = particles.capture(rect=(-10, -10, 20, 20), size=(20, 20), background="#000000")
        self.assertEqual(pixel(local, 10, 10), (0, 255, 0, 255))
        self.assertEqual((particles._time, particles._cursor, particles._world_transform), before)

    def test_cached_layer_capture_restores_live_texture_and_dirty_state(self):
        layer = Layer(x=40, y=20)
        layer.add(Rect(20, 10, fill="#00ff00"))
        self.scene.add(layer)
        self.scene._render()
        original = (layer._tex, layer._tex.handle, layer._lsize, layer._rscale, layer._dirty)
        for size in ((160, 80), (40, 20), (240, 120)):
            image = self.capture(size=size)
            self.assertEqual(pixel(image, size[0] // 2, size[1] // 2), (0, 255, 0, 255))
            self.assertEqual((layer._tex, layer._tex.handle, layer._lsize, layer._rscale, layer._dirty), original)
        layer.invalidate()
        self.capture()
        self.assertTrue(layer._dirty)

    def test_png_texture_roundtrip_and_sprite_capture(self):
        source = ImageData(2, 2, bytes((255, 0, 0, 255, 0, 255, 0, 255,
                                      0, 0, 255, 255, 255, 255, 0, 255)))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "colors.png"
            source.save(path)
            texture = gpu.Texture.from_file(path)
            self.addCleanup(texture.close)
            self.assertEqual(texture.to_image(self.window).rgba, source.rgba)
            sprite = Sprite(texture, x=10, y=10)
            self.scene.add(sprite)
            image = sprite.capture(rect=(-1, -1, 2, 2), size=(2, 2))
            self.assertEqual(image.rgba, source.rgba)

    def test_custom_shader_capture_does_not_advance_shader_time(self):
        source = """
#include <metal_stdlib>
using namespace metal;
vertex float4 capture_vertex(uint id [[vertex_id]]) {
    constexpr float2 positions[] = {{-1, -1}, {3, -1}, {-1, 3}};
    return float4(positions[id], 0, 1);
}
fragment float4 capture_fragment() { return float4(0.25, 0, 0.5, 0.5); }
"""
        shader = ShaderNode(source=source, size=(20, 20), draw_count=3,
                            vertex="capture_vertex", fragment="capture_fragment", x=30, y=20)
        self.scene.add(shader)
        self.scene._frame(0.1)
        before = (shader.time, shader._frame_count)
        image = shader.capture(rect=(-10, -10, 20, 20), size=(20, 20))
        self.assertEqual(pixel(image, 10, 10), (128, 0, 255, 128))
        self.assertEqual((shader.time, shader._frame_count), before)


if __name__ == "__main__":
    unittest.main()
