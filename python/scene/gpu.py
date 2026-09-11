"""scene.gpu — Low-level Metal GPU access.

For advanced users who need full Metal control: custom shaders,
compute pipelines, buffer read-back, texture loading, etc.

Quick start:
    from scene import gpu

    lib = gpu.Library(shader_source)
    pipe = gpu.Pipeline(lib, vertex="vs", fragment="fs")
    buf = gpu.Buffer(1024)
    buf.write(struct.pack("4f", 1.0, 2.0, 3.0, 4.0))

    with gpu.frame(window) as f:
        f.set_pipeline(pipe)
        f.set_vertex_buffer(buf, 0)
        f.draw("triangle_strip", 0, 4)
"""
from __future__ import annotations

import _metal
from pathlib import Path

from ._enums import Orientation


# ───────────────────────────────────────────────────────────────────────────
# Color
# ───────────────────────────────────────────────────────────────────────────

def normalize_color(value) -> tuple[float, float, float, float]:
    if isinstance(value, str):
        raw = value.strip().lstrip("#")
        if len(raw) == 3:
            raw = raw[0]*2 + raw[1]*2 + raw[2]*2 + "ff"
        elif len(raw) == 4:
            raw = raw[0]*2 + raw[1]*2 + raw[2]*2 + raw[3]*2
        elif len(raw) == 6:
            raw += "ff"
        if len(raw) != 8:
            raise ValueError(f"Bad color: {value!r}")
        return tuple(int(raw[i:i+2], 16) / 255.0 for i in range(0, 8, 2))
    _c = lambda v: max(0.0, min(1.0, float(v)))
    if len(value) == 3:
        return (_c(value[0]), _c(value[1]), _c(value[2]), 1.0)
    if len(value) == 4:
        return (_c(value[0]), _c(value[1]), _c(value[2]), _c(value[3]))
    raise ValueError("Color must be hex string, 3-tuple, or 4-tuple.")

# ───────────────────────────────────────────────────────────────────────────
# Shader Library
# ───────────────────────────────────────────────────────────────────────────

class Library:
    """Compiled Metal shader library."""
    __slots__ = ("_handle",)

    def __init__(self, source: str = None, *, path=None):
        if path is not None:
            self._handle = _metal.create_library(path=str(path))
        elif source is not None:
            if source == "__default__":
                import sys
                if sys.platform == "darwin":
                    path = Path(__file__).with_name("_resources") / "SceneShaders.metal"
                    self._handle = _metal.create_library(path=str(path))
                    return
            self._handle = _metal.create_library(source=source)
        else:
            raise ValueError("Either source or path must be provided")

    @classmethod
    def from_file(cls, path) -> 'Library':
        p = Path(path)
        if not p.is_absolute():
            p = Path.cwd() / p
        return cls(path=str(p))

    @property
    def handle(self):
        return self._handle

    def close(self):
        if self._handle is not None:
            _metal.destroy_library(self._handle); self._handle = None

    def __del__(self):
        try: self.close()
        except Exception: pass

# ───────────────────────────────────────────────────────────────────────────
# Render Pipeline
# ───────────────────────────────────────────────────────────────────────────

class Pipeline:
    """Metal render pipeline state."""
    __slots__ = ("_handle",)

    def __init__(self, library: Library, *, vertex: str, fragment: str,
                 blending=True, premultiplied=False,
                 format="bgra8", depth_format=None, sample_count=1,
                 stencil_format=None, color_write=True):
        self._handle = _metal.create_render_pipeline(
            library=library.handle, vertex=vertex, fragment=fragment,
            blending=blending, premultiplied=premultiplied,
            format=format, depth_format=depth_format, sample_count=sample_count,
            stencil_format=stencil_format, color_write=color_write)

    @property
    def handle(self):
        return self._handle

    def close(self):
        if self._handle is not None:
            _metal.destroy_render_pipeline(self._handle); self._handle = None

    def __del__(self):
        try: self.close()
        except Exception: pass

# ───────────────────────────────────────────────────────────────────────────
# Buffer
# ───────────────────────────────────────────────────────────────────────────

class Buffer:
    """Metal shared-memory buffer (CPU ↔ GPU)."""
    __slots__ = ("_handle", "length")

    def __init__(self, length: int):
        self.length = int(length)
        self._handle = _metal.create_buffer(length=self.length)

    @property
    def handle(self):
        return self._handle

    def write(self, data, *, offset=0):
        _metal.write_buffer(handle=self._handle, data=memoryview(data), offset=int(offset))

    def read(self, *, offset=0, length=None) -> bytes:
        n = self.length - offset if length is None else length
        return _metal.read_buffer(handle=self._handle, offset=int(offset), length=int(n))

    def close(self):
        if self._handle is not None:
            _metal.destroy_buffer(self._handle); self._handle = None

    def __del__(self):
        try: self.close()
        except Exception: pass

