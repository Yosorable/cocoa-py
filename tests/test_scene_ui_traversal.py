"""Native UI registry scans preserve postorder and Python descriptor behavior."""

import unittest

from _cocoa import _scene_accel
from scene import Group, Scene


class Input(Group):
    pass


def reference(root, input_type):
    layouts, events, inputs, focusable = [], [], [], []

    def walk(node):
        for child in list(node.children):
            walk(child)
        if node is root:
            return
        if getattr(node, "_layout", None) is not None:
            layouts.append(node)
        if getattr(node, "_dispatch_ui_events", None) is not None:
            events.append(node)
        if isinstance(node, input_type):
            inputs.append(node)
        if isinstance(node, input_type) or getattr(node, "_is_control", False):
            focusable.append(node)

    walk(root)
    return layouts, events, inputs, focusable


class UITraversalTests(unittest.TestCase):
    def test_postorder_root_exclusion_and_hidden_nodes_match_reference(self):
        root, parent, child, sibling, control = (Input() for _ in range(5))
        for node in (root, parent, child, sibling):
            node._layout = False  # The registry tests for None, not truth.
            node._dispatch_ui_events = lambda: None
        parent.visible = child.interactive = False
        child.speed = 0
        root.add(parent, sibling, control)
        parent.add(child)
        expected = reference(root, Input)
        actual = _scene_accel.ui_nodes(root, Input)
        self.assertEqual(actual, expected)
        self.assertEqual(actual[0], [child, parent, sibling])
        self.assertEqual(actual[2], [child, parent, sibling, control])

    def test_descriptor_mutation_keeps_the_original_child_snapshot(self):
        for collect in (reference, _scene_accel.ui_nodes):
            root, later, added = Group(), Group(), Group()
            later._is_control = True
            added._is_control = True
            calls = []

            class Mutating(Group):
                @property
                def _layout(self):
                    calls.append("layout")
                    root.remove(later)
                    root.add(added)
                    return None

            first = Mutating()
            root.add(first, later)
            result = collect(root, Input)
            self.assertEqual(calls, ["layout"])
            self.assertEqual(result, ([], [], [], [later]))
            self.assertEqual(root.children, [first, added])

    def test_attribute_error_defaults_and_other_errors_preserve_scene_registry(self):
        class Missing(Group):
            @property
            def _layout(self):
                raise AttributeError("no layout")

        class Broken(Group):
            @property
            def _layout(self):
                raise ValueError("layout failed")

        root = Scene()
        root.add(Missing())
        root._ensure_ui_nodes()
        old = root._ui_layout_nodes
        root.add(Broken())
        with self.assertRaisesRegex(ValueError, "layout failed"):
            root._ensure_ui_nodes()
        self.assertIs(root._ui_layout_nodes, old)
        self.assertTrue(root._ui_structure_dirty)

    def test_recursive_children_raise_without_corrupting_later_scans(self):
        root = Group()
        root.children.append(root)
        try:
            with self.assertRaises(RecursionError):
                _scene_accel.ui_nodes(root, Input)
        finally:
            root.children.clear()
        self.assertEqual(_scene_accel.ui_nodes(root, Input), ([], [], [], []))


if __name__ == "__main__":
    unittest.main()
