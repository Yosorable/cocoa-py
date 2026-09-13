"""Immutable clipping geometry shared by rendering and input."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class ClipRect:
    """Clip a node and its descendants in the node's local coordinates.

    ``x, y`` is the top-left corner. Zero width or height clips everything.
    ``radius`` rounds the corners and is clamped to half the shorter edge.
    Ancestor clips intersect this region, including through affine transforms.
    """

    x: float
    y: float
    width: float
    height: float
    radius: float = 0

    def __post_init__(self):
        for name in ("x", "y", "width", "height", "radius"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"clip {name} must be finite")
            if name in ("width", "height", "radius") and value < 0:
                raise ValueError(f"clip {name} must be non-negative")
            object.__setattr__(self, name, value)

    def _state(self, world):
        a, b, c, d, tx, ty = world
        det = a * d - b * c
        if not math.isfinite(det) or det == 0:
            # A singular transform has no visible area, rather than an
            # inverse that accidentally exposes the entire viewport.
            return (1., 0., 0., 1., 0., 0., 0., 0., 0., 0., 0.)
        inv = (d / det, -b / det, -c / det, a / det,
               (c * ty - d * tx) / det, (b * tx - a * ty) / det)
        return (*inv, self.x, self.y, self.width, self.height,
                min(self.radius, self.width / 2, self.height / 2))


def _contains(clips, x, y):
    for a, b, c, d, tx, ty, rx, ry, w, h, radius in clips:
        if w <= 0 or h <= 0:
            return False
        px, py = a * x + c * y + tx - rx, b * x + d * y + ty - ry
        if not (0 <= px <= w and 0 <= py <= h):
            return False
        dx = max(abs(px - w / 2) - (w / 2 - radius), 0)
        dy = max(abs(py - h / 2) - (h / 2 - radius), 0)
        if dx * dx + dy * dy > radius * radius:
            return False
    return True
