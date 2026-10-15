"""Cached layers preserve the same affine geometry as their uncached content."""

import os
import unittest
from unittest.mock import patch

from scene import Group, Layer, Rect, Scene, Sprite, gpu
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

    def test_anisotropic_capture_keeps_intermediates_proportional_to_output(self):
        allocate = gpu.Texture.render_target
        for nested in (False, True):
            for rotation, reflection in ((0, 1), (45, 1), (90, -1)):
                for size in ((200, 200), (300, 100)):
                    with self.subTest(nested=nested, rotation=rotation, reflection=reflection, size=size):
                        layer = Layer(x=10, y=10, rotation=rotation, scale=(20 * reflection, .1))
                        content = Layer() if nested else Group()
                        content.add(Rect(1000, 1000, fill="#ffffff"),
                                    Sprite(self.texture, size=(.2, 40), x=.25, y=20))
                        self.scene.add(layer.add(content))
                        allocations = []

                        def record(width, height, *args, **kwargs):
                            allocations.append((width, height))
                            self.assertLessEqual(max(width, height), 640,
                                                 "A compressed axis was oversampled.")
                            return allocate(width, height, *args, **kwargs)

                        try:
                            with patch.object(gpu.Texture, "render_target", side_effect=record):
                                image = self.scene.capture(rect=(0, 0, 20, 20), size=size)
                            self.assertEqual(image.size, size)
                            self.assertEqual(len(allocations), 3 if nested else 2)
                            for point, color in (((.25, 20), (255, 0, 0, 255)),
                                                 ((-.25, -20), (255, 255, 255, 255))):
                                x, y = _apply(layer._local_matrix(), point)
                                self.assertEqual(pixel(image, int(x * size[0] / 20),
                                                       int(y * size[1] / 20)), color)
                        finally:
                            self.scene.remove(layer)
                            layer.close()

    def test_anisotropic_capture_restores_live_caches_after_success_and_failure(self):
        layer = Layer()
        inner = Layer()
        inner.add(Rect(10, 1000, fill="#ffffff"))
        self.scene.add(layer.add(inner))
        self.scene._render()
        layer.scale = (20, .1)
        renderer = self.scene._renderer
        fields = ("_tex", "_lsize", "_lcenter", "_rscale", "_dirty", "_hit_snapshot")
        before = [tuple(getattr(node, name) for name in fields) for node in (layer, inner)]
        resources = gpu.resource_counts()
        allocate = gpu.Texture.render_target
        for fail in (False, True, False):
            calls = 0

            def record(width, height, *args, **kwargs):
                nonlocal calls
                calls += 1
                if fail and calls == 3:
                    raise RuntimeError("injected nested allocation failure")
                return allocate(width, height, *args, **kwargs)

            with patch.object(gpu.Texture, "render_target", side_effect=record):
                if fail:
                    with self.assertRaisesRegex(RuntimeError, "nested allocation failure"):
                        self.scene.capture(rect=(0, 0, 20, 20), size=(200, 200))
                else:
                    image = self.scene.capture(rect=(0, 0, 20, 20), size=(200, 200))
                    self.assertEqual(image.rgba, b"\xff" * (200 * 200 * 4))
            self.assertEqual([tuple(getattr(node, name) for name in fields) for node in (layer, inner)], before)
            self.assertIsNone(renderer._capture_viewport)
            self.assertIsNone(renderer._capture_pixel_scale)
            self.assertEqual(renderer.screen_scale, self.window.scale)
            after = gpu.resource_counts()
            for name in ("textures", "buffers", "offscreen_command_buffers"):
                self.assertEqual(after[name], resources[name], name)

    def test_nearly_parallel_axes_fit_capture_texture_limits(self):
        from scene import _capture

        layer = Layer(affine_transform=(1, 0, 1, .001, 0, 0))
        layer.add(Layer().add(Rect(100000, 100000, fill="#ffffff")))
        self.scene.add(layer)
        allocate = gpu.Texture.render_target
        # Use smaller limits to exercise both fallbacks without retaining
        # hundreds of megabytes of temporary GPU storage in the test suite.
        for axis_limit, pixel_limit in ((512, 512 * 512), (1024, 128 * 128)):
            with self.subTest(axis_limit=axis_limit, pixel_limit=pixel_limit):
                def bounded_allocate(width, height, *args, **kwargs):
                    self.assertLessEqual(max(width, height), axis_limit)
                    self.assertLessEqual(width * height, pixel_limit)
                    return allocate(width, height, *args, **kwargs)

                with patch.object(_capture, "_MAX_TEXTURE_SIZE", axis_limit), \
                     patch.object(_capture, "_MAX_CAPTURE_PIXELS", pixel_limit), \
                     patch.object(gpu.Texture, "render_target", side_effect=bounded_allocate):
                    image = self.scene.capture(rect=(-1, -1, 2, 2), size=(64, 64))
                self.assertEqual(image.rgba, b"\xff" * (64 * 64 * 4))


if __name__ == "__main__":
    unittest.main()
