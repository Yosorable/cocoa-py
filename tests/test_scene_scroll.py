"""ScrollView behavior using real collection, input routing, and Metal capture."""
import os
import importlib.util
from pathlib import Path
import subprocess
import sysconfig
import tempfile
from types import SimpleNamespace
import unittest

from _cocoa import _scene_accel
from scene import Group, Layer, Rect, Scene, ScrollView, Touch, TouchPhase, gesture, gpu
from scene._common import _IDENTITY


class ScrollTests(unittest.TestCase):
    def setUp(self):
        self.scene = Scene()
        self.renderer = SimpleNamespace(screen_scale=1.)
        self.scene._init(SimpleNamespace(size=(400, 400), scale=1.), "#000000", self.renderer)
        self.addCleanup(self.scene._close)
        self.view = ScrollView(200, 200, x=100, y=100, content_size=(200, 800))
        self.child = Rect(200, 800, x=100, y=400)
        self.view.add(self.child)
        self.scene.add(self.view)
        self.collect()

    def collect(self):
        self.scene._prepare_layout()
        frame = _scene_accel.collect(self.scene, _IDENTITY, 1., 1., self.renderer)
        self.scene._interactive_nodes = frame[4]

    def touch(self, phase, y=100, *, x=100, time=1., identifier=1):
        self.scene._pointer_router.feed(Touch(identifier, (x, y), (x, y), TouchPhase(phase), time))
        self.scene._dispatch_ui_events()

    def tick(self, seconds=2.):
        for _ in range(round(seconds * 60)):
            self.view._tick(1 / 60)
            self.scene._dispatch_ui_events()

    def test_close_cannot_restart_an_animation_from_its_end_callback(self):
        self.view.scroll_to(y=400, animated=True)
        self.view._tick(.1)
        self.view.on_scroll_end = lambda view: view.scroll_to(y=0, animated=True)
        self.view.close()
        self.assertFalse(self.view.scrolling)
        self.assertIsNone(self.view._animation)

    def test_scroll_tree_can_be_created_before_a_scene_window_exists(self):
        scene = Scene()
        view = ScrollView(content_size=(320, 1000))
        self.addCleanup(view.close)
        scene.add(view)
        self.assertEqual(view.max_offset, (0, 760))

    def test_max_offset_measures_content_changes_before_the_next_frame(self):
        self.view.content_size = None
        self.assertEqual(self.view.max_offset[1], 600)
        self.child.height = 1100
        self.assertEqual(self.view.max_offset[1], 900)
        self.view.scroll_to(y=self.view.max_offset[1])
        self.assertEqual(self.view.offset[1], 900)

    def test_drag_cancels_child_tap_and_finishes_momentum(self):
        taps, events = [], []
        self.child.gestures = [gesture.Tap(lambda *args: taps.append(True))]
        self.view.on_scroll_begin = lambda view: events.append("begin")
        self.view.on_scroll_end = lambda view: events.append("end")
        self.touch("began", y=150)
        self.touch("moved", y=100, time=1.05)
        self.assertTrue(self.view.dragging)
        self.assertEqual(self.view.offset, (0., 50.))
        self.touch("ended", y=100, time=1.06)
        self.assertFalse(self.view.dragging)
        self.view._tick(1 / 60)
        self.assertGreater(self.view.offset[1], 50)
        self.tick()
        self.assertFalse(self.view.scrolling)
        self.assertEqual(events, ["begin", "end"])
        self.assertEqual(taps, [])

    def test_small_movement_remains_a_tap(self):
        taps = []
        self.child.gestures = [gesture.Tap(lambda *args: taps.append(True))]
        self.touch("began")
        self.touch("moved", y=97, time=1.05)
        self.touch("ended", y=97, time=1.1)
        self.assertEqual(taps, [True])
        self.assertEqual(self.view.offset, (0., 0.))

    def test_drag_at_a_non_bouncing_edge_still_cancels_child_tap(self):
        taps = []
        self.view.bounces = False
        self.child.gestures = [gesture.Tap(lambda *args: taps.append(True), tolerance=100)]
        self.touch("began")
        self.touch("moved", y=150, time=1.1)
        self.touch("ended", y=150, time=1.2)
        self.assertEqual(self.view.offset, (0., 0.))
        self.assertEqual(taps, [])

    def test_horizontal_and_two_axis_scrolling_follow_local_units(self):
        self.view.direction = "horizontal"
        self.view.content_size = (800, 800)
        self.collect()
        self.touch("began")
        self.touch("moved", y=100, x=60, time=1.1)
        self.touch("cancelled", x=60, time=1.2)
        self.assertEqual(self.view.offset, (40., 0.))
        self.view.direction = "both"
        self.view.offset = (0, 0)
        self.collect()
        self.touch("began", time=2.)
        self.touch("moved", y=70, x=60, time=2.1)
        self.touch("cancelled", y=70, x=60, time=2.2)
        self.assertEqual(self.view.offset, (40., 30.))

    def test_nested_scroller_yields_at_its_edge_before_elastic_bounce(self):
        self.view.clear()
        inner = ScrollView(180, 180, x=100, y=100, content_size=(180, 500))
        inner.add(Rect(180, 500, x=90, y=250))
        self.view.add(inner)
        self.collect()
        self.touch("began")
        self.touch("moved", y=60, time=1.1)
        self.touch("cancelled", y=60, time=1.2)
        self.assertEqual(inner.offset[1], 40)
        self.assertEqual(self.view.offset[1], 0)
        inner.scroll_to(y=inner.max_offset[1])
        self.collect()
        self.touch("began", time=2.)
        self.touch("moved", y=60, time=2.1)
        self.assertEqual(self.view.offset[1], 40)
        self.assertEqual(inner.offset[1], 320)

    def test_second_finger_cannot_take_over_active_scroll(self):
        self.touch("began", identifier=1)
        self.touch("moved", y=80, time=1.1, identifier=1)
        self.touch("began", identifier=2, time=1.2)
        self.touch("moved", y=40, time=1.3, identifier=2)
        self.assertEqual(self.view.offset[1], 20)
        self.assertTrue(self.view.dragging)
        self.touch("ended", time=1.4, identifier=2)
        self.assertTrue(self.view.dragging)
        self.touch("cancelled", y=80, time=1.5, identifier=1)
        self.assertFalse(self.view.scrolling)

    def test_bounce_returns_to_bounds_and_disabled_inertia_stops_at_release(self):
        self.touch("began")
        self.touch("moved", y=180, time=1.1)
        self.assertLess(self.view.offset[1], 0)
        self.assertGreater(self.view.offset[1], -80)
        self.touch("ended", y=180, time=1.2)
        self.tick()
        self.assertEqual(self.view.offset, (0., 0.))
        self.assertFalse(self.view.scrolling)
        self.view.inertia = False
        self.touch("began", time=3.)
        self.touch("moved", y=50, time=3.1)
        self.touch("ended", y=50, time=3.11)
        offset = self.view.offset
        self.tick()
        self.assertEqual(self.view.offset, offset)

    def test_automatic_content_bounds_include_negative_coordinates_and_clipped_children(self):
        auto = ScrollView(100, 80)
        self.addCleanup(auto.close)
        auto.add(Rect(200, 200, x=-40, y=-20))
        self.assertEqual(auto.content_size, (200., 200.))
        self.assertEqual(auto.content.position, (90., 80.))
        auto.add(Group(clip=(0, 0, 30, 30)).add(Rect(10000, 10000)))
        self.assertEqual(auto.content_size, (200., 200.))
        auto.clear()
        self.assertEqual(auto.content_size, (0., 0.))
        self.assertEqual(auto.offset, (0., 0.))

    def test_programmatic_scroll_clamps_animates_and_reveals_descendants(self):
        self.view.scroll_to(y=2000)
        self.assertEqual(self.view.offset[1], 600)
        self.view.scroll_to(y=0, animated=True, duration=.2)
        self.view._tick(.1)
        self.assertGreater(self.view.offset[1], 0)
        self.assertLess(self.view.offset[1], 600)
        self.tick(.2)
        self.assertEqual(self.view.offset[1], 0)
        target = Rect(60, 40, x=100, y=600)
        self.view.add(target)
        self.view.ensure_visible(target, margin=10)
        self.assertEqual(self.view.offset[1], 430)
        with self.assertRaises(ValueError):
            self.view.ensure_visible(Rect(20, 20))

    def test_disabled_scroller_completes_programmatic_animation_and_callbacks(self):
        events = []
        self.view.on_scroll_begin = lambda view: events.append("begin")
        self.view.on_scroll_end = lambda view: events.append("end")
        self.view.enabled = False
        self.view.scroll_to(y=300, animated=True, duration=.25)
        self.tick(.1)
        self.assertGreater(self.view.offset[1], 0)
        self.assertLess(self.view.offset[1], 300)
        self.tick(.2)
        self.assertEqual(self.view.offset[1], 300)
        self.assertFalse(self.view.scrolling)
        self.assertEqual(events, ["begin", "end"])
        self.collect()
        self.touch("began", time=2)
        self.touch("moved", y=40, time=2.1)
        self.touch("ended", y=40, time=2.2)
        self.assertEqual(self.view.offset[1], 300)

    def test_disabling_during_programmatic_scroll_keeps_its_destination(self):
        self.view.scroll_to(y=300, animated=True, duration=.25)
        self.tick(.1)
        self.view.enabled = False
        self.tick(.2)
        self.assertEqual(self.view.offset[1], 300)
        self.assertFalse(self.view.scrolling)

    def test_keyboard_extends_scroll_range_without_resizing_the_viewport(self):
        target = Rect(80, 40, x=100, y=780)
        self.view.add(target)
        self.scene._keyboard_frame = (0, 120, 400, 280)
        self.view.ensure_visible(target, margin=0)
        self.assertEqual(self.view.offset[1], 680)
        self.assertEqual((self.view.width, self.view.height), (200., 200.))
        self.scene._keyboard_frame = None
        self.view._layout()
        self.assertEqual(self.view.offset[1], 600)

    def test_removal_from_scroll_callback_cleans_up_capture(self):
        ends = []
        self.view.on_scroll = lambda view: self.scene.remove(view)
        self.view.on_scroll_end = lambda view: ends.append(True)
        self.touch("began")
        self.touch("moved", y=50, time=1.1)
        self.assertIsNone(self.view.parent)
        self.assertFalse(self.view.dragging)
        self.assertFalse(self.scene._touch_owners)
        self.assertEqual(ends, [True])

    def test_wheel_uses_viewport_units_and_cancels_pending_child_taps(self):
        taps = []
        self.child.gestures = [gesture.Tap(lambda *args: taps.append(True))]
        self.view.scale = 2
        self.collect()
        self.touch("began")
        self.scene._pointer_router.scroll({"x":100, "y":100, "dx":0, "dy":40})
        self.touch("ended", time=1.1)
        self.assertEqual(self.view.offset, (0., 20.))
        self.assertEqual(taps, [])
        self.tick(.2)
        self.assertFalse(self.view.scrolling)

    def test_wheel_passes_unconsumed_distance_to_outer_scroller(self):
        self.view.clear()
        inner = ScrollView(180, 180, x=100, y=100, content_size=(180, 500))
        inner.add(Rect(180, 500, x=90, y=250))
        self.view.add(inner)
        inner.scroll_to(y=300)
        self.collect()
        self.scene._pointer_router.scroll({"x":100, "y":100, "dx":0, "dy":60})
        self.assertEqual(inner.offset[1], 320)
        self.assertEqual(self.view.offset[1], 40)

    def test_scrollbar_drag_is_proportional_and_does_not_fling(self):
        self.view.scrollbars = "always"
        self.collect()
        thumb = self.view._thumbs[1]
        x, y = thumb.world_position
        self.assertIs(self.scene.hit_test(x, y), thumb)
        self.touch("began", y=y, x=x)
        self.touch("moved", y=y + 30, x=x, time=1.1)
        self.assertAlmostEqual(self.view.offset[1], 125.)
        self.touch("ended", y=y + 30, x=x, time=1.11)
        position = self.view.offset
        self.tick()
        self.assertEqual(self.view.offset, position)
        self.assertFalse(self.view.scrolling)


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1", "Enable desktop UI tests.")
class ScrollRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="cocoa-py-ui-")
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
        self.window = gpu.Window("cocoa-py scroll validation")
        self.addCleanup(self.window.close)
        self.scene = Scene()
        self.scene._init(self.window, "#000000")
        self.addCleanup(self.scene._close)

    def pixel(self, image, x, y):
        start = (y * image.width + x) * 4
        return tuple(image.rgba[start:start + 4])

    def test_scroller_content_and_bars_follow_the_container_stacking_order(self):
        view = ScrollView(100, 100, x=60, y=60, z=5, scrollbars="always", background="#0000ff")
        red = Rect(100, 300, x=50, y=150, z=-100, fill="#ff0000")
        view.add(red)
        self.scene.ui.add(view)
        self.scene._render()
        self.assertIs(self.scene.hit_test(60, 60), red)
        image = self.scene.capture(rect=(0, 0, 120, 120), size=(120, 120))
        self.assertEqual(self.pixel(image, 50, 50), (255, 0, 0, 255))
        self.assertEqual(self.pixel(image, 115, 50), (0, 0, 0, 255))
        overlay = Rect(120, 120, x=60, y=60, z=6, fill="#00ff00")
        self.scene.ui.add(overlay)
        self.scene._render()
        self.assertIs(self.scene.hit_test(60, 60), overlay)
        image = self.scene.capture(rect=(0, 0, 120, 120), size=(120, 120))
        self.assertEqual(self.pixel(image, 106, 30), (0, 255, 0, 255))

    def test_appkit_wheel_reaches_scroll_view_through_native_queue(self):
        # Direct adapter injection needs the state AppKit supplies for a real
        # wheel event, independently of which application owns desktop focus.
        self.fixture.lifecycle(self.window.handle, "active")
        self.fixture.lifecycle(self.window.handle, "focus")
        view = ScrollView(200, 200, x=100, y=100, content_size=(200, 600))
        self.scene.ui.add(view.add(Rect(200, 600, x=100, y=300)))
        self.scene._render()
        self.fixture.scroll(self.window.handle, 100, 100, 0, -48)
        events = self.window.consume_scrolls()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["x"], events[0]["y"], events[0]["dy"]), (100, 100, 48))
        self.scene._pointer_router.scroll(events[0])
        self.assertEqual(view.offset, (0., 48.))
        self.assertEqual(self.window.consume_scrolls(), [])

    def test_layer_projects_cached_ui_hit_geometry_and_capture_restores_it(self):
        layer = Layer(x=60, y=60, z=4)
        view = ScrollView(100, 100, content_size=(100, 300), scrollbars="always")
        red = Rect(100, 300, x=50, y=150, fill="#ff0000")
        self.scene.ui.add(layer.add(view.add(red)))
        self.scene._render()
        self.assertIs(self.scene.hit_test(60, 60), red)
        original = layer._hit_snapshot
        texture = layer._tex
        image = self.scene.capture(rect=(40, 40, 30, 30), size=(90, 90))
        self.assertEqual(self.pixel(image, 30, 30), (255, 0, 0, 255))
        self.assertIs(layer._hit_snapshot, original)
        layer.x += 40
        layer.rotation = .3
        self.scene._render()
        self.assertIs(layer._tex, texture)
        self.assertIs(self.scene.hit_test(*layer.world_position), red)
        view.scroll_to(y=100)
        self.assertTrue(layer._dirty)
        self.scene._render()
        self.assertIs(self.scene.hit_test(*layer.world_position), red)

    def test_nested_layer_ui_hit_order_stays_below_an_overlay(self):
        outer, inner = Layer(x=80, y=80, z=2), Layer(x=20)
        view = ScrollView(100, 100, content_size=(100, 300))
        red = Rect(100, 300, x=50, y=150, fill="#ff0000")
        self.scene.ui.add(outer.add(inner.add(view.add(red))))
        self.scene._render()
        self.assertIs(self.scene.hit_test(100, 80), red)
        self.assertEqual(view.content.world_position, (50., 30.))
        cover = Rect(150, 150, x=100, y=80, z=3)
        self.scene.ui.add(cover)
        self.scene._render()
        self.assertIs(self.scene.hit_test(100, 80), cover)


if __name__ == "__main__":
    unittest.main()
