"""scene._text — Label and Image nodes."""
from __future__ import annotations

import math

from ._common import _apply, _avg_scale, _color, _rot
from ._engine import Cmd, KIND_TEX
from ._node import Node


class Label(Node):
    def __init__(self, text="", *, size=18, color="#ffffff", font=None, **kw):
        super().__init__(**kw)
        self._text = ""
        self._size = 18.0
        self._font = None
        self._rendered_size = None
        self.text = str(text)
        self.size = float(size)
        self.color = color
        self.font = font

    @property
    def text(self):
        return self._text

    @text.setter
    def text(self, value):
        value = str(value)
        if value != self._text:
            self._text = value
            self._rendered_size = None

    @property
    def size(self):
        return self._size

    @size.setter
    def size(self, value):
        value = float(value)
        if value != self._size:
            self._size = value
            self._rendered_size = None

    @property
    def font(self):
        return self._font

    @font.setter
    def font(self, value):
        if value != self._font:
            self._font = value
            self._rendered_size = None

    def _snap(self):
        return (self.x, self.y, self.rotation, self.scale, self.opacity, self.z,
                self.text, self.size, self.color, self.font)

    def _bounds(self):
        if self._rendered_size:
            w, h = self._rendered_size
            return (-w/2, -h/2, w/2, h/2)
        w = max(self.size * 0.6 * max(len(self.text), 1), self.size * 0.6)
        h = self.size * 1.2
        return (-w/2, -h/2, w/2, h/2)

    def _layout_bounds(self, renderer=None):
        if renderer is not None and self.text:
            try:
                scale = (renderer.screen_scale if getattr(renderer, "_capturing", False)
                         else renderer.window.scale)
                pixel_size = max(10, int(round(self.size * scale)))
                tex = renderer.text_texture(self.text, self.font, pixel_size)
                raster_scale = pixel_size / self.size if self.size > 0 else scale
                self._rendered_size = (tex.size[0] / raster_scale, tex.size[1] / raster_scale)
            except Exception:
                pass
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
            return
        s = max(_avg_scale(world), 0.001)
        base_pfs = max(10, int(round(self.size * renderer.screen_scale)))
        tex = renderer.text_texture(self.text, self.font, base_pfs)
        raster_scale = base_pfs / self.size if self.size > 0 else renderer.screen_scale
        dw = tex.size[0] / raster_scale * s
        dh = tex.size[1] / raster_scale * s
        self._rendered_size = (dw, dh)
        center = _apply(world, (0, 0))
        c = _color(self.color)
        order[0] += 1
        cmds.append(Cmd(self.z, order[0], KIND_TEX,
            center[0], center[1], dw/2, dh/2, _rot(world),
            (KIND_TEX, 0, 0, 0), (0, 0, opacity, 0),
            (0, 0, 0, 0), c, tex))


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