# ───────────────────────────────────────────────────────────────────────────
# Texture
# ───────────────────────────────────────────────────────────────────────────

class Texture:
    """Metal texture (2D, BGRA8Unorm)."""
    __slots__ = ("_handle", "size")

    def __init__(self, handle: int, size: tuple[int, int]):
        self._handle = int(handle)
        self.size = (int(size[0]), int(size[1]))

    @classmethod
    def from_text(cls, text: str, *, font_size: float, font_name=None) -> Texture:
        r = _metal.create_text_texture(text=text, font_size=float(font_size), font_name=font_name)
        return cls(r["handle"], r["size"])

    @classmethod
    def render_target(cls, width: int, height: int, format="bgra8") -> Texture:
        r = _metal.create_render_texture(width=int(width), height=int(height), format=format)
        return cls(r["handle"], r["size"])

    @classmethod
    def from_file(cls, path) -> Texture:
        p = Path(path)
        if not p.is_absolute():
            p = Path.cwd() / p
        r = _metal.create_image_texture(path=str(p))
        return cls(r["handle"], r["size"])

    @property
    def handle(self):
        return self._handle

    def close(self):
        if self._handle is not None:
            _metal.destroy_texture(self._handle); self._handle = None

    def __del__(self):
        try: self.close()
        except Exception: pass

# ───────────────────────────────────────────────────────────────────────────
# Compute Pipeline
# ───────────────────────────────────────────────────────────────────────────

class ComputePipeline:
    """Metal compute pipeline state."""
    __slots__ = ("_handle",)

    def __init__(self, library: Library, *, function: str):
        self._handle = _metal.create_compute_pipeline(
            library=library.handle, function=function)

    @property
    def handle(self):
        return self._handle

    def close(self):
        if self._handle is not None:
            _metal.destroy_compute_pipeline(self._handle); self._handle = None

    def __del__(self):
        try: self.close()
        except Exception: pass

# ───────────────────────────────────────────────────────────────────────────
# Frame (render pass context manager)
# ───────────────────────────────────────────────────────────────────────────

