"""Pixel-space curve accuracy, native cache invalidation and round strokes."""

import math
import os
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from _cocoa import _scene_accel
from scene import Group, Layer, Path, Scene, gpu
from scene import _path
from scene._common import _apply, _identity, _matrix, _mul


# The quadratic and cubic curves from the Path/Polygon example, unchanged.
QUADRATIC = [("M", (-64, 8)), ("Q", (-16, -72, 52, -20)),
             ("Q", (82, 16, 34, 58)), ("Q", (-18, 74, -64, 8)), ("Z", None)]
CUBIC = [("M", (-120, 0)), ("C", (-78, -66, -22, 66, 24, 0)),
         ("C", (68, -66, 116, 66, 160, 0))]


def samples(commands):
    cursor = None
    for kind, values in commands:
        if kind == "M":
            cursor = values
        elif kind in ("Q", "C"):
            controls = [cursor, *zip(values[::2], values[1::2])]
            for step in range(257):
                t = step / 256
                work = controls
                while len(work) > 1:
                    work = [((1 - t) * a[0] + t * b[0], (1 - t) * a[1] + t * b[1])
                            for a, b in zip(work, work[1:])]
                yield work[0]
            cursor = values[-2:]


def approximation_error(commands, subpaths, transform, screen_scale):
    points = [_apply(transform, point) for point in subpaths[0]["points"]]
    segments = list(zip(points, points[1:]))
    return screen_scale * max(
        math.sqrt(min(_path._distance_sq_to_segment(_apply(transform, sample), a, b)
                      for a, b in segments)) for sample in samples(commands))


