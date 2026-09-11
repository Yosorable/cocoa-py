"""scene._common — shared math, color, gradient, and data types."""
from __future__ import annotations

import math
from collections import namedtuple
from dataclasses import dataclass

from _cocoa import _scene_accel
from ._engine import normalize_color
# re-export enums for convenience (canonical home is ._enums)
from ._enums import (
    Alignment, FillRule, LineCap, LineJoin, Orientation, TouchPhase,
)

# ── Math ──

def radial_point(radius: float, angle: float) -> tuple[float, float]:
    return (math.sin(angle) * radius, -math.cos(angle) * radius)

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

def _identity():
    return _IDENTITY

_mul = _scene_accel.mul
_apply = lambda m, p: _scene_accel.apply(m, p[0], p[1])
_matrix = _scene_accel.matrix
_avg_scale = _scene_accel.avg_scale
_rot = _scene_accel.rot
_feather = _scene_accel.feather
_invert = _scene_accel.invert

def _color(v):
    return tuple(float(c) for c in normalize_color(v))

# ── Gradient ──

_Gradient = namedtuple('_Gradient', ('kind', 'color1', 'color2', 'angle'))

def linear_gradient(color1, color2, angle=0):
    """Create a linear gradient fill.

    Parameters
    ----------
    color1, color2 : color
        Start and end colors (hex string or RGBA tuple).
    angle : float
        Gradient direction in degrees (0 = left→right, 90 = top→bottom).
    """
    c1 = tuple(float(c) for c in normalize_color(color1))
    c2 = tuple(float(c) for c in normalize_color(color2))
    return _Gradient(1, c1, c2, math.radians(angle))

def radial_gradient(color1, color2):
    """Create a radial gradient fill (center → edge).

    Parameters
    ----------
    color1 : color
        Center color.
    color2 : color
        Edge color.
    """
    c1 = tuple(float(c) for c in normalize_color(color1))
    c2 = tuple(float(c) for c in normalize_color(color2))
    return _Gradient(2, c1, c2, 0.0)

def _is_gradient(fill):
    return isinstance(fill, _Gradient)

# ── Touch ──

@dataclass(slots=True)
class Touch:
    id: int
    position: tuple[float, float]
    prev_position: tuple[float, float]
    phase: TouchPhase

# Index → TouchPhase, matching the int phase code from the native layer.
# TouchPhase is a StrEnum so ``phase == "began"`` continues to work.
_PHASES = (TouchPhase.BEGAN, TouchPhase.MOVED, TouchPhase.ENDED, TouchPhase.CANCELLED)

# ── CollisionInfo ──

@dataclass(slots=True)
class CollisionInfo:
    hit: bool
    normal: tuple[float, float]
    depth: float
    point: tuple[float, float]
