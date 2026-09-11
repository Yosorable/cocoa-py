from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Easing functions  (t: 0..1 -> 0..1)
# ---------------------------------------------------------------------------

def linear(t: float) -> float:
    return t

def ease_in(t: float) -> float:
    return t * t

def ease_out(t: float) -> float:
    return t * (2.0 - t)

def ease_in_out(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)

def ease_in_cubic(t: float) -> float:
    return t * t * t

def ease_out_cubic(t: float) -> float:
    t -= 1.0
    return t * t * t + 1.0

def ease_in_out_cubic(t: float) -> float:
    if t < 0.5:
        return 4.0 * t * t * t
    t -= 1.0
    return 1.0 + 4.0 * t * t * t

def ease_out_bounce(t: float) -> float:
    if t < 1.0 / 2.75:
        return 7.5625 * t * t
    if t < 2.0 / 2.75:
        t -= 1.5 / 2.75
        return 7.5625 * t * t + 0.75
    if t < 2.5 / 2.75:
        t -= 2.25 / 2.75
        return 7.5625 * t * t + 0.9375
    t -= 2.625 / 2.75
    return 7.5625 * t * t + 0.984375

def ease_out_elastic(t: float) -> float:
    if t == 0.0 or t == 1.0:
        return t
    return pow(2.0, -10.0 * t) * math.sin((t - 0.075) * (2.0 * math.pi) / 0.3) + 1.0

# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------

class Action:
    """Immutable description of an animation. Safe to reuse across nodes."""
    __slots__ = ()
    def _bind(self, node, key=None):
        raise NotImplementedError

class _BoundAction:
    """Mutable runtime state for a running action."""
    __slots__ = ("node", "key")
    def __init__(self, node, key):
        self.node = node
        self.key = key
    def tick(self, dt: float) -> bool:
        raise NotImplementedError

# ---------------------------------------------------------------------------
# Tween helpers
# ---------------------------------------------------------------------------

def _lerp(a, b, t):
    return a + (b - a) * t

class _TweenAction(Action):
    __slots__ = ("_attr", "_target", "_duration", "_easing", "_relative")
    def __init__(self, attr, target, duration, easing, relative):
        self._attr = attr
        self._target = target
        self._duration = max(0.0, float(duration))
        self._easing = easing
        self._relative = relative
    def _bind(self, node, key=None):
        return _BoundTween(node, key, self)

class _BoundTween(_BoundAction):
    __slots__ = ("_attr", "_start", "_end", "_duration", "_easing", "_elapsed", "_leftover")
    def __init__(self, node, key, action: _TweenAction):
        super().__init__(node, key)
        self._attr = action._attr
        self._duration = action._duration
        self._easing = action._easing
        self._elapsed = 0.0
        self._leftover = 0.0
        current = getattr(node, action._attr)
        if action._relative:
            if isinstance(current, tuple):
                self._start = current
                self._end = tuple(c + d for c, d in zip(current, action._target))
            else:
                self._start = float(current)
                self._end = float(current) + float(action._target)
        else:
            self._start = current
            self._end = action._target

    def tick(self, dt):
        self._elapsed += dt
        t = min(1.0, self._elapsed / self._duration) if self._duration > 0 else 1.0
        e = self._easing(t)
        if isinstance(self._start, tuple):
            val = tuple(_lerp(a, b, e) for a, b in zip(self._start, self._end))
        else:
            val = _lerp(self._start, self._end, e)
        setattr(self.node, self._attr, val)
        done = t >= 1.0
        if done:
            self._leftover = max(0.0, self._elapsed - self._duration) if self._duration > 0 else dt
        return done

# ---------------------------------------------------------------------------
# Concrete actions
# ---------------------------------------------------------------------------

