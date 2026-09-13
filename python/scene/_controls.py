"""Scene-drawn controls with explicit capture, cancellation, and focus state."""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct

from ._clip import ClipRect
from ._common import _IDENTITY, _apply, _color, _invert, _matrix, _mul
from ._engine import _quad_verts
from ._node import Layer, Node
from ._shapes import Rect
from ._text import Label


def _finite(value, name, *, minimum=None):
    value = float(value)
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(f"{name} must be finite" + (f" and >= {minimum}" if minimum is not None else ""))
    return value


@dataclass(frozen=True)
class ControlStyle:
    """Immutable colors and geometry shared by Button, Slider, and Toggle."""
    background: object = "#28364c"
    active: object = "#4285ed"
    pressed: object = "#235bab"
    foreground: object = "#ffffff"
    border: object = "#ffffff30"
    focus: object = "#a4c8ff"
    radius: float = 10.
    border_width: float = 1.
    focus_width: float = 2.
    disabled_opacity: float = .4
    padding: float = 12.
    track_width: float = 4.
    thumb_size: float = 22.

    def __post_init__(self):
        for name in ("background", "active", "pressed", "foreground", "border", "focus"):
            object.__setattr__(self, name, _color(getattr(self, name)))
        for name in ("radius", "border_width", "focus_width", "disabled_opacity",
                     "padding", "track_width", "thumb_size"):
            object.__setattr__(self, name, _finite(getattr(self, name), name, minimum=0))
        if self.disabled_opacity > 1:
            raise ValueError("disabled_opacity must be <= 1")


def _rect(commands, renderer, world, opacity, order, width, height, *,
          fill=None, stroke=None, stroke_width=0, radius=0, position=(0, 0), z=0):
    if width <= 0 or height <= 0:
        return
    # Use the full affine transform for controls, including reflected parents.
    start = len(commands)
    Rect(width, height, fill=fill, stroke=stroke, stroke_width=stroke_width,
         radius=radius, z=z)._emit(commands, renderer, _IDENTITY, opacity, order)
    transform = _mul(world, _matrix(position, 0, 1))
    for command in commands[start:]:
        command._vb = b"".join(struct.pack("<4f", *_apply(transform, (x, y)), u, v)
                              for x, y, u, v in struct.iter_unpack("<4f", _quad_verts(command)))


