"""Application-drawn text input uses the same real native editing machinery."""
import math
import os
import sys
import unittest
from unittest.mock import patch

from _cocoa import _metal
from scene import Node, Rect, Scene, TextField, TextInputSession, gpu
from scene._text_input import process_inputs
import test_scene_text_input as controls


class SessionModelTests(unittest.TestCase):
    def test_session_is_not_a_node_and_can_prepare_text_without_a_window(self):
        session = TextInputSession("a\r\nb", max_length=4)
        self.assertNotIsInstance(session, Node)
        self.assertEqual(session.text, "a b")
        self.assertFalse(session.active)
        self.assertIsNone(session._native)
        session.selection = (0, 1)
        session.replace_selection("中文")
        self.assertEqual(session.text, "中文 b")
        session.end(); session.close(); session.close()

    def test_invalid_options_and_scene_fail_before_native_allocation(self):
        before = gpu.resource_counts()["text_inputs"]
        for options in ({"caret_rect":(0, 0, 0, 20)}, {"caret_rect":(math.inf, 0, 1, 20)},
                        {"width":200}, {"on_next":1}, {"multiline":True, "secure":True}):
            with self.subTest(options=options), self.assertRaises((TypeError, ValueError)):
                TextInputSession(**options)
        session = TextInputSession()
        with self.assertRaises(TypeError): session.begin(Node())
        with self.assertRaises(RuntimeError): session.begin(Scene())
        with self.assertRaises(TypeError): Node().add(session)
        self.assertEqual(gpu.resource_counts()["text_inputs"], before)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("COCOA_PY_UI_TESTS") == "1",
                     "Set COCOA_PY_UI_TESTS=1 for native text-session tests.")