class Frame:
    """Render command encoder context manager."""
    __slots__ = ("_wh", "_cc", "_tt", "_depth", "_sc", "_st", "_pip", "_vb", "_fb", "_ft")

    def __init__(self, window_handle, clear_color=(0, 0, 0, 1), target_texture=None,
                 depth=False, sample_count=1, stencil=False):
        self._wh = window_handle
        self._cc = normalize_color(clear_color) if isinstance(clear_color, str) else clear_color
        self._tt = target_texture
        self._depth = depth
        self._sc = sample_count
        self._st = stencil
        self._pip = None; self._vb = {}; self._fb = {}; self._ft = {}

    def __enter__(self):
        kw = {"window": self._wh, "clear_color": self._cc}
        if self._tt is not None:
            kw["target_texture"] = self._tt.handle if hasattr(self._tt, 'handle') else self._tt
        if self._depth:
            kw["depth"] = True
        if self._sc > 1:
            kw["sample_count"] = self._sc
        if self._st:
            kw["stencil"] = True
        _metal.begin_frame(**kw)
        self._pip = None; self._vb.clear(); self._fb.clear(); self._ft.clear()
        return self

    def __exit__(self, *exc):
        try: _metal.end_frame(self._wh)
        except Exception: pass
        return False

    def set_pipeline(self, p):
        h = p.handle if hasattr(p, 'handle') else p
        if h != self._pip:
            _metal.set_pipeline(window=self._wh, pipeline=h); self._pip = h

    def set_vertex_buffer(self, buf, index, offset=0):
        h = buf.handle if hasattr(buf, 'handle') else buf
        k = (h, offset)
        if self._vb.get(index) != k:
            _metal.set_vertex_buffer(window=self._wh, buffer=h, index=index, offset=offset)
            self._vb[index] = k

    def set_fragment_buffer(self, buf, index, offset=0):
        h = buf.handle if hasattr(buf, 'handle') else buf
        k = (h, offset)
        if self._fb.get(index) != k:
            _metal.set_fragment_buffer(window=self._wh, buffer=h, index=index, offset=offset)
            self._fb[index] = k

    def set_fragment_texture(self, tex, index):
        h = tex.handle if hasattr(tex, 'handle') else tex
        if h is None:
            raise RuntimeError("Cannot bind a closed Metal texture.")
        if self._ft.get(index) != h:
            _metal.set_fragment_texture(window=self._wh, texture=h, index=index)
            self._ft[index] = h

    def draw(self, primitive, start, count):
        if self._pip is None:
            raise RuntimeError("draw() called without set_pipeline()")
        if not self._vb:
            raise RuntimeError("draw() called without set_vertex_buffer()")
        _metal.draw(window=self._wh, primitive=primitive, vertex_start=start, vertex_count=count)

    def draw_indexed(self, primitive, index_buffer, count, index_type="uint16", offset=0):
        if self._pip is None:
            raise RuntimeError("draw_indexed() called without set_pipeline()")
        h = index_buffer.handle if hasattr(index_buffer, 'handle') else index_buffer
        _metal.draw_indexed(window=self._wh, primitive=primitive,
                            index_buffer=h, index_count=count, index_type=index_type,
                            offset=offset)

    def draw_instanced(self, primitive, start, count, instance_count):
        """Issue an instanced draw call. Draws `count` vertices `instance_count` times."""
        if self._pip is None:
            raise RuntimeError("draw_instanced() called without set_pipeline()")
        if not self._vb:
            raise RuntimeError("draw_instanced() called without set_vertex_buffer()")
        _metal.draw_instanced(window=self._wh, primitive=primitive,
                              vertex_start=start, vertex_count=count,
                              instance_count=instance_count)

    def draw_indexed_instanced(self, primitive, index_buffer, count,
                               instance_count, index_type="uint16", offset=0):
        """Issue an indexed instanced draw call."""
        if self._pip is None:
            raise RuntimeError("draw_indexed_instanced() called without set_pipeline()")
        h = index_buffer.handle if hasattr(index_buffer, 'handle') else index_buffer
        _metal.draw_indexed_instanced(window=self._wh, primitive=primitive,
                                      index_buffer=h, index_count=count,
                                      index_type=index_type, offset=offset,
                                      instance_count=instance_count)

    def draw_indirect(self, primitive, indirect_buffer, offset=0):
        """Issue an indirect draw call. Arguments are read from the buffer at runtime."""
        if self._pip is None:
            raise RuntimeError("draw_indirect() called without set_pipeline()")
        h = indirect_buffer.handle if hasattr(indirect_buffer, 'handle') else indirect_buffer
        _metal.draw_indirect(window=self._wh, primitive=primitive,
                             indirect_buffer=h, offset=offset)

    def draw_indexed_indirect(self, primitive, index_buffer, index_type,
                              indirect_buffer, indirect_offset=0):
        """Issue an indexed indirect draw call."""
        if self._pip is None:
            raise RuntimeError("draw_indexed_indirect() called without set_pipeline()")
        ibh = index_buffer.handle if hasattr(index_buffer, 'handle') else index_buffer
        idh = indirect_buffer.handle if hasattr(indirect_buffer, 'handle') else indirect_buffer
        _metal.draw_indexed_indirect(window=self._wh, primitive=primitive,
                                     index_buffer=ibh, index_type=index_type,
                                     indirect_buffer=idh, indirect_offset=indirect_offset)

    def set_depth_stencil(self, compare="less", write=True, *,
                          stencil_compare="always", stencil_pass="keep",
                          stencil_fail="keep", stencil_ref=0, stencil_back_pass=None):
        _metal.set_depth_stencil(window=self._wh, compare=compare, write_enabled=write,
                                 stencil_compare=stencil_compare, stencil_pass=stencil_pass,
                                 stencil_fail=stencil_fail, stencil_ref=stencil_ref,
                                 stencil_back_pass=stencil_back_pass)

# ───────────────────────────────────────────────────────────────────────────
# Blit Pass (context manager)
# ───────────────────────────────────────────────────────────────────────────

class BlitPass:
    """Blit command encoder context manager. Synchronous (waits on exit)."""
    __slots__ = ("_wh",)

    def __init__(self, window_handle):
        self._wh = window_handle

    def __enter__(self):
        _metal.begin_blit(self._wh)
        return self

    def __exit__(self, *exc):
        try: _metal.end_blit(self._wh)
        except Exception: pass
        return False

    def copy_texture_to_buffer(self, texture, buffer, bytes_per_row=0):
        th = texture.handle if hasattr(texture, 'handle') else texture
        bh = buffer.handle if hasattr(buffer, 'handle') else buffer
        _metal.copy_texture_to_buffer(window=self._wh, texture=th,
                                      buffer=bh, bytes_per_row=bytes_per_row)

    def generate_mipmaps(self, texture):
        th = texture.handle if hasattr(texture, 'handle') else texture
        _metal.generate_mipmaps(window=self._wh, texture=th)