class _Control(Node):
    _is_control = True
    _stacking_context = True

    def __init__(self, width, height, *, style=None, enabled=True, focusable=True,
                 on_focus=None, on_blur=None, **kwargs):
        super().__init__(**kwargs)
        self._closed = False
        self._closing = False
        self._events = []
        self._dispatching_events = False
        self._event_epoch = 0
        self._pointer_id = None
        self._pressed = self._focused = self._pending_focus = False
        self._focus_ring = True
        self._key = None
        self._enabled, self._focusable = bool(enabled), bool(focusable)
        self.width, self.height, self.style = width, height, style
        self._callback("on_focus", on_focus)
        self._callback("on_blur", on_blur)

    def _callback(self, name, callback):
        if callback is not None and not callable(callback):
            raise TypeError(f"{name} must be callable or None")
        setattr(self, name, callback)

    def _changed(self):
        node = self.parent
        while node is not None:
            if isinstance(node, Layer):
                node.invalidate()
            node = node.parent

    def _set_dimension(self, name, value):
        value = _finite(value, name, minimum=0)
        if value <= 0:
            raise ValueError(f"{name} must be positive")
        if value != getattr(self, "_" + name, None):
            setattr(self, "_" + name, value)
            self._changed()

    width = property(lambda self: self._width, lambda self, v: self._set_dimension("width", v))
    height = property(lambda self: self._height, lambda self, v: self._set_dimension("height", v))

    @property
    def style(self):
        return self._style

    @style.setter
    def style(self, value):
        if value is None:
            value = ControlStyle()
        if not isinstance(value, ControlStyle):
            raise TypeError("style must be a ControlStyle")
        self._style = value
        self._changed()

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = bool(value)
        if not self._enabled:
            self._event_epoch += 1
            self._cancel_capture()
            self.blur()
        self._changed()

    @property
    def focusable(self):
        return self._focusable

    @focusable.setter
    def focusable(self, value):
        self._focusable = bool(value)
        if not self._focusable:
            self.blur()

    @property
    def pressed(self):
        return self._pressed

    @property
    def focused(self):
        return self._focused

    @property
    def focus_visible(self):
        return self._focused and self._focus_ring

    def focus(self, *, show_ring=True):
        """Request scene focus, revealing this control in ancestor scrollers."""
        if self._closed:
            raise RuntimeError("Control is closed")
        if self.enabled and self.focusable:
            self._focus_ring = bool(show_ring)
            self._changed()
            self._pending_focus = True
            manager = getattr(self._tree_root(), "_focus_manager", None)
            if manager is not None:
                manager.set(self)
        return self

    def blur(self):
        self._pending_focus = False
        manager = getattr(self._tree_root(), "_focus_manager", None)
        if manager is not None and manager.current is self:
            manager.clear()
        else:
            self._set_focused(False)
        return self

    def _set_focused(self, value, *, manager=None):
        if self._focused != value:
            self._focused = value
            if not value:
                self._cancel_interaction()
            kind = "focus" if value else "blur"
            if manager is not None:
                manager.notify(self, kind)
            else:
                self._queue(kind)
            self._changed()

    def _bounds(self):
        if self._closed:
            return None
        return (-self.width / 2, -self.height / 2, self.width / 2, self.height / 2)

    def _snap(self):
        return (*super()._snap(), self.width, self.height, self.style,
                self.enabled, self.pressed, self.focused, self.focus_visible, self._closed)

    def _available(self):
        if self._closed or not self.enabled:
            return False
        world, opacity, shown, _ = self._current_geometry()
        return shown and opacity > 0 and abs(world[0] * world[3] - world[1] * world[2]) > 1e-12

    def _inside(self, position):
        world, opacity, shown, clips = self._current_geometry()
        from ._clip import _contains
        if not shown or opacity <= 0 or not _contains(clips, *position):
            return False
        if abs(world[0] * world[3] - world[1] * world[2]) < 1e-12:
            return False
        x, y = _apply(_invert(world), position)
        return abs(x) <= self.width / 2 and abs(y) <= self.height / 2

    def _queue(self, kind, *args):
        event = (self._event_epoch, kind, args, self._tree_root())
        if kind == "change" and self._events and self._events[-1][1] == kind:
            self._events[-1] = event
        else:
            self._events.append(event)

    def _dispatch_ui_events(self):
        if self._dispatching_events:
            return
        self._dispatching_events = True
        try:
            # New events from callbacks run on the next dispatch. Programmatic
            # value changes invalidate remaining stale value/commit callbacks.
            events, self._events = self._events, []
            for epoch, kind, args, origin in events:
                if self._closed:
                    break
                if epoch != self._event_epoch and kind not in ("focus", "blur", "cancel"):
                    continue
                if kind in ("click", "commit", "change") and (not self._available() or self._tree_root() is not origin):
                    continue
                callback = getattr(self, "on_" + kind, None)
                if callback is not None:
                    callback(self, *args)
        finally:
            self._dispatching_events = False

    def _cancel_capture(self):
        router = getattr(self._tree_root(), "_pointer_router", None)
        if router is not None:
            router.cancel_subtree(self)
        self._cancel_interaction()

    def _cancel_interaction(self):
        self._pointer_id = self._key = None
        self._pressed = False
        self._changed()

    def on_touch_began(self, touch):
        if self._available() and self._pointer_id is None and self._key is None:
            self._pointer_id = touch.id
            self._pressed = self._inside(touch.position)
            self._changed()

    def on_touch_moved(self, touch):
        if self._pointer_id == touch.id:
            self._pressed = self._available() and self._inside(touch.position)
            self._changed()

    def on_touch_ended(self, touch):
        if self._pointer_id != touch.id:
            return
        activate = self._available() and self._inside(touch.position)
        self._cancel_interaction()
        if activate:
            self.focus(show_ring=False)
            self._activate()

    def on_touch_cancelled(self, touch):
        if self._pointer_id == touch.id:
            self._cancel_interaction()

    def _handle_key(self, event):
        if event.key not in ("space", "enter") or not self._available():
            return False
        if event.phase == "down" and not event.repeat and self._pointer_id is None and self._key is None:
            self._key = event.key
            self._pressed = True
            self._changed()
        elif event.phase == "up" and self._key == event.key:
            self._cancel_interaction()
            self._activate()
        return True

    def _draw_focus(self, commands, renderer, world, opacity, order):
        if self.focus_visible and self.style.focus_width > 0:
            _rect(commands, renderer, world, opacity, order, self.width + 4, self.height + 4,
                  stroke=self.style.focus, stroke_width=self.style.focus_width,
                  radius=self.style.radius + 2, z=math.inf)

    def close(self):
        if self._closed or self._closing:
            return
        self._closing = True
        manager = getattr(self._tree_root(), "_focus_manager", None)
        try:
            self._cancel_capture()
            self.blur()
            if manager is not None:
                manager.dispatch()
            self._dispatch_ui_events()
        finally:
            self._closed = True
            self.interactive = False
            self._events.clear()
            self._changed()
            try:
                super().close()
            finally:
                self._closing = False


