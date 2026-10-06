"""Ordered GPU batches preserve rendering and validate before encoding."""

import os
import struct
import unittest
from unittest.mock import patch

from _cocoa import _metal
from scene import Group, Rect, Scene, Sprite, gpu


SHADER = """
#include <metal_stdlib>
using namespace metal;
vertex float4 vertex_main(const device float2 *points [[buffer(0)]], uint id [[vertex_id]]) {
    return float4(points[id], 0, 1);
}
fragment float4 red_main() { return float4(1, 0, 0, 1); }
fragment float4 green_main() { return float4(0, 1, 0, 1); }
"""


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1", "Requires a desktop Metal window.")
class DrawBatchTests(unittest.TestCase):
    def setUp(self):
        self.window = gpu.Window("GPU draw batch validation")
        self.addCleanup(self.window.close)
        self.target = gpu.Texture.render_target(32, 32)
        self.addCleanup(self.target.close)
        self.library = gpu.Library(SHADER)
        self.addCleanup(self.library.close)
        self.red = gpu.Pipeline(self.library, vertex="vertex_main", fragment="red_main")
        self.green = gpu.Pipeline(self.library, vertex="vertex_main", fragment="green_main")
        self.addCleanup(self.red.close)
        self.addCleanup(self.green.close)
        points = (-1, -1, 1, -1, -1, 1, 1, -1, 1, 1, -1, 1,
                  0, -1, 1, -1, 0, 1, 1, -1, 1, 1, 0, 1)
        data = struct.pack("24f", *points)
        self.vertices = gpu.Buffer(len(data))
        self.vertices.write(data)
        self.addCleanup(self.vertices.close)

    def render(self, batched, *, following=False):
        with self.window.frame(target_texture=self.target) as frame:
            frame.set_vertex_buffer(self.vertices, 0)
            frame.set_pipeline(self.red)
            draws = [(None, None, 0, 6), (self.green, None, 6, 6)]
            if batched:
                frame.draw_many("triangle", draws)
            else:
                frame.draw("triangle", 0, 6)
                frame.set_pipeline(self.green)
                frame.draw("triangle", 6, 6)
            if following:
                frame.set_pipeline(self.red)
                frame.draw("triangle", 0, 6)
        return self.target.to_image(self.window).rgba

    def test_order_and_subsequent_pipeline_changes_match_individual_calls(self):
        for following in (False, True):
            with self.subTest(following=following):
                expected = self.render(False, following=following)
                self.assertEqual(self.render(True, following=following), expected)
        self.assertEqual(self.render(True, following=True), bytes((255, 0, 0, 255)) * 1024)

    def test_invalid_later_entry_does_not_encode_earlier_draws(self):
        invalid = [
            [(self.red, None, 0, 6), (self.green, None, 6, -1)],
            [(self.red, None, 0, 6), (self.green, 0, 6, 6)],
            [(self.red, None, 0, 6), (999999999, None, 6, 6)],
        ]
        for draws in invalid:
            with self.subTest(draws=draws):
                with self.window.frame(target_texture=self.target) as frame:
                    frame.set_vertex_buffer(self.vertices, 0)
                    with self.assertRaises((ValueError, KeyError)):
                        frame.draw_many("triangle", draws)
                    self.assertIsNone(frame._pip)
                self.assertEqual(self.target.to_image(self.window).rgba, bytes((0, 0, 0, 255)) * 1024)

    def test_native_conversion_can_mutate_input_and_close_resources_without_deadlock(self):
        draws = []

        class Index:
            def __index__(self):
                draws.clear()
                return 0

        draws.extend([(self.red.handle, None, Index(), 6), (self.green.handle, None, 6, 6)])
        with self.window.frame(target_texture=self.target) as frame:
            frame.set_vertex_buffer(self.vertices, 0)
            _metal.draw_many(self.window.handle, "triangle", draws)
        self.assertFalse(draws)
        actual = self.target.to_image(self.window).rgba
        self.assertEqual(actual, self.render(False))
        texture = gpu.Texture.render_target(1, 1)
        self.addCleanup(texture.close)
        handle = texture.handle

        class ClosingIndex:
            def __index__(self):
                texture.close()
                return 0

        with self.window.frame(target_texture=self.target) as frame:
            frame.set_vertex_buffer(self.vertices, 0)
            with self.assertRaises(KeyError):
                _metal.draw_many(self.window.handle, "triangle", [(self.red.handle, handle, ClosingIndex(), 6)])
        self.assertEqual(self.target.to_image(self.window).rgba, bytes((0, 0, 0, 255)) * 1024)

    def test_scene_blends_and_clip_changes_match_individual_submission(self):
        scene = Scene()
        scene._init(self.window, "#000000")
        self.addCleanup(scene._close)
        textures = []
        for color in ((1, .1, .2, 1), (.2, .8, .1, 1)):
            texture = gpu.Texture.render_target(8, 8)
            self.addCleanup(texture.close)
            with self.window.frame(clear_color=color, target_texture=texture):
                pass
            textures.append(texture)
        scene.add(Sprite(textures[0], size=(60, 50), x=40, y=40, z=1))
        clipped = Group(clip=(20, 20, 40, 40))
        clipped.add(Sprite(textures[1], size=(60, 50), x=50, y=40, z=2, opacity=.5),
                    Rect(15, 15, x=30, y=30, z=3, fill="#3366ffaa"),
                    Sprite(textures[0], size=(20, 20), x=45, y=35, z=4, blend="additive", opacity=.4))
        scene.add(clipped, Sprite(textures[1], size=(12, 12), x=60, y=60, z=5))
        batched = scene.capture(rect=(0, 0, 96, 96), size=(96, 96))

        def individual(frame, primitive, draws, *, texture_index=0):
            for pipeline, texture, start, count in draws:
                if pipeline is not None:
                    frame.set_pipeline(pipeline)
                if texture is not None:
                    frame.set_fragment_texture(texture, texture_index)
                frame.draw(primitive, start, count)

        with patch.object(gpu.Frame, "draw_many", individual):
            expected = scene.capture(rect=(0, 0, 96, 96), size=(96, 96))
        self.assertEqual(batched.rgba, expected.rgba)
