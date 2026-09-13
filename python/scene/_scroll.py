"""Clipped scrolling, drag arbitration, and viewport-aware focus placement."""
from __future__ import annotations

import math

from ._clip import ClipRect
from ._common import _IDENTITY, _apply, _invert, _matrix, _mul
from ._node import Group, Layer, Node, _measure_children
from ._shapes import Rect


def _number(value, name, minimum=0):
    value = float(value)
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return value


def _clamp(value, low, high):
    return min(max(value, low), high)


class _ScrollThumb(Node):
    def __init__(self, axis):
        super().__init__(z=math.inf)
        self._scrollbar_axis = axis
        self.width = self.height = 3.
        self.color = "#ffffff80"
        self.visible = False

    def _bounds(self):
        return (-max(12, self.width) / 2, -max(12, self.height) / 2,
                max(12, self.width) / 2, max(12, self.height) / 2)

    def _snap(self):
        return (*super()._snap(), self.width, self.height, self.color)

    def _emit(self, commands, renderer, world, opacity, order):
        Rect(self.width, self.height, fill=self.color, radius=1.5, z=self.z)._emit(
            commands, renderer, world, opacity, order)


class ScrollView(Node):
    """A centered viewport containing ordinary scene nodes.

    add/remove/clear operate on content. Content bounds are measured automatically
    unless content_size=(width, height) is supplied, in which case the content
    origin is (0, 0). offset is measured rightward/downward from the content's
    top-left. All callbacks receive this ScrollView and run on the scene thread.
    """

    _is_scroll_view = True
    _stacking_context = True

    def __init__(self, width=320, height=240, *, content_size=None, direction="vertical",
                 bounces=True, inertia=True, scrollbars="auto", radius=0, background=None,
                 scrollbar_color="#ffffff80", on_scroll=None, on_scroll_begin=None,
                 on_scroll_end=None, **kwargs):
        if kwargs.get("clip") is not None:
            raise ValueError("ScrollView manages its viewport clip; configure radius instead")
        super().__init__(**kwargs)
        self._width = _number(width, "width")
        self._height = _number(height, "height")
        self._radius = _number(radius, "radius")
        self._content = Group()
        self._thumbs = (_ScrollThumb(0), _ScrollThumb(1))
        Node.add(self, self._content, *self._thumbs)
        self._offset = (0., 0.)
        self._extent = (0., 0.)
        self._origin = (0., 0.)
        self._explicit_size = None
        self._velocity = (0., 0.)
        self._animation = None
        self._drag_id = None
        self._drag_axis = None
        self._drag_mode = "content"
        self._drag_start = None
        self._scrolling = False
        self._events = []
        self._enabled = True
        self._closed = False
        self._closing = False
        self._indicator_time = 0.
        self._wheel_time = 0.
        self.background = background
        self.scrollbar_color = scrollbar_color
        self.bounces = bool(bounces)
        self.inertia = bool(inertia)
        self.direction = direction
        self.scrollbars = scrollbars
        self.drag_threshold = 8.
        self.deceleration = 7.5
        for name, callback in (("on_scroll", on_scroll), ("on_scroll_begin", on_scroll_begin),
                               ("on_scroll_end", on_scroll_end)):
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} must be callable or None")
            setattr(self, name, callback)
        self.content_size = content_size
        self._update_clip()

    @property
    def content(self):
        return self._content

    @property
    def clip(self):
        return self._clip

    @clip.setter
    def clip(self, value):
        if hasattr(self, "_content"):
            raise AttributeError("ScrollView.clip is managed by width, height, and radius")
        Node.clip.fset(self, value)

    def _update_clip(self):
        Node.clip.fset(self, ClipRect(-self.width / 2, -self.height / 2,
                                     self.width, self.height, self.radius))

    @property
    def width(self):
        return self._width

    @width.setter
    def width(self, value):
        self._width = _number(value, "width")
        self._update_clip()
        self._layout()

    @property
    def height(self):
        return self._height

    @height.setter
    def height(self, value):
        self._height = _number(value, "height")
        self._update_clip()
        self._layout()

    @property
    def radius(self):
        return self._radius

    @radius.setter
    def radius(self, value):
        self._radius = _number(value, "radius")
        self._update_clip()

    @property
    def direction(self):
        return self._direction

    @direction.setter
    def direction(self, value):
        if value not in ("horizontal", "vertical", "both"):
            raise ValueError("direction must be 'horizontal', 'vertical', or 'both'")
        self._direction = value
        if hasattr(self, "_extent"):
            self.stop_scrolling()

    @property
    def scrollbars(self):
        return self._scrollbars

    @scrollbars.setter
    def scrollbars(self, value):
        if value not in ("auto", "always", "never"):
            raise ValueError("scrollbars must be 'auto', 'always', or 'never'")
        self._scrollbars = value

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = bool(value)
        if not self._enabled and self._animation is None:
            self.stop_scrolling()

    @property
    def content_size(self):
        self._layout()
        return self._extent

    @content_size.setter
    def content_size(self, value):
        if value is not None:
            width, height = value
            value = (_number(width, "content width"), _number(height, "content height"))
        self._explicit_size = value
        self._layout()

    @property
    def offset(self):
        return self._offset

    @offset.setter
    def offset(self, value):
        x, y = value
        self.scroll_to(x, y)

    @property
    def dragging(self):
        return self._drag_id is not None

    @property
    def scrolling(self):
        return self._scrolling

    @property
    def max_offset(self):
        self._layout()
        return self._limits

    @property
    def _limits(self):
        width, height = self._visible_size()
        return (max(0., self._extent[0] - width) if self.direction != "vertical" else 0.,
                max(0., self._extent[1] - height) if self.direction != "horizontal" else 0.)

    def _visible_size(self):
        keyboard = getattr(self._tree_root(), "_keyboard_frame", None)
        if keyboard is None:
            return self.width, self.height
        x, y, w, h = keyboard
        inverse = _invert(self._current_geometry()[0])
        points = [_apply(inverse, (px, py)) for px in (x, x + w) for py in (y, y + h)]
        if min(p[0] for p in points) < self.width / 2 and max(p[0] for p in points) > -self.width / 2:
            top = min(p[1] for p in points) + self.height / 2
            if max(p[1] for p in points) > -self.height / 2:
                return self.width, _clamp(top, 0., self.height)
        return self.width, self.height

    def add(self, *nodes):
        if self._closed or self._closing:
            raise RuntimeError("ScrollView is closed")
        self._content.add(*nodes)
        self._invalidate_layers()
        self._layout()
        return self

    def remove(self, *nodes):
        if self._closed:
            raise RuntimeError("ScrollView is closed")
        self._content.remove(*nodes)
        self._invalidate_layers()
        self._layout()
        return self

    def clear(self):
        if self._closed:
            raise RuntimeError("ScrollView is closed")
        self._content.clear()
        self._invalidate_layers()
        self.stop_scrolling()
        self._layout()
        return self

    def _bounds(self):
        if self._closed:
            return None
        return (-self.width / 2, -self.height / 2, self.width / 2, self.height / 2)

    def _bounded(self, value):
        return tuple(_clamp(v, 0., limit) for v, limit in zip(value, self._limits))

    def _invalidate_layers(self):
        parent = self.parent
        while parent is not None:
            if isinstance(parent, Layer):
                parent.invalidate()
            parent = parent.parent

    def _queue(self, kind):
        if kind != "scroll" or not self._events or self._events[-1] != kind:
            self._events.append(kind)

    def _set_offset(self, value):
        value = tuple(value)
        if value != self._offset:
            self._offset = value
            self._indicator_time = .8
            self._queue("scroll")
            self._invalidate_layers()
        self._content.position = (-self.width / 2 - self._origin[0] - value[0],
                                  -self.height / 2 - self._origin[1] - value[1])

    def _layout(self):
        if getattr(self, "_closed", False):
            return
        if not hasattr(self, "_content"):
            return
        before = self._extent
        if self._explicit_size is None:
            bounds = _measure_children(self._content.children)
            self._origin = (bounds[0], bounds[1]) if bounds else (0., 0.)
            self._extent = (bounds[2] - bounds[0], bounds[3] - bounds[1]) if bounds else (0., 0.)
        else:
            self._origin = (0., 0.)
            self._extent = self._explicit_size
        if before != self._extent:
            self._invalidate_layers()
        if before != self._extent or not (self.dragging or self.scrolling):
            self._set_offset(self._bounded(self._offset))
        else:
            self._set_offset(self._offset)
        self._update_scrollbars()
        snapshot = self._snap()
        if snapshot != getattr(self, "_layout_snapshot", None):
            self._invalidate_layers()
            self._layout_snapshot = snapshot

    def _start_session(self):
        if not self._scrolling:
            self._scrolling = True
            self._queue("scroll_begin")

    def _finish_session(self):
        if self._scrolling:
            self._scrolling = False
            self._queue("scroll_end")

    def stop_scrolling(self):
        """Cancel dragging and momentum, returning immediately within bounds."""
        if self._closed:
            return self
        self._drag_id = None
        self._animation = None
        self._wheel_time = 0.
        self._velocity = (0., 0.)
        self._set_offset(self._bounded(self._offset))
        self._finish_session()
        return self

    def _interrupt_motion(self):
        if not self.dragging:
            self.stop_scrolling()

    def scroll_to(self, x=None, y=None, *, animated=False, duration=.25):
        """Scroll to an absolute offset; omitted axes retain their position."""
        if self._closed:
            raise RuntimeError("ScrollView is closed")
        if self._closing:
            return self
        self._layout()
        point = (self.offset[0] if x is None else _number(x, "x", -math.inf),
                 self.offset[1] if y is None else _number(y, "y", -math.inf))
        duration = _number(duration, "duration")
        target = self._bounded(point)
        self.stop_scrolling()
        if animated and duration > 0 and target != self.offset:
            self._animation = (self.offset, target, 0., duration)
            self._start_session()
        else:
            self._set_offset(target)
        return self

    def scroll_by(self, dx=0, dy=0, **kwargs):
        return self.scroll_to(self.offset[0] + _number(dx, "dx", -math.inf),
                              self.offset[1] + _number(dy, "dy", -math.inf), **kwargs)

    def ensure_visible(self, node, *, margin=8, animated=False):
        """Reveal a descendant, accounting for the current keyboard occlusion."""
        margin = _number(margin, "margin")
        self._layout()
        transform = _IDENTITY
        current = node
        while current is not self._content:
            if current is None or current is self:
                raise ValueError("node must be a descendant of ScrollView.content")
            transform = _mul(_matrix(current.position, current.rotation, current.scale), transform)
            current = current.parent
        bounds = node._bounds()
        if bounds is None:
            bounds = _measure_children(node.children)
        if bounds is None:
            return self
        corners = [_apply(transform, (x, y)) for x in (bounds[0], bounds[2]) for y in (bounds[1], bounds[3])]
        low = [min(p[i] for p in corners) - self._origin[i] for i in (0, 1)]
        high = [max(p[i] for p in corners) - self._origin[i] for i in (0, 1)]
        visible = self._visible_size()
        target = list(self.offset)
        for i in (0, 1):
            padding = min(margin, visible[i] / 2)
            if high[i] - low[i] > visible[i] - 2 * padding:
                target[i] = low[i] - padding
            elif low[i] < target[i] + padding:
                target[i] = low[i] - padding
            elif high[i] > target[i] + visible[i] - padding:
                target[i] = high[i] - visible[i] + padding
        return self.scroll_to(*target, animated=animated)

    def on_touch_began(self, touch):
        pass

    def _touch_axis(self, began, touch):
        inverse = _invert(self._current_geometry()[0])
        start = _apply(inverse, began.position)
        point = _apply(inverse, touch.position)
        delta = (start[0] - point[0], start[1] - point[1])
        if math.dist(began.position, touch.position) < self.drag_threshold:
            return None, delta
        axis = "horizontal" if abs(delta[0]) > abs(delta[1]) else "vertical"
        if self.direction != "both" and self.direction != axis:
            return None, delta
        return axis, delta

    def _can_scroll_touch(self, began, touch, owner, *, allow_edge=False):
        if not self.enabled or self._closed or (self._drag_id is not None and self._drag_id != touch.id):
            return False
        axis, delta = self._touch_axis(began, touch)
        if axis is None or getattr(owner, "_scroll_lock_axis", None) == axis:
            return False
        i = 0 if axis == "horizontal" else 1
        limit, offset = self._limits[i], self.offset[i]
        can_move = (delta[i] > 0 and offset < limit - .01) or (delta[i] < 0 and offset > .01)
        return can_move or (allow_edge and limit > 0)

    def _begin_scroll(self, began, touch):
        self._animation = None
        self._wheel_time = 0.
        self._velocity = (0., 0.)
        self._drag_id = touch.id
        self._drag_mode = "content"
        self._drag_axis, _ = self._touch_axis(began, touch)
        self._drag_start = _apply(_invert(self._current_geometry()[0]), began.position)
        self._drag_origin = self.offset
        self._last_drag_time = began.timestamp
        self._start_session()
        self._move_scroll(touch)

    def _move_scroll(self, touch):
        if self._drag_id != touch.id or not self.enabled:
            return
        point = _apply(_invert(self._current_geometry()[0]), touch.position)
        if self._drag_mode == "thumb":
            axis = self._drag_axis
            viewport = self._visible_size()[axis]
            thumb = self._thumbs[axis]
            length = thumb.width if axis == 0 else thumb.height
            track = max(1., viewport - 8 - length)
            value = self._drag_origin[axis] + (point[axis] - self._drag_start[axis]) * self._limits[axis] / track
            target = list(self.offset)
            target[axis] = _clamp(value, 0, self._limits[axis])
            self._set_offset(target)
            self._update_scrollbars()
            return
        raw = tuple(self._drag_origin[i] + self._drag_start[i] - point[i] for i in (0, 1))
        target = list(self.offset)
        for i, axis in enumerate(("horizontal", "vertical")):
            if self.direction != "both" and self._drag_axis != axis:
                continue
            limit = self._limits[i]
            bounded = _clamp(raw[i], 0, limit)
            extra = raw[i] - bounded
            extent = self.width if i == 0 else self.height
            if self.bounces and limit > 0 and extent > 0:
                extra = math.copysign(extent * .55 * abs(extra) / (extent + .55 * abs(extra)), extra)
            else:
                extra = 0.
            target[i] = bounded + extra
        elapsed = touch.timestamp - self._last_drag_time
        if elapsed > 0:
            blend = min(1., elapsed * 20)
            self._velocity = tuple((1 - blend) * old + blend * _clamp((new - previous) / elapsed, -15000, 15000)
                                   for old, new, previous in zip(self._velocity, target, self.offset))
        self._last_drag_time = touch.timestamp
        self._set_offset(target)

    def _end_scroll(self, touch, *, cancelled):
        if self._drag_id != touch.id:
            return
        self._drag_id = None
        if cancelled:
            self.stop_scrolling()
        else:
            if self._drag_mode == "thumb" or not self.inertia or touch.timestamp - self._last_drag_time > .1:
                self._velocity = (0., 0.)
            if max(map(abs, self._velocity)) < 2 and self.offset == self._bounded(self.offset):
                self._finish_session()

    def _begin_scrollbar(self, touch, axis):
        self._animation = None
        self._wheel_time = 0.
        self._velocity = (0., 0.)
        self._drag_id = touch.id
        self._drag_mode = "thumb"
        self._drag_axis = axis
        self._drag_start = _apply(_invert(self._current_geometry()[0]), touch.position)
        self._drag_origin = self.offset
        self._last_drag_time = touch.timestamp
        self._start_session()

    def _tick_self(self, dt):
        super()._tick_self(dt)
        indicator_was_shown = self._indicator_time > 0
        self._indicator_time = max(0., self._indicator_time - dt)
        if indicator_was_shown != (self._indicator_time > 0):
            self._invalidate_layers()
        if self.dragging:
            return
        self._layout()
        if self._wheel_time > 0:
            self._wheel_time = max(0., self._wheel_time - dt)
            if self._wheel_time == 0:
                self._finish_session()
            return
        if self._animation is not None:
            start, target, elapsed, duration = self._animation
            elapsed = min(duration, elapsed + dt)
            t = 1 - (1 - elapsed / duration) ** 3
            self._set_offset(tuple(a + (b - a) * t for a, b in zip(start, self._bounded(target))))
            self._animation = None if elapsed >= duration else (start, target, elapsed, duration)
            if self._animation is None:
                self._finish_session()
        elif self.scrolling:
            position, velocity = [], []
            for p, v, limit in zip(self.offset, self._velocity, self._limits):
                bound = _clamp(p, 0, limit)
                if abs(p - bound) > .01:
                    q, omega = p - bound, 18.
                    decay = math.exp(-omega * dt)
                    c = v + omega * q
                    p, v = bound + (q + c * dt) * decay, (v - omega * c * dt) * decay
                else:
                    rate = _number(self.deceleration, "deceleration", .01)
                    decay = math.exp(-rate * dt)
                    p, v = p + v * (1 - decay) / rate, v * decay
                    if not self.bounces:
                        clamped = _clamp(p, 0, limit)
                        if p != clamped:
                            p, v = clamped, 0.
                if abs(v) < 2 and abs(p - _clamp(p, 0, limit)) < .1:
                    p, v = _clamp(p, 0, limit), 0.
                position.append(p)
                velocity.append(v)
            self._velocity = tuple(velocity)
            self._set_offset(position)
            if not any(velocity):
                self._finish_session()

    def _dispatch_ui_events(self):
        if self._closed:
            self._events.clear()
            return
        events, self._events = self._events, []
        for kind in events:
            callback = getattr(self, "on_" + kind)
            if callback is not None:
                callback(self)
            if self._closed:
                break

    def _wheel_scroll(self, dx, dy):
        if self.dragging or self._closed:
            return dx, dy
        world = self._current_geometry()[0]
        inverse = _invert(world)
        dx, dy = inverse[0] * dx + inverse[2] * dy, inverse[1] * dx + inverse[3] * dy
        self._layout()
        old = self.offset
        mapped = self.direction == "horizontal" and abs(dx) < .001
        x, y = (dy, 0) if mapped else (dx, dy)
        new = self._bounded((old[0] + x, old[1] + y))
        if new != old:
            self._animation = None
            self._velocity = (0., 0.)
            self._wheel_time = .12
            self._start_session()
            self._set_offset(new)
        consumed = (new[0] - old[0], new[1] - old[1])
        if mapped:
            rx, ry = dx, dy - consumed[0]
        else:
            rx, ry = dx - consumed[0], dy - consumed[1]
        return world[0] * rx + world[2] * ry, world[1] * rx + world[3] * ry

    def _snap(self):
        return (*super()._snap(), self.width, self.height, self.radius, self.offset, self._extent,
                self.background, self.scrollbar_color, self.scrollbars, self.dragging,
                self.scrolling, self._indicator_time > 0, self._visible_size())

    def _emit(self, commands, renderer, world, opacity, order):
        if self._closed:
            return
        if self.background is not None:
            Rect(self.width, self.height, fill=self.background, z=-math.inf, radius=self.radius)._emit(
                commands, renderer, world, opacity, order)

    def _update_scrollbars(self):
        shown = self.scrollbars == "always" or (self.scrollbars == "auto" and (self.scrolling or self._indicator_time > 0))
        for i, (extent, viewport, limit) in enumerate(zip(self._extent, self._visible_size(), self._limits)):
            thumb = self._thumbs[i]
            thumb.visible = shown and limit > 0 and viewport > 12 and not self._closed
            if not thumb.visible:
                continue
            thumb.color = self.scrollbar_color
            track = viewport - 8
            length = min(track, max(20., track * viewport / extent))
            length = max(6., length - abs(self.offset[i] - _clamp(self.offset[i], 0, limit)))
            origin = -(self.width if i == 0 else self.height) / 2
            center = origin + 4 + length / 2 + (track - length) * _clamp(self.offset[i] / limit, 0, 1)
            if i == 0:
                thumb.width, thumb.height = length, 3
                thumb.position = (center, -self.height / 2 + self._visible_size()[1] - 4)
            else:
                thumb.width, thumb.height = 3, length
                thumb.position = (self.width / 2 - 4, center)

    def _cancel_interaction(self):
        self.stop_scrolling()
        self._dispatch_ui_events()

    def close(self):
        if self._closed or self._closing:
            return
        self._closing = True
        try:
            self._invalidate_layers()
            self.stop_scrolling()
            self._dispatch_ui_events()
        finally:
            self._closed = True
            self._drag_id = self._animation = None
            self._velocity = (0., 0.)
            self._scrolling = False
            self._events.clear()
            try:
                super().close()
            finally:
                self._closing = False
