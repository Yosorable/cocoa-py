"""Text editing contracts and real AppKit input-method integration."""
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import unittest

from _cocoa import _metal
from scene import ClipRect, Group, Layer, Node, Rect, Scene, ScrollView, TextField, TextView, TextInputSession, Touch, TouchPhase, gpu
from scene._text_input import process_inputs


class TextInputModelTests(unittest.TestCase):
    def test_newlines_and_grapheme_limits(self):
        family = "👨‍👩‍👧‍👦"
        field = TextField(text="a\r\nb\rc\nd")
        self.assertEqual(field.text, "a b c d")
        view = TextView(text="a\r\nb\rc")
        self.assertEqual(view.text, "a\nb\nc")
        field.max_length = 2
        field.text = "e\u0301" + family + "z"
        self.assertEqual(field.text, "e\u0301" + family)
        field.max_length = 1
        self.assertEqual(field.text, "e\u0301")
        field.max_length = 0
        self.assertEqual(field.text, "")

    def test_selection_uses_python_indices(self):
        field = TextField(text="A🌙中文")
        field.selection = (1, 2)
        field.replace_selection("星")
        self.assertEqual((field.text, field.selection), ("A星中文", (2, 2)))
        with self.assertRaises(ValueError):
            field.selection = (0, 100)
        with self.assertRaises(TypeError):
            field.selection = (0, 1.5)

    def test_detached_replacement_places_caret_after_normalized_text(self):
        for factory, options in ((TextField, {}), (TextView, {}),
                                 (TextInputSession, {}), (TextInputSession, {"multiline": True})):
            field = factory(**options)
            self.addCleanup(field.close)
            for replacement, single, multiline in (("A\r\nB", "A B", "A\nB"),
                                                    ("A\r\n🌙\rB\nC", "A 🌙 B C", "A\n🌙\nB\nC")):
                with self.subTest(factory=factory.__name__, options=options, replacement=replacement):
                    field.text = "xy"
                    field.selection = (1, 1)
                    field.replace_selection(replacement)
                    normalized = multiline if field.multiline else single
                    self.assertIsNone(field._native)
                    self.assertEqual(field.text, "x" + normalized + "y")
                    self.assertEqual(field.selection, (1 + len(normalized),) * 2)
                    field.replace_selection("!")
                    self.assertEqual(field.text, "x" + normalized + "!y")

    def test_invalid_options_fail_before_native_ui(self):
        before = gpu.resource_counts()["text_inputs"]
        for options in ({"width":0}, {"height":math.inf}, {"font_size":math.nan},
                        {"max_length":-1}, {"padding":(1, 2)}, {"text_color":(0, 0, math.nan, 1)},
                        {"submit_behavior":"newline"}, {"keyboard_type":"invalid"}):
            with self.subTest(options=options), self.assertRaises((ValueError, TypeError)):
                TextField(**options)
        with self.assertRaises(ValueError):
            TextView(secure=True)
        with self.assertRaises(UnicodeEncodeError):
            TextField(text="\ud800")
        self.assertEqual(gpu.resource_counts()["text_inputs"], before)

    def test_detached_and_closed_commands(self):
        field = TextField()
        field.focus(select_all=True).blur()
        with self.assertRaises(RuntimeError):
            field.undo()
        field.close()
        field.close()
        with self.assertRaises(RuntimeError):
            field.focus()
        self.assertIsNone(Scene().focused_input)
        self.assertIsNone(Scene().keyboard_frame)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("COCOA_PY_UI_TESTS") == "1",
                     "Set COCOA_PY_UI_TESTS=1 for AppKit editing tests.")
class TextInputNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="cocoa-py-text-input-")
        cls.addClassCleanup(cls.temp.cleanup)
        extension = Path(cls.temp.name) / ("_scene_text_input_fixture" + sysconfig.get_config_var("EXT_SUFFIX"))
        source = Path(__file__).parent / "native" / "scene_text_input_fixture.mm"
        subprocess.run(["xcrun", "clang++", "-std=c++17", "-fobjc-arc", "-bundle", "-undefined", "dynamic_lookup",
                        "-mmacosx-version-min=14.0", "-I" + sysconfig.get_paths()["include"],
                        "-framework", "AppKit", str(source), "-o", str(extension)], check=True)
        spec = importlib.util.spec_from_file_location("_scene_text_input_fixture", extension)
        cls.fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixture)

    def setUp(self):
        self.window = gpu.Window("Scene text input validation")
        self.addCleanup(self.window.close)
        self.scene = Scene()
        self.scene._init(self.window, "#cccccc")
        self.addCleanup(self.scene._close)

    def control(self, cls=TextField, **kwargs):
        field = cls(x=200, y=150, **kwargs)
        self.scene.add(field)
        self.scene._render()
        field.focus()
        self.scene._render()
        process_inputs(self.scene)
        self.assertTrue(field.focused)
        return field

    def native(self, field, command="inspect", text="", start=0, count=0, *, drain=True):
        result = json.loads(self.fixture.perform(field._native, command, text, start, count))
        if drain:
            process_inputs(self.scene)
        return result

    def test_real_edits_selection_and_undo(self):
        changes = []
        field = self.control(text="A🌙B", on_change=lambda c: changes.append(c.text))
        field.selection = (1, 2)
        self.assertEqual(self.native(field)["utf16_selection"], [1, 2])
        self.native(field, "insert", "中文")
        self.assertEqual((field.text, field.selection), ("A中文B", (3, 3)))
        self.assertEqual(changes[-1], "A中文B")
        field.undo(); process_inputs(self.scene)
        self.assertEqual(field.text, "A🌙B")
        field.redo(); process_inputs(self.scene)
        self.assertEqual(field.text, "A中文B")

    def test_native_editor_clips_without_restarting_composition(self):
        parent = Group(x=200, y=150, rotation=.25, clip=ClipRect(-50, -40, 100, 80, 15))
        field = TextField(240, 60, text="A", clip=ClipRect(-110, -30, 220, 60, 8))
        self.scene.add(parent.add(field))
        self.scene._render()
        field.focus()
        self.scene._render()
        process_inputs(self.scene)
        self.native(field, "marked", "拼", start=1)
        handle, marked = field._native, field.marked_range
        inside = self.native(field, "pointer", start=120, count=30)
        outside = self.native(field, "pointer", start=20, count=30)
        self.assertTrue(inside["pointer_hit"])
        self.assertFalse(outside["pointer_hit"])
        self.assertEqual(inside["clip_count"], 2)
        self.assertTrue(inside["has_mask"])
        parent.x += 30
        self.scene._render()
        self.assertEqual((field._native, field.marked_range), (handle, marked))
        parent.clip = field.clip = None
        self.scene._render()
        outside = self.native(field, "pointer", start=20, count=30)
        self.assertTrue(outside["pointer_hit"])
        self.assertFalse(outside["has_mask"])

    def test_native_clip_bridge_rejects_invalid_regions(self):
        field = self.control()
        for region in ((0,) * 10, (math.nan,) * 11, (1, 0, 0, 1, 0, 0, 0, 0, -1, 10, 0)):
            with self.subTest(region=region), self.assertRaises(ValueError):
                _metal.text_input_frame(field._native, field._world_transform, 1., True, (region,))

    def test_input_in_screen_layer_keeps_native_editor_at_viewport_position(self):
        field = TextField(180, 50, x=200, y=150)
        self.scene.ui.add(field)
        self.scene.position = (100, 100)
        self.scene.camera.zoom = 2
        self.scene.camera.rotation = .3
        self.scene._render()
        field.focus()
        self.scene._render()
        process_inputs(self.scene)
        state = self.native(field)
        x, y, width, height = state["frame"]
        self.assertEqual((x + width / 2, y + height / 2), (200, 150))
        self.assertEqual(state["rotation"], 0)

    def test_scrolling_reveals_native_input_and_keyboard_preserves_editor_height(self):
        view = ScrollView(220, 180, x=200, y=200, content_size=(220, 600))
        field = TextField(180, 50, x=110, y=570, text="A")
        self.scene.ui.add(view.add(field))
        self.scene._render()
        field.focus()
        self.scene._render()
        process_inputs(self.scene)
        self.assertTrue(field.focused)
        self.assertGreater(view.offset[1], 400)
        self.native(field, "marked", "拼", start=1)
        marked = field.marked_range
        self.scene._keyboard_frame = (0, 220, self.scene.width, 400)
        self.scene._render()
        state = self.native(field)
        self.assertEqual(state["frame"][3], 50)
        self.assertLessEqual(state["frame"][1] + state["frame"][3], 220)
        self.assertEqual(field.marked_range, marked)
        self.assertEqual(view.height, 180)

    def test_cropped_layer_native_editor_keeps_the_cached_quad_anchor(self):
        layer = Layer(x=20, y=50, scale=(2, 1), clip=(20, -15, 30, 30))
        field = TextField(40, 20, x=40, text="A", z=1)
        self.scene.ui.add(layer.add(Rect(200, 100), field))
        self.scene._render()
        field.focus()
        self.scene._render()
        state = self.native(field)
        self.assertEqual(field.world_position, (80., 50.))
        self.assertAlmostEqual(state["center"][0], field.world_position[0])
        self.assertAlmostEqual(state["center"][1], field.world_position[1])

    def test_keyboard_avoidance_updates_scroll_content_and_native_editor_immediately(self):
        from unittest.mock import patch

        view = ScrollView(220, 180, x=200, y=200, content_size=(220, 600))
        field = TextField(180, 50, x=110, y=570, text="A")
        decoration = Rect(20, 20, x=110, y=520)
        self.scene.ui.add(view.add(field, decoration))
        self.scene._render()
        field.focus()
        self.scene._render()
        process_inputs(self.scene)
        self.native(field, "marked", "拼", start=1)
        marked = field.marked_range
        original = view.offset[1]
        self.scene.speed = 0
        target = (0, 220, self.scene.width, self.scene.height - 220)
        changes = []
        self.scene.keyboard_changed = changes.append
        with patch.object(_metal, "text_input_keyboard", return_value=target):
            self.scene._tick_frame(0)
            self.scene._render()
            raised = view.offset[1]
            self.assertGreater(raised, original)
            self.assertEqual(raised, view.max_offset[1])
            state = self.native(field)
            self.assertTrue(state["scene_managed_placement"])
            native_center = state["frame"][1] + state["frame"][3] / 2
            self.assertAlmostEqual(native_center, field.world_position[1])
            self.assertAlmostEqual(field.world_position[1] - decoration.world_position[1], 50)
            self.assertLessEqual(state["frame"][1] + state["frame"][3], target[1])
            self.assertEqual(field.marked_range, marked)
            self.assertEqual(state["frame"][3], 50)
            self.assertEqual(view.height, 180)
            self.scene._tick_frame(.5)
            self.scene._render()
            self.assertEqual(view.offset[1], raised)
            self.assertEqual(changes, [target])

        field.blur()
        process_inputs(self.scene)
        with patch.object(_metal, "text_input_keyboard", return_value=None):
            self.scene._tick_frame(0)
            self.scene._render()
            self.assertEqual(view.offset[1], original)
            self.assertAlmostEqual(field.world_position[1] - decoration.world_position[1], 50)
            self.assertEqual(changes, [target, None])
            self.assertEqual(view.height, 180)

    def test_native_keyboard_avoidance_is_only_managed_by_vertical_scroll_ancestors(self):
        view = ScrollView(220, 180, x=200, y=200, content_size=(220, 600))
        field = TextField(180, 50, x=110, y=570)
        self.scene.ui.add(view.add(field))
        self.scene._render()
        field.focus()
        self.scene._render()
        self.assertTrue(self.native(field)["scene_managed_placement"])
        field.avoid_keyboard = False
        self.scene._render()
        self.assertFalse(self.native(field)["scene_managed_placement"])
        before = view.offset
        self.scene._keyboard_frame = (0, 220, self.scene.width, 400)
        self.scene._render()
        self.assertEqual(view.offset, before)
        field.avoid_keyboard = True
        view.direction = "horizontal"
        self.scene._render()
        self.assertFalse(self.native(field)["scene_managed_placement"])

    def test_dragging_over_inactive_input_defers_focus_until_a_completed_tap(self):
        view = ScrollView(220, 180, x=200, y=200, content_size=(220, 600))
        field = TextField(180, 50, x=110, y=60)
        self.scene.ui.add(view.add(field))
        self.scene._render()
        def touch(phase, point, timestamp):
            self.scene._pointer_router.feed(Touch(1, point, point, TouchPhase(phase), timestamp))
        point = field.world_position
        touch("began", point, 1.)
        self.scene._render()
        self.assertFalse(field.focused)
        touch("moved", (point[0], point[1] - 30), 1.1)
        touch("cancelled", (point[0], point[1] - 30), 1.2)
        self.scene._render()
        self.assertFalse(field.focused)
        point = field.world_position
        touch("began", point, 2.)
        touch("ended", point, 2.1)
        self.scene._render()
        process_inputs(self.scene)
        self.assertTrue(field.focused)

    def test_horizontal_drag_over_input_in_vertical_scroller_is_not_a_tap(self):
        view = ScrollView(240, 180, x=200, y=200, content_size=(240, 600))
        field = TextField(200, 50, x=120, y=60)
        self.scene.ui.add(view.add(field))
        self.scene._render()
        x, y = field.world_position
        for phase, point, timestamp in (("began", (x, y), 1.), ("moved", (x + 30, y), 1.1),
                                         ("ended", (x + 30, y), 1.2)):
            self.scene._pointer_router.feed(Touch(1, point, point, TouchPhase(phase), timestamp))
        self.scene._render()
        self.assertFalse(field.focused)
        self.assertIsNone(field._pending_focus)

    def test_drag_in_scrolling_content_keeps_editor_focused_and_background_tap_blurs(self):
        view = ScrollView(240, 180, x=200, y=200, content_size=(240, 600))
        field = TextField(140, 40, x=120, y=40)
        self.scene.ui.add(view.add(field))
        self.scene._render()
        field.focus()
        self.scene._render()
        process_inputs(self.scene)
        point = (90, 260)
        for phase, position, timestamp in (("began", point, 1.), ("moved", (90, 220), 1.1),
                                            ("cancelled", (90, 220), 1.2)):
            self.scene._pointer_router.feed(Touch(1, position, position, TouchPhase(phase), timestamp))
        self.scene._render()
        self.assertTrue(field.focused)
        for phase, timestamp in (("began", 2.), ("ended", 2.1)):
            self.scene._pointer_router.feed(Touch(2, point, point, TouchPhase(phase), timestamp))
        process_inputs(self.scene)
        self.assertFalse(field.focused)

    def test_programmatic_replacement_emits_change_but_assignment_is_silent(self):
        changes = []
        field = self.control(on_change=lambda c: changes.append(c.text))
        field.replace_selection("abc"); process_inputs(self.scene)
        self.assertEqual(changes, ["abc"])
        field.text = "reset"; process_inputs(self.scene)
        self.assertEqual(changes, ["abc"])
        self.assertFalse(self.native(field)["can_undo"])

    def test_queued_edits_do_not_revert_a_programmatic_write(self):
        changes = []
        field = self.control(on_change=lambda c: changes.append(c.text))
        self.native(field, "insert", "old", drain=False)
        field.text = "new"
        process_inputs(self.scene)
        self.assertEqual(field.text, "new")
        self.assertEqual(changes, [])

    def test_ime_provisional_text_survives_render_and_style_updates(self):
        field = self.control(max_length=2)
        self.native(field, "marked", "zhongwen", 8)
        self.assertTrue(field.is_composing)
        self.assertEqual(field.text, "zhongwen")
        field.border_color = "#4488ff"
        self.scene._render()
        self.assertTrue(self.native(field)["marked_range"] is not None)
        self.native(field, "marked", "中文", 2)
        self.native(field, "commit")
        self.assertEqual(field.text, "中文")
        self.assertFalse(field.is_composing)

    def test_ime_commit_enforces_limit_without_splitting_graphemes(self):
        field = self.control(max_length=1)
        self.native(field, "marked", "中文", 2)
        self.native(field, "commit")
        self.assertEqual(field.text, "中")
        self.assertFalse(field.is_composing)
        self.native(field, "insert", "x")
        self.assertEqual(field.text, "中")
        field.undo(); process_inputs(self.scene)
        self.assertEqual(field.text, "")
        field.redo(); process_inputs(self.scene)
        self.assertEqual(field.text, "中")

    def test_typing_at_limit_rejects_extra_but_allows_replacing_selection(self):
        field = self.control(text="ab", max_length=2)
        field.selection = (2, 2)
        self.native(field, "insert", "c")
        self.assertEqual(field.text, "ab")
        field.selection = (0, 1)
        self.native(field, "insert", "🌙")
        self.assertEqual(field.text, "🌙b")

    def test_single_line_paste_normalizes_newlines_and_preserves_undo(self):
        field = self.control()
        self.native(field, "insert", "one\ntwo\r\nthree")
        self.assertEqual(field.text, "one two three")
        field.undo(); process_inputs(self.scene)
        self.assertEqual(field.text, "")

    def test_secure_editing_limit_and_selection_events(self):
        selected = []
        field = self.control(secure=True, text="ab", max_length=2,
                             on_selection_change=lambda c: selected.append(c.selection))
        field.selection = (1, 1)
        self.native(field, "insert", "x")
        self.assertEqual(field.text, "ab")
        self.native(field, "selection", start=0, count=1)
        self.assertEqual(field.selection, (0, 1))
        self.assertEqual(selected[-1], (0, 1))
        self.native(field, "insert", "z")
        self.assertEqual(field.text, "zb")

    def test_submit_and_multiline_return(self):
        submitted = []
        field = self.control(text="query", on_submit=lambda c: submitted.append(c.text))
        self.native(field, "command", "insertNewline:")
        self.assertEqual(submitted, ["query"])
        self.assertFalse(field.focused)
        view = self.control(TextView, text="one")
        view.selection = (3, 3)
        self.native(view, "command", "insertNewline:")
        self.assertEqual(view.text, "one\n")
        self.assertTrue(view.focused)

    def test_tab_navigation_skips_hidden_disabled_and_read_only(self):
        a = self.control()
        hidden = TextField()
        hidden.visible = False
        self.scene.add(TextField(enabled=False), TextField(read_only=True), hidden)
        b = TextField(x=200, y=250)
        self.scene.add(b); self.scene._render()
        self.native(a, "command", "insertTab:")
        self.scene._render(); process_inputs(self.scene)
        self.assertTrue(b.focused)
        self.assertFalse(a.focused)
        self.native(b, "command", "insertBacktab:")
        self.scene._render(); process_inputs(self.scene)
        self.assertTrue(a.focused)

    def test_disabled_hidden_and_read_only(self):
        field = self.control(text="keep")
        field.read_only = True
        self.scene._render()
        self.native(field, "insert", "bad")
        self.assertEqual(field.text, "keep")
        field.read_only = False
        field.visible = False
        self.scene._render(); process_inputs(self.scene)
        self.assertFalse(field.focused)
        self.assertTrue(self.native(field)["hidden"])

    def test_capture_preserves_composition_and_masks_secure_text(self):
        field = self.control(text="A")
        field.selection = (1, 1)
        self.native(field, "marked", "zhong", 5)
        before = self.native(field)
        image = field.capture(rect=(-120, -22, 240, 44), size=(480, 88))
        self.assertEqual(image.size, (480, 88))
        after = self.native(field)
        for key in ("text", "marked_range", "selection", "focused", "can_undo"):
            self.assertEqual(before[key], after[key], key)
        secret = self.control(secure=True, text="abcdef")
        rect = (-120, -22, 240, 44)
        first = secret.capture(rect=rect, size=(240, 44))
        secret.text = "UVWXYZ"
        second = secret.capture(rect=rect, size=(240, 44))
        self.assertEqual(first.rgba, second.rgba)
        self.assertGreater(len(set(first.rgba)), 3)

    def test_detach_preserves_latest_text_and_recreates_position(self):
        field = self.control()
        self.native(field, "insert", "latest", drain=False)
        old = field._native
        self.scene.remove(field)
        self.assertEqual(field.text, "latest")
        self.assertIsNone(field._native)
        self.scene.add(field); field.focus(); self.scene._render(); process_inputs(self.scene)
        self.assertNotEqual(field._native, old)
        self.assertTrue(self.native(field)["first_responder"])

    def test_reparent_in_one_scene_preserves_active_composition(self):
        field = self.control()
        self.native(field, "marked", "zhong", 5)
        original = field._native
        container = Node(x=20, y=15)
        self.scene.add(container)
        container.add(field)
        self.scene._render()
        self.assertEqual(field._native, original)
        self.assertTrue(self.native(field)["first_responder"])
        self.assertEqual(field.marked_range, (0, 5))

    def test_native_rejects_invalid_updates_transactionally(self):
        field = self.control(text="keep")
        for update in ({"selection":(0, 100)}, {"padding":(1, 2)}, {"width":float("inf")},
                       {"background":(1, 1, -1, 1)}, {"secure":True}, {"unknown":1}):
            with self.subTest(update=update), self.assertRaises((ValueError, TypeError)):
                _metal.text_input_update(field._native, update)
        self.assertEqual(self.native(field)["text"], "keep")
        with self.assertRaises(ValueError):
            _metal.text_input_update(field._native, {"width":1e308})
        with self.assertRaises(ValueError):
            _metal.text_input_frame(field._native, (1e308, 0, 0, 1, 0, 0), 1, True)

    def test_numeric_conversion_owns_a_snapshot_of_mutable_sequences(self):
        field = self.control()
        values = []
        class Number:
            def __float__(self):
                values.clear()
                return 1.0
        values[:] = [Number(), 1.0, 1.0, 1.0]
        _metal.text_input_update(field._native, {"background":values})
        self.assertEqual(values, [])

    def test_transition_releases_the_old_editor_and_activates_the_incoming_one(self):
        from scene import transition
        from scene._scene import _SceneDirector
        class Form(Scene):
            def setup(self):
                self.field = TextField(x=200, y=150)
                self.add(self.field)
                self.field.focus()
        director = _SceneDirector(self.window, "#cccccc", 60)
        self.addCleanup(director._close)
        director._setup_first(Form)
        director._frame(0.01)
        old = director._current.field
        director._present(Form, transition.fade(0.02))
        director._frame(0.01)
        self.assertFalse(old.focused)
        director._frame(0.02)
        director._frame(0.01)
        self.assertIsNone(old._native)
        self.assertTrue(director._current.field.focused)
        self.assertEqual(gpu.resource_counts()["text_inputs"], 1)

    def test_removal_in_a_callback_discards_remaining_native_events(self):
        field = self.control()
        field.on_change = lambda c: self.scene.remove(c)
        self.native(field, "insert", "keep", drain=False)
        self.native(field, "selection", start=0, count=1)
        self.assertIsNone(field.parent)
        self.assertFalse(field.focused)
        self.assertEqual(field.text, "keep")

    def test_removing_a_control_commits_composition(self):
        field = self.control(max_length=1)
        self.native(field, "marked", "中文", 2)
        self.scene.remove(field)
        self.assertEqual(field.text, "中")
        self.assertFalse(field.is_composing)

    def test_focus_state_is_consistent_immediately_after_render(self):
        first = self.control()
        second = TextField(x=200, y=250)
        self.scene.add(second); second.focus(); self.scene._render()
        self.assertIs(self.scene.focused_input, second)
        self.assertFalse(first.focused)

    def test_close_and_failed_snapshot_release_resources(self):
        before = gpu.resource_counts()["text_inputs"]
        field = self.control()
        with self.assertRaises(ValueError):
            _metal.text_input_snapshot(field._native, 1e308)
        self.scene.remove(field); field.close(); field.close()
        self.assertEqual(gpu.resource_counts()["text_inputs"], before)
        field = self.control()
        self.window.close()
        self.assertEqual(gpu.resource_counts()["text_inputs"], before)
        field.close()

    def test_closing_an_attached_control_does_not_recreate_it(self):
        field = self.control()
        field.close()
        self.scene._render()
        self.assertIsNone(field._native)
        self.assertEqual(gpu.resource_counts()["text_inputs"], 0)

    def test_pending_style_is_applied_with_a_text_write(self):
        field = self.control()
        field.width = 320
        field.text = "changed"
        self.scene._render()
        self.assertAlmostEqual(self.native(field)["frame"][2], 320)

    def test_pending_read_only_prevents_undo_before_the_next_render(self):
        field = self.control()
        self.native(field, "insert", "keep")
        field.read_only = True
        field.undo(); process_inputs(self.scene)
        self.assertEqual(field.text, "keep")

    def test_rotated_native_editor_matches_the_scene_quad(self):
        field = self.control(rotation=math.radians(30), scale=(1.5, 0.8))
        state = self.native(field)
        self.assertAlmostEqual(state["rotation"], 30, places=5)
        self.assertAlmostEqual(state["center"][0], field.world_position[0])
        self.assertAlmostEqual(state["center"][1], field.world_position[1])

    def test_event_queue_is_bounded_and_retains_latest_state(self):
        field = self.control()
        for i in range(160):
            self.native(field, "insert", "x", drain=False)
        events = _metal.text_input_events(field._native)
        self.assertLessEqual(len(events), 256)
        self.assertEqual(events[-1]["text"], "x" * 160)

    def test_replaced_snapshots_release_python_texture_references(self):
        reference = object()
        field = TextField(text="old", x=200, y=150)
        self.scene.add(field); self.scene._render()
        old = field._snapshot_texture
        field.text = "new"; self.scene._render()
        self.assertEqual(sys.getrefcount(old), sys.getrefcount(reference))
        latest = field._snapshot_texture
        field.close()
        self.assertEqual(sys.getrefcount(latest), sys.getrefcount(reference))

    def test_controls_have_independent_undo_histories(self):
        first = self.control()
        self.native(first, "insert", "first")
        second = self.control()
        self.native(second, "insert", "second")
        second.text = "reset"
        first.focus(); self.scene._render(); process_inputs(self.scene)
        first.undo(); process_inputs(self.scene)
        self.assertEqual(first.text, "")
        self.assertEqual(second.text, "reset")

    def test_layer_snapshot_resolution_does_not_leak(self):
        layer = Layer(x=200, y=150, scale=1.2)
        field = TextField(text="Layer")
        layer.add(field); self.scene.add(layer); self.scene._render()
        original = field._snapshot_texture
        count = gpu.resource_counts()["textures"]
        for size in ((120, 22), (720, 132)):
            field.capture(rect=(-120, -22, 240, 44), size=size)
            self.assertIs(field._snapshot_texture, original)
        self.assertEqual(gpu.resource_counts()["textures"], count)
        field.focus(); self.scene._render(); process_inputs(self.scene)
        self.assertTrue(field.focused)

    def test_offscreen_render_includes_a_focused_input_in_a_layer(self):
        layer = Layer()
        field = TextField(text="Visible", x=200, y=150)
        layer.add(field); self.scene.add(layer)
        self.scene._render(); field.focus(); self.scene._render(); process_inputs(self.scene)
        self.scene._render()
        width, height = (round(v * self.window.scale) for v in self.window.size)
        texture = gpu.Texture.render_target(width, height)
        self.addCleanup(texture.close)
        self.scene._render(target_texture=texture)
        image = texture.to_image(self.window)
        x, y = round(200 * self.window.scale), round(165 * self.window.scale)
        pixel = image.rgba[(y * width + x) * 4: (y * width + x) * 4 + 4]
        self.assertEqual(pixel, b"\xff\xff\xff\xff")
        self.assertTrue(self.native(field)["first_responder"])


if __name__ == "__main__":
    unittest.main()
