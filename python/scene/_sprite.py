"""scene._sprite — Sprite, SpriteAtlas, and NineSlice nodes."""
from __future__ import annotations

import json
import math
from collections import namedtuple
from pathlib import Path

from . import gpu
from .gpu import Texture
from ._common import _apply, _avg_scale, _color, _rot
from ._engine import Cmd, KIND_TEX, _quad_verts_uv
from ._node import Node

# ───────────────────────────────────────────────────────────────────────────
# Sprite
# ───────────────────────────────────────────────────────────────────────────

SpriteFrame = namedtuple('SpriteFrame', ('texture', 'uv_rect'))

_sprite_textures: dict[str, list] = {}

def _acquire_sprite_texture(path: str) -> Texture:
    entry = _sprite_textures.get(path)
    if entry is None:
        tex = Texture.from_file(path)
        _sprite_textures[path] = [tex, 1]
        return tex
    entry[1] += 1
    return entry[0]

def _release_sprite_texture(path: str | None):
    if path is None:
        return
    entry = _sprite_textures.get(path)
    if entry is None:
        return
    entry[1] -= 1
    if entry[1] <= 0:
        tex = entry[0]
        del _sprite_textures[path]
        try:
            tex.close()
        except Exception:
            pass

class SpriteAtlas:
    """Load a sprite sheet from an image + TexturePacker JSON hash format."""

    def __init__(self, image_path, json_path):
        self._image_path = image_path
        self._texture = _acquire_sprite_texture(image_path)
        with open(json_path, 'r') as f:
            data = json.load(f)
        meta = data.get('meta', {})
        sz = meta.get('size', {})
        img_w = sz.get('w', self._texture.size[0])
        img_h = sz.get('h', self._texture.size[1])
        self._frames: dict[str, SpriteFrame] = {}
        for name, info in data.get('frames', {}).items():
            fr = info['frame']
            u0 = fr['x'] / img_w
            v0 = fr['y'] / img_h
            u1 = (fr['x'] + fr['w']) / img_w
            v1 = (fr['y'] + fr['h']) / img_h
            self._frames[name] = SpriteFrame(self._texture, (u0, v0, u1, v1))

    def __getitem__(self, name):
        return self._frames[name]

    def __contains__(self, name):
        return name in self._frames

    def frames(self, prefix=''):
        return [f for _, f in sorted(
            ((n, f) for n, f in self._frames.items() if n.startswith(prefix)),
            key=lambda p: p[0]
        )]

    @property
    def names(self):
        return list(self._frames.keys())

    def close(self):
        if self._texture is not None:
            _release_sprite_texture(self._image_path)
            self._texture = None
            self._frames = {}


