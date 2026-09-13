"""scene._shapes — Circle, Rect, and Line nodes."""
from __future__ import annotations

import math

from ._common import (
    _apply, _avg_scale, _color, _feather, _is_gradient, _rot, radial_point,
)
from ._engine import Cmd, KIND_CIRCLE, KIND_LINE, KIND_RING, KIND_RRECT, KIND_STROKE_RRECT
from ._node import Node


class Circle(Node):
    def __init__(self, radius, *, fill="#ffffff", stroke=None, stroke_width=0, **kw):
        super().__init__(**kw)
        self.radius = float(radius)
        self.fill = fill
        self.stroke = stroke
        self.stroke_width = float(stroke_width)

    def _snap(self):
        return (self.x, self.y, self.rotation, self.scale, self.opacity, self.z,
                self.radius, self.fill, self.stroke, self.stroke_width)

    def _bounds(self):
        e = self.radius + self.stroke_width * 0.5
        return (-e, -e, e, e)

    def contains_point(self, wx, wy):
        if not self._inside_clip(wx, wy):
            return False
        lx, ly = self.convert_from_world(wx, wy)
        r = self.radius + self.stroke_width * 0.5
        return lx * lx + ly * ly <= r * r

    def _collider(self):
        wt = self._world_transform
        cx, cy = _apply(wt, (0, 0))
        r = self.radius * _avg_scale(wt)
        return ('circle', cx, cy, r)

    def _emit(self, cmds, renderer, world, opacity, order):
        center = _apply(world, (0, 0))
        s = _avg_scale(world)
        r = self.radius * s
        f = _feather(s)
        m = f + 1
        if self.fill is not None:
            if _is_gradient(self.fill):
                g = self.fill
                c = g.color1
                extra = (g.color2[0], g.color2[1], g.color2[2], g.angle)
                gt = float(g.kind)
            else:
                c = _color(self.fill)
                extra = (0, 0, 0, 0)
                gt = 0.0
            order[0] += 1
            hw = hh = r + m
            cmds.append(Cmd(self.z, order[0], KIND_CIRCLE,
                center[0], center[1], hw, hh, 0,
                (KIND_CIRCLE, hw, hh, r), (0, f, opacity, gt),
                c, extra, None))
        if self.stroke is not None and self.stroke_width > 0:
            c = _color(self.stroke)
            sw = self.stroke_width * s
            order[0] += 1
            hw = hh = r + sw * 0.5 + m
            cmds.append(Cmd(self.z + 0.0001, order[0], KIND_RING,
                center[0], center[1], hw, hh, 0,
                (KIND_RING, hw, hh, r), (sw, f, opacity, 0),
                c, (0, 0, 0, 0), None))


class Rect(Node):
    def __init__(self, width, height, *, radius=0, fill="#ffffff", stroke=None, stroke_width=0, **kw):
        super().__init__(**kw)
        self.width = float(width)
        self.height = float(height)
        self.radius = float(radius)
        self.fill = fill
        self.stroke = stroke
        self.stroke_width = float(stroke_width)

    def _snap(self):
        return (self.x, self.y, self.rotation, self.scale, self.opacity, self.z,
                self.width, self.height, self.radius, self.fill, self.stroke, self.stroke_width)

    def _bounds(self):
        p = self.stroke_width * 0.5
        return (-self.width/2 - p, -self.height/2 - p, self.width/2 + p, self.height/2 + p)

    def _collider(self):
        wt = self._world_transform
        cx, cy = _apply(wt, (0, 0))
        sx = math.hypot(wt[0], wt[1])
        sy = math.hypot(wt[2], wt[3])
        return ('obb', cx, cy, self.width * sx / 2, self.height * sy / 2, _rot(wt))

    def _emit(self, cmds, renderer, world, opacity, order):
        center = _apply(world, (0, 0))
        sx = math.hypot(world[0], world[1])
        sy = math.hypot(world[2], world[3])
        w, h = self.width * sx, self.height * sy
        cr = self.radius * min(sx, sy)
        f = _feather((sx + sy) * 0.5)
        m = f + 1
        if self.fill is not None:
            if _is_gradient(self.fill):
                g = self.fill
                c = g.color1
                extra = (g.color2[0], g.color2[1], g.color2[2], g.angle)
                gt = float(g.kind)
            else:
                c = _color(self.fill)
                extra = (0, 0, 0, 0)
                gt = 0.0
            order[0] += 1
            hw, hh = w/2 + m, h/2 + m
            cmds.append(Cmd(self.z, order[0], KIND_RRECT,
                center[0], center[1], hw, hh, _rot(world),
                (KIND_RRECT, hw, hh, cr), (0, f, opacity, gt),
                c, extra, None))
        if self.stroke is not None and self.stroke_width > 0:
            c = _color(self.stroke)
            sw = self.stroke_width * min(sx, sy)
            order[0] += 1
            hw, hh = w/2 + sw/2 + m, h/2 + sw/2 + m
            cmds.append(Cmd(self.z + 0.0001, order[0], KIND_STROKE_RRECT,
                center[0], center[1], hw, hh, _rot(world),
                (KIND_STROKE_RRECT, hw, hh, cr), (sw, f, opacity, 0),
                c, (0, 0, 0, 0), None))


