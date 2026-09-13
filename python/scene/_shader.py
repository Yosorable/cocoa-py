"""scene._shader — ShaderNode for custom Metal shaders."""
from __future__ import annotations

import math
import struct

from . import gpu
from ._common import _apply, _avg_scale, _color, _rot
from ._engine import Cmd, KIND_TEX, Texture
from ._node import Node

# ───────────────────────────────────────────────────────────────────────────
# ShaderNode
# ───────────────────────────────────────────────────────────────────────────

_GLOBALS_PACK = struct.Struct("<8f")

class ShaderNode(Node):
    """Custom Metal shader rendered as a scene graph node.

    The user provides complete, portable Metal source code.  The shader
    renders to a private off-screen texture each frame (or only when dirty),
    and the texture is composited into the scene at this node's position,
    rotation, scale, z-order, and opacity.

    Pass exactly one of *source* (shader code string) or *file* (path to
    a ``.metal`` file).  Both *vertex* and *fragment* name the Metal
    functions to use.

    *format* selects the off-screen texture pixel format ("bgra8",
    "rgba8", "rgba16f", "r8", "r16f", "r32f", "rg16f", "rg32f").  Float
    formats are useful for simulation passes chained via set_texture().
    """

    def __init__(self, *, source=None, file=None, size=(256, 256),
                 vertex, fragment,
                 draw_count=6, primitive="triangle",
                 dynamic=True, clear_color=(0, 0, 0, 0),
                 blending=True, premultiplied=True, format="bgra8", **kw):
        super().__init__(**kw)
        if (source is None) == (file is None):
            raise TypeError("exactly one of 'source' or 'file' is required")
        self._source = source
        self._file = file
        self._vertex_name = vertex
        self._fragment_name = fragment
        self._draw_count = int(draw_count)
        self._primitive = primitive
        self._blending = blending
        self._premultiplied = premultiplied
        self._format = str(format)
        self._shader_size = (float(size[0]), float(size[1]))
        self._clear_color = (
            _color(clear_color) if isinstance(clear_color, str)
            else tuple(float(c) for c in clear_color))
        self.dynamic = bool(dynamic)
        self._dirty = True
        self._time = 0.0
        self._frame_count = 0
        self._dt = 0.0
        self._uniforms: list = []
        self._user_textures: dict = {}   # index → Texture | ShaderNode
        self._user_vbufs: dict = {}      # index → Buffer
        self._user_fbufs: dict = {}      # index → Buffer
        # GPU resources (lazy init)
        self._lib = None
        self._pipe = None
        self._globals_buf = None
        self._uniforms_buf = None
        self._tex = None
        self._renderer = None

    # ── properties ──

    @property
    def shader_size(self):
        return self._shader_size

    @shader_size.setter
    def shader_size(self, v):
        new = (float(v[0]), float(v[1]))
        if new != self._shader_size:
            self._shader_size = new
            if self._tex is not None:
                self._tex.close()
                self._tex = None
            self._dirty = True
            # Bust C cache (old CCmd references the closed texture)
            try:
                self._c_cache = None
            except Exception:
                pass

    @property
    def texture(self):
        """Output texture (read-only). Use for shader chaining."""
        return self._tex

    @property
    def time(self):
        return self._time

    @time.setter
    def time(self, v):
        self._time = float(v)
        self._dirty = True

    @property
    def uniforms(self):
        return self._uniforms

    @uniforms.setter
    def uniforms(self, v):
        self._uniforms = list(v)
        self._dirty = True

    # ── public methods ──

    def set_texture(self, tex, index):
        """Bind a Texture or ShaderNode to fragment texture(index)."""
        self._user_textures[int(index)] = tex
        self._dirty = True

    def set_vertex_data(self, buf, index):
        """Bind a gpu.Buffer to vertex buffer(index)."""
        self._user_vbufs[int(index)] = buf
        self._dirty = True

    def set_fragment_data(self, buf, index):
        """Bind a gpu.Buffer to fragment buffer(index)."""
        self._user_fbufs[int(index)] = buf
        self._dirty = True

    def invalidate(self):
        """Force re-render on next frame (for dynamic=False nodes)."""
        self._dirty = True

    # ── internal: Node overrides ──

    def _snap(self):
        return (self.x, self.y, self.rotation, self.scale, self.opacity, self.z,
                self._shader_size, self._tex._handle if self._tex else None, self._dirty)

    def _bounds(self):
        w, h = self._shader_size
        return (-w / 2, -h / 2, w / 2, h / 2)

    def _tick_self(self, dt):
        self._dt = dt
        self._time += dt
        self._frame_count += 1
        if self._renderer is not None and self._pipe is not None:
            if self.dynamic or self._dirty:
                self._render_shader()
                self._dirty = False
        super()._tick_self(dt)

    def _emit(self, cmds, renderer, world, opacity, order):
        # First frame: init GPU + render
        if self._pipe is None:
            self._init_gpu(renderer)
        if self._dirty:
            self._render_shader()
            self._dirty = False
        if self._tex is None:
            return
        sx = math.hypot(world[0], world[1])
        sy = math.hypot(world[2], world[3])
        center = _apply(world, (0, 0))
        w, h = self._shader_size
        hw = w * sx / 2
        hh = h * sy / 2
        rot = _rot(world)
        order[0] += 1
        cmds.append(Cmd(self.z, order[0], KIND_TEX,
            center[0], center[1], hw, hh, rot,
            (KIND_TEX, 0, 0, 0), (0, 0, opacity, 0),
            (0, 0, 0, 0), (1, 1, 1, 1), self._tex))

    def _collider(self):
        wt = self._world_transform
        cx, cy = _apply(wt, (0, 0))
        sx = math.hypot(wt[0], wt[1])
        sy = math.hypot(wt[2], wt[3])
        w, h = self._shader_size
        return ('obb', cx, cy, w * sx / 2, h * sy / 2, _rot(wt))

    # ── internal: GPU ──

    def _init_gpu(self, renderer):
        self._renderer = renderer
        if self._file is not None:
            self._lib = gpu.Library.from_file(self._file)
        else:
            self._lib = gpu.Library(self._source)
        self._pipe = gpu.Pipeline(self._lib, vertex=self._vertex_name,
                                  fragment=self._fragment_name,
                                  blending=self._blending,
                                  premultiplied=self._premultiplied,
                                  format=self._format)
        self._globals_buf = gpu.Buffer(256)
        self._ensure_texture(renderer)

    def _ensure_texture(self, renderer):
        """Allocate the output texture if needed (idempotent)."""
        if self._tex is None:
            w, h = self._shader_size
            ss = renderer.screen_scale
            pw = max(1, int(round(w * ss)))
            ph = max(1, int(round(h * ss)))
            self._tex = Texture.render_target(pw, ph, format=self._format)

    def _render_shader(self):
        renderer = self._renderer
        if renderer is None or self._pipe is None:
            return
        # Ensure texture (may have been reset by shader_size change)
        self._ensure_texture(renderer)
        # Pack Globals: time, dt, res.xy, res_px.xy, scale, frame
        w, h = self._shader_size
        ss = renderer.screen_scale
        self._globals_buf.write(_GLOBALS_PACK.pack(
            self._time, self._dt, w, h,
            w * ss, h * ss, ss, float(self._frame_count)))
        # Pack user uniforms → fragment buffer(1)
        if self._uniforms:
            floats = []
            for v in self._uniforms:
                if isinstance(v, (int, float)):
                    floats.append(float(v))
                else:
                    floats.extend(float(f) for f in v)
            if floats:
                data = struct.pack(f'<{len(floats)}f', *floats)
                if self._uniforms_buf is None or self._uniforms_buf.length < len(data):
                    if self._uniforms_buf is not None:
                        self._uniforms_buf.close()
                    self._uniforms_buf = gpu.Buffer(max(256, len(data)))
                self._uniforms_buf.write(data)
        # Render to off-screen texture
        with renderer._frame(self._clear_color, self._tex) as f:
            f.set_pipeline(self._pipe)
            # Globals as vertex buffer(0) — also satisfies Frame.draw() check
            f.set_vertex_buffer(self._globals_buf, 0)
            for idx, buf in self._user_vbufs.items():
                f.set_vertex_buffer(buf, idx)
            # Globals as fragment buffer(0)
            f.set_fragment_buffer(self._globals_buf, 0)
            if self._uniforms_buf is not None and self._uniforms:
                f.set_fragment_buffer(self._uniforms_buf, 1)
            for idx, buf in self._user_fbufs.items():
                f.set_fragment_buffer(buf, idx)
            for idx, src in self._user_textures.items():
                if isinstance(src, ShaderNode):
                    # Fully init lazy sources so circular chains
                    # (ping-pong feedback) have a valid binding on the
                    # very first frame.  Init must be all-or-nothing:
                    # the C collector treats "_tex is set" as "node is
                    # initialized" and skips the Python _emit fallback,
                    # so allocating just the texture would leave the
                    # source permanently un-rendered.  Contents are
                    # undefined until the source renders — guard with
                    # g.frame in shaders.
                    if src._pipe is None:
                        src._init_gpu(renderer)
                    tex = src.texture
                else:
                    tex = src
                if tex is not None:
                    f.set_fragment_texture(tex, idx)
            f.draw(self._primitive, 0, self._draw_count)

    def close(self):
        for r in (self._tex, self._uniforms_buf, self._globals_buf,
                  self._pipe, self._lib):
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass
        self._tex = None
        self._uniforms_buf = None
        self._globals_buf = None
        self._pipe = None
        self._lib = None
        self._renderer = None
        self._uniforms.clear()
        self._user_textures.clear()
        self._user_vbufs.clear()
        self._user_fbufs.clear()
        super().close()
