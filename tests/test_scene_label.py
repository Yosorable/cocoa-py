"""Paragraph measurement, shaping, and rendered layout regressions."""
import math
import os
import unittest

from _cocoa import _metal, _scene_accel
from scene import Group, Label, Layer, Scene, gpu
from scene._common import _apply


class LabelLayoutTests(unittest.TestCase):
    def resource_counts(self):
        return {key: value for key, value in gpu.resource_counts().items()
                if key not in ("resident_bytes", "phys_footprint_bytes")}

    def test_measurement_needs_no_window_or_texture(self):
        before = self.resource_counts()
        label = Label("Hello 世界\n👩🏽‍💻 e\u0301", size=24)
        width, height = label.measure()
        self.assertGreater(width, 50)
        self.assertGreater(height, 48)
        self.assertEqual(label.line_count, 2)
        self.assertFalse(label.truncated)
        self.assertEqual(self.resource_counts(), before)

    def test_wrapping_explicit_blank_lines_spacing_and_max_lines(self):
        label = Label("one two three four five six", max_width=80, size=20)
        self.assertEqual(label.measure()[0], 80)
        self.assertGreater(label.line_count, 2)
        count, height = label.line_count, label.measure()[1]
        label.line_spacing = 7
        self.assertAlmostEqual(label.measure()[1], height + (count - 1) * 7)
        label.max_lines = 2
        label.overflow = "ellipsis"
        self.assertEqual(label.line_count, 2)
        self.assertTrue(label.truncated)
        label.text = "a\r\n\r\nb\n"
        label.max_lines = None
        self.assertEqual(label.line_count, 4)
        label.text = ""
        self.assertEqual(label.line_count, 1)
        self.assertEqual(label.measure()[0], 80)
        self.assertFalse(label.truncated)

    def test_narrow_wrap_keeps_emoji_and_combining_clusters_together(self):
        for text in ("👨‍👩‍👧‍👦", "👩🏽‍💻", "e\u0301", "🇨🇳"):
            for wrap in ("word", "char"):
                with self.subTest(text=text, wrap=wrap):
                    label = Label(text * 3, size=24, max_width=1, wrap=wrap)
                    self.assertEqual(label.line_count, 3)
                    self.assertTrue(label.truncated)

    def test_invalid_layout_is_rejected_before_raster_allocation(self):
        for kwargs in ({"size": 0}, {"size": math.nan}, {"max_width": -1},
                       {"max_width": math.inf}, {"line_spacing": -1},
                       {"max_lines": 0}, {"max_lines": 1.5}, {"alignment": "bad"},
                       {"wrap": "bad"}, {"overflow": "bad"}, {"font": 42}):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, TypeError)):
                Label(**kwargs)
        before = self.resource_counts()
        with self.assertRaises(ValueError):
            Label("x", max_width=100000).measure()
        with self.assertRaises(ValueError):
            _metal.text_layout("x", 20., None, 40., "left", 0., 0, "clip", "word", math.inf, True)
        self.assertEqual(self.resource_counts(), before)


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1", "Enable desktop Metal tests")
class LabelRenderingTests(unittest.TestCase):
    def setUp(self):
        self.window = gpu.Window("Scene label validation")
        self.addCleanup(self.window.close)
        self.scene = Scene()
        self.scene._init(self.window, "#000000")
        self.addCleanup(self.scene._close)

    def ink_bounds(self, image):
        points = [(i // 4 % image.width, i // 4 // image.width)
                  for i in range(0, len(image.rgba), 4) if max(image.rgba[i:i + 3]) > 80]
        self.assertTrue(points)
        return (min(p[0] for p in points), min(p[1] for p in points),
                max(p[0] for p in points), max(p[1] for p in points))

    def capture(self):
        return self.scene.capture(rect=(0, 0, 240, 160), size=(240, 160))

    def test_alignment_changes_pixels_and_keeps_box_measurement(self):
        label = Label("Hello", size=28, max_width=180, x=120, y=80)
        self.scene.add(label)
        boxes = []
        measured = label.measure()
        for alignment in ("left", "center", "right"):
            label.alignment = alignment
            boxes.append(self.ink_bounds(self.capture()))
            self.assertEqual(label.measure(), measured)
        self.assertLess(boxes[0][0] + 30, boxes[1][0])
        self.assertLess(boxes[1][0] + 30, boxes[2][0])
        self.assertEqual(boxes[0][1::2], boxes[2][1::2])

    def test_text_is_upright_and_explicit_lines_keep_their_order(self):
        label = Label("WWWW\nI", size=28, max_width=180, x=120, y=80)
        self.scene.add(label)
        image = self.capture()
        widths = []
        for low, high in ((0, 80), (80, 160)):
            xs = [x for y in range(low, high) for x in range(image.width)
                  if image.rgba[(y * image.width + x) * 4] > 80]
            widths.append(max(xs) - min(xs))
        self.assertGreater(widths[0], widths[1] * 4)

    def test_scaled_label_hit_bounds_are_local_and_match_native(self):
        label = Label("Scale", size=20, x=100, y=75, scale=(2., .75), rotation=.15)
        self.scene.add(label)
        self.scene._render()
        width, height = label.measure()
        self.assertEqual(label._rendered_size, (width, height))
        for factor, expected in ((.49, True), (.55, False)):
            point = _apply(label._world_transform, (width * factor, 0))
            self.assertEqual(label.contains_point(*point), expected)
            self.assertEqual(_scene_accel.contains_point(label, *point), expected)
        self.assertAlmostEqual(label._collider()[3], width)

    def test_native_and_cached_layer_render_the_same_paragraph(self):
        label = Label("中文 👩🏽‍💻\nParagraph", size=22, max_width=180, alignment="center", x=120, y=80)
        self.scene.add(label)
        direct = self.capture()
        self.scene.remove(label)
        layer = Layer()
        layer.add(label)
        self.scene.add(layer)
        cached = self.capture()
        # Offscreen Layer padding can shift sampling by a fraction of a pixel.
        for a, b in zip(self.ink_bounds(direct), self.ink_bounds(cached)):
            self.assertLessEqual(abs(a - b), 1)
        label.text = "Changed"
        changed = self.capture()
        self.assertNotEqual(changed.rgba, cached.rgba)

    def test_ellipsis_truncates_and_capture_preserves_live_texture_and_measurement(self):
        label = Label("This is a long sentence that wraps over several lines.",
                      size=24, max_width=150, max_lines=2, x=120, y=80)
        self.scene.add(label)
        clip = self.capture()
        label.overflow = "ellipsis"
        ellipsis = self.capture()
        self.assertNotEqual(clip.rgba, ellipsis.rgba)
        self.scene._render()
        renderer = self.scene._renderer
        entries = dict(renderer._tc)
        size = label._rendered_size
        self.scene.capture(rect=(0, 0, 240, 160), size=(720, 480))
        self.assertEqual(renderer._tc, entries)
        self.assertEqual(label._rendered_size, size)
        label.max_width = 210
        self.scene._render()
        self.assertEqual(label._rendered_size[0], 210)


if __name__ == "__main__":
    unittest.main()