class Line(Node):
    def __init__(self, start=(0,0), end=(0,0), *, width=1, color="#ffffff", **kw):
        super().__init__(**kw)
        self.start = (float(start[0]), float(start[1]))
        self.end = (float(end[0]), float(end[1]))
        self.width = float(width)
        self.color = color

    def _snap(self):
        return (self.x, self.y, self.rotation, self.scale, self.opacity, self.z,
                self.start, self.end, self.width, self.color)

    @classmethod
    def polar(cls, length, *, angle=0, origin=(0,0), width=1, color="#ffffff", **kw):
        dx, dy = radial_point(length, angle)
        return cls(origin, (origin[0]+dx, origin[1]+dy), width=width, color=color, **kw)

    def set_polar(self, angle, length, *, origin=(0,0)):
        dx, dy = radial_point(length, angle)
        self.start = (float(origin[0]), float(origin[1]))
        self.end = (origin[0]+dx, origin[1]+dy)
        return self

    def _bounds(self):
        p = self.width * 0.5
        xs = (self.start[0], self.end[0])
        ys = (self.start[1], self.end[1])
        return (min(xs)-p, min(ys)-p, max(xs)+p, max(ys)+p)

    def contains_point(self, wx, wy):
        if not self._inside_clip(wx, wy):
            return False
        lx, ly = self.convert_from_world(wx, wy)
        sx, sy = self.start
        ex, ey = self.end
        dx, dy = ex - sx, ey - sy
        len_sq = dx * dx + dy * dy
        if len_sq < 1e-12:
            return math.hypot(lx - sx, ly - sy) <= self.width * 0.5
        t = max(0.0, min(1.0, ((lx - sx) * dx + (ly - sy) * dy) / len_sq))
        px, py = sx + t * dx, sy + t * dy
        return math.hypot(lx - px, ly - py) <= self.width * 0.5

    def _collider(self):
        wt = self._world_transform
        ws = _apply(wt, self.start)
        we = _apply(wt, self.end)
        s = _avg_scale(wt)
        cx = (ws[0] + we[0]) / 2
        cy = (ws[1] + we[1]) / 2
        half_len = math.hypot(ws[0] - we[0], ws[1] - we[1]) / 2
        angle = math.atan2(we[1] - ws[1], we[0] - ws[0])
        return ('obb', cx, cy, half_len + self.width * s / 2, self.width * s / 2, angle)

    def _emit(self, cmds, renderer, world, opacity, order):
        ws = _apply(world, self.start)
        we = _apply(world, self.end)
        s = _avg_scale(world)
        lw = self.width * s
        f = _feather(s)
        m = lw/2 + f + 1
        cx = (ws[0] + we[0]) / 2
        cy = (ws[1] + we[1]) / 2
        hw = abs(ws[0] - we[0]) / 2 + m
        hh = abs(ws[1] - we[1]) / 2 + m
        c = _color(self.color)
        order[0] += 1
        cmds.append(Cmd(self.z, order[0], KIND_LINE,
            cx, cy, hw, hh, 0,
            (KIND_LINE, hw, hh, 0), (lw, f, opacity, 0),
            c, (ws[0]-cx, ws[1]-cy, we[0]-cx, we[1]-cy), None))