class _MoveTo(Action):
    __slots__ = ("_x", "_y", "_duration", "_easing")
    def __init__(self, x, y, duration, easing):
        self._x, self._y = float(x), float(y)
        self._duration = max(0.0, float(duration))
        self._easing = easing
    def _bind(self, node, key=None):
        return _BoundTween(node, key, _TweenAction("position", (self._x, self._y), self._duration, self._easing, False))

class _MoveBy(Action):
    __slots__ = ("_dx", "_dy", "_duration", "_easing")
    def __init__(self, dx, dy, duration, easing):
        self._dx, self._dy = float(dx), float(dy)
        self._duration = max(0.0, float(duration))
        self._easing = easing
    def _bind(self, node, key=None):
        return _BoundTween(node, key, _TweenAction("position", (self._dx, self._dy), self._duration, self._easing, True))

class _Wait(Action):
    __slots__ = ("_duration",)
    def __init__(self, duration):
        self._duration = max(0.0, float(duration))
    def _bind(self, node, key=None):
        return _BoundWait(node, key, self._duration)

class _BoundWait(_BoundAction):
    __slots__ = ("_duration", "_elapsed", "_leftover")
    def __init__(self, node, key, duration):
        super().__init__(node, key)
        self._duration = duration
        self._elapsed = 0.0
        self._leftover = 0.0
    def tick(self, dt):
        self._elapsed += dt
        done = self._elapsed >= self._duration
        if done:
            self._leftover = self._elapsed - self._duration
        return done

class _Call(Action):
    __slots__ = ("_fn",)
    def __init__(self, fn):
        self._fn = fn
    def _bind(self, node, key=None):
        return _BoundCall(node, key, self._fn)

class _BoundCall(_BoundAction):
    __slots__ = ("_fn", "_called", "_leftover")
    def __init__(self, node, key, fn):
        super().__init__(node, key)
        self._fn = fn
        self._called = False
        self._leftover = 0.0
    def tick(self, dt):
        if not self._called:
            self._fn()
            self._called = True
        self._leftover = dt
        return True

class _Remove(Action):
    __slots__ = ()
    def _bind(self, node, key=None):
        return _BoundRemove(node, key)

class _BoundRemove(_BoundAction):
    __slots__ = ("_done", "_leftover")
    def __init__(self, node, key):
        super().__init__(node, key)
        self._done = False
        self._leftover = 0.0
    def tick(self, dt):
        if not self._done:
            self._done = True
            if self.node.parent is not None:
                self.node.parent.remove(self.node)
        self._leftover = dt
        return True

class _Sequence(Action):
    __slots__ = ("_actions",)
    def __init__(self, actions):
        self._actions = list(actions)
    def _bind(self, node, key=None):
        return _BoundSequence(node, key, self._actions)

class _BoundSequence(_BoundAction):
    __slots__ = ("_actions", "_index", "_current", "_leftover")
    def __init__(self, node, key, actions):
        super().__init__(node, key)
        self._actions = actions
        self._index = 0
        self._current = actions[0]._bind(node) if actions else None
        self._leftover = 0.0
    def tick(self, dt):
        while self._current is not None:
            if not self._current.tick(dt):
                return False
            dt = self._current._leftover
            self._index += 1
            if self._index < len(self._actions):
                self._current = self._actions[self._index]._bind(self.node)
            else:
                self._current = None
        self._leftover = dt
        return True

class _Parallel(Action):
    __slots__ = ("_actions",)
    def __init__(self, actions):
        self._actions = list(actions)
    def _bind(self, node, key=None):
        return _BoundParallel(node, key, self._actions)

class _BoundParallel(_BoundAction):
    __slots__ = ("_running", "_leftover")
    def __init__(self, node, key, actions):
        super().__init__(node, key)
        self._running = [a._bind(node) for a in actions]
        self._leftover = 0.0
    def tick(self, dt):
        still_running = []
        for ba in self._running:
            if not ba.tick(dt):
                still_running.append(ba)
        self._running = still_running
        return len(self._running) == 0