class Button(_Control):
    """A button. on_click(button) runs after a valid touch/key release."""

    def __init__(self, text="", width=160, height=44, *, font_size=18, font=None,
                 on_click=None, **kwargs):
        super().__init__(width, height, **kwargs)
        self._label = Label(text, size=font_size, font=font, alignment="center", max_lines=1, overflow="ellipsis")
        self._label.interactive = False
        self.add(self._label)
        self._callback("on_click", on_click)
        self._layout()

    @property
    def label(self):
        """The decorative Label; its font and text can be customized."""
        return self._label

    text = property(lambda self: self.label.text, lambda self, v: setattr(self.label, "text", v))

    def _layout(self):
        label = self.label
        width = max(.01, self.width - self.style.padding * 2)
        label.max_width = width
        label.clip = ClipRect(-width / 2, -self.height / 2 + 2, width, max(0, self.height - 4))
        label.color = self.style.foreground
        label.opacity = 1. if self.enabled else self.style.disabled_opacity
        label.visible = not self._closed

    def _activate(self):
        self._queue("click")

    def _emit(self, commands, renderer, world, opacity, order):
        if self._closed:
            return
        opacity *= 1. if self.enabled else self.style.disabled_opacity
        _rect(commands, renderer, world, opacity, order, self.width, self.height,
              fill=self.style.pressed if self.pressed else self.style.background,
              stroke=self.style.border, stroke_width=self.style.border_width,
              radius=self.style.radius, z=-math.inf)
        self._draw_focus(commands, renderer, world, opacity, order)


class Toggle(_Control):
    """A switch. Assigning value is silent; user changes call on_change(toggle, value)."""

    def __init__(self, value=False, width=56, height=34, *, on_change=None, **kwargs):
        super().__init__(width, height, **kwargs)
        self._value = bool(value)
        self._callback("on_change", on_change)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self.set_value(value)

    def set_value(self, value, *, notify=False):
        if self._closed:
            raise RuntimeError("Control is closed")
        self._event_epoch += 1
        self._cancel_capture()
        value = bool(value)
        changed = value != self._value
        self._value = value
        self._changed()
        if changed and notify:
            self._queue("change", value)
        return self

    def _activate(self):
        self._value = not self._value
        self._changed()
        self._queue("change", self.value)

    def _snap(self):
        return (*super()._snap(), self.value)

    def _emit(self, commands, renderer, world, opacity, order):
        if self._closed:
            return
        opacity *= 1. if self.enabled else self.style.disabled_opacity
        _rect(commands, renderer, world, opacity, order, self.width, self.height,
              fill=self.style.pressed if self.pressed else self.style.active if self.value else self.style.background,
              radius=self.height / 2, z=-math.inf)
        diameter = max(0., min(self.width, self.height) - 6)
        x = max(0., (self.width - diameter) / 2 - 3) * (1 if self.value else -1)
        _rect(commands, renderer, world, opacity, order, diameter, diameter,
              fill=self.style.foreground, radius=diameter / 2, position=(x, 0))
        self._draw_focus(commands, renderer, world, opacity, order)


