"""Cached layers preserve the same affine geometry as their uncached content."""

import os
import unittest

from scene import Group, Layer, Scene, Sprite, gpu
from scene._common import _IDENTITY, _apply, _mul


def pixel(image, x, y):
    offset = (y * image.width + x) * 4
    return tuple(image.rgba[offset:offset + 4])


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1",
                     "Set COCOA_PY_UI_TESTS=1 for desktop Metal layer tests.")
class LayerAffineTests(unittest.TestCase):
    def setUp(self):
        self.window = gpu.Window("Layer affine validation")
        self.addCleanup(self.window.close)
        self.scene = Scene()
        self.scene._init(self.window, "#000000")
        self.addCleanup(self.scene._close)
        self.texture = gpu.Texture.render_target(80, 80)
        self.addCleanup(self.texture.close)
        with self.window.frame(clear_color=(1, 0, 0, 1), target_texture=self.texture):
            pass

    def draw(self, *, python=False):
        if not python:
            return self.scene.capture(rect=(0, 0, 240, 200), size=(240, 200))
        target = gpu.Texture.render_target(240, 200)
        try:
            renderer = self.scene._renderer
            commands = []
            self.scene._collect(commands, renderer, _IDENTITY, 1., [0])
            renderer.render(commands, clear_color=(0, 0, 0, 1),
                            target_texture=target, viewport=(240, 200))
            return target.to_image(self.window)
        finally:
            target.close()

    def test_squashed_clipped_layer_preserves_width_and_compresses_height(self):
        root = Group(x=40, y=80, scale=(1, .25))
        layer = Layer(clip=(-20, -20, 120, 120))
        layer.add(Sprite(self.texture, anchor=(0, 0)))
        self.scene.add(root.add(layer))
        for python in (False, True):
            with self.subTest(python=python):
                image = self.draw(python=python)
                for x, y in ((41, 81), (118, 81), (41, 98), (118, 98)):
                    self.assertEqual(pixel(image, x, y), (255, 0, 0, 255))
                for x, y in ((38, 90), (122, 90), (80, 78), (80, 102)):
                    self.assertEqual(pixel(image, x, y), (0, 0, 0, 255))

    def test_nested_layers_match_sprites_under_reflection_shear_and_rotation(self):
        for nested in (False, True):
            for reflected in (False, True):
                with self.subTest(nested=nested, reflected=reflected):
                    root = Group(x=120, y=100, rotation=.2,
                                 scale=(-1.5 if reflected else 1.5, .55))
                    outer = Layer(rotation=-.45, clip=(-45, -45, 90, 90))
                    inner = (Layer if nested else Group)(affine_transform=(1, .2, .35, 1, 0, 0))
                    sprite = Sprite(self.texture)
                    self.scene.add(root.add(outer.add(inner.add(sprite))))
                    matrix = _mul(root._local_matrix(), _mul(outer._local_matrix(), inner._local_matrix()))
                    for python in (False, True):
                        image = self.draw(python=python)
                        for local in ((-22, -22), (22, -22), (-22, 22), (22, 22), (0, 0)):
                            point = _apply(matrix, local)
                            self.assertEqual(pixel(image, *(round(v) for v in point)), (255, 0, 0, 255))
                        for got, want in zip(sprite._current_geometry()[0], matrix):
                            self.assertAlmostEqual(got, want)
                    self.scene._render()
                    self.assertIs(self.scene.hit_test(*_apply(matrix, (0, 0))), sprite)
                    for got, want in zip(sprite._world_transform, matrix):
                        self.assertAlmostEqual(got, want)
                    self.scene.remove(root)
                    root.close()

    def test_mirroring_moves_asymmetric_content_and_reuses_local_cache(self):
        layer = Layer(x=120, y=100, scale=(2, .5), clip=(-40, -40, 80, 80))
        sprite = Sprite(self.texture, size=(12, 20), x=20)
        self.scene.add(layer.add(sprite))
        self.scene._render()
        texture = layer._tex
        self.assertEqual(pixel(self.draw(), 160, 100), (255, 0, 0, 255))
        layer.scale = (-2, .5)
        self.scene._render()
        self.assertIs(layer._tex, texture)
        image = self.draw()
        self.assertEqual(pixel(image, 80, 100), (255, 0, 0, 255))
        self.assertEqual(pixel(image, 160, 100), (0, 0, 0, 255))
        self.assertIs(self.scene.hit_test(80, 100), sprite)

    def test_singular_layer_is_empty_and_recovers_after_scale_changes(self):
        layer = Layer(x=120, y=100)
        layer.add(Sprite(self.texture))
        self.scene.add(layer)
        for scale in ((0, 1), (1, 0), (0, 0)):
            layer.scale = scale
            for python in (False, True):
                with self.subTest(scale=scale, python=python):
                    self.assertEqual(self.draw(python=python).rgba, bytes((0, 0, 0, 255)) * (240 * 200))
        layer.scale = (1, .25)
        self.assertEqual(pixel(self.draw(), 120, 100), (255, 0, 0, 255))


if __name__ == "__main__":
    unittest.main()
