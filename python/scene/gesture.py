"""Gesture recognizers for scene nodes.

Usage:
    from scene import gesture

    class MyScene(scene.Scene):
        def setup(self):
            btn = scene.Rect(100, 50, fill="#333")
            btn.gestures = [gesture.Tap(self.on_tap)]
            self.add(btn)

        def on_tap(self, node, touch):
            print("tapped!")
"""
from __future__ import annotations

import math
import time
# ── State constants ──

_POSSIBLE = 0
_RECOGNIZED = 1
_FAILED = 2
_BEGAN = 3
_CHANGED = 4

# ── Base ──

class Gesture:
    """Base class for gesture recognizers."""

    __slots__ = ('state', '_node')

    def __init__(self):
        self.state = _POSSIBLE
        self._node = None

    def reset(self):
        self.state = _POSSIBLE

    def touch_began(self, touch) -> bool:
        """Process touch began. Return True to consume the event."""
        return False

    def touch_moved(self, touch) -> bool:
        return False

    def touch_ended(self, touch) -> bool:
        return False

    def touch_cancelled(self, touch) -> bool:
        self.reset()
        return False

    def tick(self, dt):
        """Called each frame for time-based gestures (e.g., long press)."""
        pass

    @property
    def is_active(self) -> bool:
        """Whether this gesture still needs per-frame bookkeeping."""
        return False

# ── Tap ──

class Tap(Gesture):
    """Single or multi-tap gesture.

    callback(node, touch) is called when the tap count is reached.
    """

    __slots__ = ('callback', 'count', 'tolerance', '_start_pos',
                 '_tap_count', '_last_tap_time', '_touch_id')

    def __init__(self, callback, count=1, tolerance=10):
        super().__init__()
        self.callback = callback
        self.count = count
        self.tolerance = tolerance
        self._start_pos = None
        self._tap_count = 0
        self._last_tap_time = 0
        self._touch_id = None

    def reset(self):
        super().reset()
        self._start_pos = None
        self._touch_id = None

    def touch_began(self, touch):
        if self._touch_id is not None:
            return False
        self._touch_id = touch.id
        self._start_pos = touch.position
        self.state = _POSSIBLE
        return True

    def touch_moved(self, touch):
        if touch.id != self._touch_id:
            return False
        if self._start_pos:
            dx = touch.position[0] - self._start_pos[0]
            dy = touch.position[1] - self._start_pos[1]
            if dx * dx + dy * dy > self.tolerance * self.tolerance:
                self.state = _FAILED
                self._touch_id = None
        return True

    def touch_ended(self, touch):
        if touch.id != self._touch_id:
            return False
        self._touch_id = None
        if self.state == _FAILED:
            self.reset()
            return True
        now = time.monotonic()
        if now - self._last_tap_time > 0.4:
            self._tap_count = 0
        self._tap_count += 1
        self._last_tap_time = now
        if self._tap_count >= self.count:
            self.state = _RECOGNIZED
            self._tap_count = 0
            self.callback(self._node, touch)
            self.reset()
        return True

    @property
    def is_active(self):
        return self._touch_id is not None

# ── LongPress ──

class LongPress(Gesture):
    """Long press gesture.

    callback(node, touch) is called after the specified duration.
    """

    __slots__ = ('callback', 'duration', 'tolerance',
                 '_start_pos', '_elapsed', '_touch_id', '_fired', '_last_touch')

    def __init__(self, callback, duration=0.5, tolerance=10):
        super().__init__()
        self.callback = callback
        self.duration = duration
        self.tolerance = tolerance
        self._start_pos = None
        self._elapsed = 0.0
        self._touch_id = None
        self._fired = False
        self._last_touch = None

    def reset(self):
        super().reset()
        self._start_pos = None
        self._elapsed = 0.0
        self._touch_id = None
        self._fired = False
        self._last_touch = None

    def touch_began(self, touch):
        if self._touch_id is not None:
            return False
        self._touch_id = touch.id
        self._start_pos = touch.position
        self._elapsed = 0.0
        self._fired = False
        self._last_touch = touch
        self.state = _POSSIBLE
        return True

    def touch_moved(self, touch):
        if touch.id != self._touch_id:
            return False
        self._last_touch = touch
        if self._start_pos and not self._fired:
            dx = touch.position[0] - self._start_pos[0]
            dy = touch.position[1] - self._start_pos[1]
            if dx * dx + dy * dy > self.tolerance * self.tolerance:
                self.state = _FAILED
                self._touch_id = None
        return True

    def touch_ended(self, touch):
        if touch.id != self._touch_id:
            return False
        self._touch_id = None
        self.reset()
        return True

    def tick(self, dt):
        if (self.state == _POSSIBLE and self._touch_id is not None
                and not self._fired):
            self._elapsed += dt
            if self._elapsed >= self.duration:
                self._fired = True
                self.state = _RECOGNIZED
                self.callback(self._node, self._last_touch)

    @property
    def is_active(self):
        return self._touch_id is not None and not self._fired

