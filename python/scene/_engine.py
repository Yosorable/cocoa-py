"""Internal rendering engine. Not part of the public API."""
from __future__ import annotations

import math
import struct
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass

from .gpu import normalize_color, Library, Pipeline, Buffer, Texture, Frame, Window


# iOS hosts compile the bundled SceneShaders.metal into their default library.
# Desktop builds load the same shader source from the installed scene package.

# ───────────────────────────────────────────────────────────────────────────
# QuadCommand + Renderer
# ───────────────────────────────────────────────────────────────────────────

KIND_TEX = 0
KIND_CIRCLE = 1
KIND_RING = 2
KIND_RRECT = 3
KIND_STROKE_RRECT = 4
KIND_LINE = 5
KIND_PARTICLE = 7  # matches C accel KIND_PARTICLE

@dataclass(slots=True)
class Cmd:
    z: float; order: int; kind: int
    cx: float; cy: float; hw: float; hh: float; rot: float
    params: tuple; style: tuple; fill: tuple; extra: tuple
    texture: Texture | None
    _vb: bytes | None = None
    _qb: bytes | None = None

_VTX = struct.Struct("<4f")
_QD  = struct.Struct("<16f")
_UNI = struct.Struct("<4f")
_VEC_RES = struct.Struct("<4f")
_VEC_COL = struct.Struct("<4f")
_VPQ = 6
_VB  = 96   # 16 * 6
_QB  = 64   # 16 * 4
_TEXT_LIMIT = 256
_FRAME_SLOTS = 3
_ATLAS_INITIAL_CHARS = ''.join(chr(c) for c in range(32, 127))


@dataclass(slots=True)
class _FrameSlot:
    vb: Buffer
    qb: Buffer
    ub: Buffer
    vc: int
    qc: int
    pool: list[tuple[int, Buffer]]
    used: list[tuple[int, Buffer]]

def _quad_verts(cmd):
    cx, cy, hw, hh = cmd.cx, cmd.cy, cmd.hw, cmd.hh
    c, s = math.cos(cmd.rot), math.sin(cmd.rot)
    def p(lx, ly, u, v):
        return _VTX.pack(cx + c*lx - s*ly, cy + s*lx + c*ly, u, v)
    tl, tr = p(-hw,-hh,0,0), p(hw,-hh,1,0)
    bl, br = p(-hw,hh,0,1), p(hw,hh,1,1)
    return tl + tr + bl + tr + br + bl

def _quad_verts_uv(cx, cy, hw, hh, rot, u0, v0, u1, v1):
    c, s = math.cos(rot), math.sin(rot)
    def p(lx, ly, u, v):
        return _VTX.pack(cx + c*lx - s*ly, cy + s*lx + c*ly, u, v)
    tl, tr = p(-hw,-hh,u0,v0), p(hw,-hh,u1,v0)
    bl, br = p(-hw,hh,u0,v1), p(hw,hh,u1,v1)
    return tl + tr + bl + tr + br + bl

def _quad_data(cmd):
    return _QD.pack(*cmd.params, *cmd.style, *cmd.fill, *cmd.extra)