class Slider(_Control):
    """A numeric slider, horizontal (left to right) or vertical (bottom to top).

    on_change(slider, value) reports live changes; on_commit(slider, value)
    reports a completed drag/key adjustment. Cancellation retains the live
    value and calls on_cancel(slider, value), with no commit. Programmatic
    value/range changes are silent and end any pending adjustment.
    """
    _defer_in_scroll = True

    def __init__(self, value=0, width=220, height=40, *, minimum=0, maximum=1,
                 step=None, direction="horizontal", on_change=None, on_commit=None,
                 on_cancel=None, **kwargs):
        super().__init__(width, height, **kwargs)
        self._editing = False
        self._keys = set()
        self._minimum, self._maximum, self._step = self._validate_range(minimum, maximum, step)
        self._value = self._normalize(value)
        self.direction = direction
        for name, callback in (("on_change", on_change), ("on_commit", on_commit), ("on_cancel", on_cancel)):
            self._callback(name, callback)

    @staticmethod
    def _validate_range(minimum, maximum, step):
        low, high = _finite(minimum, "minimum"), _finite(maximum, "maximum")
        if high < low or not math.isfinite(high - low):
            raise ValueError("maximum must be >= minimum and the range must be finite")
        if step is not None:
            step = _finite(step, "step", minimum=0)
            if step <= 0 or not math.isfinite((high - low) / step):
                raise ValueError("step must be positive and representable within the range")
        return low, high, step

    def _normalize(self, value):
        value = min(max(_finite(value, "value"), self.minimum), self.maximum)
        if self.step is not None and value != self.maximum:
            count = math.floor((value - self.minimum) / self.step + .5)
            rounded = min(self.maximum, self.minimum + count * self.step)
            value = self.maximum if self.maximum - value < abs(rounded - value) else rounded
        return value

    @property
    def minimum(self):
        return self._minimum

    @minimum.setter
    def minimum(self, value):
        self.set_range(value, self.maximum, step=self.step)

    @property
    def maximum(self):
        return self._maximum

    @maximum.setter
    def maximum(self, value):
        self.set_range(self.minimum, value, step=self.step)

    @property
    def step(self):
        return self._step

    @step.setter
    def step(self, value):
        self.set_range(self.minimum, self.maximum, step=value)

    def set_range(self, minimum, maximum, *, step=None):
        options = self._validate_range(minimum, maximum, step)
        if self._closed:
            raise RuntimeError("Control is closed")
        self._minimum, self._maximum, self._step = options
        return self.set_value(self.value)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self.set_value(value)

    def set_value(self, value, *, notify=False):
        if self._closed:
            raise RuntimeError("Control is closed")
        value = self._normalize(value)
        self._editing = False
        self._event_epoch += 1
        self._cancel_capture()
        self._assign(value, notify=notify)
        return self

    def _assign(self, value, *, notify=True):
        value = self._normalize(value)
        if value != self._value:
            self._value = value
            self._changed()
            if notify:
                self._queue("change", value)

    @property
    def direction(self):
        return self._direction

    @direction.setter
    def direction(self, value):
        if value not in ("horizontal", "vertical"):
            raise ValueError("direction must be 'horizontal' or 'vertical'")
        self._direction = value
        self._cancel_capture()
        self._changed()

    def _track(self):
        length = self.width if self.direction == "horizontal" else self.height
        diameter = min(self.style.thumb_size, self.width, self.height)
        return max(0., length - diameter), diameter

    def _fraction(self):
        return (self.value - self.minimum) / (self.maximum - self.minimum) if self.maximum > self.minimum else 0.

    def _coordinate(self, position):
        point = _apply(_invert(self._current_geometry()[0]), position)
        return point[0] if self.direction == "horizontal" else -point[1]

    def _claims_drag(self, began, touch):
        if not self._available() or self._pointer_id not in (None, touch.id) or math.dist(began.position, touch.position) < 8:
            return False
        inverse = _invert(self._current_geometry()[0])
        a, b = _apply(inverse, began.position), _apply(inverse, touch.position)
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        return dx > dy if self.direction == "horizontal" else dy >= dx

    def on_touch_began(self, touch):
        if not self._available() or self._pointer_id is not None or self._keys:
            return
        self._pointer_id = touch.id
        self._editing = self._pressed = True
        self.focus(show_ring=False)
        length, diameter = self._track()
        coordinate = self._coordinate(touch.position)
        center = (self._fraction() - .5) * length
        self._grab_offset = coordinate - center if abs(coordinate - center) <= diameter / 2 else 0.
        self._move_value(touch)
        self._changed()

    def _move_value(self, touch):
        length, _ = self._track()
        if length > 0:
            fraction = (self._coordinate(touch.position) - self._grab_offset) / length + .5
            self._assign(self.minimum + min(max(fraction, 0.), 1.) * (self.maximum - self.minimum))

    def on_touch_moved(self, touch):
        if self._pointer_id == touch.id and self._available():
            self._move_value(touch)

    def on_touch_ended(self, touch):
        if self._pointer_id != touch.id:
            return
        if not self._available():
            self._cancel_interaction()
            return
        self._move_value(touch)
        self._editing = False
        self._cancel_interaction()
        self._queue("commit", self.value)

    def _cancel_interaction(self):
        editing = getattr(self, "_editing", False)
        self._editing = False
        if hasattr(self, "_keys"):
            self._keys.clear()
        super()._cancel_interaction()
        if editing:
            self._queue("cancel", self.value)

    def _handle_key(self, event):
        names = ("left", "right", "up", "down", "home", "end", "page_up", "page_down")
        if event.key not in names or not self._available():
            return False
        if self._pointer_id is not None:
            return True
        if event.phase == "down":
            self._keys.add(event.key)
            self._editing = self._pressed = True
            increment = self.step or (self.maximum - self.minimum) / 100
            if "shift" in event.modifiers or event.key in ("page_up", "page_down"):
                increment *= 10
            if event.key in ("left", "down", "page_down"):
                increment *= -1
            value = self.minimum if event.key == "home" else self.maximum if event.key == "end" else self.value + increment
            self._assign(value)
            self._changed()
        elif event.key in self._keys:
            self._keys.remove(event.key)
            if not self._keys:
                self._editing = False
                self._cancel_interaction()
                self._queue("commit", self.value)
        return True

    def _snap(self):
        return (*super()._snap(), self.value, self.minimum, self.maximum, self.step, self.direction)

    def _emit(self, commands, renderer, world, opacity, order):
        if self._closed:
            return
        opacity *= 1. if self.enabled else self.style.disabled_opacity
        length, diameter = self._track()
        amount = self._fraction() * length
        horizontal = self.direction == "horizontal"
        def bar(distance, color, center=0):
            _rect(commands, renderer, world, opacity, order,
                  distance if horizontal else self.style.track_width,
                  self.style.track_width if horizontal else distance,
                  fill=color, radius=self.style.track_width / 2,
                  position=(center, 0) if horizontal else (0, -center), z=-math.inf)
        bar(length, self.style.background)
        if amount > 0:
            bar(amount, self.style.active, (amount - length) / 2)
        center = amount - length / 2
        _rect(commands, renderer, world, opacity, order, diameter, diameter,
              fill=self.style.pressed if self.pressed else self.style.foreground,
              radius=diameter / 2, position=(center, 0) if horizontal else (0, -center))
        self._draw_focus(commands, renderer, world, opacity, order)
