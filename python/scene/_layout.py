"""scene._layout — Layout container nodes."""
from __future__ import annotations

from ._enums import Alignment
from ._node import Node, Group, _measure_children


def _find_renderer(node):
    root = node
    while getattr(root, 'parent', None) is not None:
        root = root.parent
    return getattr(root, '_renderer', None)


def _layout_bounds(node, renderer):
    measure = getattr(node, '_layout_bounds', None)
    if measure is not None:
        return measure(renderer)
    return node._bounds()


class Spacer(Node):
    """Invisible spacer for layout containers."""

    def __init__(self, width=0, height=0, **kw):
        super().__init__(**kw)
        self.spacer_width = float(width)
        self.spacer_height = float(height)

    def _bounds(self):
        w, h = self.spacer_width, self.spacer_height
        return (-w / 2, -h / 2, w / 2, h / 2)

    def _emit(self, cmds, renderer, world, opacity, order):
        pass


class ZStack(Group):
    """Layer-stacking container. All children are centered at the container's origin."""

    def __init__(self, alignment="center", *, children=None, **kw):
        super().__init__(**kw)
        self.alignment = alignment
        if children:
            self.add(*children)

    def _tick(self, dt):
        self._layout()
        super()._tick(dt)

    def _bounds(self):
        self._layout()
        return _measure_children(self.children)

    def _layout(self):
        items = []
        renderer = _find_renderer(self)
        for c in self.children:
            if not c.visible:
                continue
            b = _layout_bounds(c, renderer)
            if b is None:
                continue
            items.append((c, b, b[2] - b[0], b[3] - b[1]))
        if not items:
            return

        max_w = max(w for _, _, w, _ in items)
        max_h = max(h for _, _, _, h in items)
        al = self.alignment

        for c, b, w, h in items:
            cx_off = (b[0] + b[2]) / 2.0
            cy_off = (b[1] + b[3]) / 2.0
            if "left" in al:
                c.x = -max_w / 2.0 - b[0]
            elif "right" in al:
                c.x = max_w / 2.0 - b[2]
            else:
                c.x = -cx_off
            if "top" in al:
                c.y = -max_h / 2.0 - b[1]
            elif "bottom" in al:
                c.y = max_h / 2.0 - b[3]
            else:
                c.y = -cy_off


class HStack(Group):
    """Horizontal layout container. Children are arranged left-to-right."""

    def __init__(self, spacing=0, alignment: Alignment | str = Alignment.CENTER,
                 *, padding=0, children=None, **kw):
        super().__init__(**kw)
        self.spacing = float(spacing)
        self.alignment = Alignment(alignment)
        self._padding = _parse_padding(padding)
        if children:
            self.add(*children)

    @property
    def padding(self):
        return self._padding

    @padding.setter
    def padding(self, value):
        self._padding = _parse_padding(value)

    def _tick(self, dt):
        self._layout()
        super()._tick(dt)

    def _bounds(self):
        self._layout()
        b = _measure_children(self.children)
        if b is None:
            return None
        pl, pt, pr, pb = self._padding
        return (b[0] - pl, b[1] - pt, b[2] + pr, b[3] + pb)

    def _layout(self):
        items = []
        renderer = _find_renderer(self)
        for c in self.children:
            if not c.visible:
                continue
            b = _layout_bounds(c, renderer)
            if b is None:
                continue
            items.append((c, b, b[2] - b[0], b[3] - b[1]))

        if not items:
            return

        pl, pt, pr, pb = self._padding
        total_w = sum(w for _, _, w, _ in items) + self.spacing * max(0, len(items) - 1)
        max_h = max(h for _, _, _, h in items)
        cursor = -total_w / 2.0 + (pl - pr) / 2.0

        for c, b, w, h in items:
            cx_off = (b[0] + b[2]) / 2.0
            cy_off = (b[1] + b[3]) / 2.0
            c.x = cursor + w / 2.0 - cx_off
            if self.alignment == Alignment.START:
                c.y = -max_h / 2.0 - b[1] + (pt - pb) / 2.0
            elif self.alignment == Alignment.END:
                c.y = max_h / 2.0 - b[3] + (pt - pb) / 2.0
            else:
                c.y = -cy_off + (pt - pb) / 2.0
            cursor += w + self.spacing


class VStack(Group):
    """Vertical layout container. Children are arranged top-to-bottom."""

    def __init__(self, spacing=0, alignment: Alignment | str = Alignment.CENTER,
                 *, padding=0, children=None, **kw):
        super().__init__(**kw)
        self.spacing = float(spacing)
        self.alignment = Alignment(alignment)
        self._padding = _parse_padding(padding)
        if children:
            self.add(*children)

    @property
    def padding(self):
        return self._padding

    @padding.setter
    def padding(self, value):
        self._padding = _parse_padding(value)

    def _tick(self, dt):
        self._layout()
        super()._tick(dt)

    def _bounds(self):
        self._layout()
        b = _measure_children(self.children)
        if b is None:
            return None
        pl, pt, pr, pb = self._padding
        return (b[0] - pl, b[1] - pt, b[2] + pr, b[3] + pb)

    def _layout(self):
        items = []
        renderer = _find_renderer(self)
        for c in self.children:
            if not c.visible:
                continue
            b = _layout_bounds(c, renderer)
            if b is None:
                continue
            items.append((c, b, b[2] - b[0], b[3] - b[1]))

        if not items:
            return

        pl, pt, pr, pb = self._padding
        total_h = sum(h for _, _, _, h in items) + self.spacing * max(0, len(items) - 1)
        max_w = max(w for _, _, w, _ in items)
        cursor = -total_h / 2.0 + (pt - pb) / 2.0

        for c, b, w, h in items:
            cx_off = (b[0] + b[2]) / 2.0
            cy_off = (b[1] + b[3]) / 2.0
            c.y = cursor + h / 2.0 - cy_off
            if self.alignment == Alignment.START:
                c.x = -max_w / 2.0 - b[0] + (pl - pr) / 2.0
            elif self.alignment == Alignment.END:
                c.x = max_w / 2.0 - b[2] + (pl - pr) / 2.0
            else:
                c.x = -cx_off + (pl - pr) / 2.0
            cursor += h + self.spacing


def _parse_padding(value):
    if isinstance(value, (int, float)):
        v = float(value)
        return (v, v, v, v)
    if len(value) == 2:
        h, v = float(value[0]), float(value[1])
        return (h, v, h, v)
    if len(value) == 4:
        return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    raise ValueError("padding must be a number, (h, v), or (left, top, right, bottom)")
