"""Sprite geometry must retain inherited shear and reflected transforms."""

import math
import struct
import unittest

from _cocoa import _scene_accel
from scene import Group, Sprite, SpriteFrame, gpu


class _CollectOnlyTexture(gpu.Texture):
    def __init__(self):
        super().__init__(1, (4, 2))

    def close(self):
        self._handle = None


class SpriteAffineTests(unittest.TestCase):
    def check_vertices(self, reflected, subclass):
        texture = _CollectOnlyTexture()
        root = Group(position=(30, 40), scale=(-2 if reflected else 2, 1))
        self.addCleanup(texture.close)
        self.addCleanup(root.close)

        class CustomSprite(Sprite):
            pass

        frame = SpriteFrame(texture, (.25, .125, .75, .875))
        sprite = (CustomSprite if subclass else Sprite)(frame, size=(4, 2), anchor=(0, 0), rotation=math.pi / 4)
        root.add(sprite)
        result = _scene_accel.collect(root, (1, 0, 0, 1, 0, 0), 1, 1, None)
        vertices = tuple(struct.iter_unpack("<4f", result[0]))
        s = math.sqrt(0.5)
        sx = -2 if reflected else 2
        corners = ((0, 0), (4, 0), (0, 2), (4, 0), (4, 2), (0, 2))
        for (actual_x, actual_y, _, _), (x, y) in zip(vertices, corners):
            self.assertAlmostEqual(actual_x, 30 + sx * s * (x - y), places=4)
            self.assertAlmostEqual(actual_y, 40 + s * (x + y), places=4)
        self.assertTrue(sprite.contains_point(30 + sx * s, 40 + 3 * s))
        self.assertEqual(_scene_accel.collect(root, (1, 0, 0, 1, 0, 0), 1, 1, None, result[-1]),
                         (None, result[-1]))
        sprite.flip_x = True
        flipped = _scene_accel.collect(root, (1, 0, 0, 1, 0, 0), 1, 1, None, result[-1])
        for before, after in zip(vertices, struct.iter_unpack("<4f", flipped[0])):
            self.assertEqual(before[:2], after[:2])
            self.assertAlmostEqual(after[2], 1 - before[2])

    def test_native_sprite_under_nonuniform_scale(self):
        self.check_vertices(False, False)

    def test_native_sprite_under_reflection(self):
        self.check_vertices(True, False)

    def test_custom_sprite_under_nonuniform_scale(self):
        self.check_vertices(False, True)

    def test_custom_sprite_under_reflection(self):
        self.check_vertices(True, True)


if __name__ == "__main__":
    unittest.main()
