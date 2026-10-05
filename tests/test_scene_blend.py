"""Alpha and additive compositing must survive batching, clipping, and caches."""

import os
from pathlib import Path
import tempfile
import unittest

from scene import Group, ImageData, ParticleEmitter, Scene, Sprite, gpu


def pixel(image, x, y):
    offset = (y * image.width + x) * 4
    return tuple(image.rgba[offset:offset + 4])


class BlendValidationTests(unittest.TestCase):
    def test_particle_mode_validation_does_not_silently_ignore_a_typo(self):
        with self.assertRaises(ValueError):
            ParticleEmitter(blend="addition")
        emitter = ParticleEmitter()
        self.addCleanup(emitter.close)
        self.assertEqual(emitter.blend, "additive")
        emitter.blend = "alpha"
        self.assertEqual(emitter.blend, "alpha")


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
                                  x=20, y=15, blend="alpha")
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

    def test_gpu_pipeline_rejects_unknown_compositing(self):
        library = gpu.Library("__default__")
        self.addCleanup(library.close)
        with self.assertRaisesRegex(ValueError, "blend_mode"):
            gpu.Pipeline(library, vertex="quad_vertex", fragment="tex_frag", blend_mode="invalid")


if __name__ == "__main__":
    unittest.main()
