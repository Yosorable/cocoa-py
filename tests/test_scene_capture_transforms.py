"""Capture regressions for Layer composition, tile culling and small transforms."""

import math
import os
import unittest
from unittest.mock import patch

from scene import Group, Layer, Rect, Scene, TileMap, TileSet, gpu


WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)
GREEN = (0, 255, 0, 255)


def pixel(image, x, y):
    offset = (y * image.width + x) * 4
    return tuple(image.rgba[offset:offset + 4])


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1",
                     "Set COCOA_PY_UI_TESTS=1 for desktop Metal capture tests.")
class CaptureTransformTests(unittest.TestCase):
    def setUp(self):
        self.window = gpu.Window("Capture transform validation")
        self.addCleanup(self.window.close)
        self.scene = Scene()
        self.scene._init(self.window, "#000000")
        self.addCleanup(self.scene._close)

    def normal_image(self):
        width, height = self.window.size
        texture = gpu.Texture.render_target(round(width * self.window.scale),
                                            round(height * self.window.scale))
        try:
            self.scene._render(target_texture=texture)
            return texture.to_image(self.window)
        finally:
            texture.close()

    def test_nonuniform_layer_crop_matches_normal_render(self):
        layer = Layer(scale=(2, 1))
        layer.add(Rect(200, 200, fill="#ffffff"))
        self.scene.add(layer)
        normal = self.normal_image()
        scale = self.window.scale
        for x in (121, 130, 139):
            self.assertEqual(pixel(normal, round(x * scale), round(10 * scale)), WHITE)
        image = self.scene.capture(rect=(120, 0, 20, 20), size=(200, 200))
        self.assertEqual(image.rgba, bytes(WHITE) * (200 * 200))

    def test_nonuniform_layer_crop_preserves_offset_content(self):
        layer = Layer(scale=(2, 1))
        layer.add(Rect(40, 40, x=100, y=50, fill="#ffffff"))
        self.scene.add(layer)
        image = self.scene.capture(rect=(205, 40, 20, 20), size=(200, 200))
        self.assertEqual(image.rgba, bytes(WHITE) * (200 * 200))

    def test_nested_layer_crops_match_normal_render_with_affine_transforms(self):
        outer = Layer(x=240, y=200, rotation=math.radians(15), scale=(1.25, 0.75))
        inner = Layer(x=30, y=10, rotation=math.radians(25))
        inner.add(Rect(80, 60, fill="#ff0000"), Rect(20, 60, x=20, fill="#00ff00"))
        outer.add(inner)
        self.scene.add(outer)
        scale = self.window.scale
        colors = {BLACK, (255, 0, 0, 255), GREEN}
        for transform in ((2, 1), (-2, 1), (0, 1), (0, 0)):
            with self.subTest(scale=transform):
                inner.scale = transform
                inner.invalidate()
                outer.invalidate()
                normal = self.normal_image()
                image = self.scene.capture(rect=(160, 120, 160, 160),
                                           size=(round(160 * scale), round(160 * scale)))
                colored_samples = 0
                for y in range(4, 160, 8):
                    for x in range(4, 160, 8):
                        expected = pixel(normal, round((x + 160) * scale), round((y + 120) * scale))
                        if expected not in colors:
                            continue
                        actual = pixel(image, round(x * scale), round(y * scale))
                        for got, want in zip(actual, expected):
                            self.assertAlmostEqual(got, want, delta=2, msg=(transform, x, y, expected, actual))
                        colored_samples += expected != BLACK
                if transform != (0, 0):
                    self.assertGreater(colored_samples, 5)

    def tilemap(self):
        texture = gpu.Texture.render_target(16, 16)
        self.addCleanup(texture.close)
        with self.window.frame(clear_color=(0, 1, 0, 1), target_texture=texture):
            pass
        tileset = TileSet(texture, (16, 16))
        self.addCleanup(tileset.close)
        width, height = self.window.size
        tilemap = TileMap(tileset, 2 * math.ceil(width / 16), 2 * math.ceil(height / 16))
        tilemap.fill(0, [0] * (tilemap.columns * tilemap.rows))
        return tilemap

    def assert_full_map(self, node, tilemap):
        size = (tilemap.columns * 4, tilemap.rows * 4)
        image = node.capture(rect=(0, 0, tilemap.map_width, tilemap.map_height), size=size,
                             background="#000000")
        # Sample every tile's interior. Layer resampling can filter the outer
        # image edge, which is independent of missing off-window tiles.
        for row in range(tilemap.rows):
            for column in range(tilemap.columns):
                self.assertEqual(pixel(image, column * 4 + 2, row * 4 + 2), GREEN,
                                 (column, row))

    def test_full_tilemap_capture_includes_tiles_outside_window(self):
        tilemap = self.tilemap()
        self.scene.add(tilemap)
        self.assert_full_map(tilemap, tilemap)

    def test_layer_tilemap_capture_uses_nested_viewport(self):
        tilemap = self.tilemap()
        layer = Layer(x=20, y=40)
        layer.add(tilemap)
        self.scene.add(layer)
        self.assert_full_map(layer, tilemap)

    def test_local_capture_ignores_small_and_large_valid_scales(self):
        group = Group()
        group.add(Rect(12, 12, x=10, y=10, fill="#ffffff"))
        self.scene.add(group)
        expected = group.capture(rect=(0, 0, 20, 20), size=(20, 20), background="#000000")
        self.assertEqual(pixel(expected, 10, 10), WHITE)
        for scale in (1e-7, 1e-200, 1e200, (1e-200, 1e200)):
            with self.subTest(scale=scale):
                group.scale = scale
                image = group.capture(rect=(0, 0, 20, 20), size=(20, 20), background="#000000")
                self.assertEqual(image.rgba, expected.rgba)

    def test_unrepresentable_capture_inverse_raises_before_allocating(self):
        group = Group()
        group.add(Rect(12, 12, x=10, y=10))
        self.scene.add(group)
        before = gpu.resource_counts()
        for scale in (0, (0, 1), 1e-320, float("nan")):
            with self.subTest(scale=scale):
                group.scale = scale
                with self.assertRaises(ValueError):
                    group.capture(rect=(0, 0, 20, 20), size=(20, 20))
                self.assertEqual(gpu.resource_counts(), before)

    def test_failed_capture_restores_layer_center_and_live_texture(self):
        layer = Layer(x=40, y=40, scale=(2, 1))
        layer.add(Rect(100, 100, fill="#ffffff"))
        self.scene.add(layer)
        self.normal_image()
        renderer = self.scene._renderer
        before = (layer._tex, layer._lsize, layer._lcenter, layer._rscale, layer._dirty,
                  getattr(layer, "_capture_center", None))
        counts = gpu.resource_counts()
        with patch.object(renderer, "render_packed", side_effect=RuntimeError("capture failure")):
            with self.assertRaisesRegex(RuntimeError, "capture failure"):
                self.scene.capture(rect=(20, 20, 20, 20), size=(100, 100))
        self.assertEqual((layer._tex, layer._lsize, layer._lcenter, layer._rscale, layer._dirty,
                          getattr(layer, "_capture_center", None)), before)
        for key in ("textures", "buffers", "offscreen_command_buffers"):
            self.assertEqual(gpu.resource_counts()[key], counts[key], key)


if __name__ == "__main__":
    unittest.main()
