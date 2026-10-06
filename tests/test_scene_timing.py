"""Frame-rate initialization and node update ordering without a native window."""

from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import patch

from scene import Group, Scene, Sprite, SpriteFrame, action
from scene import _scene


class SceneFrameRateTests(unittest.TestCase):
    def test_default_rate_is_applied_once_to_each_new_window(self):
        view = Scene()
        with patch.object(_scene._metal, "set_target_fps", side_effect=lambda handle, rate: rate) as apply:
            view._init(SimpleNamespace(_handle=11, state={}), "#000000", renderer=object())
            view.fps = 60
            apply.assert_called_once_with(11, 60)
            view.fps = 60
            self.assertEqual(apply.call_count, 1)
            view.fps = 30
            apply.assert_called_with(11, 30)
            view._init(SimpleNamespace(_handle=12, state={}), "#000000", renderer=object())
            view.fps = 60
            apply.assert_called_with(12, 60)
            self.assertEqual(apply.call_count, 3)

    def test_failed_rate_request_can_be_retried(self):
        view = Scene()
        view._init(SimpleNamespace(_handle=11, state={}), "#000000", renderer=object())
        with patch.object(_scene._metal, "set_target_fps", side_effect=[RuntimeError("unavailable"), 60]) as apply:
            with self.assertRaises(RuntimeError):
                view.fps = 60
            view.fps = 60
            self.assertEqual(view.fps, 60)
            self.assertEqual(apply.call_count, 2)


class NodeTickOrderingTests(unittest.TestCase):
    def test_action_added_after_idle_ticks_runs_with_inherited_speed(self):
        root = Group(speed=2)
        child = Group(speed=.5)
        root.add(child)
        root._tick(.25)
        child.run_action(action.move_to(8, 0, 1, easing=action.linear))
        root._tick(.25)
        self.assertEqual(child.x, 2)
        child.remove_action(None)
        root._tick(.25)
        self.assertEqual(child.x, 2)

    def test_sprite_completion_precedes_actions_and_ticks_new_children(self):
        frame = SpriteFrame(object(), (0, 0, 1, 1))
        sprite = Sprite(frame, size=(1, 1), speed=.5)
        root = Group(speed=2)
        root.add(sprite)
        calls = []
        root._tick(.2)

        def complete(node):
            calls.append("complete")
            child = Group()
            child.run_action(action.call(lambda: calls.append("child")))
            node.add(child)
            node.run_action(action.call(lambda: calls.append("action")))

        sprite.play([frame, frame], fps=10, loop=False, on_complete=complete)
        root._tick(.1)
        self.assertTrue(sprite.playing)
        self.assertEqual(sprite.frame_index, 1)
        root._tick(.1)
        self.assertEqual(calls, ["complete", "action", "child"])
        self.assertFalse(sprite.playing)

    def test_custom_and_instance_hooks_run_even_when_hidden_or_speed_is_zero(self):
        calls = []

        class Custom(Group):
            def _tick(self, dt):
                calls.append(("before", dt))
                super()._tick(dt)
                calls.append(("after", dt))

        root, child = Group(speed=2), Custom(speed=0)
        child.visible = False
        child._tick_self = lambda dt: calls.append(("self", dt))
        root.add(child)
        root._tick(.25)
        self.assertEqual(calls, [("before", .5), ("self", 0), ("after", .5)])
        child._tick = MethodType(lambda node, dt: calls.append(("instance", dt)), child)
        root._tick(.1)
        self.assertEqual(calls[-1], ("instance", .2))

    def test_patching_the_base_callback_is_not_mistaken_for_an_idle_builtin(self):
        from scene import Node
        root, child = Group(), Group()
        root.add(child)
        calls = []
        with patch.object(Node, "_tick_self", lambda node, dt: calls.append(node)):
            root._tick(.1)
        self.assertEqual(calls, [root, child])

    def test_exception_stops_later_siblings_and_allows_a_subsequent_frame(self):
        root, first, second = Group(), Group(), Group()
        root.add(first, second)
        calls = []

        def fail(dt):
            raise ValueError("callback failed")

        first._tick_self = fail
        second._tick_self = lambda dt: calls.append(dt)
        with self.assertRaisesRegex(ValueError, "callback failed"):
            root._tick(.25)
        self.assertFalse(calls)
        del first._tick_self
        root._tick(.25)
        self.assertEqual(calls, [.25])

    def test_nodes_added_by_their_own_callback_tick_in_the_same_frame(self):
        calls = []

        class Probe(Group):
            def __init__(self, name, **kw):
                super().__init__(**kw)
                self.name = name

            def _tick_self(self, dt):
                calls.append((self.name, dt))
                if self.name == "parent" and not self.children:
                    self.add(Probe("child", speed=.5))

        parent = Probe("parent", speed=2)
        parent._tick(.25)
        self.assertEqual(calls, [("parent", .5), ("child", .25)])

    def test_sibling_removal_does_not_change_the_initial_iteration_snapshot(self):
        calls = []
        parent = Group()

        class First(Group):
            def _tick_self(self, dt):
                calls.append("first")
                parent.remove(second)

        class Second(Group):
            def _tick_self(self, dt):
                calls.append("second")

        second = Second()
        parent.add(First(), second)
        parent._tick(.1)
        self.assertEqual(calls, ["first", "second"])


if __name__ == "__main__":
    unittest.main()
