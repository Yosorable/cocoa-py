"""scene._text — Label and Image nodes."""
from __future__ import annotations

import math
import operator
import struct

from ._common import _apply, _avg_scale, _color, _rot
from ._engine import Cmd, KIND_TEX
from ._node import Layer, Node


class Label(Node):
    """Shaped text with optional paragraph layout, in local point coordinates.

    max_width fixes the paragraph box width; None measures the longest explicit
    line. line_spacing adds space between lines. max_lines limits visible lines;
    overflow selects clipping or an ellipsis on the final visible line. Empty
    text has one line of metrics and no ink. measure() does not open a window.
    """

    def __init__(self, text="", *, size=18, color="#ffffff", font=None,
                 max_width=None, alignment="left", line_spacing=0,
                 max_lines=None, overflow="clip", wrap="word", **kw):
        super().__init__(**kw)
        self._measurement = self._rendered_size = None
        self.text, self.size, self.color, self.font = text, size, color, font
        self.max_width, self.alignment, self.line_spacing = max_width, alignment, line_spacing
        self.max_lines, self.overflow, self.wrap = max_lines, overflow, wrap

    def _set_option(self, name, value):
        if value == getattr(self, "_" + name, object()):
            return
        setattr(self, "_" + name, value)
        if name != "color":
            self._measurement = self._rendered_size = None
        node = self.parent
        while node is not None:
            if isinstance(node, Layer):
                node.invalidate()
            node = node.parent

    @staticmethod
    def _dimension(value, name, *, zero=False):
        value = float(value)
        if not math.isfinite(value) or value < 0 or (not zero and value == 0):
            raise ValueError(f"{name} must be finite and {'nonnegative' if zero else 'positive'}")
        return value

    def _choice(self, name, value, choices):
        if value not in choices:
            raise ValueError(f"{name} must be one of {choices}")
        self._set_option(name, value)

    text = property(lambda self: self._text, lambda self, v: self._set_option("text", str(v)))
    size = property(lambda self: self._size,
                    lambda self, v: self._set_option("size", self._dimension(v, "size")))
    color = property(lambda self: self._color, lambda self, v: self._set_option("color", _color(v)))
    max_width = property(lambda self: self._max_width,
                         lambda self, v: self._set_option("max_width", None if v is None else self._dimension(v, "max_width")))
    line_spacing = property(lambda self: self._line_spacing,
                            lambda self, v: self._set_option("line_spacing", self._dimension(v, "line_spacing", zero=True)))
    alignment = property(lambda self: self._alignment,
                         lambda self, v: self._choice("alignment", v, ("left", "center", "right")))
    overflow = property(lambda self: self._overflow,
                        lambda self, v: self._choice("overflow", v, ("clip", "ellipsis")))
    wrap = property(lambda self: self._wrap,
                    lambda self, v: self._choice("wrap", v, ("word", "char", "none")))

    @property
    def font(self):
        return self._font

    @font.setter
    def font(self, value):
        if value is not None and not isinstance(value, str):
            raise TypeError("font must be a name or None")
        if value is not None and "\x00" in value:
            raise ValueError("font must not contain a null character")
        self._set_option("font", value)

    @property
    def max_lines(self):
        return self._max_lines

    @max_lines.setter
    def max_lines(self, value):
        if value is not None:
            value = operator.index(value)
            if not 1 <= value <= 100000:
                raise ValueError("max_lines must be between 1 and 100000, or None")
        self._set_option("max_lines", value)

    @property
    def _layout_options(self):
        return (self.size, self.max_width or 0., self.alignment, self.line_spacing,
                self.max_lines or 0, self.overflow, self.wrap)

    def _measure(self):
        if self._measurement is None:
            from _cocoa import _metal
            size, width, alignment, spacing, lines, overflow, wrap = self._layout_options
            self._measurement = _metal.text_layout(self.text, size, self.font, width,
                alignment, spacing, lines, overflow, wrap, 1., False)
            self._rendered_size = self._measurement["logical_size"]
        return self._measurement

    def measure(self):
        """Return (width, height) before node, parent, or camera transforms."""
        return self._measure()["logical_size"]

    @property
    def line_count(self):
        return self._measure()["line_count"]

    @property
    def truncated(self):
        return self._measure()["truncated"]

    def _snap(self):
        return (*super()._snap(), self.text, self.color, self.font, self._layout_options)

    def _bounds(self):
        width, height = self.measure()
        return (-width / 2, -height / 2, width / 2, height / 2)

    def _layout_bounds(self, renderer=None):
        return self._bounds()

    def _collider(self):
        wt = self._world_transform
        cx, cy = _apply(wt, (0, 0))
        sx = math.hypot(wt[0], wt[1])
        sy = math.hypot(wt[2], wt[3])
        b = self._bounds()
        return ('obb', cx, cy, (b[2] - b[0]) * sx / 2, (b[3] - b[1]) * sy / 2, _rot(wt))

    def _emit(self, cmds, renderer, world, opacity, order):
        if not self.text:
            self._measure()
            return
        requested = self.size * renderer.screen_scale
        if not math.isfinite(requested) or requested > 16384:
            raise ValueError("Label raster font size must be finite and at most 16384 pixels.")
        base_pfs = max(10, int(round(requested)))
        tex = renderer.text_texture(self.text, self.font, base_pfs, layout=self._layout_options)
        dw, dh = tex.draw_size
        self._rendered_size = tex.logical_size
        self._measurement = {"logical_size": tex.logical_size, "line_count": tex.line_count,
                             "truncated": tex.truncated}
        center = _apply(world, (0, 0))
        c = _color(self.color)
        order[0] += 1
        command = Cmd(self.z, order[0], KIND_TEX,
            center[0], center[1], dw/2, dh/2, _rot(world),
            (KIND_TEX, 0, 0, 0), (0, 0, opacity, 0),
            (0, 0, 0, 0), c, tex)
        # Transform all four corners, including reflection, shear, and
        # nonuniform scale. Hit tests use the same local paragraph rectangle.
        def vertex(x, y, u, v):
            return struct.pack("<4f", *_apply(world, (x, y)), u, v)
        tl = vertex(-dw / 2, -dh / 2, 0, 0)
        tr = vertex(dw / 2, -dh / 2, 1, 0)
        bl = vertex(-dw / 2, dh / 2, 0, 1)
        br = vertex(dw / 2, dh / 2, 1, 1)
        command._vb = tl + tr + bl + tr + br + bl
        cmds.append(command)