class PathQualityTests(unittest.TestCase):
    def setUp(self):
        self.root = Group()
        self.addCleanup(self.root.close)
        self.renderer = SimpleNamespace(screen_scale=2.0)

    def collect(self, transform=None, screen_scale=2.0):
        self.renderer.screen_scale = screen_scale
        return _scene_accel.collect(self.root, transform or _identity(),
                                    1.0, screen_scale, self.renderer)

    def path(self, commands=CUBIC, **kwargs):
        node = Path(commands, fill="#ffffff", stroke="#ff0000", stroke_width=10, **kwargs)
        self.root.add(node)
        return node

    def test_demo_curves_meet_pixel_error_target_under_affine_transforms(self):
        transforms = [_identity(), _matrix((12, -9), 0.71, 3),
                      _matrix((0, 0), 1.1, (-8, 0.25)),
                      _mul(_matrix((0, 0), 0.5, (6, 0.5)),
                           _matrix((0, 0), 0.9, (0.25, 3)))]
        for commands in (QUADRATIC, CUBIC):
            node = self.path(commands)
            node._bounds()  # A query before rendering must not freeze coarse geometry.
            version = node._path_version
            for transform in transforms:
                for scale in (1.0, 2.0, 3.0):
                    with self.subTest(commands=commands, transform=transform, scale=scale):
                        self.collect(transform, scale)
                        error = approximation_error(commands, node._flattened_subpaths, transform, scale)
                        self.assertLessEqual(error, 0.126)
                        self.assertEqual(node._path_version, version)
            self.root.remove(node)
            node.close()

    def test_native_mesh_cache_refines_and_reuses_across_animation(self):
        node = self.path(QUADRATIC)
        get_meshes = Path._get_meshes
        calls = []

        def tracked(path, renderer):
            calls.append(path)
            return get_meshes(path, renderer)

        with patch.object(Path, "_get_meshes", tracked):
            first = self.collect()
            counts = [mesh.vertex_count for mesh in node._cached_meshes]
            node.scale = 1.06
            enlarged = self.collect()
            self.assertTrue(all(mesh.vertex_count > count
                                for mesh, count in zip(node._cached_meshes, counts)))
            self.assertGreater(len(enlarged[5]), len(first[5]))
            meshes = node._cached_meshes
            for scale in (1.0, 1.03, 1.06, 1.0):
                node.scale = scale
                node.rotation += 0.2
                node.x += 2
                self.collect()
                self.assertIs(node._cached_meshes, meshes)
            self.assertEqual(len(calls), 2)
            self.collect(screen_scale=32)
            self.assertIsNot(node._cached_meshes, meshes)
            self.collect()
            self.assertLess(node._cached_mesh_scale, 32)
            self.assertEqual(len(calls), 4)

    def test_native_and_python_flattening_agree_without_a_local_tolerance_floor(self):
        for commands in (QUADRATIC, CUBIC,
                         [("M", (0, 0)), ("Q", (20, 0, 1, 0))],
                         [("M", (0, 0)), ("C", (-60, 0, 60, 0, 1, 0))]):
            coarse = _path.flatten_commands(commands, tolerance=0.05)
            fine = _path.flatten_commands(commands, tolerance=0.001)
            self.assertEqual(fine, _path._flatten_commands_py(commands, tolerance=0.001))
            self.assertGreater(len(fine[0]["points"]), len(coarse[0]["points"]))
            self.assertLessEqual(approximation_error(commands, fine, _identity(), 1), 0.00101)
        limited = _path.flatten_commands(CUBIC, tolerance=1e-12, max_depth=2)
        self.assertLessEqual(len(limited[0]["points"]), 9)

    def test_invalid_flattening_tolerances_fail_in_both_backends(self):
        for value in (0, -1, math.inf, math.nan):
            for flatten in (_path.flatten_commands, _path._flatten_commands_py):
                with self.subTest(value=value, flatten=flatten), self.assertRaises(ValueError):
                    flatten(CUBIC, tolerance=value)
            with self.assertRaises(ValueError):
                _scene_accel.path_flatten(CUBIC, value, 10)

    def test_round_strokes_meet_sagitta_target_and_backends_agree(self):
        subpaths = [{"points": [(0, 0), (100, 0), (100, 100)], "closed": False}]
        for width in (10, 2000):
            for scale in (1, 16):
                tolerance = 0.125 / scale
                options = dict(fill_color=None, stroke_color=(1, 0, 0, 1),
                               stroke_style=_path.StrokeStyle(width, "round", "round", 4),
                               tolerance=tolerance)
                _, native = _path.build_path_meshes(subpaths, **options)
                _, fallback = _path._build_path_meshes_py(subpaths, **options)
                self.assertEqual(len(native), 1)
                self.assertEqual(len(fallback), 1)
                self.assertEqual(native[0].index_bytes, fallback[0].index_bytes)
                vertices = list(struct.iter_unpack("<fff", native[0].vertex_bytes))
                other = list(struct.iter_unpack("<fff", fallback[0].vertex_bytes))
                for actual, expected in zip(vertices, other):
                    for a, b in zip(actual, expected):
                        self.assertAlmostEqual(a, b, delta=0.0002)
                # Two rectangles (four triangles) precede round join/cap fans.
                for i in range(12, len(vertices), 3):
                    center, a, b = vertices[i:i + 3]
                    distance = math.hypot((a[0] + b[0]) / 2 - center[0],
                                          (a[1] + b[1]) / 2 - center[1])
                    self.assertLessEqual((width / 2 - distance) * scale, 0.127)

    def test_command_bounds_include_quadratic_and_cubic_extrema(self):
        cases = [
            ([("M", (0, 0)), ("Q", (5, 0.7, 10, 0))], (0, 0, 10, 0.35)),
            ([("M", (0, 0)), ("Q", (2, 0, 1, 0))], (0, 0, 4 / 3, 0)),
            ([("M", (0, 0)), ("C", (1, 1, 2, 1, 3, 0))], (0, 0, 3, 0.75)),
            ([("M", (0, 0)), ("C", (-1, 0, 1, 0, 0, 0))],
             (-math.sqrt(3) / 6, 0, math.sqrt(3) / 6, 0)),
        ]
        for commands, expected in cases:
            with self.subTest(commands=commands):
                actual = _path.command_bounds(commands)
                for value, target in zip(actual, expected):
                    self.assertAlmostEqual(value, target)
                commands += [("M", (10000, 10000)), ("L", (10000, 10000)),
                             ("Q", (10000, 10000, 10000, 10000)), ("Z", None)]
                self.assertEqual(_path.command_bounds(commands), actual)

    def test_curve_bounds_stay_stable_across_quality_and_stroke_changes(self):
        for backend in (_path._PATH_ACCEL, None):
            with patch.object(_path, "_PATH_ACCEL", backend):
                for commands in (QUADRATIC, CUBIC):
                    node = self.path(commands)
                    for join, cap in (("round", "round"), ("miter", "square"), ("bevel", "butt")):
                        node.join, node.cap = join, cap
                        bounds = node._bounds()
                        for scale in (1, 32, 0.01, 8):
                            with self.subTest(backend=backend, commands=commands, join=join, scale=scale):
                                meshes = node._get_meshes(self.renderer, pixel_scale=scale)
                                self.assertEqual(node._bounds(), bounds)
                                for mesh in meshes:
                                    for x, y, _ in struct.iter_unpack("<fff", mesh.vertex_bytes):
                                        self.assertGreaterEqual(x, bounds[0] - 0.0001)
                                        self.assertLessEqual(x, bounds[2] + 0.0001)
                                        self.assertGreaterEqual(y, bounds[1] - 0.0001)
                                        self.assertLessEqual(y, bounds[3] + 0.0001)


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1",
                     "Set COCOA_PY_UI_TESTS=1 for desktop Metal path tests.")