class Sprite(Node):
    """Image node with anchor, flip, tint, and animation support.

    source: file path (str) or SpriteFrame from atlas.
    """

    def __init__(self, source, *, size=None, anchor=(0.5, 0.5),
                 flip_x=False, flip_y=False, tint=(1, 1, 1, 1), **kw):
        super().__init__(**kw)
        self._texture_path: str | None = None
        if isinstance(source, SpriteFrame):
            self.texture = source.texture
            self._uv_rect = source.uv_rect
            if size is None:
                u0, v0, u1, v1 = source.uv_rect
                pw = abs(u1 - u0) * source.texture.size[0]
                ph = abs(v1 - v0) * source.texture.size[1]
                size = (pw, ph)
        elif isinstance(source, Texture):
            self.texture = source
            self._uv_rect = (0.0, 0.0, 1.0, 1.0)
            if size is None:
                size = (float(source.size[0]), float(source.size[1]))
        elif isinstance(source, str):
            tex = _acquire_sprite_texture(source)
            self._texture_path = source
            self.texture = tex
            self._uv_rect = (0.0, 0.0, 1.0, 1.0)
            if size is None:
                size = (float(tex.size[0]), float(tex.size[1]))
        else:
            raise TypeError("source must be a file path, Texture, or SpriteFrame")

        self.sprite_size = (float(size[0]), float(size[1]))
        self.anchor = (float(anchor[0]), float(anchor[1]))
        self.flip_x = bool(flip_x)
        self.flip_y = bool(flip_y)
        self.tint = _color(tint) if isinstance(tint, str) else tuple(float(c) for c in tint)

        self._anim_frames: list[SpriteFrame] | None = None
        self._anim_fps = 12.0
        self._anim_loop = True
        self._anim_ping_pong = False
        self._anim_reverse = False
        self._anim_time = 0.0
        self._on_anim_complete = None
        self.frame_index = 0

    def play(self, frames, fps=12, loop=True, ping_pong=False, on_complete=None):
        self._anim_frames = list(frames)
        self._anim_fps = float(fps)
        self._anim_loop = loop
        self._anim_ping_pong = ping_pong
        self._anim_reverse = False
        self._anim_time = 0.0
        self._on_anim_complete = on_complete
        self.frame_index = 0
        if self._anim_frames:
            sf = self._anim_frames[0]
            self.texture = sf.texture
            self._uv_rect = sf.uv_rect

    def stop(self):
        self._anim_frames = None

    @property
    def playing(self):
        return self._anim_frames is not None

    def _tick_self(self, dt):
        if self._anim_frames:
            self._anim_time += dt
            n = len(self._anim_frames)
            frame_dur = 1.0 / self._anim_fps
            raw_idx = int(self._anim_time / frame_dur)

            if self._anim_ping_pong:
                cycle = 2 * (n - 1) if n > 1 else 1
                if self._anim_loop:
                    pos = raw_idx % cycle
                else:
                    pos = min(raw_idx, cycle)
                    if raw_idx >= cycle:
                        cb = self._on_anim_complete
                        self._anim_frames = None
                        if cb:
                            cb(self)
                idx = pos if pos < n else cycle - pos
            elif self._anim_loop:
                idx = raw_idx % n
            else:
                if raw_idx >= n:
                    idx = n - 1
                    cb = self._on_anim_complete
                    self._anim_frames = None
                    if cb:
                        cb(self)
                else:
                    idx = raw_idx

            if idx != self.frame_index:
                self.frame_index = idx
                frames = self._anim_frames
                if frames and 0 <= idx < len(frames):
                    sf = frames[idx]
                    self.texture = sf.texture
                    self._uv_rect = sf.uv_rect
        super()._tick_self(dt)

    def _snap(self):
        return (self.x, self.y, self.rotation, self.scale, self.opacity, self.z,
                self.texture._handle if self.texture else None,
                self.sprite_size, self.anchor, self.flip_x, self.flip_y,
                self.tint, self._uv_rect)

    def _bounds(self):
        w, h = self.sprite_size
        ax, ay = self.anchor
        x0 = -ax * w
        y0 = -ay * h
        return (x0, y0, x0 + w, y0 + h)

    def contains_point(self, wx, wy):
        if not self._inside_clip(wx, wy):
            return False
        lx, ly = self.convert_from_world(wx, wy)
        b = self._bounds()
        return b[0] <= lx <= b[2] and b[1] <= ly <= b[3]

    def _collider(self):
        wt = self._world_transform
        ax, ay = self.anchor
        off_x = (0.5 - ax) * self.sprite_size[0]
        off_y = (0.5 - ay) * self.sprite_size[1]
        cx, cy = _apply(wt, (off_x, off_y))
        sx = math.hypot(wt[0], wt[1])
        sy = math.hypot(wt[2], wt[3])
        return ('obb', cx, cy,
                self.sprite_size[0] * sx / 2, self.sprite_size[1] * sy / 2,
                _rot(wt))

    def _emit(self, cmds, renderer, world, opacity, order):
        if self.texture is None:
            return
        ax, ay = self.anchor
        off_x = (0.5 - ax) * self.sprite_size[0]
        off_y = (0.5 - ay) * self.sprite_size[1]
        center = _apply(world, (off_x, off_y))
        sx = math.hypot(world[0], world[1])
        sy = math.hypot(world[2], world[3])
        hw = self.sprite_size[0] * sx / 2
        hh = self.sprite_size[1] * sy / 2
        rot = _rot(world)

        u0, v0, u1, v1 = self._uv_rect
        if self.flip_x:
            u0, u1 = u1, u0
        if self.flip_y:
            v0, v1 = v1, v0

        order[0] += 1
        cmds.append(Cmd(self.z, order[0], KIND_TEX,
            center[0], center[1], hw, hh, rot,
            (KIND_TEX, 0, 0, 0), (0, 0, opacity, 0),
            (0, 0, 0, 0), self.tint, self.texture,
            _vb=_quad_verts_uv(center[0], center[1], hw, hh, rot, u0, v0, u1, v1)))

    def close(self):
        if self._texture_path is not None:
            _release_sprite_texture(self._texture_path)
            self._texture_path = None
        self.texture = None
        self._anim_frames = None
        self._on_anim_complete = None
        super().close()