class _Repeat(Action):
    __slots__ = ("_action", "_count")
    def __init__(self, action, count):
        self._action = action
        self._count = int(count)
    def _bind(self, node, key=None):
        return _BoundRepeat(node, key, self._action, self._count)

class _BoundRepeat(_BoundAction):
    __slots__ = ("_template", "_max", "_current", "_iteration", "_leftover")
    def __init__(self, node, key, action, count):
        super().__init__(node, key)
        self._template = action
        self._max = count
        self._current = action._bind(node)
        self._iteration = 1
        self._leftover = 0.0
    def tick(self, dt):
        while True:
            if not self._current.tick(dt):
                return False
            if self._max > 0 and self._iteration >= self._max:
                self._leftover = self._current._leftover
                return True
            dt = self._current._leftover
            if dt < 1e-6:
                return False
            self._iteration += 1
            self._current = self._template._bind(self.node)

# ---------------------------------------------------------------------------
# Public factory functions
# ---------------------------------------------------------------------------

def move_to(x, y, duration, easing=ease_in_out):
    return _MoveTo(x, y, duration, easing)

def move_by(dx, dy, duration, easing=ease_in_out):
    return _MoveBy(dx, dy, duration, easing)

def rotate_to(angle, duration, easing=ease_in_out):
    return _TweenAction("rotation", float(angle), duration, easing, False)

def rotate_by(angle, duration, easing=ease_in_out):
    return _TweenAction("rotation", float(angle), duration, easing, True)

def scale_to(s, duration, easing=ease_in_out):
    return _TweenAction("scale", float(s), duration, easing, False)

def fade_to(alpha, duration, easing=ease_in_out):
    return _TweenAction("opacity", float(alpha), duration, easing, False)

def fade_in(duration, easing=ease_in_out):
    return fade_to(1.0, duration, easing)

def fade_out(duration, easing=ease_in_out):
    return fade_to(0.0, duration, easing)

def wait(duration):
    return _Wait(duration)

def call(fn):
    return _Call(fn)

def remove():
    return _Remove()

def sequence(*actions):
    return _Sequence(actions)

def parallel(*actions):
    return _Parallel(actions)

def repeat(action, count=0):
    return _Repeat(action, count)

# ---------------------------------------------------------------------------
# Follow-path helpers
# ---------------------------------------------------------------------------

def _quad_at(p0, p1, p2, t):
    mt = 1.0 - t
    return (mt*mt*p0[0] + 2*mt*t*p1[0] + t*t*p2[0],
            mt*mt*p0[1] + 2*mt*t*p1[1] + t*t*p2[1])

def _cubic_at(p0, p1, p2, p3, t):
    mt = 1.0 - t
    mt2 = mt * mt; mt3 = mt2 * mt
    t2 = t * t; t3 = t2 * t
    return (mt3*p0[0] + 3*mt2*t*p1[0] + 3*mt*t2*p2[0] + t3*p3[0],
            mt3*p0[1] + 3*mt2*t*p1[1] + 3*mt*t2*p2[1] + t3*p3[1])

def _quad_tangent(p0, p1, p2, t):
    mt = 1.0 - t
    return (2*mt*(p1[0]-p0[0]) + 2*t*(p2[0]-p1[0]),
            2*mt*(p1[1]-p0[1]) + 2*t*(p2[1]-p1[1]))

def _cubic_tangent(p0, p1, p2, p3, t):
    mt = 1.0 - t
    return (3*mt*mt*(p1[0]-p0[0]) + 6*mt*t*(p2[0]-p1[0]) + 3*t*t*(p3[0]-p2[0]),
            3*mt*mt*(p1[1]-p0[1]) + 6*mt*t*(p2[1]-p1[1]) + 3*t*t*(p3[1]-p2[1]))

_SEG_LINE  = 0
_SEG_QUAD  = 1
_SEG_CUBIC = 2