class PathRenderQualityTests(unittest.TestCase):
    def setUp(self):
        self.window = gpu.Window("Path quality regression validation")
        self.addCleanup(self.window.close)
        self.scene = Scene()
        self.scene._init(self.window, "#000000")
        self.addCleanup(self.scene._close)

    def test_capture_refines_curves_and_preserves_following_live_render(self):
        node = Path(CUBIC, fill=None, stroke="#ffffff", stroke_width=10, x=180, y=100)
        self.scene.add(node)
        self.scene._render()
        points = len(node._flattened_subpaths[0]["points"])
        image = node.capture(rect=(-132, -36, 304, 72), size=(2432, 576))
        self.assertGreater(len(node._flattened_subpaths[0]["points"]), points)
        self.assertGreater(sum(image.rgba[3::4]), 0)
        self.scene._render()
        self.assertEqual(self.scene._renderer.screen_scale, self.window.scale)
        self.assertEqual(node._world_transform[4:], (180, 100))

    def test_texture_and_direct_rendering_do_not_share_offset_or_stale_style(self):
        node = Path(QUADRATIC, fill="#ff0000", stroke=None, x=120, y=100)
        self.scene.add(node)
        renderer = self.scene._renderer
        self.scene._render()
        local = node._cached_meshes[0].vertex_bytes
        node._ensure_texture(renderer, 1)
        self.assertEqual(node._cached_meshes[0].vertex_bytes, local)
        old_size = node._tex.size
        renderer.screen_scale *= 2
        try:
            node._ensure_texture(renderer, 1)
            self.assertGreaterEqual(node._tex.size[0], old_size[0] * 2 - 1)
            node.fill = "#00ff00"
            node._get_meshes(renderer)  # Clears direct-render dirty flags.
            node._ensure_texture(renderer, 1)
            image = node._tex.to_image(self.window)
            x = round((0 - node._local_bounds[0] + 2) * renderer.screen_scale)
            y = round((0 - node._local_bounds[1] + 2) * renderer.screen_scale)
            offset = (y * image.width + x) * 4
            self.assertEqual(image.rgba[offset:offset + 4], b"\0\xff\0\xff")
        finally:
            renderer.screen_scale = self.window.scale

    def test_layer_and_custom_path_emitter_refine_for_capture(self):
        class TexturePath(Path):
            def _emit(self, *args):
                return super()._emit(*args)

        layer = Layer(x=150, y=100)
        node = TexturePath(CUBIC, fill=None, stroke="#ffffff", stroke_width=10)
        layer.add(node)
        self.scene.add(layer)
        self.scene._render()
        points = len(node._flattened_subpaths[0]["points"])
        texture = node._tex
        texture_size = node._texture_size
        texture_scale = node._texture_pixel_scale
        image = layer.capture(rect=(-132, -36, 304, 72), size=(2432, 576))
        self.assertGreater(len(node._flattened_subpaths[0]["points"]), points)
        self.assertGreater(sum(image.rgba[3::4]), 0)
        self.assertIs(node._tex, texture)
        self.assertEqual(node._texture_size, texture_size)
        self.assertEqual(node._texture_pixel_scale, texture_scale)
        self.scene._render()

    def test_live_layer_zoom_passes_its_pixel_scale_to_paths(self):
        layer = Layer(x=150, y=100)
        node = Path(CUBIC, fill=None, stroke="#ffffff", stroke_width=10)
        layer.add(node)
        self.scene.add(layer)
        self.scene._render()
        points = len(node._flattened_subpaths[0]["points"])
        layer.scale = 4
        layer.invalidate()
        self.scene._render()
        self.assertGreater(len(node._flattened_subpaths[0]["points"]), points)
        self.assertGreaterEqual(node._cached_mesh_scale, self.window.scale * 4)

    def test_oversized_path_intermediate_restores_live_resources_after_failure(self):
        layer = Layer(x=150, y=100)
        node = Path(CUBIC, fill=None, stroke="#ffffff", stroke_width=10)
        layer.add(node)
        self.scene.add(layer)
        self.scene._render()
        path_texture, layer_texture = node._tex, layer._tex
        size, scale = node._texture_size, node._texture_pixel_scale
        with self.assertRaisesRegex(ValueError, "16384"):
            layer.capture(rect=(0, 0, 10, 10), size=(1000, 1000))
        self.assertIs(node._tex, path_texture)
        self.assertIs(layer._tex, layer_texture)
        self.assertEqual((node._texture_size, node._texture_pixel_scale), (size, scale))
        self.assertEqual(self.scene._renderer.screen_scale, self.window.scale)
        self.assertFalse(self.scene._renderer._capturing)
        self.scene._render()

    def test_failed_texture_resize_keeps_the_previous_valid_texture(self):
        node = Path(CUBIC, fill=None, stroke="#ffffff", stroke_width=10)
        self.scene.add(node)
        renderer = self.scene._renderer
        node._ensure_texture(renderer, 1)
        texture, size = node._tex, node._texture_size
        with self.assertRaisesRegex(ValueError, "16384"):
            node._ensure_texture(renderer, 100)
        self.assertIs(node._tex, texture)
        self.assertEqual(node._texture_size, size)
        self.assertTrue(node._ensure_texture(renderer, 1))
        self.assertGreater(sum(texture.to_image(self.window).rgba[3::4]), 0)

    def normal_image(self):
        width, height = self.window.size
        texture = gpu.Texture.render_target(round(width * self.window.scale),
                                            round(height * self.window.scale))
        try:
            self.scene._render(target_texture=texture)
            return texture.to_image(self.window)
        finally:
            texture.close()

    def test_first_layer_render_contains_refined_curve_without_invalidation(self):
        layer = Layer(x=20, y=100)
        path = Path([("M", (0, 0)), ("Q", (5, 0.7, 10, 0))],
                    fill=None, stroke="#ffffff", stroke_width=0.1, scale=100)
        layer.add(path)
        self.scene.add(layer)
        first = self.normal_image()
        scale = self.window.scale
        offset = (round(135 * scale) * first.width + round(520 * scale)) * 4
        self.assertEqual(first.rgba[offset:offset + 4], b"\xff\xff\xff\xff")
        size = layer._lsize
        layer.invalidate()
        second = self.normal_image()
        self.assertEqual(layer._lsize, size)
        self.assertEqual(first.rgba, second.rgba)

    def test_nested_layer_first_render_matches_invalidation_after_capture(self):
        outer = Layer(x=20, y=100)
        inner = Layer()
        path = Path([("M", (0, 0)), ("C", (3, 0.7, 7, 0.7, 10, 0))],
                    fill=None, stroke="#ffffff", stroke_width=0.1, scale=50)
        inner.add(path)
        outer.add(inner)
        self.scene.add(outer)
        first = self.normal_image()
        bounds = path._bounds()
        path.capture(rect=(-0.1, -0.1, 10.2, 1), size=(1020, 100))
        outer.invalidate()
        inner.invalidate()
        second = self.normal_image()
        self.assertEqual(path._bounds(), bounds)
        self.assertEqual(first.rgba, second.rgba)

    def test_large_path_can_be_exported_as_a_small_thumbnail(self):
        layer = Layer()
        path = Path(fill="#ffffff", stroke=None)
        path.polygon([(0, 0), (20000, 0), (20000, 100), (0, 100)])
        layer.add(path)
        self.scene.add(layer)
        allocations = []
        allocate = gpu.Texture.render_target

        def tracked(width, height, *args, **kwargs):
            allocations.append((width, height))
            return allocate(width, height, *args, **kwargs)

        with patch.object(gpu.Texture, "render_target", side_effect=tracked):
            image = layer.capture(rect=(0, 0, 20000, 100), size=(200, 1))
        self.assertEqual(image.size, (200, 1))
        self.assertEqual(image.rgba[400:404], b"\xff\xff\xff\xff")
        self.assertLessEqual(max(width for width, _ in allocations), 201)
        self.assertLessEqual(max(height for _, height in allocations), 2)

    def test_texture_cache_tracks_actual_downsampling_scale(self):
        path = Path(QUADRATIC, fill="#ffffff", stroke=None)
        self.scene.add(path)
        renderer = self.scene._renderer
        try:
            renderer.screen_scale = 0.125
            path._ensure_texture(renderer, 1)
            small = path._tex.size
            self.assertEqual(path._texture_pixel_scale, 0.125)
            renderer.screen_scale = 0.5
            path._ensure_texture(renderer, 1)
            self.assertGreater(path._tex.size[0], small[0] * 3)
            self.assertEqual(path._texture_pixel_scale, 0.5)
            renderer.screen_scale = 0.125
            path._ensure_texture(renderer, 1)
            texture = path._tex
            renderer.screen_scale = 0.05
            path._ensure_texture(renderer, 1)
            self.assertIsNot(path._tex, texture)
            self.assertEqual(path._texture_pixel_scale, 0.05)
        finally:
            renderer.screen_scale = self.window.scale


if __name__ == "__main__":
    unittest.main()
