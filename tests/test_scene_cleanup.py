"""Teardown must complete even when user callbacks fail or reenter cleanup."""
import os
from types import SimpleNamespace
import unittest

from scene import Button, Group, Layer, Node, Rect, Scene, ScrollView, TextField, gpu


class NodeCleanupTests(unittest.TestCase):
    def scene(self):
        scene = Scene()
        scene._init(SimpleNamespace(size=(400, 400), scale=1.), "#000000", SimpleNamespace(screen_scale=1.))
        self.addCleanup(scene._close)
        return scene

    def test_clear_allows_a_cancel_callback_to_remove_its_own_node(self):
        scene = self.scene()
        first = ScrollView(content_size=(320, 800))
        self.addCleanup(first.close)
        second, third = Group(), Group()
        scene.add(first, second, third)
        first.scroll_to(y=300, animated=True)
        first.on_scroll_end = lambda view: scene.remove(view)
        scene.clear()
        self.assertEqual(scene.children, [])
        self.assertTrue(all(node.parent is None for node in (first, second, third)))
        scene.add(second)
        self.assertEqual(scene.children, [second])

    def test_clear_preserves_callback_additions_and_reparented_siblings(self):
        scene = self.scene()
        first = ScrollView(content_size=(320, 800))
        second, third, replacement, destination = Group(), Group(), Group(), Group()
        self.addCleanup(first.close)
        self.addCleanup(destination.close)
        scene.add(first, second, third)
        first.scroll_to(y=300, animated=True)
        def cancelled(view):
            destination.add(second)
            scene.clear()
            scene.add(replacement)
        first.on_scroll_end = cancelled
        scene.clear()
        self.assertEqual(scene.children, [replacement])
        self.assertIs(replacement.parent, scene)
        self.assertEqual(destination.children, [second])
        self.assertIs(second.parent, destination)
        self.assertIsNone(third.parent)

    def test_failed_child_close_does_not_leave_siblings_or_parent_links(self):
        class Child(Node):
            def __init__(self, fail=False):
                super().__init__()
                self.fail = fail
                self.closed = False
            def close(self):
                self.closed = True
                if self.fail:
                    self.fail = False
                    raise RuntimeError("close failure")
                super().close()
        parent = Group()
        first, second = Child(True), Child()
        descendant = Child()
        first.add(descendant)
        parent.add(first, second)
        self.addCleanup(parent.close)
        with self.assertRaisesRegex(RuntimeError, "close failure"):
            parent.close()
        self.assertTrue(second.closed)
        self.assertTrue(descendant.closed)
        self.assertFalse(parent.children)
        self.assertIsNone(first.parent)
        self.assertIsNone(second.parent)


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1", "Enable desktop UI tests")
class SceneCleanupTests(unittest.TestCase):
    def test_failed_blur_still_releases_text_layers_and_renderer(self):
        window = gpu.Window("Scene cleanup validation")
        self.addCleanup(window.close)
        scene = Scene()
        scene._init(window, "#000000")
        self.addCleanup(scene._close)
        def failed_blur(control):
            raise RuntimeError("blur failure")
        button = Button("Focus", on_blur=failed_blur)
        field = TextField(y=80)
        layer = Layer(y=160)
        layer.add(Rect(40, 40))
        scene.add(button, field, layer)
        scene._render()
        button.focus()
        scene._dispatch_ui_events()
        with self.assertRaisesRegex(RuntimeError, "blur failure"):
            scene._close()
        self.assertIsNone(scene._renderer)
        self.assertIsNone(layer._tex)
        self.assertIsNone(field._native)
        self.assertFalse(scene.children)
        scene._close()

    def test_scene_close_can_be_reentered_from_stop(self):
        window = gpu.Window("Scene cleanup reentrancy")
        self.addCleanup(window.close)
        scene = Scene()
        scene._init(window, "#000000")
        self.addCleanup(scene._close)
        calls = []
        def stopped():
            calls.append(True)
            if len(calls) < 3:
                scene._close()
        scene.stop = stopped
        scene._close()
        self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()