class SDFGlyphAtlas:
    """SDF-based glyph atlas. Bitmap atlas is rasterized at base_size with generous
    padding, then the SDF distance field is generated on GPU via JFA compute shader.
    Resolution-independent: any display scale is crisp."""

    __slots__ = ('font_name', 'base_size', 'texture', 'glyphs',
                 '_atlas_size', '_sdf_spread')

    SDF_SPREAD = 8

    def __init__(self, font_name, base_size):
        self.font_name = font_name
        self.base_size = base_size
        self.texture = None  # SDF atlas (generated CPU-side in _build)
        self.glyphs = {}
        self._atlas_size = None
        self._sdf_spread = self.SDF_SPREAD
        # No pre-build — atlas is created lazily on first compose() with only needed chars

    def _build(self, chars):
        import _metal
        existing = {chr(c) for c in self.glyphs}
        all_chars = ''.join(sorted(set(chars) | existing))
        if self.texture:
            self.texture.close()
        pad = float(self.SDF_SPREAD + 2)
        result = _metal.create_glyph_atlas(
            font_name=self.font_name, font_size=self.base_size, chars=all_chars,
            padding=pad, sdf=True, sdf_spread=float(self.SDF_SPREAD))
        self.texture = Texture(result['handle'], result['size'])
        self._atlas_size = result['size']
        self.glyphs = {int(k): v for k, v in result['glyphs'].items()}

    def ensure_chars(self, text):
        if self.texture is None or any(ord(c) not in self.glyphs for c in text):
            self._build(text)

    def compose(self, text, renderer, pixel_size=None):
        """Compose a string texture from SDF glyph atlas.

        First call uses bitmap atlas directly (instant, no compute stall).
        """
        self.ensure_chars(text)
        aw, ah = self._atlas_size
        if aw == 0 or ah == 0:
            return None
        sc = (pixel_size / self.base_size) if pixel_size else 1.0
        sp = self._sdf_spread
        padding = max(4, int(self.base_size * 0.18 * sc))
        cursor_x = float(padding)
        max_h = 0.0
        layouts = []
        for ch in text:
            g = self.glyphs.get(ord(ch))
            if g is None:
                continue
            gx, gy, gw, gh, advance, bx, by = g
            u_gx, u_gy = gx - sp, gy - sp
            u_gw, u_gh = gw + sp * 2, gh + sp * 2
            dx = cursor_x + bx * sc - sp * sc
            dy = padding + by * sc - sp * sc
            dw = u_gw * sc
            dh = u_gh * sc
            layouts.append((dx, dy, dw, dh, u_gx, u_gy, u_gw, u_gh))
            cursor_x += advance * sc
            if dh > max_h:
                max_h = dh
        if not layouts:
            return None
        total_w = int(cursor_x + padding)
        total_h = int(max_h + padding * 2)
        if total_w < 1 or total_h < 1:
            return None
        tex = Texture.render_target(total_w, total_h)
        n = len(layouts)
        verts = bytearray()
        quads = bytearray()
        # 1.5 pixel transition zone (softer than 1px, no visible blur)
        smooth_w = 0.75 / max(sp * sc, 1.0)
        for dx, dy, dw, dh, u_gx, u_gy, u_gw, u_gh in layouts:
            x0, y0 = dx, dy
            x1, y1 = dx + dw, dy + dh
            u0, v0 = u_gx / aw, u_gy / ah
            u1, v1 = (u_gx + u_gw) / aw, (u_gy + u_gh) / ah
            verts += _VTX.pack(x0, y0, u0, v0)
            verts += _VTX.pack(x1, y0, u1, v0)
            verts += _VTX.pack(x0, y1, u0, v1)
            verts += _VTX.pack(x1, y0, u1, v0)
            verts += _VTX.pack(x1, y1, u1, v1)
            verts += _VTX.pack(x0, y1, u0, v1)
            quads += _QD.pack(
                0.0, smooth_w, 0.0, 0.0,
                0.0, 0.0, 1.0, 0.0,
                0.0, 0.0, 0.0, 0.0,
                1.0, 1.0, 1.0, 1.0,
            )
        verts = bytes(verts)
        quads = bytes(quads)
        vb = renderer._acquire(len(verts))
        qb = renderer._acquire(len(quads))
        ub = renderer._acquire(_UNI.size)
        vb.write(verts)
        qb.write(quads)
        ub.write(_UNI.pack(total_w, total_h, 1.0, 0))
        with renderer._frame((0, 0, 0, 0), tex) as f:
            f.set_pipeline(renderer._sdf_tp)
            f.set_vertex_buffer(vb, 0)
            f.set_vertex_buffer(ub, 1)
            f.set_fragment_buffer(qb, 0)
            f.set_fragment_texture(self.texture, 0)
            f.draw("triangle", 0, n * 6)
        return tex

    def close(self):
        if self.texture:
            self.texture.close()
            self.texture = None
        self.glyphs.clear()