class Image(Node):
    def __init__(self, texture, size, **kw):
        super().__init__(**kw)
        self.texture = texture
        self.img_size = (float(size[0]), float(size[1]))

    def _snap(self):
        return (self.x, self.y, self.rotation, self.scale, self.opacity, self.z,
                self.texture._handle if self.texture else None, self.img_size)

    def _bounds(self):
        return (-self.img_size[0]/2, -self.img_size[1]/2, self.img_size[0]/2, self.img_size[1]/2)

    def _collider(self):
        wt = self._world_transform
        cx, cy = _apply(wt, (0, 0))
        sx = math.hypot(wt[0], wt[1])
        sy = math.hypot(wt[2], wt[3])
        return ('obb', cx, cy, self.img_size[0] * sx / 2, self.img_size[1] * sy / 2, _rot(wt))

    def _emit(self, cmds, renderer, world, opacity, order):
        if self.texture is None:
            return
        sx = math.hypot(world[0], world[1])
        sy = math.hypot(world[2], world[3])
        center = _apply(world, (0, 0))
        order[0] += 1
        cmds.append(Cmd(self.z, order[0], KIND_TEX,
            center[0], center[1],
            self.img_size[0]*sx/2, self.img_size[1]*sy/2, _rot(world),
            (KIND_TEX, 0, 0, 0), (0, 0, opacity, 0),
            (0, 0, 0, 0), (1, 1, 1, 1), self.texture))
