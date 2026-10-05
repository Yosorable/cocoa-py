"""Hardware-key routing and lifecycle behavior through AppKit adapters."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sysconfig
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from _cocoa import _metal
from scene import Button, KeyEvent, Layer, PointerEvent, Rect, Scene, ScrollEvent, ScrollView, Slider, TextField, Touch, TouchPhase, WindowState, gpu
from scene._scene import _SceneDirector


class EventModelTests(unittest.TestCase):
    def setUp(self):
        self.window = SimpleNamespace(size=(400, 400), scale=1, sync=lambda: None,
                                      consume_touches=lambda: [], _metrics={})
        self.scene = Scene()
        self.scene._init(self.window, "#000000", SimpleNamespace(screen_scale=1))
        self.addCleanup(self.scene._close)
        self.events = []
        self.scene.key_down = lambda event: self.events.append(event)
        self.scene.key_up = lambda event: self.events.append(event)

    def key(self, phase, key="a", code=4, **kwargs):
        self.scene._platform_input.feed_key(dict(key=key, code=code, phase=phase, modifiers=0, timestamp=1., **kwargs))

    def test_hover_is_not_a_touch_and_stale_epochs_do_not_move_the_pointer(self):
        events, touches = [], []
        self.scene.pointer_moved = events.append
        self.scene.touch_began = touches.append
        raw = dict(x=40., y=50., prev_x=30., prev_y=50., phase="moved", timestamp=1., epoch=0)
        self.window.consume_pointer_events = lambda: [raw]
        self.scene._process_touches()
        self.assertEqual(events, [PointerEvent((40, 50), (30, 50), "moved", 1)])
        self.assertEqual(self.scene.pointer_position, (40, 50))
        self.assertFalse(touches)
        self.assertFalse(self.scene._pointer_router.active)
        self.scene._platform_input.epoch = 1
        raw["x"] = 99.
        self.scene._process_touches()
        self.assertEqual(self.scene.pointer_position, (40, 50))

    def test_hover_is_cancelled_once_when_input_ownership_is_lost(self):
        self.scene._last_pointer_event = PointerEvent((40, 50), (30, 50))
        exited = []
        self.scene.pointer_exited = exited.append
        self.scene._platform_input.cancel_interactions()
        self.scene._platform_input.cancel_interactions()
        self.assertIsNone(self.scene.pointer_position)
        self.assertEqual(len(exited), 1)
        self.assertEqual(exited[0].phase, "exited")
        self.assertTrue(exited[0].cancelled)

    def test_secondary_pointer_is_scene_owned_and_does_not_press_controls(self):
        clicks, touches = [], []
        button = Button("Primary", x=100, y=100, on_click=lambda _: clicks.append(True))
        self.scene.add(button)
        self.scene.touch_began = touches.append
        self.scene.touch_ended = touches.append
        with patch.object(self.scene, "hit_test", return_value=button):
            for phase in (TouchPhase.BEGAN, TouchPhase.ENDED):
                self.scene._pointer_router.feed(Touch(1, (100, 100), (100, 100), phase, button=1, source="mouse"))
        self.assertFalse(clicks)
        self.assertFalse(button.pressed)
        self.assertEqual([(e.button, e.source) for e in touches], [(1, "mouse"), (1, "mouse")])
        legacy = Touch(2, (0, 0), (0, 0), TouchPhase.BEGAN)
        self.assertEqual((legacy.button, legacy.source), (0, "touch"))

    def test_unconsumed_scroll_reaches_the_scene_without_starting_a_touch(self):
        events = []
        self.scene.scroll = events.append
        self.window.consume_scrolls = lambda: [dict(x=20., y=30., dx=0., dy=45., timestamp=1., precise=True, momentum=False)]
        self.scene._process_touches()
        self.assertEqual(events, [ScrollEvent((20, 30), (0, 45), True, False, 1)])
        self.assertFalse(self.scene._pointer_router.active)

    def test_game_keys_track_physical_and_logical_holds(self):
        self.key("down")
        self.assertEqual(self.scene.keys_down, {"a"})
        self.assertTrue(self.scene.is_key_down(4))
        self.assertTrue(self.scene.is_key_down("A"))
        self.key("down", repeat=True)
        self.assertTrue(self.events[-1].repeat)
        self.key("up")
        self.assertFalse(self.scene.keys_down)
        self.assertEqual([event.phase for event in self.events], ["down", "down", "up"])

    def test_control_owned_keys_do_not_become_game_keys_after_focus_changes(self):
        button = Button("Key")
        self.scene.add(button)
        button.focus()
        self.key("down", "space", 44)
        self.assertTrue(button.pressed)
        self.assertFalse(self.scene.keys_down)
        button.blur()
        self.key("up", "space", 44)
        self.assertFalse(button.pressed)
        self.assertEqual(self.events, [])

    def test_lifecycle_deduplicates_cancels_keys_and_resets_dt(self):
        platform = self.scene._platform_input
        self.scene.pause_when_inactive = True
        self.key("down")
        states = []
        self.scene.window_state_changed = states.append
        payload = {"state": dict(active=False, foreground=False, focused=False, gpu_allowed=False, epoch=1),
                   "events": [{"kind":"reset", "epoch":1, "reason":"background"},
                              {"kind":"state", "active":False, "foreground":False, "focused":False}]}
        platform.poll(payload)
        platform.poll(dict(payload, events=[]))
        self.assertEqual(states, [WindowState(False, False, False)])
        self.assertTrue(self.events[-1].cancelled)
        self.assertEqual(self.events[-1].phase, "up")
        self.assertFalse(self.scene.keys_down)
        self.assertIsNone(platform.frame_dt(5))
        platform.poll({"state":dict(active=True, foreground=True, focused=True, gpu_allowed=True, epoch=1), "events":[]})
        self.assertEqual(platform.frame_dt(5), 0)
        self.assertAlmostEqual(platform.frame_dt(.01), .01)

    def test_synthesized_repeat_is_bounded_and_stops_on_cancel(self):
        platform = self.scene._platform_input
        platform.native_repeat = False
        self.key("down")
        platform.repeat_keys(1.4)
        self.assertEqual(len(self.events), 1)
        platform.repeat_keys(1.5)
        platform.repeat_keys(100)
        self.assertEqual(len(self.events), 3)
        self.assertTrue(self.events[-1].repeat)
        platform.cancel_keys()
        platform.repeat_keys(200)
        self.assertEqual(len(self.events), 4)
        self.assertTrue(self.events[-1].cancelled)

    def test_safe_area_only_changes_are_reported(self):
        self.window._metrics = {"safe_area":(1, 2, 3, 4)}
        insets, sizes = [], []
        self.scene.safe_area_changed = insets.append
        self.scene.resize = lambda *size: sizes.append(size)
        self.scene._tick_frame(.01)
        self.window._metrics["safe_area"] = (4, 3, 2, 1)
        self.scene._tick_frame(.01)
        self.assertEqual(insets, [(1, 2, 3, 4), (4, 3, 2, 1)])
        self.assertEqual(sizes, [(400, 400)])

    def test_desktop_inactivity_keeps_updates_running_unless_pause_is_requested(self):
        platform = self.scene._platform_input
        platform.state = WindowState(False, True, False)
        updates = []
        self.scene.update = updates.append
        self.scene._tick_frame(.2)
        self.assertEqual(updates, [.2])
        self.scene.pause_when_inactive = True
        self.scene._tick_frame(.3)
        self.assertEqual(updates, [.2])

    def test_resume_does_not_feed_the_suspended_delta_into_new_gestures(self):
        ticks = []
        node = Rect(50, 50)
        node.gestures = [SimpleNamespace(is_active=True, tick=ticks.append)]
        self.scene.add(node)
        self.scene._gesture_nodes.add(node)
        self.scene._platform_input.reset_dt = True
        self.scene._tick_frame(1000)
        self.scene._dispatch_frame_touches(1000)
        self.assertEqual(ticks, [0.])


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1", "Enable desktop UI tests")
class NativeEventTests(unittest.TestCase):
    def test_appkit_hover_coalesces_motion_and_keeps_enter_exit_boundaries(self):
        events, touches = [], []
        self.scene.pointer_moved = events.append
        self.scene.pointer_exited = events.append
        self.scene.touch_began = touches.append
        self.scene.camera.zoom = 2
        for phase, x in (("entered", 20), ("moved", 30), ("moved", 45), ("exited", 50)):
            self.fixture.pointer(self.window.handle, phase, x, 60)
        self.scene._process_touches()
        self.assertEqual([event.phase for event in events], ["entered", "moved", "exited"])
        self.assertEqual(events[1].position, (45, 60))
        self.assertEqual(events[1].prev_position, (20, 60))
        self.assertFalse(touches)
        self.assertIsNone(self.scene.pointer_position)

    def enqueue_hover_barrier(self, kind):
        if kind == "key":
            self.key("down", dispatch=False)
            self.key("up", dispatch=False)
            return ("key_down", "key_up")
        if kind == "mouse":
            self.fixture.pointer(self.window.handle, "down", 100, 20)
            self.fixture.pointer(self.window.handle, "up", 100, 20)
            return ("touch_began", "touch_ended")
        self.fixture.scroll(self.window.handle, 100, 20, 0, -12)
        return ("scroll",)

    def test_hover_coalescing_preserves_interleaved_input_order(self):
        events = []
        self.scene.pointer_moved = lambda event: events.append(("hover", event.position))
        for name in ("key_down", "key_up", "touch_began", "touch_ended", "scroll"):
            setattr(self.scene, name, lambda event, name=name: events.append((name, self.scene.pointer_position)))
        for kind in ("key", "mouse", "scroll"):
            with self.subTest(kind=kind):
                self.fixture.pointer(self.window.handle, "entered", 10, 20)
                self.scene._process_touches()
                events.clear()
                for x in (50, 100):
                    self.fixture.pointer(self.window.handle, "moved", x, 20)
                callbacks = self.enqueue_hover_barrier(kind)
                for x in (150, 200):
                    self.fixture.pointer(self.window.handle, "moved", x, 20)
                self.scene._process_touches()
                self.assertEqual(events, [("hover", (100, 20)),
                                          *((name, (100, 20)) for name in callbacks),
                                          ("hover", (200, 20))])

    def test_draining_other_input_does_not_allow_hover_to_merge_across_it(self):
        for kind, consume in (("key", self.window.consume_platform_events),
                              ("mouse", self.window.consume_touches),
                              ("scroll", self.window.consume_scrolls)):
            with self.subTest(kind=kind):
                self.fixture.pointer(self.window.handle, "entered", 10, 20)
                self.window.consume_pointer_events()
                self.fixture.pointer(self.window.handle, "moved", 100, 20)
                self.enqueue_hover_barrier(kind)
                consume()
                for x in (150, 200):
                    self.fixture.pointer(self.window.handle, "moved", x, 20)
                samples = self.window.consume_pointer_events()
                self.assertEqual([(e["x"], e["prev_x"]) for e in samples],
                                 [(100, 10), (200, 100)])

    def test_appkit_secondary_and_middle_buttons_preserve_identity(self):
        events, clicks = [], []
        button = Button("Primary", x=100, y=100, on_click=lambda _: clicks.append(True))
        self.scene.add(button)
        self.scene._render()
        self.assertIs(self.scene.hit_test(100, 100), button)
        self.scene.touch_began = events.append
        self.scene.touch_moved = events.append
        self.scene.touch_ended = events.append
        for number in (1, 2):
            for phase in ("down", "dragged", "up"):
                self.fixture.pointer(self.window.handle, phase, 100, 100, number)
            self.scene._process_touches()
        self.assertEqual([event.button for event in events], [1, 1, 1, 2, 2, 2])
        self.assertTrue(all(event.source == "mouse" for event in events))
        self.assertFalse(clicks)
        for phase in ("down", "up"):
            self.fixture.pointer(self.window.handle, phase, 100, 100, 0)
        self.scene._process_touches()
        self.scene._dispatch_ui_events()
        self.assertEqual(clicks, [True])

    def test_native_focus_loss_discards_queued_hover_and_cancels_current_hover(self):
        events = []
        self.scene.pointer_exited = events.append
        self.fixture.pointer(self.window.handle, "entered", 20, 30)
        self.scene._process_touches()
        self.assertEqual(self.scene.pointer_position, (20, 30))
        self.fixture.pointer(self.window.handle, "moved", 100, 120)
        self.lifecycle("blur")
        self.assertIsNone(self.scene.pointer_position)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].cancelled)
        self.assertFalse(self.window.consume_pointer_events())

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="cocoa-py-events-")
        cls.addClassCleanup(cls.temp.cleanup)
        extension = Path(cls.temp.name) / ("_scene_ui_fixture" + sysconfig.get_config_var("EXT_SUFFIX"))
        source = Path(__file__).parent / "native" / "scene_ui_fixture.mm"
        subprocess.run(["xcrun", "clang++", "-std=c++17", "-fobjc-arc", "-bundle", "-undefined", "dynamic_lookup",
                        "-mmacosx-version-min=14.0", "-I" + sysconfig.get_paths()["include"],
                        "-framework", "AppKit", str(source), "-o", str(extension)], check=True)
        spec = importlib.util.spec_from_file_location("_scene_ui_fixture", extension)
        cls.fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixture)

    def setUp(self):
        self.window = gpu.Window("Scene event validation")
        self.addCleanup(self.window.close)
        self.scene = Scene()
        self.scene._init(self.window, "#000000")
        self.addCleanup(self.scene._close)
        for state in ("active", "foreground", "focus"):
            self.fixture.lifecycle(self.window.handle, state)
        self.scene._platform_input.poll()
        self.events = []
        self.scene.key_down = lambda event: self.events.append(event)
        self.scene.key_up = lambda event: self.events.append(event)

    def key(self, phase, code=0, text="a", flags=0, repeat=False, *, dispatch=True):
        self.fixture.keyboard(self.window.handle, code, phase, text, flags, repeat)
        if dispatch:
            self.scene._process_touches()
            self.scene._dispatch_ui_events()

    def lifecycle(self, state):
        self.fixture.lifecycle(self.window.handle, state)
        self.scene._platform_input.poll()

    def test_real_appkit_adapter_preserves_key_code_repeat_and_release(self):
        self.key("down")
        self.key("down", repeat=True)
        self.key("up", text="A")
        self.assertEqual([(event.key, event.code, event.phase, event.repeat) for event in self.events],
                         [("a", 4, "down", False), ("a", 4, "down", True), ("a", 4, "up", False)])
        self.assertFalse(self.scene.keys_down)

    def test_fullscreen_roundtrip_preserves_scene_and_window_size(self):
        self.assertFalse(self.scene.fullscreen)
        original_size = self.window.size
        for requested in (True, False):
            self.scene.fullscreen = requested
            self.scene.fullscreen = requested
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                self.window.consume_actions()
                self.window.sync()
                if self.scene.fullscreen == requested:
                    break
                time.sleep(.01)
            self.assertEqual(self.scene.fullscreen, requested)
            # AppKit sets the style bit before its transition finishes.
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                self.window.consume_actions()
                time.sleep(.01)
        self.window.sync()
        self.assertEqual(self.window.size, original_size)
        self.scene._render()
        with self.assertRaises(TypeError):
            self.scene.fullscreen = "yes"

    def test_fullscreen_request_during_transition_keeps_the_latest_target(self):
        self.scene.fullscreen = True
        self.scene.fullscreen = False
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.window.consume_actions()
            time.sleep(.01)
        self.assertFalse(self.scene.fullscreen)
        self.scene._render()

    def test_left_and_right_modifiers_are_independent(self):
        self.key("flags", code=56, text="", flags=0x20000 | 2)
        self.key("flags", code=60, text="", flags=0x20000 | 2 | 4)
        self.key("flags", code=56, text="", flags=0x20000 | 4)
        self.assertEqual(self.scene.keys_down, {"right_shift"})
        self.key("flags", code=60, text="", flags=0)
        self.assertFalse(self.scene.keys_down)
        self.assertIn("shift", self.events[0].modifiers)

    def test_command_shortcuts_are_not_delivered_as_game_keys(self):
        self.key("down", flags=0x100000)
        self.key("up", flags=0x100000)
        self.assertEqual(self.events, [])

    def test_native_focus_loss_cancels_active_touches_keys_and_control_state(self):
        button = Button("Press", x=100, y=100)
        self.scene.add(button)
        self.scene._render()
        self.scene._pointer_router.feed(Touch(1, (100, 100), (100, 100), TouchPhase.BEGAN, 1.))
        self.key("down")
        states = []
        self.scene.window_state_changed = states.append
        self.lifecycle("blur")
        self.assertFalse(button.pressed)
        self.assertFalse(self.scene._pointer_router.active)
        self.assertFalse(self.scene.keys_down)
        self.assertTrue(self.events[-1].cancelled)
        self.lifecycle("blur")
        self.assertEqual(states, [WindowState(True, True, False)])

    def test_lifecycle_cleanup_runs_once_and_callbacks_see_the_new_state(self):
        view = ScrollView(content_size=(320, 800))
        self.scene.add(view)
        view.scroll_to(y=400, animated=True)
        view._tick(.1)
        ends = []
        def ended(node):
            ends.append(self.scene.window_state.focused)
            node.scroll_to(y=0, animated=True)
        view.on_scroll_end = ended
        self.lifecycle("blur")
        self.assertEqual(ends, [False])

    def test_key_queue_overflow_cancels_a_hold_without_restarting_from_late_repeat(self):
        self.key("down")
        for _ in range(4098):
            self.key("down", repeat=True, dispatch=False)
        self.scene._process_touches()
        self.assertFalse(self.scene.keys_down)
        self.assertTrue(self.events[-1].cancelled)
        self.assertEqual(len(self.events), 2)

    def test_background_blocks_gpu_and_resume_uses_zero_dt(self):
        ticks = []
        self.scene.update = ticks.append
        self.scene._frame(.01)
        count = len(ticks)
        self.lifecycle("background")
        before = gpu.resource_counts()["submitted_frames"]
        self.scene._frame(50)
        self.assertEqual(len(ticks), count)
        self.assertEqual(gpu.resource_counts()["submitted_frames"], before)
        with self.assertRaises(_metal.WindowSuspendedError):
            with self.window.frame():
                pass
        with self.assertRaises(_metal.WindowSuspendedError):
            self.scene.capture(rect=(0, 0, 10, 10), size=(10, 10))
        self.lifecycle("foreground")
        self.scene._frame(50)
        self.assertEqual(ticks[-1], 0)

    def test_deactivation_during_encoding_discards_the_frame_and_releases_fence(self):
        frame = self.window.frame()
        frame.__enter__()
        before = gpu.resource_counts()["submitted_frames"]
        self.fixture.lifecycle(self.window.handle, "background")
        with self.assertRaises(_metal.WindowSuspendedError):
            frame.__exit__(None, None, None)
        self.assertEqual(gpu.resource_counts()["active_frames"], 0)
        self.assertEqual(gpu.resource_counts()["submitted_frames"], before)
        self.lifecycle("foreground")
        with self.window.frame():
            pass

    def test_discarded_offscreen_batch_rebuilds_previously_encoded_layer_on_resume(self):
        first, second = Layer(x=80, y=80), Layer(x=180, y=80)
        red, green = Rect(40, 40, fill="#ff0000"), Rect(40, 40, fill="#00ff00")
        first.add(red)
        second.add(green)
        self.scene.add(first, second)
        self.scene._frame(.01)
        red.fill, green.fill = "#0000ff", "#ffff00"
        first.invalidate()
        second.invalidate()
        original = _metal.end_frame
        passes = []
        def end_frame(handle):
            passes.append(handle)
            if len(passes) == 2:
                self.fixture.lifecycle(handle, "background")
            return original(handle)
        with patch.object(_metal, "end_frame", side_effect=end_frame):
            self.scene._frame(.01)
        self.assertEqual(len(passes), 2)
        self.lifecycle("foreground")
        self.scene._frame(.01)
        image = first._tex.to_image(self.window)
        offset = (image.height // 2 * image.width + image.width // 2) * 4
        self.assertEqual(image.rgba[offset:offset + 4], bytes((0, 0, 255, 255)))

    def test_text_focus_cancels_game_keys_and_prevents_ime_leakage(self):
        field = TextField(x=150, y=70)
        self.scene.add(field)
        self.scene._render()
        self.key("down")
        field.focus()
        self.scene._render()
        self.scene._process_touches()
        self.assertTrue(field.focused)
        self.assertFalse(self.scene.keys_down)
        self.assertTrue(self.events[-1].cancelled)
        count = len(self.events)
        self.key("down", code=11, text="b")
        self.assertEqual(len(self.events), count)
        field.blur()
        self.scene._process_touches()
        self.key("down", code=11, text="b")
        self.key("up", code=11, text="b")
        self.assertEqual([event.key for event in self.events[-2:]], ["b", "b"])

    def test_button_and_slider_consume_native_keyboard_events(self):
        clicks, commits = [], []
        button = Button("Activate", x=100, y=70, on_click=lambda node: clicks.append(True))
        slider = Slider(.5, step=.1, x=100, y=140, on_commit=lambda node, value: commits.append(value))
        self.scene.add(button, slider)
        self.scene._render()
        button.focus()
        self.key("down", code=49, text=" ")
        self.key("down", code=49, text=" ", repeat=True)
        self.key("up", code=49, text=" ")
        self.assertEqual(clicks, [True])
        self.assertEqual(self.events, [])
        self.key("down", code=48, text="\t")
        self.key("up", code=48, text="\t")
        self.assertIs(self.scene.focused_node, slider)
        self.key("down", code=124, text="")
        self.key("up", code=124, text="")
        self.assertAlmostEqual(commits[0], .6)

    def test_escape_is_handleable_and_unhandled_escape_requests_close(self):
        self.scene.key_down = lambda event: True
        self.key("down", code=53, text="\x1b")
        self.key("up", code=53, text="\x1b")
        self.assertFalse(self.scene._close_requested)
        self.assertEqual(self.window.consume_actions()["close"], 0)
        self.scene.key_down = lambda event: False
        self.key("down", code=53, text="\x1b")
        self.assertTrue(self.scene._close_requested)

    def test_closing_windows_releases_observers_and_can_restore_another_scene_window(self):
        before = gpu.resource_counts()["window_event_observers"]
        other = gpu.Window("Another scene event window")
        other.close()
        self.assertEqual(gpu.resource_counts()["window_event_observers"], before)


if __name__ == "__main__":
    unittest.main()