class GlyphAtlas:
    """Caches per-glyph rasterizations in a single texture atlas for a (font, size) pair.
    Fallback for when SDF is not available."""

    __slots__ = ('font_name', 'pixel_size', 'texture', 'glyphs', '_atlas_size')

    def __init__(self, font_name, pixel_size):
        self.font_name = font_name
        self.pixel_size = pixel_size
        self.texture = None
        self.glyphs = {}       # {char_code: (x, y, w, h, advance, bx, by)}
        self._atlas_size = None
        self._build(_ATLAS_INITIAL_CHARS)

    def _build(self, chars):
        import _metal
        existing = {chr(c) for c in self.glyphs}
        all_chars = ''.join(sorted(set(chars) | existing))
        if self.texture:
            self.texture.close()
        result = _metal.create_glyph_atlas(
            font_name=self.font_name, font_size=self.pixel_size, chars=all_chars)
        self.texture = Texture(result['handle'], result['size'])
        self._atlas_size = result['size']
        self.glyphs = {int(k): v for k, v in result['glyphs'].items()}

    def ensure_chars(self, text):
        missing = any(ord(c) not in self.glyphs for c in text)
        if missing:
            self._build(text)

    def compose(self, text, renderer):
        """Compose a string texture from glyph atlas via Metal rendering."""
        self.ensure_chars(text)
        aw, ah = self._atlas_size
        if aw == 0 or ah == 0:
            return None
        padding = max(4, int(self.pixel_size * 0.18))
        cursor_x = float(padding)
        max_h = 0.0
        layouts = []
        for ch in text:
            g = self.glyphs.get(ord(ch))
            if g is None:
                continue
            gx, gy, gw, gh, advance, bx, by = g
            layouts.append((cursor_x + bx, padding + by, gw, gh, gx, gy))
            cursor_x += advance
            if gh > max_h:
                max_h = gh
        if not layouts:
            return None
        total_w = int(cursor_x + padding)
        total_h = int(max_h + padding * 2)
        if total_w < 1 or total_h < 1:
            return None
        tex = Texture.render_target(total_w, total_h)
        # Build vertex + quad data for all glyphs in one render pass
        n = len(layouts)
        verts = bytearray()
        quads = bytearray()
        for dx, dy, gw, gh, gx, gy in layouts:
            # Quad position in render target coords
            x0, y0 = dx, dy
            x1, y1 = dx + gw, dy + gh
            # UV in atlas
            u0, v0 = gx / aw, gy / ah
            u1, v1 = (gx + gw) / aw, (gy + gh) / ah
            verts += _VTX.pack(x0, y0, u0, v0)
            verts += _VTX.pack(x1, y0, u1, v0)
            verts += _VTX.pack(x0, y1, u0, v1)
            verts += _VTX.pack(x1, y0, u1, v0)
            verts += _VTX.pack(x1, y1, u1, v1)
            verts += _VTX.pack(x0, y1, u0, v1)
            # QD: params, style, fill, extra (tex_frag uses extra.rgb*extra.a*style.z)
            quads += _QD.pack(
                0.0, 0.0, 0.0, 0.0,     # params (unused for tex)
                0.0, 0.0, 1.0, 0.0,     # style: style.z = 1.0 (full opacity)
                0.0, 0.0, 0.0, 0.0,     # fill (unused)
                1.0, 1.0, 1.0, 1.0,     # extra: white tint, full alpha
            )
        verts = bytes(verts)
        quads = bytes(quads)
        vb = renderer._acquire(len(verts))
        qb = renderer._acquire(len(quads))
        ub = renderer._acquire(_UNI.size)
        vb.write(verts)
        qb.write(quads)
        ub.write(_UNI.pack(total_w, total_h, 1.0, 0))
        with renderer._frame((0, 0, 0, 0), tex) as f:
            f.set_pipeline(renderer._tp)
            f.set_vertex_buffer(vb, 0)
            f.set_vertex_buffer(ub, 1)
            f.set_fragment_buffer(qb, 0)
            f.set_fragment_texture(self.texture, 0)
            f.draw("triangle", 0, n * 6)
        return tex

    def close(self):
        if self.texture:
            self.texture.close()
            self.texture = None
        self.glyphs.clear()


