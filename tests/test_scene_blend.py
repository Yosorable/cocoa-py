"""Alpha and additive compositing must survive batching, clipping, and caches."""

import os
from itertools import product
from pathlib import Path
import tempfile
import unittest

from scene import Group, ImageData, ParticleEmitter, Path as ShapePath, Scene, Sprite, gpu


def pixel(image, x, y):
    offset = (y * image.width + x) * 4
    return tuple(image.rgba[offset:offset + 4])


class BlendValidationTests(unittest.TestCase):
    def test_particle_mode_validation_does_not_silently_ignore_a_typo(self):
        with self.assertRaises(ValueError):
            ParticleEmitter(blend="addition")
        emitter = ParticleEmitter()
        self.addCleanup(emitter.close)
        self.assertEqual(emitter.blend, "alpha")
        emitter.blend = "additive"
        self.assertEqual(emitter.blend, "additive")


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1", "Requires a desktop Metal window.")
class SceneBlendTests(unittest.TestCase):
    def setUp(self):
        self.window = gpu.Window("cocoa-py blend validation")
        self.addCleanup(self.window.close)
        self.scene = Scene()
        self.scene._init(self.window, "#14283c")
        self.addCleanup(self.scene._close)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "red.png")
        ImageData(1, 1, bytes((100, 0, 0, 128))).save(self.path)

    def capture(self):
        return self.scene.capture(rect=(0, 0, 60, 30), size=(60, 30))

    def assert_color(self, image, xy, expected):
        for actual, want in zip(pixel(image, *xy), expected):
            self.assertLessEqual(abs(actual - want), 2)

    def test_sprite_modes_split_batches_and_invalidate_native_and_python_caches(self):
        class CustomSprite(Sprite):
            pass

        for sprite_type in (Sprite, CustomSprite):
            with self.subTest(sprite_type=sprite_type.__name__):
                self.scene.clear()
                group = Group(clip=(0, 0, 50, 30))
                alpha = sprite_type(self.path, size=(20, 20), x=10, y=10)
                additive = sprite_type(self.path, size=(30, 20), x=40, y=10, blend="additive")
                group.add(alpha, additive)
                self.scene.add(group)
                captured = self.capture()
                self.assert_color(captured, (10, 10), (60, 20, 30, 255))
                self.assert_color(captured, (40, 10), (70, 40, 60, 255))
                self.assert_color(captured, (53, 10), (20, 40, 60, 255))
                additive.blend = "alpha"
                self.assert_color(self.capture(), (40, 10), (60, 20, 30, 255))
                with self.assertRaises(ValueError):
                    additive.blend = "unknown"

    def test_particle_mode_changes_actual_compositing(self):
        emitter = ParticleEmitter(rate=0, max_particles=1, speed=0, gravity=0,
                                  lifetime=100, colors=[(100 / 255, 0, 0, .5)],
                                  size=20, size_over_life=(1, 1), opacity_over_life=(1, 1),
                                  x=20, y=15)
        self.scene.add(emitter)
        self.scene._render()
        emitter.emit(1)
        self.scene._frame(.01)
        alpha = pixel(self.capture(), 20, 15)
        emitter.blend = "additive"
        additive = pixel(self.capture(), 20, 15)
        self.assertGreater(additive[1], alpha[1] + 10)
        self.assertGreater(additive[2], alpha[2] + 15)
        self.assertEqual(additive[3], 255)

    def test_textured_particles_preserve_premultiplied_color(self):
        rgba = (192, 128, 64, 128)
        path = Path(self.path).with_name("particle.png")
        ImageData(1, 1, bytes(rgba)).save(path)
        texture = gpu.Texture.from_file(path)
        self.addCleanup(texture.close)
        appearances = (((1, 1, 1, 1), 1), ((.5, .5, 1, .5), .5))
        for blend, clipped, msaa, (color, opacity) in product(
                ("alpha", "additive"), (False, True), (False, True), appearances):
            with self.subTest(blend=blend, clipped=clipped, msaa=msaa, color=color, opacity=opacity):
                group = Group(clip=(0, 0, 25, 30) if clipped else None)
                emitter = ParticleEmitter(rate=0, max_particles=1, speed=0, gravity=0,
                                          lifetime=100, colors=[color], texture=texture,
                                          size=20, size_over_life=(1, 1), opacity_over_life=(1, 1),
                                          x=20, y=15, opacity=opacity, blend=blend)
                group.add(emitter)
                self.scene.add(group)
                mesh = None
                if msaa:
                    # A separate vector draw selects the multisampled scene pass.
                    mesh = ShapePath(fill="#ffffff").move_to(50, 24).line_to(58, 24).line_to(58, 29).close_path()
                    self.scene.add(mesh)
                try:
                    self.scene._render()
                    emitter.emit(1)
                    self.scene._frame(.01)
                    alpha = rgba[3] / 255 * color[3] * opacity
                    background_factor = 1 - alpha if blend == "alpha" else 1
                    expected = tuple(round(rgba[i] * color[i] * alpha + bg * background_factor)
                                     for i, bg in enumerate((20, 40, 60))) + (255,)
                    opaque = self.capture()
                    self.assert_color(opaque, (20, 15), expected)
                    transparent = self.scene.capture(rect=(0, 0, 60, 30), size=(60, 30), background=(0, 0, 0, 0))
                    self.assert_color(transparent, (20, 15),
                                      tuple(round(rgba[i] * color[i]) for i in range(3)) + (round(alpha * 255),))
                    if clipped:
                        self.assert_color(opaque, (30, 15), (20, 40, 60, 255))
                        self.assert_color(transparent, (30, 15), (0, 0, 0, 0))
                finally:
                    group.close()
                    if mesh is not None:
                        mesh.close()

    def test_gpu_pipeline_rejects_unknown_compositing(self):
        library = gpu.Library("__default__")
        self.addCleanup(library.close)
        with self.assertRaisesRegex(ValueError, "blend_mode"):
            gpu.Pipeline(library, vertex="quad_vertex", fragment="tex_frag", blend_mode="invalid")


if __name__ == "__main__":
    unittest.main()