# ── Pan (Drag) ──

class Pan(Gesture):
    """Drag gesture.

    Callbacks receive (node, touch, dx, dy).
    """

    __slots__ = ('on_began', 'on_moved', 'on_ended',
                 '_touch_id', '_prev_pos')

    def __init__(self, on_began=None, on_moved=None, on_ended=None):
        super().__init__()
        self.on_began = on_began
        self.on_moved = on_moved
        self.on_ended = on_ended
        self._touch_id = None
        self._prev_pos = None

    def reset(self):
        super().reset()
        self._touch_id = None
        self._prev_pos = None

    def touch_began(self, touch):
        if self._touch_id is not None:
            return False
        self._touch_id = touch.id
        self._prev_pos = touch.position
        self.state = _BEGAN
        if self.on_began:
            self.on_began(self._node, touch, 0, 0)
        return True

    def touch_moved(self, touch):
        if touch.id != self._touch_id:
            return False
        self.state = _CHANGED
        dx = touch.position[0] - self._prev_pos[0]
        dy = touch.position[1] - self._prev_pos[1]
        self._prev_pos = touch.position
        if self.on_moved:
            self.on_moved(self._node, touch, dx, dy)
        return True

    def touch_ended(self, touch):
        if touch.id != self._touch_id:
            return False
        dx = touch.position[0] - self._prev_pos[0]
        dy = touch.position[1] - self._prev_pos[1]
        if self.on_ended:
            self.on_ended(self._node, touch, dx, dy)
        self.reset()
        return True

    @property
    def is_active(self):
        return self._touch_id is not None

# ── Pinch ──

class Pinch(Gesture):
    """Two-finger pinch gesture.

    on_changed(node, scale_delta, center_x, center_y) is called as fingers move.
    scale_delta is the incremental scale factor since the last callback
    (e.g., 1.05 = 5% larger, 0.95 = 5% smaller). Multiply into your
    current scale to accumulate: ``node.scale *= scale_delta``.
    """

    __slots__ = ('on_changed', '_touches', '_prev_dist')

    def __init__(self, on_changed=None):
        super().__init__()
        self.on_changed = on_changed
        self._touches: dict[int, tuple] = {}
        self._prev_dist = 0

    def reset(self):
        super().reset()
        self._touches.clear()
        self._prev_dist = 0

    def touch_began(self, touch):
        if len(self._touches) >= 2:
            return False
        self._touches[touch.id] = touch.position
        if len(self._touches) == 2:
            self._prev_dist = self._distance()
            self.state = _BEGAN
        return True

    def touch_moved(self, touch):
        if touch.id not in self._touches:
            return False
        self._touches[touch.id] = touch.position
        if len(self._touches) == 2 and self._prev_dist > 1e-6:
            self.state = _CHANGED
            cur_dist = self._distance()
            scale_delta = cur_dist / self._prev_dist
            self._prev_dist = cur_dist
            cx, cy = self._center()
            if self.on_changed:
                self.on_changed(self._node, scale_delta, cx, cy)
        return True

    def touch_ended(self, touch):
        if touch.id not in self._touches:
            return False
        del self._touches[touch.id]
        # Keep the remaining finger so a replacement can start a fresh pinch.
        self._prev_dist = 0
        self.state = _POSSIBLE
        return True

    def touch_cancelled(self, touch):
        return self.touch_ended(touch)

    @property
    def is_active(self):
        return bool(self._touches)

    def _distance(self):
        pts = list(self._touches.values())
        if len(pts) < 2:
            return 0
        dx = pts[1][0] - pts[0][0]
        dy = pts[1][1] - pts[0][1]
        return math.hypot(dx, dy)

    def _center(self):
        pts = list(self._touches.values())
        if len(pts) < 2:
            return (0, 0)
        return ((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2)
