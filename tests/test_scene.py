"""Scene physics and optional native desktop Metal rendering regressions."""

import os
import unittest

from scene import Circle, Label, PhysicsBody, Scene, gpu, run


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
