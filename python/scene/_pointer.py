"""Touch capture and drag arbitration for scene controls and scroll views."""
from dataclasses import dataclass, field, replace
import math
import time

from ._enums import TouchPhase


def _in_subtree(node, root):
    while node is not None:
        if node is root:
            return True
        node = node.parent
    return False


@dataclass
class _Pointer:
    began: object
    latest: object
    target: object
    candidates: list = field(default_factory=list)
    deferred: bool = False
    dragging: object = None
    scene_owned: bool = False
    blur_on_tap: bool = False
    clear_focus_on_tap: bool = False
    tap_cancelled: bool = False
    locked: bool = False


class PointerRouter:
    def __init__(self, scene):
        self.scene = scene
        self.active = {}

    def feed(self, touch):
        scene = self.scene
        if touch.timestamp <= 0:
            touch = replace(touch, timestamp=time.monotonic())
        if touch.phase == TouchPhase.BEGAN:
            self.cancel(touch.id)
            target = scene.hit_test(*touch.position)
            record = _Pointer(touch, touch, target)
            self.active[touch.id] = record
            current = target
            while current is not None and current is not scene:
                if getattr(current, "_is_scroll_view", False) and current.enabled:
                    current._interrupt_motion()
                    record.candidates.append(current)
                current = current.parent
            from ._text_input import _TextInput
            editor = target
            while editor is not None and not isinstance(editor, _TextInput):
                editor = editor.parent
            control = target
            while control is not None and not getattr(control, "_defer_in_scroll", False):
                control = control.parent
            focus_control = target
            while focus_control is not None and not getattr(focus_control, "_is_control", False):
                focus_control = focus_control.parent
            if editor is None and focus_control is None:
                if record.candidates:
                    record.clear_focus_on_tap = True
                else:
                    scene._focus_manager.clear()
            record.blur_on_tap = bool(record.candidates and editor is None)
            if scene._text_inputs and not record.candidates and (editor is None or not editor.enabled):
                for control in list(scene._text_inputs.values()):
                    if isinstance(control, _TextInput):
                        control.blur()
            if self.active.get(touch.id) is not record:
                return
            thumb = getattr(target, "_scrollbar_axis", None)
            if thumb is not None and target.parent.enabled:
                record.blur_on_tap = False
                record.clear_focus_on_tap = False
                scene._touch_owners[touch.id] = target.parent
                if not target.parent.dragging:
                    record.dragging = target.parent
                    target.parent._begin_scrollbar(touch, thumb)
                return
            # An inactive editor inside a scroller opens only after a tap has
            # finished, so dragging through an input does not open the keyboard.
            if (editor is not None or control is not None) and record.candidates:
                scene._touch_owners[touch.id] = editor if editor is not None else control
                record.deferred = True
            else:
                owner = scene._dispatch_touch(target, "on_touch_began", touch)
                if self.active.get(touch.id) is not record:
                    return
                if owner is not None:
                    scene._touch_owners[touch.id] = owner
                else:
                    record.scene_owned = True
                    scene.touch_began(touch)
            return

        record = self.active.get(touch.id)
        if record is None:
            return
        record.latest = touch
        if math.dist(record.began.position, touch.position) > 8:
            record.tap_cancelled = True
        if touch.phase != TouchPhase.MOVED:
            self._finish(touch.id, touch)
            return

        owner = scene._touch_owners.get(touch.id)
        if record.dragging is None and not record.locked:
            claim = getattr(owner, "_claims_drag", None)
            if claim is not None and claim(record.began, touch):
                record.locked = True
                if record.deferred:
                    record.deferred = False
                    scene._call_handler(owner, "on_touch_began", record.began)
                    if self.active.get(touch.id) is not record or owner._tree_root() is not scene:
                        return
        if record.dragging is None and not record.locked:
            candidates = [node for node in record.candidates
                          if node._tree_root() is scene and node.enabled]
            winner = next((node for node in candidates if node._can_scroll_touch(record.began, touch, owner)), None)
            if winner is None:
                winner = next((node for node in candidates
                               if node._can_scroll_touch(record.began, touch, owner, allow_edge=True)), None)
            if winner is not None:
                cancelled = replace(touch, phase=TouchPhase.CANCELLED)
                was_deferred, was_scene_owned = record.deferred, record.scene_owned
                record.deferred = record.scene_owned = False
                scene._touch_owners.pop(touch.id, None)
                if owner is not None and not was_deferred:
                    scene._call_handler(owner, "on_touch_cancelled", cancelled)
                    scene._prune_gesture_node(owner)
                elif was_scene_owned:
                    scene.touch_cancelled(cancelled)
                if self.active.get(touch.id) is not record or winner._tree_root() is not scene:
                    return
                record.deferred = record.scene_owned = False
                record.dragging = winner
                scene._touch_owners[touch.id] = winner
                winner._begin_scroll(record.began, touch)
                return
        if record.dragging is not None:
            record.dragging._move_scroll(touch)
        elif owner is not None and not record.deferred:
            scene._call_handler(owner, "on_touch_moved", touch)
            scene._prune_gesture_node(owner)
        elif record.scene_owned:
            scene.touch_moved(touch)

    def _finish(self, identifier, touch):
        record = self.active.pop(identifier, None)
        owner = self.scene._touch_owners.pop(identifier, None)
        if record is None:
            return
        cancelled = touch.phase == TouchPhase.CANCELLED
        if not cancelled and not record.tap_cancelled and record.dragging is None and record.clear_focus_on_tap:
            self.scene._focus_manager.clear()
        if not cancelled and not record.tap_cancelled and record.dragging is None and record.blur_on_tap:
            from ._text_input import _TextInput
            for control in list(self.scene._text_inputs.values()):
                if isinstance(control, _TextInput):
                    control.blur()
        if record.dragging is not None:
            record.dragging._end_scroll(touch, cancelled=cancelled)
        elif record.deferred:
            if not cancelled and not record.tap_cancelled and owner is not None and owner._tree_root() is self.scene and owner.contains_point(*touch.position):
                owner.on_touch_began(record.began)
                if getattr(owner, "_defer_in_scroll", False) and owner._tree_root() is self.scene:
                    self.scene._call_handler(owner, "on_touch_ended", touch)
        elif owner is not None:
            self.scene._call_handler(owner, "on_touch_cancelled" if cancelled else "on_touch_ended", touch)
            self.scene._prune_gesture_node(owner)
        elif record.scene_owned:
            if cancelled:
                self.scene.touch_cancelled(touch)
            else:
                self.scene.touch_ended(touch)

    def cancel(self, identifier):
        record = self.active.get(identifier)
        if record is not None:
            self._finish(identifier, replace(record.latest, phase=TouchPhase.CANCELLED))

    def scroll(self, event):
        node = self.scene.hit_test(event["x"], event["y"])
        remaining = (event["dx"], event["dy"])
        while node is not None and node is not self.scene:
            if getattr(node, "_is_scroll_view", False) and node.enabled:
                previous = remaining
                remaining = node._wheel_scroll(*remaining)
                if remaining != previous:
                    self.cancel_subtree(node.content)
                if max(map(abs, remaining)) < .001:
                    break
            node = node.parent

    def cancel_subtree(self, node):
        if node is self.scene:
            self.cancel_all()
            return
        for identifier, record in list(self.active.items()):
            owner = self.scene._touch_owners.get(identifier)
            if _in_subtree(record.target, node) or _in_subtree(owner, node):
                self.cancel(identifier)

    def cancel_all(self):
        for identifier in list(self.active):
            self.cancel(identifier)
