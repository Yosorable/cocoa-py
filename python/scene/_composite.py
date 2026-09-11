"""scene._composite — Convenience composite nodes."""
from __future__ import annotations

import math

from ._common import radial_point
from ._node import Group
from ._shapes import Line
from ._text import Label


class RadialLabels(Group):
    def __init__(self, labels, radius, *, size=18, font=None, color="#ffffff", offset_turns=None, **kw):
        super().__init__(**kw)
        labels = [str(l) for l in labels]
        if not labels: return
        step = 1.0 / len(labels)
        off = step if offset_turns is None else float(offset_turns)
        for i, text in enumerate(labels):
            self.add(Label(text, size=size, font=font, color=color,
                          position=radial_point(radius, (i * step + off) * math.tau)))


class RadialTicks(Group):
    def __init__(self, count, *, inner, outer, width=1, color="#ffffff",
                 major_every=None, major_inner=None, major_outer=None,
                 major_width=None, major_color=None, offset_turns=0, **kw):
        super().__init__(**kw)
        if count <= 0: return
        for i in range(count):
            maj = bool(major_every and i % major_every == 0)
            ir = major_inner if maj and major_inner is not None else inner
            er = major_outer if maj and major_outer is not None else outer
            lw = major_width if maj and major_width is not None else width
            lc = major_color if maj and major_color is not None else color
            angle = (i / count + offset_turns) * math.tau
            self.add(Line(radial_point(ir, angle), radial_point(er, angle),
                         width=lw, color=lc))
