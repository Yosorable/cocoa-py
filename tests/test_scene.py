"""Scene caching, physics and optional native desktop Metal regressions."""

import os
import struct
import unittest

from _cocoa import _scene_accel
from scene import Circle, Group, Label, PhysicsBody, Rect, Scene, gpu, run


class SceneCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = Group()
        self.addCleanup(self.root.close)

    def collect(self, previous_fingerprint=0):
        return _scene_accel.collect(
            self.root, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
            1.0, 2.0, None, previous_fingerprint)

    def test_stationary_button_subclass_updates_fill_on_every_toggle(self):
        class Button(Rect):
            def set_on(self, on):
                self.fill = "#2ecc71cc" if on else "#333333cc"

        button = Button(80, 32, radius=8, fill="#333333cc")
        self.root.add(button)
        frame = self.collect()
        for on in (True, False, True, False):
            with self.subTest(on=on):
                button.set_on(on)
                updated = self.collect(frame[-1])
                self.assertIsNotNone(updated[0], "A style change must render a new frame.")
                self.assertEqual(updated[0], frame[0], "The button must stay in place.")
                rgba = struct.unpack_from("4f", updated[1], 32)
                expected = (46, 204, 113, 204) if on else (51, 51, 51, 204)
                for actual, value in zip(rgba, expected):
                    self.assertAlmostEqual(actual, value / 255, places=6)
                self.assertEqual(self.collect(updated[-1]), (None, updated[-1]))
                frame = updated

    def test_stationary_shape_subclass_updates_geometry(self):
        class Dot(Circle):
            pass

        dot = Dot(10, fill="#f1c40f")
        self.root.add(dot)
        first = self.collect()
        dot.radius = 20
        enlarged = self.collect(first[-1])
        self.assertIsNotNone(enlarged[0])
        self.assertNotEqual(enlarged[0], first[0])
        self.assertNotEqual(enlarged[1], first[1])
        self.assertEqual(self.collect(enlarged[-1]), (None, enlarged[-1]))

    def test_custom_snapshot_and_emitter_preserve_static_frame_skipping(self):
        class Indicator(Rect):
            active = False
            emissions = 0

            def _snap(self):
                return super()._snap() + (self.active,)

            def _emit(self, cmds, renderer, world, opacity, order):
                self.emissions += 1
                self.fill = "#2ecc71" if self.active else "#333333"
                super()._emit(cmds, renderer, world, opacity, order)

        indicator = Indicator(80, 32)
        self.root.add(indicator)
        initial = self.collect()
        self.assertEqual(self.collect(initial[-1]), (None, initial[-1]))
        self.assertEqual(indicator.emissions, 1)
        indicator.active = True
        changed = self.collect(initial[-1])
        self.assertIsNotNone(changed[0])
        self.assertNotEqual(changed[1], initial[1])
        self.assertEqual(indicator.emissions, 2)
        self.assertEqual(self.collect(changed[-1]), (None, changed[-1]))
        self.assertEqual(indicator.emissions, 2)


class PhysicsTests(unittest.TestCase):
    def test_falling_body_uses_the_bundled_box2d_engine(self):
        scene = Scene()
        world = scene.physics_world
        self.addCleanup(world.destroy)
        world.gravity = (0, 10)
        ball = Circle(1)
        ball.physics_body = PhysicsBody.circle(1)
        scene.add(ball)
        for _ in range(120):
            world.step(1 / 120)
        self.assertGreater(ball.y, 4)
        self.assertLess(ball.y, 6)
        self.assertGreater(ball.physics_body.velocity[1], 8)


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1", "Set COCOA_PY_UI_TESTS=1 for desktop window tests.")
class MetalDesktopTests(unittest.TestCase):
    def test_render_target_clear_and_readback(self):
        before = gpu.resource_counts()
        window = gpu.Window("cocoa-py render validation")
        texture = gpu.Texture.render_target(64, 64)
        buffer = gpu.Buffer(64 * 64 * 4)
        try:
            self.assertGreater(window.size[0], 100)
            self.assertGreaterEqual(window.scale, 1)
            with window.frame(clear_color=(1, 0.5, 0, 1), target_texture=texture):
                pass
            with window.blit() as transfer:
                transfer.copy_texture_to_buffer(texture, buffer, bytes_per_row=256)
            pixels = buffer.read()
            self.assertEqual(len(pixels), 64 * 64 * 4)
            for offset in (0, 4 * 65, len(pixels) - 4):
                blue, green, red, alpha = pixels[offset:offset + 4]
                self.assertEqual((blue, red, alpha), (0, 255, 255))
                self.assertIn(green, (127, 128))
        finally:
            buffer.close()
            texture.close()
            window.close()
        after = gpu.resource_counts()
        for key in ("windows", "buffers", "textures", "active_frames", "active_blit_passes"):
            self.assertEqual(after[key], before[key], key)

    def test_scene_loads_packaged_shaders_and_closes_after_python_exception(self):
        class EndScene(Exception):
            pass

        class Preview(Scene):
            def setup(self):
                self.frames = 0
                self.add(Circle(45, position=(120, 120), fill="#43b9ee"))
                self.add(Label("cocoa-py on macOS", position=(280, 220), size=28))

            def update(self, dt):
                self.frames += 1
                if self.frames >= 3:
                    raise EndScene

        before = gpu.resource_counts()
        with self.assertRaises(EndScene):
            run(Preview, title="cocoa-py scene validation")
        after = gpu.resource_counts()
        for key in ("windows", "libraries", "pipelines", "buffers", "textures", "active_frames", "frame_autorelease_pools"):
            self.assertEqual(after[key], before[key], key)


if __name__ == "__main__":
    unittest.main()