# ───────────────────────────────────────────────────────────────────────────
# Compute Pass (context manager)
# ───────────────────────────────────────────────────────────────────────────

class ComputePass:
    """Compute command encoder context manager."""
    __slots__ = ("_wh",)

    def __init__(self, window_handle):
        self._wh = window_handle

    def __enter__(self):
        _metal.begin_compute(self._wh)
        return self

    def __exit__(self, *exc):
        try: _metal.end_compute(self._wh)
        except Exception: pass
        return False

    def set_pipeline(self, p):
        _metal.set_compute_pipeline(window=self._wh, pipeline=p.handle)

    def set_buffer(self, buf, index, offset=0):
        _metal.set_compute_buffer(window=self._wh, buffer=buf.handle, index=index, offset=offset)

    def set_texture(self, tex, index):
        _metal.set_compute_texture(window=self._wh, texture=tex.handle, index=index)

    def dispatch(self, grid, threadgroup):
        _metal.dispatch_compute(
            window=self._wh,
            grid_x=grid[0], grid_y=grid[1], grid_z=grid[2] if len(grid) > 2 else 1,
            tg_x=threadgroup[0], tg_y=threadgroup[1], tg_z=threadgroup[2] if len(threadgroup) > 2 else 1)

# ───────────────────────────────────────────────────────────────────────────
# Window (for standalone gpu usage without scene graph)
# ───────────────────────────────────────────────────────────────────────────

class Window:
    """Metal-backed presentation window."""
    __slots__ = ("_handle", "background", "_metrics", "_rev")

    def __init__(self, title="GPU", background="#000000",
                 orientation: Orientation | str = Orientation.AUTO):
        orientation = Orientation(orientation)
        self._handle = _metal.create_window(title=title, orientation=orientation)
        self.background = normalize_color(background)
        self._metrics = None; self._rev = 0
        self.sync()

    def sync(self):
        if self._metrics is None:
            self._metrics = _metal.window_metrics(self._handle)
            self._rev = int(self._metrics.get("revision", 0))
        else:
            u = _metal.window_metrics_if_changed(self._handle, self._rev)
            if u is not None:
                self._metrics = u; self._rev = int(u["revision"])

    @property
    def handle(self):
        return self._handle

    @property
    def size(self):
        return tuple(self._metrics["size"]) if self._metrics else (393.0, 852.0)

    @property
    def scale(self):
        return float(self._metrics["scale"]) if self._metrics else 3.0

    def frame(self, clear_color=None, target_texture=None,
              depth=False, sample_count=1, stencil=False) -> Frame:
        cc = clear_color if clear_color is not None else self.background
        return Frame(self._handle, cc, target_texture, depth=depth, sample_count=sample_count, stencil=stencil)

    def compute(self) -> ComputePass:
        return ComputePass(self._handle)

    def blit(self) -> BlitPass:
        return BlitPass(self._handle)

    def set_fps(self, fps):
        """Set target frame rate. Returns the applied target fps."""
        return _metal.set_target_fps(self._handle, int(fps))

    def acquire_frame_slot(self):
        """Wait until an onscreen frame slot is safe to reuse."""
        _metal.acquire_frame_slot(self._handle)

    def release_frame_slot(self):
        """Release an acquired onscreen frame slot before encoding starts."""
        _metal.release_frame_slot(self._handle)

    def vsync(self):
        """Block until the next display refresh."""
        _metal.vsync(self._handle)

    def consume_touches(self):
        """Return and clear queued touch events."""
        try: return _metal.consume_touches(self._handle)
        except Exception: return []

    def consume_actions(self):
        """Return and clear queued action/close button presses."""
        return _metal.consume_actions(self._handle)

    def should_close(self) -> bool:
        return self.consume_actions().get("close", 0) > 0

    def close(self):
        if self._handle is not None:
            _metal.close_window(self._handle); self._handle = None

    def __del__(self):
        try: self.close()
        except Exception: pass


def resource_counts():
    """Return internal Metal resource counts for diagnostics."""
    return _metal.resource_counts()
