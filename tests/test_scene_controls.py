"""Scene control state, real rendering, focus, and gesture arbitration."""
import math
import os
from types import SimpleNamespace
import unittest

from scene import Button, ControlStyle, Group, Label, Layer, Scene, ScrollView, Slider, TextField, Toggle, Touch, TouchPhase, gpu
from scene._common import _apply


class ControlModelTests(unittest.TestCase):
    def test_ranges_clamp_quantize_and_update_atomically(self):
        slider = Slider(.96, step=.3)
        self.assertEqual(slider.value, 1.)
        slider.value = .44
        self.assertAlmostEqual(slider.value, .3)
        slider.value = -100
        self.assertEqual(slider.value, 0)
        with self.assertRaises(ValueError):
            slider.set_range(5, 2)
        self.assertEqual((slider.minimum, slider.maximum, slider.step), (0., 1., .3))
        slider.set_range(7, 7)
        self.assertEqual(slider.value, 7)

    def test_programmatic_values_are_silent_unless_requested(self):
        events = []
        for control in (Toggle(), Slider()):
            control.on_change = lambda node, value: events.append(value)
            control.value = 1
            control._dispatch_ui_events()
            self.assertEqual(events, [])
            control.set_value(0, notify=True)
            control._dispatch_ui_events()
            self.assertEqual(events, [0])
            events.clear()

    def test_invalid_options_fail_without_native_resources(self):
        for make in (lambda: Button(width=0), lambda: Slider(step=0), lambda: Slider(value=math.nan),
                     lambda: Slider(minimum=2, maximum=1), lambda: Slider(direction="both"),
                     lambda: Toggle(on_change=42), lambda: Button(style={}),
                     lambda: ControlStyle(disabled_opacity=2), lambda: ControlStyle(thumb_size=-1)):
            with self.assertRaises((ValueError, TypeError)):
                make()


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1", "Enable desktop Metal tests")
class ControlInteractionTests(unittest.TestCase):
    def setUp(self):
        self.window = gpu.Window("Scene controls validation")
        self.addCleanup(self.window.close)
        self.scene = Scene()
        self.scene._init(self.window, "#000000")
        self.addCleanup(self.scene._close)
        self.events = []

    def touch(self, phase, position=(120, 80), *, identifier=1, dispatch=True, timestamp=1.):
        self.scene._pointer_router.feed(Touch(identifier, position, position, TouchPhase(phase), timestamp))
        if dispatch:
            self.scene._dispatch_ui_events()

    def key(self, name, phase="down", *, repeat=False, modifiers=()):
        result = self.scene._focus_manager.handle_key(SimpleNamespace(
            key=name, phase=phase, repeat=repeat, modifiers=frozenset(modifiers)))
        self.scene._dispatch_ui_events()
        return result

    def button(self, **kwargs):
        button = Button("Continue", x=120, y=80, on_click=lambda node: self.events.append("click"), **kwargs)
        self.scene.add(button)
        self.scene._render()
        return button

    def scroller(self, node):
        view = ScrollView(240, 200, x=120, y=100, content_size=(240, 600))
        node.position = (120, 80)
        view.add(node)
        self.scene.add(view)
        self.scene._render()
        return view

    def test_button_press_leave_return_and_release_or_cancel(self):
        button = self.button()
        self.touch("began")
        self.assertTrue(button.pressed)
        self.touch("moved", (300, 80))
        self.assertFalse(button.pressed)
        self.touch("moved")
        self.assertTrue(button.pressed)
        self.touch("ended")
        self.assertEqual(self.events, ["click"])
        self.assertIs(self.scene.focused_node, button)
        self.assertFalse(button.focus_visible)
        button.focus()
        self.assertTrue(button.focus_visible)
        self.touch("began")
        self.touch("cancelled")
        self.touch("ended")
        self.assertEqual(self.events, ["click"])
        self.assertFalse(button.pressed)

    def test_disabled_hidden_removed_and_second_finger_do_not_activate(self):
        button = self.button()
        self.touch("began")
        self.touch("began", identifier=2)
        self.touch("ended", identifier=2)
        self.assertTrue(button.pressed)
        button.enabled = False
        button.enabled = True
        self.touch("ended")
        self.assertEqual(self.events, [])
        self.touch("began")
        button.visible = False
        self.scene._prepare_layout()
        self.assertFalse(button.pressed)
        self.touch("ended")
        button.visible = True
        self.scene._render()
        self.touch("began")
        self.scene.remove(button)
        self.touch("ended")
        self.assertEqual(self.events, [])

    def test_focus_callback_removing_control_suppresses_queued_click(self):
        button = self.button(on_focus=lambda node: self.scene.remove(node))
        self.touch("began")
        self.touch("ended")
        self.assertEqual(self.events, [])
        self.assertIsNone(self.scene.focused_node)

    def test_scroll_preserves_control_focus_and_background_tap_clears_it(self):
        button = Button("Focus", x=120, y=60)
        view = ScrollView(240, 200, x=120, y=100, content_size=(240, 600))
        view.add(button)
        self.scene.add(view)
        self.scene._render()
        button.focus()
        self.touch("began", (120, 160))
        self.touch("moved", (120, 120), timestamp=1.1)
        self.touch("cancelled", (120, 120), timestamp=1.2)
        self.assertIs(self.scene.focused_node, button)
        self.scene._render()
        self.touch("began", (120, 160))
        self.touch("ended", (120, 160))
        self.assertIsNone(self.scene.focused_node)

    def test_nonfocusable_control_keeps_touch_capture_across_frames(self):
        button = self.button(focusable=False)
        self.touch("began")
        self.scene._render()
        self.assertTrue(button.pressed)
        self.touch("ended")
        self.assertEqual(self.events, ["click"])
        self.assertIsNone(self.scene.focused_node)

    def test_capture_does_not_activate_pending_focus_or_scroll_to_it(self):
        button = Button("Later", x=120, y=500)
        button.focus()
        view = ScrollView(240, 200, x=120, y=100, content_size=(240, 600))
        view.add(button)
        self.scene.add(view)
        self.scene.capture(rect=(0, 0, 240, 200), size=(240, 200))
        self.assertIsNone(self.scene.focused_node)
        self.assertTrue(button._pending_focus)
        self.assertEqual(view.offset, (0, 0))
        self.scene._render()
        self.assertIs(self.scene.focused_node, button)
        self.assertGreater(view.offset[1], 300)

    def test_vertical_slider_and_toggle_keep_value_and_commit_contracts(self):
        slider = Slider(.5, width=40, height=160, direction="vertical", x=60, y=100,
                        on_commit=lambda node, value: self.events.append(value))
        toggle = Toggle(x=160, y=100, on_change=lambda node, value: self.events.append(value))
        self.scene.add(slider, toggle)
        self.scene._render()
        self.touch("began", (60, 100))
        self.touch("moved", (60, 20))
        self.touch("ended", (60, 20))
        self.assertEqual(self.events, [1.])
        self.touch("began", (160, 100))
        self.touch("ended", (160, 100))
        self.assertTrue(toggle.value)
        self.assertEqual(self.events, [1., True])

    def test_scroll_cancels_button_and_toggle_without_focus_or_change(self):
        for node in (Button("Tap", on_click=lambda node: self.events.append("click")),
                     Toggle(on_change=lambda node, value: self.events.append(value))):
            with self.subTest(control=type(node).__name__):
                view = self.scroller(node)
                self.touch("began")
                self.touch("moved", (120, 30), timestamp=1.1)
                self.touch("ended", (120, 30), timestamp=1.2)
                self.assertGreater(view.offset[1], 0)
                self.assertEqual(self.events, [])
                self.assertFalse(node.pressed)
                self.assertFalse(node.focused)
                self.scene.clear()
                view.close()

    def test_slider_defers_value_until_axis_wins_and_keeps_drag_capture(self):
        slider = Slider(.5, on_change=lambda node, value: self.events.append(("change", value)),
                        on_commit=lambda node, value: self.events.append(("commit", value)))
        view = self.scroller(slider)
        self.touch("began", (175, 80))
        self.assertEqual(slider.value, .5)
        self.touch("moved", (175, 30), timestamp=1.1)
        self.touch("ended", (175, 30), timestamp=1.2)
        self.assertEqual(slider.value, .5)
        self.assertEqual(self.events, [])
        view.scroll_to(0, 0)
        self.scene._render()
        self.touch("began")
        self.touch("moved", (180, 80), timestamp=2.1)
        self.assertGreater(slider.value, .7)
        self.assertEqual(view.offset[1], 0)
        self.touch("moved", (230, 10), timestamp=2.2)
        self.assertEqual(view.offset[1], 0)
        self.touch("ended", (230, 10), timestamp=2.3)
        self.assertEqual(slider.value, 1)
        self.assertEqual(self.events[-1], ("commit", 1))

    def test_rotated_slider_uses_local_drag_axis(self):
        slider = Slider(.5, rotation=math.pi / 2)
        view = self.scroller(slider)
        self.touch("began")
        self.touch("moved", (120, 125), timestamp=1.1)
        self.touch("ended", (120, 125), timestamp=1.2)
        self.assertGreater(slider.value, .6)
        self.assertEqual(view.offset[1], 0)

    def test_slider_tap_cancel_and_programmatic_reset_semantics(self):
        slider = Slider(.5, on_change=lambda node, value: self.events.append(("change", value)),
                        on_commit=lambda node, value: self.events.append(("commit", value)),
                        on_cancel=lambda node, value: self.events.append(("cancel", value)))
        self.scroller(slider)
        self.touch("began", (190, 80))
        self.touch("ended", (190, 80))
        self.assertGreater(slider.value, .8)
        self.assertEqual(self.events[-1][0], "commit")
        self.events.clear()
        self.touch("began")
        self.touch("moved", (80, 80), timestamp=2.1)
        self.touch("cancelled", (80, 80), timestamp=2.2)
        self.assertEqual(self.events[-1][0], "cancel")
        value = slider.value
        self.touch("ended")
        self.assertEqual(slider.value, value)
        self.assertNotIn("commit", [kind for kind, value in self.events])
        self.events.clear()
        self.touch("began")
        self.touch("moved", (90, 80), dispatch=False, timestamp=3.1)
        slider.value = .9
        self.touch("ended", (10, 80), timestamp=3.2)
        self.assertEqual(slider.value, .9)
        self.assertEqual(self.events, [])

    def test_change_callback_can_replace_value_and_suppress_stale_commit(self):
        slider = Slider(.5, x=120, y=80)
        slider.on_change = lambda node, value: node.set_value(.25)
        slider.on_commit = lambda node, value: self.events.append(value)
        self.scene.add(slider)
        self.scene._render()
        self.touch("began", (180, 80), dispatch=False)
        self.touch("ended", (180, 80))
        self.assertEqual(slider.value, .25)
        self.assertEqual(self.events, [])

    def test_focus_navigation_reveals_offscreen_control_and_skips_disabled(self):
        first = Button("First", x=120, y=60)
        disabled = Toggle(enabled=False, x=120, y=150)
        last = Button("Last", x=120, y=500)
        view = ScrollView(240, 200, x=120, y=100, content_size=(240, 600))
        view.add(first, disabled, last)
        self.scene.add(view)
        self.scene._render()
        self.assertIs(self.scene.focus_next(), first)
        self.assertIs(self.scene.focus_next(), last)
        self.assertGreater(view.offset[1], 300)
        self.assertIs(self.scene.focus_next(reverse=True), first)
        self.assertGreaterEqual(view.offset[1], 0)
        self.assertLess(view.offset[1], 60)
        first.visible = False
        self.scene._prepare_layout()
        self.assertIsNone(self.scene.focused_node)

    def test_focus_moves_between_native_input_and_scene_controls(self):
        text = TextField(x=120, y=40)
        button = Button("Next", x=120, y=110)
        self.scene.add(text, button)
        self.scene._render()
        text.focus()
        self.scene._render()
        self.assertIs(self.scene.focused_node, text)
        text._navigate_input()
        self.assertIs(self.scene.focused_node, button)
        self.assertFalse(text.focused)
        self.scene.focus_next()
        self.scene._render()
        self.assertIs(self.scene.focused_node, text)
        self.assertFalse(button.focused)

    def test_focus_callbacks_keep_blur_before_focus_in_both_tree_directions(self):
        events = []
        first = Button("First", on_focus=lambda node: events.append("first focus"), on_blur=lambda node: events.append("first blur"))
        second = Button("Second", on_focus=lambda node: events.append("second focus"), on_blur=lambda node: events.append("second blur"))
        self.scene.add(first, second)
        second.focus()
        self.scene._dispatch_ui_events()
        first.focus()
        self.scene._dispatch_ui_events()
        second.focus()
        self.scene._dispatch_ui_events()
        self.assertEqual(events, ["second focus", "second blur", "first focus", "first blur", "second focus"])

    def test_focus_callbacks_are_ordered_across_native_editors_and_controls(self):
        events = []
        field = TextField(on_focus=lambda node: events.append("text focus"), on_blur=lambda node: events.append("text blur"))
        button = Button("Next", on_focus=lambda node: events.append("button focus"), on_blur=lambda node: events.append("button blur"))
        self.scene.add(field, button)
        self.scene._render()
        button.focus()
        self.scene._dispatch_ui_events()
        field.focus()
        self.scene._render()
        self.scene._dispatch_ui_events()
        field._navigate_input()
        self.scene._dispatch_ui_events()
        self.assertEqual(events, ["button focus", "button blur", "text focus", "text blur", "button focus"])

    def test_keyboard_activation_repeat_and_slider_adjustment(self):
        button = self.button()
        button.focus()
        self.key("space")
        self.key("space", repeat=True)
        self.assertTrue(button.pressed)
        self.key("space", "up")
        self.assertEqual(self.events, ["click"])
        self.key("space")
        self.scene.clear_focus()
        self.key("space", "up")
        self.assertEqual(self.events, ["click"])
        slider = Slider(.5, step=.1, x=120, y=150, on_commit=lambda node, value: self.events.append(value))
        self.scene.add(slider)
        self.scene._render()
        slider.focus()
        self.key("right")
        self.key("right", repeat=True)
        self.assertAlmostEqual(slider.value, .7)
        self.key("right", "up")
        self.assertAlmostEqual(self.events[-1], .7)
        self.assertFalse(slider.pressed)
        self.assertFalse(self.key("right", modifiers=("command",)))

    def test_control_state_and_text_changes_invalidate_cached_layer(self):
        layer = Layer(x=120, y=80)
        button = Button("Before")
        layer.add(button)
        self.scene.add(layer)
        self.scene._render()
        self.assertIs(self.scene.hit_test(120, 80), button)
        previous = layer._tex.to_image(self.window).rgba
        self.touch("began")
        self.assertTrue(layer._dirty)
        self.scene._render()
        self.assertFalse(layer._dirty)
        self.assertNotEqual(layer._tex.to_image(self.window).rgba, previous)
        self.touch("cancelled")
        button.text = "中文 👩🏽‍💻"
        self.scene._render()
        image = self.scene.capture(rect=(0, 0, 240, 160), size=(480, 320))
        self.assertTrue(any(image.rgba[i] for i in range(0, len(image.rgba), 4)))
        button.close()
        self.scene._render()
        self.assertIsNone(self.scene.hit_test(120, 80))

    def test_close_from_blur_callback_is_reentrant_and_releases_children(self):
        button = self.button(on_blur=lambda node: node.close())
        button.focus()
        button.close()
        self.assertFalse(button.children)
        self.assertIsNone(self.scene.focused_node)


if __name__ == "__main__":
    unittest.main()