def _seg_length(kind, pts, n=20):
    if kind == _SEG_LINE:
        dx = pts[1][0] - pts[0][0]; dy = pts[1][1] - pts[0][1]
        return math.sqrt(dx*dx + dy*dy)
    eval_fn = _quad_at if kind == _SEG_QUAD else _cubic_at
    length = 0.0
    px, py = pts[0]
    for i in range(1, n + 1):
        x, y = eval_fn(*pts, i / n)
        dx = x - px; dy = y - py
        length += math.sqrt(dx*dx + dy*dy)
        px, py = x, y
    return length

def _seg_point(kind, pts, u):
    if kind == _SEG_LINE:
        return (_lerp(pts[0][0], pts[1][0], u),
                _lerp(pts[0][1], pts[1][1], u))
    if kind == _SEG_QUAD:
        return _quad_at(*pts, u)
    return _cubic_at(*pts, u)

def _seg_tangent(kind, pts, u):
    if kind == _SEG_LINE:
        return (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
    if kind == _SEG_QUAD:
        return _quad_tangent(*pts, u)
    return _cubic_tangent(*pts, u)

def _build_segments(commands):
    segs = []        # [(kind, pts_tuple)]
    cum = [0.0]      # cumulative lengths, len = len(segs) + 1
    pen = (0.0, 0.0)
    subpath_start = pen
    for cmd, vals in commands:
        if cmd == "M":
            pen = (vals[0], vals[1])
            subpath_start = pen
        elif cmd == "L":
            end = (vals[0], vals[1])
            segs.append((_SEG_LINE, (pen, end)))
            cum.append(cum[-1] + _seg_length(_SEG_LINE, (pen, end)))
            pen = end
        elif cmd == "Q":
            cp = (vals[0], vals[1]); end = (vals[2], vals[3])
            pts = (pen, cp, end)
            segs.append((_SEG_QUAD, pts))
            cum.append(cum[-1] + _seg_length(_SEG_QUAD, pts))
            pen = end
        elif cmd == "C":
            cp1 = (vals[0], vals[1]); cp2 = (vals[2], vals[3]); end = (vals[4], vals[5])
            pts = (pen, cp1, cp2, end)
            segs.append((_SEG_CUBIC, pts))
            cum.append(cum[-1] + _seg_length(_SEG_CUBIC, pts))
            pen = end
        elif cmd == "Z":
            if pen != subpath_start:
                segs.append((_SEG_LINE, (pen, subpath_start)))
                cum.append(cum[-1] + _seg_length(_SEG_LINE, (pen, subpath_start)))
            pen = subpath_start
    return segs, cum

def _sample_path(segs, cum, e):
    total = cum[-1]
    if total < 1e-9:
        if segs:
            return _seg_point(segs[0][0], segs[0][1], 0.0), _seg_tangent(segs[0][0], segs[0][1], 0.0)
        return (0.0, 0.0), (1.0, 0.0)
    dist = e * total
    lo, hi = 0, len(segs) - 1
    while lo < hi:
        mid = (lo + hi) >> 1
        if cum[mid + 1] < dist:
            lo = mid + 1
        else:
            hi = mid
    idx = lo
    seg_start = cum[idx]; seg_len = cum[idx + 1] - seg_start
    u = (dist - seg_start) / seg_len if seg_len > 1e-9 else 0.0
    kind, pts = segs[idx]
    return _seg_point(kind, pts, u), _seg_tangent(kind, pts, u)

# ---------------------------------------------------------------------------
# follow_path action
# ---------------------------------------------------------------------------

class _FollowPath(Action):
    __slots__ = ("_commands", "_duration", "_easing", "_rotate")
    def __init__(self, commands, duration, easing, rotate):
        self._commands = commands
        self._duration = max(0.0, float(duration))
        self._easing = easing
        self._rotate = bool(rotate)
    def _bind(self, node, key=None):
        return _BoundFollowPath(node, key, self)

class _BoundFollowPath(_BoundAction):
    __slots__ = ("_segs", "_cum", "_duration", "_easing", "_rotate",
                 "_elapsed", "_leftover")
    def __init__(self, node, key, action: _FollowPath):
        super().__init__(node, key)
        self._duration = action._duration
        self._easing = action._easing
        self._rotate = action._rotate
        self._elapsed = 0.0
        self._leftover = 0.0
        self._segs, self._cum = _build_segments(action._commands)

    def tick(self, dt):
        self._elapsed += dt
        t = min(1.0, self._elapsed / self._duration) if self._duration > 0 else 1.0
        e = self._easing(t)
        pos, tan = _sample_path(self._segs, self._cum, e)
        self.node.position = pos
        if self._rotate:
            self.node.rotation = math.atan2(tan[1], tan[0])
        done = t >= 1.0
        if done:
            self._leftover = max(0.0, self._elapsed - self._duration) if self._duration > 0 else dt
        return done

def follow_path(path, duration, easing=ease_in_out, *, rotate=False, offset=None):
    if hasattr(path, '_commands'):
        cmds = list(path._commands)
        if offset is None:
            offset = (getattr(path, 'x', 0.0), getattr(path, 'y', 0.0))
    else:
        cmds = [(str(c).upper(), tuple(float(v) for v in vs) if vs else None) for c, vs in path]
    if offset and (offset[0] or offset[1]):
        ox, oy = float(offset[0]), float(offset[1])
        shifted = []
        for cmd, vals in cmds:
            if vals is None:
                shifted.append((cmd, vals))
            elif cmd == "M" or cmd == "L":
                shifted.append((cmd, (vals[0]+ox, vals[1]+oy)))
            elif cmd == "Q":
                shifted.append((cmd, (vals[0]+ox, vals[1]+oy, vals[2]+ox, vals[3]+oy)))
            elif cmd == "C":
                shifted.append((cmd, (vals[0]+ox, vals[1]+oy, vals[2]+ox, vals[3]+oy, vals[4]+ox, vals[5]+oy)))
            else:
                shifted.append((cmd, vals))
        cmds = shifted
    return _FollowPath(cmds, duration, easing, rotate)

# ---------------------------------------------------------------------------
# Color animation
# ---------------------------------------------------------------------------

class _ColorTo(Action):
    __slots__ = ("_target", "_duration", "_easing", "_attr")
    def __init__(self, target, duration, easing, attr):
        self._target = target
        self._duration = max(0.0, float(duration))
        self._easing = easing
        self._attr = attr
    def _bind(self, node, key=None):
        return _BoundColorTween(node, key, self)

class _BoundColorTween(_BoundAction):
    __slots__ = ("_attr", "_start", "_end", "_duration", "_easing", "_elapsed", "_leftover")
    def __init__(self, node, key, action: _ColorTo):
        super().__init__(node, key)
        self._duration = action._duration
        self._easing = action._easing
        self._elapsed = 0.0
        self._leftover = 0.0
        from .gpu import normalize_color
        attr = action._attr
        if attr is None:
            for a in ('tint', 'fill', 'color'):
                v = getattr(node, a, None)
                if v is not None:
                    attr = a; break
            if attr is None:
                attr = 'fill'
        self._attr = attr
        current = getattr(node, attr)
        self._start = tuple(float(c) for c in normalize_color(current))
        self._end = tuple(float(c) for c in normalize_color(action._target))

    def tick(self, dt):
        self._elapsed += dt
        t = min(1.0, self._elapsed / self._duration) if self._duration > 0 else 1.0
        e = self._easing(t)
        val = tuple(_lerp(a, b, e) for a, b in zip(self._start, self._end))
        setattr(self.node, self._attr, val)
        done = t >= 1.0
        if done:
            self._leftover = max(0.0, self._elapsed - self._duration) if self._duration > 0 else dt
        return done

def color_to(color, duration, easing=ease_in_out, *, attr=None):
    return _ColorTo(color, duration, easing, attr)
