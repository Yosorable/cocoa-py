"""Scene transition effects."""
from __future__ import annotations

import struct

from .action import ease_in_out


# Vertex format: (x, y, u, v) * 6 vertices = 1 fullscreen quad
_VF = struct.Struct("24f")
# QD format matching tex_frag: params(4f), style(4f), fill(4f), extra(4f)
_QF = struct.Struct("16f")


def _build_quad(x, y, w, h):
    """Build 2-triangle fullscreen quad vertex data (position + UV)."""
    x1, y1 = x + w, y + h
    return _VF.pack(
        x, y, 0.0, 0.0,
        x1, y, 1.0, 0.0,
        x, y1, 0.0, 1.0,
        x1, y, 1.0, 0.0,
        x1, y1, 1.0, 1.0,
        x, y1, 0.0, 1.0,
    )


def _build_qd(opacity):
    """Build QD buffer for tex_frag: tint white, given opacity."""
    # params: (kind=0, half_w, half_h, radius) — unused for tex
    # style: (stroke_w, feather, opacity, pad)
    # fill: (r, g, b, a) — unused for tex
    # extra: (tint_r, tint_g, tint_b, tint_a) — tex_frag uses extra.rgb * extra.a * style.z
    return _QF.pack(
        0.0, 0.0, 0.0, 0.0,       # params (unused by tex_frag)
        0.0, 0.0, opacity, 0.0,    # style: style.z = opacity
        1.0, 1.0, 1.0, 1.0,        # fill (unused by tex_frag)
        1.0, 1.0, 1.0, 1.0,        # extra: tint = white, extra.a = 1
    )


class Transition:
    """Describes how to animate between two scenes.

    duration: seconds
    easing: t->t function (default ease_in_out)
    _compose: (t, old_tex, new_tex, renderer, window) -> draws to screen
    """
    __slots__ = ("duration", "easing", "_compose")

    def __init__(self, duration, easing, compose_fn):
        self.duration = max(0.001, float(duration))
        self.easing = easing
        self._compose = compose_fn

    def compose(self, progress, old_tex, new_tex, renderer, window):
        t = self.easing(max(0.0, min(1.0, progress)))
        self._compose(t, old_tex, new_tex, renderer, window)


# ── Compose functions ───────────────────────────────────────────────────────

def _draw_tex_quad(f, renderer, tex, x, y, w, h, opacity, res_buf):
    """Draw a single textured quad within an open render pass."""
    verts = _build_quad(x, y, w, h)
    qd = _build_qd(opacity)
    vb = renderer._acquire(len(verts))
    qb = renderer._acquire(len(qd))
    vb.write(verts)
    qb.write(qd)
    f.set_pipeline(renderer._tp)
    f.set_vertex_buffer(vb, 0)
    f.set_vertex_buffer(res_buf, 1)
    f.set_fragment_buffer(qb, 0)
    f.set_fragment_texture(tex, 0)
    f.draw("triangle", 0, 6)


def _compose_fade(t, old_tex, new_tex, renderer, window):
    from ._engine import _UNI
    w, h = window.size
    ub = renderer._acquire(_UNI.size)
    ub.write(_UNI.pack(w, h, window.scale, 0))
    with renderer._frame((0, 0, 0, 1)) as f:
        _draw_tex_quad(f, renderer, old_tex, 0, 0, w, h, 1.0 - t, ub)
        _draw_tex_quad(f, renderer, new_tex, 0, 0, w, h, t, ub)


def _make_push_compose(dx_fn, dy_fn):
    def compose(t, old_tex, new_tex, renderer, window):
        from ._engine import _UNI
        w, h = window.size
        dx, dy = dx_fn(w, t), dy_fn(h, t)
        ub = renderer._acquire(_UNI.size)
        ub.write(_UNI.pack(w, h, window.scale, 0))
        with renderer._frame((0, 0, 0, 1)) as f:
            _draw_tex_quad(f, renderer, old_tex, dx, dy, w, h, 1.0, ub)
            ndx = dx + w if dx < 0 else (dx - w if dx > 0 else 0)
            ndy = dy + h if dy < 0 else (dy - h if dy > 0 else 0)
            _draw_tex_quad(f, renderer, new_tex, ndx, ndy, w, h, 1.0, ub)
    return compose


# ── Public API ──────────────────────────────────────────────────────────────

def fade(duration=0.4, easing=ease_in_out):
    """Crossfade: new scene fades in over old scene."""
    return Transition(duration, easing, _compose_fade)


def push_left(duration=0.4, easing=ease_in_out):
    """New scene enters from right, old scene exits left."""
    return Transition(duration, easing,
                      _make_push_compose(lambda w, t: -w * t, lambda h, t: 0))


def push_right(duration=0.4, easing=ease_in_out):
    """New scene enters from left, old scene exits right."""
    return Transition(duration, easing,
                      _make_push_compose(lambda w, t: w * t, lambda h, t: 0))


def push_up(duration=0.4, easing=ease_in_out):
    """New scene enters from bottom, old scene exits upward."""
    return Transition(duration, easing,
                      _make_push_compose(lambda w, t: 0, lambda h, t: -h * t))


def push_down(duration=0.4, easing=ease_in_out):
    """New scene enters from top, old scene exits downward."""
    return Transition(duration, easing,
                      _make_push_compose(lambda w, t: 0, lambda h, t: h * t))