class SessionNativeTests(unittest.TestCase):
    setUpClass = classmethod(controls.TextInputNativeTests.setUpClass.__func__)
    setUp = controls.TextInputNativeTests.setUp
    native = controls.TextInputNativeTests.native

    def session(self, **options):
        session = TextInputSession(caret_rect=(180, 100, 2, 24), **options)
        session.begin(self.scene)
        self.scene._render()
        process_inputs(self.scene)
        self.assertTrue(session.active)
        return session

    def test_focus_without_drawing_or_intercepting_scene_touches(self):
        textures = gpu.resource_counts()["textures"]
        session = self.session()
        state = self.native(session)
        self.assertTrue(state["first_responder"])
        self.assertTrue(state["headless"])
        self.assertEqual(state["alpha"], 0)
        self.assertFalse(state["accepts_pointer"])
        self.assertEqual(self.scene.children, [])
        self.assertEqual(gpu.resource_counts()["textures"], textures)
        self.assertIs(self.scene.focused_input, session)

    def test_typing_selection_and_undo(self):
        changes = []
        session = self.session(text="A🌙B", on_change=lambda s: changes.append(s.text))
        session.selection = (1, 2)
        self.native(session, "insert", "中文")
        self.assertEqual((session.text, session.selection), ("A中文B", (3, 3)))
        self.assertEqual(changes[-1], "A中文B")
        session.undo(); process_inputs(self.scene)
        self.assertEqual(session.text, "A🌙B")
        session.redo(); process_inputs(self.scene)
        self.assertEqual(session.text, "A中文B")

    def test_caret_updates_preserve_composition_and_place_input_method_ui(self):
        session = self.session(max_length=2)
        self.native(session, "marked", "zhongwen", 8)
        session.caret_rect = (310, 210, 3, 28)
        self.scene._render()
        state = self.native(session)
        self.assertEqual(state["caret_rect"], [310, 210, 3, 28])
        self.assertEqual(session.text, "zhongwen")
        self.assertTrue(session.is_composing)
        self.native(session, "marked", "中文", 2)
        self.native(session, "commit")
        self.assertEqual(session.text, "中文")
        self.assertFalse(session.is_composing)

    def test_end_commits_and_begin_resumes_without_allocating_another_editor(self):
        session = self.session(max_length=1)
        handle = session._native
        self.native(session, "marked", "中文", 2)
        session.end(); process_inputs(self.scene)
        self.assertEqual(session.text, "中")
        self.assertFalse(session.active)
        self.assertFalse(session.is_composing)
        session.begin(self.scene); self.scene._render(); process_inputs(self.scene)
        self.assertEqual(session._native, handle)
        self.assertTrue(session.active)
        session.undo(); process_inputs(self.scene)
        self.assertEqual(session.text, "")
        session.redo(); process_inputs(self.scene)
        self.assertEqual(session.text, "中")

    def test_begin_on_an_active_session_does_not_discard_composition(self):
        session = self.session()
        self.native(session, "marked", "zhong", 5)
        session.begin(self.scene); self.scene._render()
        self.assertTrue(self.native(session)["marked_range"] is not None)

    def test_commit_composition_keeps_the_keyboard_session_active(self):
        session = self.session(max_length=1)
        self.native(session, "marked", "中文", 2)
        session.commit_composition(); process_inputs(self.scene)
        self.assertEqual(session.text, "中")
        self.assertFalse(session.is_composing)
        self.assertTrue(session.active)

    def test_background_touch_is_owned_by_the_application(self):
        session = self.session()
        self.scene.add(Rect(200, 100, x=200, y=200))
        self.scene._render()
        event = dict(phase=0, id=1, x=200, y=200, prev_x=200, prev_y=200)
        with patch.object(gpu.Window, "consume_touches", return_value=[event]):
            self.scene._process_touches()
        self.assertTrue(session.active)
        self.scene.dismiss_keyboard(); process_inputs(self.scene)
        self.assertFalse(session.active)

    def test_focus_can_switch_between_a_control_and_a_session(self):
        session = self.session()
        control = TextField(x=200, y=200)
        self.scene.add(control); control.focus(); self.scene._render()
        self.assertTrue(control.focused)
        self.assertFalse(session.active)
        session.begin(self.scene); self.scene._render()
        self.assertFalse(control.focused)
        self.assertTrue(session.active)

    def test_submit_and_multiline_are_independent_of_a_visible_control(self):
        submitted = []
        session = self.session(text="query", on_submit=lambda s: submitted.append(s.text))
        self.native(session, "command", "insertNewline:")
        self.assertEqual(submitted, ["query"])
        self.assertFalse(session.active)
        multi = self.session(multiline=True)
        self.native(multi, "insert", "first")
        self.native(multi, "command", "insertNewline:")
        self.native(multi, "insert", "second")
        self.assertEqual(multi.text, "first\nsecond")

    def test_navigation_callbacks_do_not_focus_unrelated_nodes(self):
        navigation = []
        session = self.session(on_next=lambda s: navigation.append("next"),
                               on_previous=lambda s: navigation.append("previous"))
        self.scene.add(TextField(x=200, y=200)); self.scene._render()
        self.native(session, "command", "insertTab:")
        self.native(session, "command", "insertBacktab:")
        self.scene._render()
        self.assertEqual(navigation, ["next", "previous"])
        self.assertTrue(session.active)

    def test_session_contributes_no_pixels_to_capture(self):
        before = self.scene.capture(rect=(0, 0, 400, 300), size=(400, 300))
        session = self.session(text="Only the application may draw this")
        after = self.scene.capture(rect=(0, 0, 400, 300), size=(400, 300))
        self.assertEqual(before.rgba, after.rgba)
        with self.assertRaises(ValueError):
            _metal.text_input_snapshot(session._native, 1.0)

    def test_scene_shutdown_closes_sessions_outside_the_node_tree(self):
        session = self.session()
        self.scene._close()
        self.assertIsNone(session._native)
        self.assertFalse(session.active)
        self.assertEqual(gpu.resource_counts()["text_inputs"], 0)
        with self.assertRaises(RuntimeError): session.begin(self.scene)
        session.close()

    def test_session_can_move_to_another_scene(self):
        session = self.session(text="move")
        other = Scene(); other._init(self.window, "#cccccc")
        self.addCleanup(other._close)
        session.begin(other); other._render(); process_inputs(other)
        self.assertEqual(self.scene._text_inputs, {})
        self.assertEqual(session.text, "move")
        self.scene._close()
        self.assertTrue(session.active)

    def test_callback_can_close_its_session(self):
        session = self.session(on_change=lambda s: s.close())
        self.native(session, "insert", "last")
        self.assertIsNone(session._native)
        self.assertEqual(session.text, "last")
        self.assertEqual(self.scene._text_inputs, {})


if __name__ == "__main__":
    unittest.main()