class Renderer:
    def __init__(self, window):
        self.window = window
        # All shaders pre-compiled in SceneShaders.metal → default.metallib
        self._lib = Library("__default__")
        self._vlib = self._lib
        self._sp = Pipeline(self._lib, vertex="quad_vertex", fragment="shape_frag", premultiplied=True)
        self._tp = Pipeline(self._lib, vertex="quad_vertex", fragment="tex_frag", premultiplied=True)
        self._vp = Pipeline(self._vlib, vertex="vector_vertex", fragment="vector_frag",
                            blending=True, premultiplied=True)
        self._vpn = Pipeline(self._vlib, vertex="vector_vertex", fragment="vector_frag",
                             blending=False, premultiplied=True)
        # SDF text pipeline (SDF generated CPU-side in create_glyph_atlas, no compute needed)
        self._sdf_tp = Pipeline(self._lib, vertex="sdf_text_vertex", fragment="sdf_text_frag", premultiplied=True)
        # MSAA variants for main scene rendering
        _MSAA = 4
        self._msaa = _MSAA
        self._sp_ms = Pipeline(self._lib, vertex="quad_vertex", fragment="shape_frag", premultiplied=True, sample_count=_MSAA, stencil_format="stencil8")
        self._tp_ms = Pipeline(self._lib, vertex="quad_vertex", fragment="tex_frag", premultiplied=True, sample_count=_MSAA, stencil_format="stencil8")
        self._vp_ms = Pipeline(self._vlib, vertex="vector_vertex", fragment="vector_frag",
                               blending=True, premultiplied=True, sample_count=_MSAA,
                               stencil_format="stencil8")
        self._vpn_ms = Pipeline(self._vlib, vertex="vector_vertex", fragment="vector_frag",
                                blending=False, premultiplied=True, sample_count=_MSAA,
                                stencil_format="stencil8")
        # Stencil-write pipeline: no color output, writes to stencil only
        self._vs_ms = Pipeline(self._vlib, vertex="vector_vertex", fragment="vector_frag",
                               blending=False, premultiplied=False, sample_count=_MSAA,
                               stencil_format="stencil8", color_write=False)
        # Particle pipelines (pre-multiplied alpha): circle + textured variants
        self._pp_ms = Pipeline(self._lib, vertex="particle_vs", fragment="particle_fs",
                               premultiplied=True, sample_count=_MSAA, stencil_format="stencil8")
        self._pp = Pipeline(self._lib, vertex="particle_vs", fragment="particle_fs",
                            premultiplied=True)
        self._pp_tex_ms = Pipeline(self._lib, vertex="particle_vs", fragment="particle_tex_fs",
                                   premultiplied=True, sample_count=_MSAA, stencil_format="stencil8")
        self._pp_tex = Pipeline(self._lib, vertex="particle_vs", fragment="particle_tex_fs",
                                premultiplied=True)
        self._vub = Buffer(256)
        self._vcb = Buffer(256)
        cap = 512
        self._frame_slots = [self._make_frame_slot(cap) for _ in range(_FRAME_SLOTS)]
        self._frame_slot_index = -1
        self._active_frame_slot: _FrameSlot | None = None
        self._tc: OrderedDict[tuple, Texture] = OrderedDict()
        self._ga: dict[tuple, GlyphAtlas | SDFGlyphAtlas] = {}
        self._sdf_base_size = 64  # base pixel size for SDF atlas
        self.screen_scale = window.scale
        # Fallback pool for offscreen renders that happen outside a screen frame.
        self._buf_pool: list[tuple[int, Buffer]] = []   # (length, Buffer)
        self._buf_used: list[tuple[int, Buffer]] = []   # checked out this frame

    def _make_frame_slot(self, cap):
        return _FrameSlot(
            vb=Buffer(cap * _VB),
            qb=Buffer(cap * _QB),
            ub=Buffer(256),
            vc=cap,
            qc=cap,
            pool=[],
            used=[],
        )

    def _begin_onscreen_slot(self):
        if self._active_frame_slot is not None:
            return self._active_frame_slot
        self.window.acquire_frame_slot()
        self._frame_slot_index = (self._frame_slot_index + 1) % len(self._frame_slots)
        slot = self._frame_slots[self._frame_slot_index]
        self._reclaim(slot)
        self._active_frame_slot = slot
        return slot

    def _end_onscreen_slot(self):
        self._active_frame_slot = None

    def _abort_onscreen_slot(self):
        if self._active_frame_slot is not None:
            try:
                self.window.release_frame_slot()
            except Exception:
                pass
            finally:
                self._active_frame_slot = None

    @contextmanager
    def _frame(self, clear_color, target_texture=None, **kwargs):
        onscreen = target_texture is None
        entered = False
        if onscreen:
            self._begin_onscreen_slot()
        try:
            with self.window.frame(clear_color, target_texture, **kwargs) as f:
                entered = True
                yield f
        except Exception:
            if onscreen and not entered:
                self._abort_onscreen_slot()
            raise
        finally:
            if onscreen:
                self._end_onscreen_slot()

    def _buffers_for(self, slot):
        if slot is None:
            return self._buf_pool, self._buf_used
        return slot.pool, slot.used

    def _acquire(self, size, slot=None):
        if slot is None:
            slot = self._active_frame_slot
        pool, used = self._buffers_for(slot)
        while pool:
            ln, buf = pool.pop()
            if ln >= size:
                used.append((ln, buf))
                return buf
            buf.close()
        buf = Buffer(size)
        used.append((size, buf))
        return buf

    def _reclaim(self, slot=None):
        pool, used = self._buffers_for(slot)
        for _, b in pool:
            b.close()
        used.reverse()
        if slot is None:
            self._buf_pool = used
            self._buf_used = []
        else:
            slot.pool = used
            slot.used = []

    def text_texture(self, text, font_name, pixel_size):
        """Render text using bitmap glyph atlas (CoreText native rasterization)."""
        key = (text, font_name, pixel_size)
        t = self._tc.get(key)
        if t is not None:
            self._tc.move_to_end(key); return t
        atlas_key = (font_name, pixel_size)
        atlas = self._ga.get(atlas_key)
        if atlas is None:
            atlas = GlyphAtlas(font_name, pixel_size)
            self._ga[atlas_key] = atlas
        t = atlas.compose(text, self)
        self._tc[key] = t
        self._trim_text_cache()
        return t

    def text_texture_sdf(self, text, font_name, pixel_size):
        """Render text using SDF atlas (resolution-independent, for dynamic scaling).

        Use this for text that will be zoomed/scaled at runtime.
        One atlas per font, any display size is crisp without re-rasterization.
        """
        key = ('sdf', text, font_name)
        t = self._tc.get(key)
        if t is not None:
            self._tc.move_to_end(key); return t
        sdf_key = ('sdf', font_name)
        atlas = self._ga.get(sdf_key)
        if atlas is None:
            atlas = SDFGlyphAtlas(font_name, self._sdf_base_size)
            self._ga[sdf_key] = atlas
        t = atlas.compose(text, self, pixel_size)
        self._tc[key] = t
        self._trim_text_cache()
        return t

    def _trim_text_cache(self):
        while len(self._tc) > _TEXT_LIMIT:
            # Drop only the lookup entry. If no node/cache still references the
            # Texture object, Texture.__del__ will close the Metal handle. If a
            # C node cache still references it, the texture must remain bindable.
            self._tc.popitem(last=False)


    def _encode(self, cmds, n, vb, qb, ub, clear_color, target_texture, viewport):
        # Pack quads, skipping KIND_PARTICLE markers
        quad_cmds = [c for c in cmds if c.kind != KIND_PARTICLE]
        qn = len(quad_cmds)
        # Remap indices: original cmd index → packed quad index
        quad_idx = {}
        for qi, c in enumerate(quad_cmds):
            quad_idx[id(c)] = qi
        if qn > 0:
            vd = bytearray(qn * _VB)
            qd = bytearray(qn * _QB)
            for i, c in enumerate(quad_cmds):
                v = c._vb
                if v is None:
                    v = _quad_verts(c); c._vb = v
                vd[i*_VB:(i+1)*_VB] = v
                q = c._qb
                if q is None:
                    q = _quad_data(c); c._qb = q
                qd[i*_QB:(i+1)*_QB] = q
            vb.write(vd)
            qb.write(qd)
        res = viewport or self.window.size
        ub.write(_UNI.pack(res[0], res[1], self.screen_scale, 0))

        # Build unified batch list (quad batches + particle entries)
        batches = []  # (start, end, is_tex, tex_handle) or (-2, emitter)
        bs, pk = -1, None
        for c in cmds:
            if c.kind == KIND_PARTICLE:
                # Flush pending quad batch
                if bs >= 0 and pk is not None:
                    batches.append((bs, quad_idx[id(c)] if id(c) in quad_idx else (qi + 1), pk))
                    bs, pk = -1, None
                batches.append((-2, c._emitter))
            else:
                qi_c = quad_idx[id(c)]
                k = (c.kind == KIND_TEX, c.texture.handle if c.texture else 0)
                if bs < 0:
                    bs = qi_c; pk = k
                elif k != pk:
                    batches.append((bs, qi_c, pk))
                    bs, pk = qi_c, k
        if bs >= 0 and pk is not None:
            batches.append((bs, qn, pk))

        cc = clear_color or self.window.background
        with self._frame(cc, target_texture) as f:
            f.set_vertex_buffer(vb, 0)
            f.set_vertex_buffer(ub, 1)
            f.set_fragment_buffer(qb, 0)
            cp = None
            for batch in batches:
                if batch[0] == -2:
                    emitter = batch[1]
                    emitter._render_particles(f, self, emitter._world_opacity)
                    cp = None
                else:
                    s, e, (is_tex, _) = batch
                    p = self._tp if is_tex else self._sp
                    if p is not cp: f.set_pipeline(p); cp = p
                    if is_tex: f.set_fragment_texture(quad_cmds[s].texture, 0)
                    f.draw("triangle", s * _VPQ, (e - s) * _VPQ)

    def _draw_mesh(self, frame, index_buffer, index_count, index_type, *,
                   color, blend, is_stroke, fill_rule, offset=0, slot=None):
        cb = self._acquire(_VEC_COL.size, slot=slot)
        cb.write(_VEC_COL.pack(*color))
        masked = fill_rule is not None or (is_stroke and color[3] < 1.0)
        if masked:
            back_pass = None
            if fill_rule == "even_odd":
                stencil_pass = "invert"
            elif fill_rule == "non_zero":
                # Signed winding must wrap through zero for either orientation.
                stencil_pass = "incr_wrap"
                back_pass = "decr_wrap"
            else:
                stencil_pass = "replace"
            reference = 0 if fill_rule is not None else 1
            frame.set_depth_stencil(
                compare="always", write=False, stencil_compare="always",
                stencil_pass=stencil_pass, stencil_back_pass=back_pass,
                stencil_ref=reference,
            )
            frame.set_pipeline(self._vs_ms)
            frame.set_fragment_buffer(cb, 0)
            frame.draw_indexed("triangle", index_buffer, index_count,
                               index_type=index_type, offset=offset)
            # Consume covered samples as they are painted: overlapping triangles
            # blend once, and holes leave the existing color attachment alone.
            frame.set_depth_stencil(
                compare="always", write=False,
                stencil_compare="not_equal" if fill_rule is not None else "equal",
                stencil_pass="zero", stencil_ref=reference,
            )
            frame.set_pipeline(self._vp_ms)
        else:
            frame.set_pipeline(self._vp_ms if blend or is_stroke else self._vpn_ms)
        frame.set_fragment_buffer(cb, 0)
        frame.draw_indexed("triangle", index_buffer, index_count,
                           index_type=index_type, offset=offset)
        if masked:
            frame.set_depth_stencil(compare="always", write=False)

    def render_packed(self, vertex_bytes, quad_bytes, count, batches, *, clear_color=None,
                      mesh_vb=None, mesh_ib=None, mesh_batches=None, target_texture=None):
        """Render pre-packed GPU data from C accelerator."""
        has_quads = count > 0
        has_meshes = mesh_vb is not None and mesh_batches and len(mesh_batches) > 0
        slot = self._begin_onscreen_slot() if target_texture is None else None
        if not has_quads and not has_meshes:
            with self._frame(clear_color or self.window.background, target_texture):
                pass
            return
        self.screen_scale = self.window.scale
        res = self.window.size
        if target_texture is not None:
            # Off-screen passes can be encoded back-to-back before the command
            # buffer executes, so each pass needs distinct buffers.
            vb = self._acquire(len(vertex_bytes)) if has_quads else None
            qb = self._acquire(len(quad_bytes)) if has_quads else None
            ub = self._acquire(_UNI.size)
        else:
            if has_quads:
                if count > slot.vc:
                    slot.vb.close(); slot.vc = count * 2; slot.vb = Buffer(slot.vc * _VB)
                if count > slot.qc:
                    slot.qb.close(); slot.qc = count * 2; slot.qb = Buffer(slot.qc * _QB)
                vb = slot.vb
                qb = slot.qb
            else:
                vb = qb = None
            ub = slot.ub
        ub.write(_UNI.pack(res[0], res[1], self.screen_scale, 0))
        if has_quads:
            vb.write(vertex_bytes)
            qb.write(quad_bytes)
        cc = clear_color or self.window.background
        mvb = mib = None
        if has_meshes:
            mvb = self._acquire(len(mesh_vb), slot=slot)
            mib = self._acquire(len(mesh_ib), slot=slot)
            mvb.write(mesh_vb)
            mib.write(mesh_ib)
        it = {0: "uint16", 1: "uint32"}
        last_mode = None  # 'quad' or 'mesh' — track to rebind buffers on switch
        use_msaa_pass = has_meshes
        quad_shape_pipeline = self._sp_ms if use_msaa_pass else self._sp
        quad_tex_pipeline = self._tp_ms if use_msaa_pass else self._tp
        with self._frame(
            cc,
            target_texture,
            sample_count=self._msaa if use_msaa_pass else 1,
            stencil=use_msaa_pass,
        ) as f:
            for start, end_or_idx, is_tex, tex in batches:
                if start == -1:
                    # Mesh batch (end_or_idx = mesh_batch_idx)
                    mb = mesh_batches[end_or_idx]
                    idx_offset, idx_count, idx_is_32, color, blend, is_stroke, fill_rule = mb
                    if last_mode != 'mesh':
                        f.set_vertex_buffer(mvb, 0)
                        f.set_vertex_buffer(ub, 1)
                        last_mode = 'mesh'
                    self._draw_mesh(
                        f, mib, idx_count, it[idx_is_32], offset=idx_offset,
                        color=color, blend=blend, is_stroke=is_stroke,
                        fill_rule=fill_rule, slot=slot,
                    )
                elif start == -2:
                    # Particle batch: end_or_idx is the emitter object
                    end_or_idx._render_particles(f, self, end_or_idx._world_opacity, msaa=use_msaa_pass)
                    last_mode = None
                else:
                    if last_mode != 'quad':
                        f.set_vertex_buffer(vb, 0)
                        f.set_vertex_buffer(ub, 1)
                        f.set_fragment_buffer(qb, 0)
                        last_mode = 'quad'
                    p = quad_tex_pipeline if is_tex else quad_shape_pipeline
                    f.set_pipeline(p)
                    if is_tex and tex is not None: f.set_fragment_texture(tex, 0)
                    f.draw("triangle", start * _VPQ, (end_or_idx - start) * _VPQ)

    def render(self, cmds, *, clear_color=None, target_texture=None, viewport=None):
        n = len(cmds)
        if n == 0:
            with self._frame(clear_color or self.window.background, target_texture):
                pass
            return
        self.screen_scale = self.window.scale
        if target_texture is not None:
            # Off-screen: use the active frame slot when called during scene render.
            vb = self._acquire(n * _VB)
            qb = self._acquire(n * _QB)
            ub = self._acquire(256)
            self._encode(cmds, n, vb, qb, ub, clear_color, target_texture, viewport)
        else:
            slot = self._begin_onscreen_slot()
            if n > slot.vc:
                slot.vb.close(); slot.vc = n * 2; slot.vb = Buffer(slot.vc * _VB)
            if n > slot.qc:
                slot.qb.close(); slot.qc = n * 2; slot.qb = Buffer(slot.qc * _QB)
            self._encode(cmds, n, slot.vb, slot.qb, slot.ub, clear_color, None, None)

    def close(self):
        for t in self._tc.values():
            try: t.close()
            except Exception: pass
        self._tc.clear()
        for a in self._ga.values():
            try: a.close()
            except Exception: pass
        self._ga.clear()
        for _, b in self._buf_pool:
            try: b.close()
            except Exception: pass
        for _, b in self._buf_used:
            try: b.close()
            except Exception: pass
        self._buf_pool.clear()
        self._buf_used.clear()
        for slot in self._frame_slots:
            for _, b in slot.pool:
                try: b.close()
                except Exception: pass
            for _, b in slot.used:
                try: b.close()
                except Exception: pass
            slot.pool.clear()
            slot.used.clear()
            for r in (slot.vb, slot.qb, slot.ub):
                try: r.close()
                except Exception: pass
        self._active_frame_slot = None
        for r in (self._vub, self._vcb, self._sp, self._tp, self._vp, self._vpn,
                  self._sp_ms, self._tp_ms, self._vp_ms, self._vpn_ms, self._vs_ms,
                  self._sdf_tp, self._pp, self._pp_ms,
                  self._pp_tex, self._pp_tex_ms, self._lib):
            if r is not None:
                try: r.close()
                except Exception: pass

    def __del__(self):
        try: self.close()
        except Exception: pass

    def render_color_meshes(self, meshes, *, clear_color=(0, 0, 0, 0), target_texture, viewport):
        res = viewport or self.window.size
        vub = self._acquire(_VEC_RES.size)
        vub.write(_VEC_RES.pack(res[0], res[1], 0.0, 0.0))
        with self._frame(clear_color, target_texture, sample_count=self._msaa, stencil=True) as f:
            f.set_vertex_buffer(vub, 1)
            for mesh in meshes:
                vb = self._acquire(len(mesh.vertex_bytes))
                ib = self._acquire(len(mesh.index_bytes))
                vb.write(mesh.vertex_bytes)
                ib.write(mesh.index_bytes)
                r, g, b, a = mesh.color
                f.set_vertex_buffer(vb, 0)
                self._draw_mesh(
                    f, ib, mesh.index_count, mesh.index_type,
                    color=(r * a, g * a, b * a, a), blend=mesh.blend,
                    is_stroke=mesh.is_stroke, fill_rule=mesh.fill_rule,
                )
