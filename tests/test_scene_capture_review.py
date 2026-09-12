"""Regressions for capture resolution, intermediate allocation and ownership."""

import math
import os
import unittest
from unittest.mock import patch

from _cocoa import _metal
from scene import Label, Layer, Rect, Scene, ShaderNode, gpu


SHADER = """
#include <metal_stdlib>
using namespace metal;
vertex float4 image_vertex(uint id [[vertex_id]]) {
    constexpr float2 positions[] = {{-1, -1}, {3, -1}, {-1, 3}};
    return float4(positions[id], 0, 1);
}
fragment float4 image_fragment() { return float4(1, 0, 0, 1); }
"""


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1",
                     "Set COCOA_PY_UI_TESTS=1 for desktop Metal capture tests.")
class CaptureReviewTests(unittest.TestCase):
    def setUp(self):
        self.window = gpu.Window("Capture regression validation")
        self.addCleanup(self.window.close)
        self.scene = Scene()
        self.scene._init(self.window, "#000000")
        self.addCleanup(self.scene._close)

    def test_high_magnification_crop_never_requests_oversized_intermediate(self):
        layer = Layer()
        layer.add(Rect(200, 200, fill="#ffffff"))
        self.scene.add(layer)
        rejected = []
        allocate = gpu.Texture.render_target

        def bounded_allocate(width, height, *args, **kwargs):
            if max(width, height) > 16384 or width * height > 64 * 1024 * 1024:
                rejected.append((width, height))
                # Stop before asking Metal to allocate unsafe descriptors.
                raise ValueError("Test blocked an oversized intermediate texture.")
            return allocate(width, height, *args, **kwargs)

        with patch.object(gpu.Texture, "render_target", side_effect=bounded_allocate):
            image = self.scene.capture(rect=(0, 0, 10, 10), size=(1000, 1000))
        self.assertEqual(rejected, [], f"Unsafe intermediate requests: {rejected}")
        self.assertEqual(image.size, (1000, 1000))
        self.assertEqual(image.rgba[:4], b"\xff\xff\xff\xff")

    def test_first_shader_capture_preserves_live_initialization_resolution(self):
        shader = ShaderNode(source=SHADER, size=(20, 20), draw_count=3,
                            vertex="image_vertex", fragment="image_fragment")
        self.scene.add(shader)
        shader.capture(rect=(-10, -10, 20, 20), size=(10, 10))
        self.scene._render()
        expected = tuple(round(v * self.window.scale) for v in shader.shader_size)
        self.assertEqual(shader.texture.size, expected,
                         "Capture resolution leaked into the live shader texture.")

    def test_thumbnail_keeps_text_proportional_to_the_scene(self):
        self.scene.add(Label("MMMM", size=20, x=100, y=100))
        large = self.scene.capture(rect=(0, 0, 200, 200), size=(200, 200))
        small = self.scene.capture(rect=(0, 0, 200, 200), size=(20, 20))

        def ink_width(image):
            columns = [x for y in range(image.height) for x in range(image.width)
                       if image.rgba[(y * image.width + x) * 4] > 32]
            return max(columns) - min(columns) + 1 if columns else 0

        expected = math.ceil(ink_width(large) / 10)
        actual = ink_width(small)
        self.assertLessEqual(actual, expected + 2,
                             f"Thumbnail text is {actual}px wide; expected about {expected}px.")
        self.assertGreater(actual, 0)

    def test_sibling_layers_use_the_same_capture_resolution(self):
        layers = [Layer(x=25, y=20), Layer(x=75, y=20)]
        for layer in layers:
            layer.add(Rect(20, 20, fill="#ffffff"))
        self.scene.add(*layers)
        rebuild = Layer._rebuild
        recorded = []

        def record_rebuild(layer, renderer, scale):
            requested = renderer.screen_scale
            rebuild(layer, renderer, scale)
            recorded.append((requested, layer._tex.size))

        with patch.object(Layer, "_rebuild", record_rebuild):
            self.scene.capture(rect=(0, 0, 100, 40), size=(400, 160))
        self.assertEqual(recorded, [(4.0, (128, 128)), (4.0, (128, 128))])

    def test_varying_capture_sizes_do_not_accumulate_live_glyph_atlases(self):
        self.scene.add(Label("Capture", size=20, x=50, y=50))
        self.scene._render()
        renderer = self.scene._renderer
        before_keys = set(renderer._ga)
        before_textures = gpu.resource_counts()["textures"]
        for size in (50, 60, 70, 80, 90):
            self.scene.capture(rect=(0, 0, 100, 100), size=(size, size))
        self.assertEqual(set(renderer._ga), before_keys,
                         "Capture-specific font sizes remain in the live glyph cache.")
        self.assertEqual(gpu.resource_counts()["textures"], before_textures)

    def test_failed_label_redraw_releases_the_previous_cached_texture(self):
        label = Label("Before", size=20, x=50, y=50)
        self.scene.add(label)
        self.scene._render()
        renderer = self.scene._renderer
        renderer._tc.clear()
        before = gpu.resource_counts()["textures"]
        label.text = "After"
        with patch.object(renderer, "text_texture", side_effect=ValueError("raster failure")):
            with self.assertRaisesRegex(ValueError, "raster failure"):
                self.scene._render()
        self.scene.remove(label)
        label.close()
        self.assertEqual(gpu.resource_counts()["textures"], before - 1)

    def test_failed_capture_drains_queued_text_rendering(self):
        self.scene.add(Label("Queued text", size=20, x=50, y=50), Layer())
        self.scene.children[-1].add(Rect(20, 20))
        before = gpu.resource_counts()
        with patch.object(Layer, "_rebuild", side_effect=ValueError("layer failure")):
            with self.assertRaisesRegex(ValueError, "layer failure"):
                self.scene.capture(rect=(0, 0, 100, 100), size=(100, 100))
        after = gpu.resource_counts()
        for key in ("textures", "buffers", "offscreen_command_buffers"):
            self.assertEqual(after[key], before[key], key)
        self.assertFalse(self.scene._renderer._capturing)

    def test_nested_rotated_layers_support_subpoint_crops(self):
        outer = Layer(x=50, y=50, rotation=30)
        inner = Layer(x=20, rotation=-20, scale=1.2)
        inner.add(Rect(200, 200, fill="#ffffff"))
        outer.add(inner)
        self.scene.add(outer)
        center_x = 50 + 20 * math.cos(math.radians(30))
        center_y = 50 + 20 * math.sin(math.radians(30))
        image = self.scene.capture(rect=(center_x - 0.05, center_y - 0.05, 0.1, 0.1), size=(100, 80))
        self.assertEqual(image.rgba, b"\xff" * (100 * 80 * 4))
        self.assertIsNone(outer._tex)
        self.assertIsNone(inner._tex)

    def test_cold_shader_dependencies_are_restored_after_capture(self):
        source = ShaderNode(source=SHADER, size=(20, 20), draw_count=3,
                            vertex="image_vertex", fragment="image_fragment")
        target = ShaderNode(source=SHADER, size=(20, 20), draw_count=3,
                            vertex="image_vertex", fragment="image_fragment")
        self.addCleanup(source.close)
        target.set_texture(source, 0)
        self.scene.add(target)
        before = gpu.resource_counts()
        target.capture(rect=(-10, -10, 20, 20), size=(10, 10))
        for shader in (source, target):
            self.assertIsNone(shader._tex)
            self.assertIsNone(shader._pipe)
            self.assertIsNone(shader._renderer)
        after = gpu.resource_counts()
        for key in ("textures", "buffers", "pipelines", "libraries"):
            self.assertEqual(after[key], before[key], key)

    def test_native_limits_reject_unsafe_descriptors_without_allocating(self):
        before = gpu.resource_counts()
        for width, height in ((16385, 1), (8193, 8192)):
            with self.assertRaises(ValueError):
                _metal.create_render_texture(width=width, height=height)
        with self.assertRaises(ValueError):
            _metal.create_glyph_atlas(font_name=None, font_size=1024,
                                      chars=''.join(chr(code) for code in range(32, 127)))
        self.assertEqual(gpu.resource_counts()["textures"], before["textures"])

    def test_oversized_lazy_shader_raises_and_rolls_back_gpu_state(self):
        shader = ShaderNode(source=SHADER, size=(200, 200), draw_count=3,
                            vertex="image_vertex", fragment="image_fragment")
        self.scene.add(shader)
        before = gpu.resource_counts()
        with self.assertRaisesRegex(ValueError, "Texture dimensions"):
            shader.capture(rect=(0, 0, 10, 10), size=(1000, 1000))
        self.assertIsNone(shader._tex)
        self.assertIsNone(shader._pipe)
        after = gpu.resource_counts()
        for key in ("textures", "buffers", "pipelines", "libraries"):
            self.assertEqual(after[key], before[key], key)

    def test_extreme_label_magnification_raises_before_font_allocation(self):
        self.scene.add(Label("Capture", size=20))
        before = gpu.resource_counts()
        with self.assertRaisesRegex(ValueError, "Label raster font size"):
            self.scene.capture(rect=(0, 0, 1e-10, 1e-10), size=(1, 1))
        after = gpu.resource_counts()
        for key in ("textures", "buffers", "offscreen_command_buffers"):
            self.assertEqual(after[key], before[key], key)


if __name__ == "__main__":
    unittest.main()
