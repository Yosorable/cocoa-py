"""Shared focus navigation for scene controls and native text editors."""

from ._node import Node
from ._pointer import _in_subtree


class FocusManager:
    def __init__(self, scene):
        self.scene = scene
        self.current = None
        self._notifications = []
        self._dispatching = False
        self._text_blur_pending = False

    def eligible(self, node):
        if (node._tree_root() is not self.scene or getattr(node, "_closed", False)
                or not node.enabled or not getattr(node, "focusable", True)
                or getattr(node, "read_only", False)):
            return False
        world, opacity, shown, _ = node._current_geometry()
        return shown and opacity > 0 and abs(world[0] * world[3] - world[1] * world[2]) > 1e-12

    def set(self, node):
        if not self.eligible(node):
            return False
        node._pending_focus = False
        if self.current is not node:
            self.clear()
            self._text_blur_pending |= self.scene.focused_input is not None
            self.scene.dismiss_keyboard()
            self.current = node
            node._set_focused(True, manager=self)
        self.reveal(node)
        return True

    def reveal(self, node):
        parent = node.parent
        while parent is not None:
            if getattr(parent, "_is_scroll_view", False):
                parent.ensure_visible(node)
            parent = parent.parent

    def clear(self):
        previous, self.current = self.current, None
        if previous is not None:
            platform = getattr(self.scene, "_platform_input", None)
            if platform is not None:
                platform.cancel_owner(previous)
            previous._set_focused(False, manager=self)

    def notify(self, node, kind):
        self._notifications.append((node, kind))

    def dispatch(self):
        if self._dispatching:
            return
        self._dispatching = True
        try:
            events, self._notifications = self._notifications, []
            for node, kind in events:
                if node._closed or (kind == "focus" and node._tree_root() is not self.scene):
                    continue
                callback = getattr(node, "on_" + kind, None)
                if callback is not None:
                    callback(node)
        finally:
            self._dispatching = False

    def remove_subtree(self, node):
        if _in_subtree(self.current, node):
            self.clear()

    def maintain(self):
        self.scene._ensure_ui_nodes()
        if self.current is not None and not self.eligible(self.current):
            self.clear()
        for node in self.scene._focusable_nodes:
            if getattr(node, "_is_control", False):
                if (node._pointer_id is not None or node._key is not None or getattr(node, "_keys", None)) and (node._tree_root() is not self.scene or not node._available()):
                    node._cancel_capture()
                if node._pending_focus:
                    self.set(node)

    @property
    def focused_node(self):
        editor = self.scene.focused_input
        return editor if isinstance(editor, Node) else self.current

    def next(self, current=None, *, reverse=False):
        self.scene._ensure_ui_nodes()
        nodes = [node for node in self.scene._focusable_nodes if self.eligible(node)]
        if not nodes:
            return None
        current = self.focused_node if current is None else current
        index = nodes.index(current) if current in nodes else (0 if reverse else -1)
        target = nodes[(index + (-1 if reverse else 1)) % len(nodes)]
        target.focus()
        return target

    def handle_key(self, event):
        self.maintain()
        if event.modifiers.intersection(("command", "control", "alt")):
            return False
        if event.key == "tab":
            if event.phase == "down" and not event.repeat:
                return self.next(reverse="shift" in event.modifiers) is not None
            return self.focused_node is not None
        if self.current is None:
            return False
        if event.key == "escape":
            if event.phase == "down":
                self.clear()
            return True
        if not self.current._focus_ring:
            self.current._focus_ring = True
            self.current._changed()
        return self.current._handle_key(event)
