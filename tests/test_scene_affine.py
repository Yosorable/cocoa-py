"""Direct affine transforms share geometry across rendering, input and capture."""

import math
import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace
import unittest

from _cocoa import _scene_accel
from scene import Group, HStack, Label, Layer, Rect, Scene, Sprite, SpriteFrame, VStack, ZStack, gpu
from scene._common import _IDENTITY, _apply, _matrix, _mul


class AffineTransformTests(unittest.TestCase):
    def test_numeric_conversion_keeps_a_snapshot_when_the_input_list_changes(self):
        # A native regression must fail this test without crashing the suite.
        source = textwrap.dedent("""
            from scene import Group

            expected = (2., .25, -.5, 1., 12., -3.)
            node = Group()
            for operation in ("clear", "replace", "raise"):
                values = []

                class Number:
                    def __float__(self):
                        values.clear()
                        if operation == "replace":
                            values.extend([99.] * 6)
                        if operation == "raise":
                            raise ValueError("conversion failed")
                        return 2.

                values.extend([Number(), *expected[1:]])
                try:
                    node.affine_transform = values
                except ValueError as error:
                    assert operation == "raise" and str(error) == "conversion failed"
                else:
                    assert operation != "raise"
                assert node.affine_transform == expected, node.affine_transform
        """)
        result = subprocess.run([sys.executable, "-c", source],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_input_is_copied_and_invalid_assignment_preserves_previous_transform(self):
        values = [1, .25, -.5, 1, 12, -3]
        node = Group(affine_transform=values)
        expected = tuple(float(value) for value in values)
        values[0] = 99
        self.assertEqual(node.affine_transform, expected)
        for invalid in ((), (1,) * 5, (1,) * 7, (1, 0, 0, 1, math.inf, 0),
                        (1, 0, 0, 1, 0, math.nan), (1, 0, 0, 1, object(), 0)):
            with self.subTest(value=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    node.affine_transform = invalid
                self.assertEqual(node.affine_transform, expected)
        node.affine_transform = None
        self.assertEqual(node._local_matrix(), _IDENTITY)

    def test_nested_native_and_python_geometry_include_shear_reflection_and_clips(self):
        root = Group(x=10, y=20, scale=(1.25, .75), rotation=.3)
        outer = Group(affine_transform=(-1, .2, .4, 1, 50, 5), clip=(-20, -20, 40, 40))
        child = Rect(12, 8, x=3, y=4, rotation=-.2,
                     affine_transform=(1, -.3, .15, 1, 2, -1))
        root.add(outer)
        outer.add(child)
        expected = _mul(_matrix((10, 20), .3, (1.25, .75)), outer.affine_transform)
        expected = _mul(expected, _mul(_matrix((3, 4), -.2, 1), child.affine_transform))
        self.assertEqual(child._current_geometry()[0], expected)
        renderer = SimpleNamespace(screen_scale=1)
        for native in (False, True):
            with self.subTest(native=native):
                if native:
                    _scene_accel.collect(root, _IDENTITY, 1., 1., renderer)
                else:
                    root._collect([], renderer, _IDENTITY, 1., [0])
                for point in ((0, 0), (2, 3), (-6, 4)):
                    actual = child.convert_to_world(*point)
                    for got, want in zip(actual, _apply(expected, point)):
                        self.assertAlmostEqual(got, want, places=12)
                    for got, want in zip(child.convert_from_world(*actual), point):
                        self.assertAlmostEqual(got, want, places=12)
                self.assertTrue(child.contains_point(*child.convert_to_world(0, 0)))
                self.assertFalse(child.contains_point(*child.convert_to_world(30, 0)))
        child.affine_transform = (1, .2, 0, 1, 25, 0)
        _scene_accel.collect(root, _IDENTITY, 1., 1., renderer)
        self.assertEqual(child._world_transform, child._current_geometry()[0])


class AffineLayoutTests(unittest.TestCase):
    @staticmethod
    def world_bounds(node):
        x0, y0, x1, y1 = node._bounds()
        matrix = node._current_geometry()[0]
        points = [_apply(matrix, (x, y)) for x in (x0, x1) for y in (y0, y1)]
        return (min(x for x, _ in points), min(y for _, y in points),
                max(x for x, _ in points), max(y for _, y in points))

    def test_axis_stacks_keep_spacing_after_a_child_transform_changes(self):
        for stack_type, axis in ((HStack, 0), (VStack, 1)):
            with self.subTest(stack=stack_type):
                first, second = Rect(20, 10), Rect(20, 10)
                stack = stack_type(spacing=5, children=[first, second])
                for affine in ((2, 0, 0, 2, 7, -3), (-1, .5, .75, 1, -4, 9), None):
                    first.affine_transform = affine
                    stack._tick(0)
                    a, b = self.world_bounds(first), self.world_bounds(second)
                    self.assertAlmostEqual(b[axis] - a[axis + 2], 5)

    def test_axis_alignment_padding_and_repeated_layout_use_composed_bounds(self):
        frame = SpriteFrame(object(), (0, 0, 1, 1))
        for stack_type, axis in ((HStack, 0), (VStack, 1)):
            for alignment in ("start", "center", "end"):
                with self.subTest(stack=stack_type, alignment=alignment):
                    first = Sprite(frame, size=(20, 10), anchor=(0, 0),
                                   x=1e30, y=-1e30, rotation=.4, scale=(1.5, -.75),
                                   affine_transform=(-1, .5, .25, 1, 9, -7))
                    second = Rect(8, 30)
                    stack = stack_type(spacing=7, alignment=alignment, padding=(3, 5, 11, 13),
                                       children=[first, second])
                    stack._tick(0)
                    a, b = self.world_bounds(first), self.world_bounds(second)
                    self.assertAlmostEqual(b[axis] - a[axis + 2], 7)
                    cross = 1 - axis
                    if alignment == "start":
                        self.assertAlmostEqual(a[cross], b[cross])
                    elif alignment == "end":
                        self.assertAlmostEqual(a[cross + 2], b[cross + 2])
                    else:
                        self.assertAlmostEqual(a[cross] + a[cross + 2], b[cross] + b[cross + 2])
                    bounds = stack._bounds()
                    self.assertAlmostEqual(bounds[0] + bounds[2], 0)
                    self.assertAlmostEqual(bounds[1] + bounds[3], 0)
                    positions = (first.position, second.position)
                    for _ in range(3):
                        stack._tick(0)
                        self.assertEqual((first.position, second.position), positions)

    def test_zstack_aligns_transformed_edges_and_centers(self):
        for alignment in ("top left", "top right", "bottom left", "bottom right", "center"):
            with self.subTest(alignment=alignment):
                first = Rect(20, 10, affine_transform=(-2, 0, 0, 1, 17, 8))
                second = Rect(8, 30, affine_transform=(1, .25, .1, 1, -5, 11))
                stack = ZStack(alignment=alignment, children=[first, second])
                stack._tick(0)
                a, b = self.world_bounds(first), self.world_bounds(second)
                for axis, low, high in ((0, "left", "right"), (1, "top", "bottom")):
                    if low in alignment:
                        self.assertAlmostEqual(a[axis], b[axis])
                    elif high in alignment:
                        self.assertAlmostEqual(a[axis + 2], b[axis + 2])
                    else:
                        self.assertAlmostEqual(a[axis] + a[axis + 2], b[axis] + b[axis + 2])

    def test_component_and_affine_forms_have_the_same_layout(self):
        for stack_type in (HStack, VStack, ZStack):
            with self.subTest(stack=stack_type):
                component = Rect(20, 10, rotation=.4, scale=(1.5, -.75))
                affine = Rect(20, 10, affine_transform=_matrix((7, -3), .4, (1.5, -.75)))
                reference = stack_type(children=[component, Rect(8, 30)])
                flattened = stack_type(children=[affine, Rect(8, 30)])
                reference._tick(0)
                flattened._tick(0)
                for first, second in zip(reference.children, flattened.children):
                    for a, b in zip(self.world_bounds(first), self.world_bounds(second)):
                        self.assertAlmostEqual(a, b)

    def test_nested_stacks_and_label_measurement_preserve_spacing(self):
        label = Label("Layout", size=20, affine_transform=(1.5, .25, 0, 1, 4, -3))
        inner = HStack(spacing=6, children=[label, Rect(12, 8)],
                       affine_transform=(1, .2, .5, 1, 7, -4))
        last = Rect(20, 10)
        outer = VStack(spacing=9, children=[inner, last])
        outer._tick(0)
        a, b = self.world_bounds(inner), self.world_bounds(last)
        self.assertAlmostEqual(b[1] - a[3], 9)
        expected = (inner.position, label.position, last.position)
        outer._tick(0)
        self.assertEqual((inner.position, label.position, last.position), expected)


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1", "Requires a desktop Metal window.")
class AffineCaptureTests(unittest.TestCase):
    def setUp(self):
        self.window = gpu.Window("Direct affine transform validation")
        self.addCleanup(self.window.close)
        self.scene = Scene()
        self.scene._init(self.window, "#000000")
        self.addCleanup(self.scene._close)
        self.texture = gpu.Texture.render_target(24, 20)
        self.addCleanup(self.texture.close)
        with self.window.frame(clear_color=(.8, .3, .1, 1), target_texture=self.texture):
            pass

    def test_stack_spacing_matches_the_rendered_sprite_edges(self):
        first = Sprite(self.texture, affine_transform=(2, 0, 0, 1, 7, -3))
        second = Sprite(self.texture)
        self.scene.add(HStack(x=64, y=32, spacing=6, children=[first, second]))
        image = self.scene.capture(rect=(0, 0, 128, 64), size=(128, 64))
        row = image.rgba[32 * 128 * 4:33 * 128 * 4]
        occupied = [x for x in range(128) if row[4 * x] > 0]
        self.assertEqual(occupied, [*range(25, 73), *range(79, 103)])

    def test_direct_matrix_matches_equivalent_parent_chain_and_local_capture(self):
        outer = Group(x=100, y=90, rotation=.4, scale=(1.7, -.8), clip=(-80, -80, 160, 160))
        inner = Group(rotation=-.25)
        sprite = Sprite(self.texture)
        inner.add(sprite)
        outer.add(inner)
        self.scene.add(outer)
        rectangle = (0, 0, 200, 180)
        expected = self.scene.capture(rect=rectangle, size=(200, 180))
        local = sprite.capture(rect=(-12, -10, 24, 20), size=(24, 20))
        transform = _mul(_matrix((100, 90), .4, (1.7, -.8)), _matrix((0, 0), -.25, 1))
        self.scene.remove(outer)
        inner.remove(sprite)
        sprite.affine_transform = transform
        self.scene.add(sprite)
        actual = self.scene.capture(rect=rectangle, size=(200, 180))
        self.assertEqual(actual.rgba, expected.rgba)
        self.assertEqual(sprite.capture(rect=(-12, -10, 24, 20), size=(24, 20)).rgba, local.rgba)

    def test_layer_rebuild_preserves_a_changed_child_affine_matrix(self):
        layer = Layer(x=80, y=70)
        sprite = Sprite(self.texture, affine_transform=(1, 0, .4, 1, 0, 0))
        layer.add(sprite)
        self.scene.add(layer)
        rectangle = (0, 0, 180, 160)
        before = self.scene.capture(rect=rectangle, size=(180, 160))
        sprite.affine_transform = (-1, .2, -.4, 1, 30, 0)
        layer.invalidate()
        after = self.scene.capture(rect=rectangle, size=(180, 160))
        self.assertNotEqual(after.rgba, before.rgba)
        layer.invalidate()
        fresh = self.scene.capture(rect=rectangle, size=(180, 160))
        self.assertEqual(after.rgba, fresh.rgba)

    def test_cached_sprite_matches_an_equivalent_flattened_transform_chain(self):
        layer = Layer(x=80, y=70, scale=.5, clip=(-100, -100, 200, 200))
        outer = Group(x=4.25, y=6.75, rotation=.47, scale=(1.3, .8))
        sprite = Sprite(self.texture, rotation=-.3)
        outer.add(sprite)
        layer.add(outer)
        self.scene.add(layer)
        rectangle = (0, 0, 160, 140)
        before = self.scene.capture(rect=rectangle, size=(160, 140))
        matrix = _mul(outer._local_matrix(), sprite._local_matrix())
        outer.remove(sprite)
        layer.remove(outer)
        sprite.rotation = 0
        sprite.affine_transform = matrix
        layer.add(sprite)
        after = self.scene.capture(rect=rectangle, size=(160, 140))
        self.assertEqual(after.rgba, before.rgba)