# ───────────────────────────────────────────────────────────────────────────
# NineSlice
# ───────────────────────────────────────────────────────────────────────────

class NineSlice(Node):
    """Nine-slice image node for stretchable UI panels.

    insets: (left, top, right, bottom) in pixels — the non-stretching border.
    size: (width, height) display size in points.
    """

    def __init__(self, source, *, insets, size, tint=(1, 1, 1, 1), **kw):
        super().__init__(**kw)
        self._texture_path: str | None = None
        if isinstance(source, str):
            tex = _acquire_sprite_texture(source)
            self._texture_path = source
            self.texture = tex
        elif isinstance(source, Texture):
            self.texture = source
        else:
            raise TypeError("source must be a file path or Texture")
        self.insets = (float(insets[0]), float(insets[1]), float(insets[2]), float(insets[3]))
        self.nine_size = (float(size[0]), float(size[1]))
        self.tint = _color(tint) if isinstance(tint, str) else tuple(float(c) for c in tint)

    def _snap(self):
        return (self.x, self.y, self.rotation, self.scale, self.opacity, self.z,
                self.texture._handle if self.texture else None,
                self.nine_size, self.insets, self.tint)

    def _bounds(self):
        w, h = self.nine_size
        return (-w/2, -h/2, w/2, h/2)

    def _collider(self):
        wt = self._world_transform
        cx, cy = _apply(wt, (0, 0))
        sx = math.hypot(wt[0], wt[1])
        sy = math.hypot(wt[2], wt[3])
        w, h = self.nine_size
        return ('obb', cx, cy, w * sx / 2, h * sy / 2, _rot(wt))

    def _emit(self, cmds, renderer, world, opacity, order):
        if self.texture is None:
            return
        tw, th = self.texture.size
        il, it, ir, ib = self.insets
        w, h = self.nine_size

        sx = math.hypot(world[0], world[1])
        sy = math.hypot(world[2], world[3])
        rot = _rot(world)
        center = _apply(world, (0, 0))

        # UV coordinates for the 3 columns / 3 rows
        ul = il / tw;           ur = 1.0 - ir / tw
        vt = it / th;           vb = 1.0 - ib / th
        # Display sizes (in points) for the border
        dl = il;  dr = ir;  dt_ = it; db = ib
        # Center stretch size
        cw = w - dl - dr
        ch = h - dt_ - db
        if cw < 0: cw = 0
        if ch < 0: ch = 0

        # 9 patches: (local_x, local_y, patch_w, patch_h, u0, v0, u1, v1)
        x0 = -w / 2;  x1 = x0 + dl;  x2 = x1 + cw
        y0 = -h / 2;  y1 = y0 + dt_; y2 = y1 + ch
        patches = [
            # row 0 (top)
            (x0, y0, dl,  dt_, 0,  0,  ul, vt),  # top-left
            (x1, y0, cw,  dt_, ul, 0,  ur, vt),  # top-center
            (x2, y0, dr,  dt_, ur, 0,  1,  vt),  # top-right
            # row 1 (middle)
            (x0, y1, dl,  ch,  0,  vt, ul, vb),  # mid-left
            (x1, y1, cw,  ch,  ul, vt, ur, vb),  # mid-center
            (x2, y1, dr,  ch,  ur, vt, 1,  vb),  # mid-right
            # row 2 (bottom)
            (x0, y2, dl,  db,  0,  vb, ul, 1),   # bot-left
            (x1, y2, cw,  db,  ul, vb, ur, 1),   # bot-center
            (x2, y2, dr,  db,  ur, vb, 1,  1),   # bot-right
        ]

        for lx, ly, pw, ph, u0, v0, u1, v1 in patches:
            if pw <= 0 or ph <= 0:
                continue
            pcx = lx + pw / 2
            pcy = ly + ph / 2
            wc = _apply(world, (pcx, pcy))
            hw = pw * sx / 2
            hh = ph * sy / 2
            order[0] += 1
            cmds.append(Cmd(self.z, order[0], KIND_TEX,
                wc[0], wc[1], hw, hh, rot,
                (KIND_TEX, 0, 0, 0), (0, 0, opacity, 0),
                (0, 0, 0, 0), self.tint, self.texture,
                _vb=_quad_verts_uv(wc[0], wc[1], hw, hh, rot, u0, v0, u1, v1)))

    def close(self):
        if self._texture_path is not None:
            _release_sprite_texture(self._texture_path)
            self._texture_path = None
        self.texture = None
        super().close()
