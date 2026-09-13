"""scene._path_node — Path and Polygon nodes."""
from __future__ import annotations

import math

from _cocoa import _scene_accel
from ._common import _apply, _color, _rot
from ._engine import Cmd, KIND_TEX, Texture
from ._enums import FillRule
from ._node import Node
from ._path import (
    DrawMesh, StrokeStyle, build_path_meshes, command_bounds, commands_snapshot,
    fill_contains_point, flatten_commands, path_bounds, stroke_contains_point,
)


# Split the screen-space error budget between the centerline and round strokes.
_CURVE_PIXEL_TOLERANCE = 0.125


def _max_scale(world):
    """Largest singular value of the affine transform's linear part."""
    a, b, c, d = world[:4]
    peak = max(abs(a), abs(b), abs(c), abs(d))
    if not peak or not math.isfinite(peak):
        return peak
    a, b, c, d = a / peak, b / peak, c / peak, d / peak
    return peak * (0.5 * math.hypot(a + d, b - c) +
                   0.5 * math.hypot(a - d, b + c))


def _usable_scale(cached, required):
    # Keep finer geometry through small animation reversals; release it after
    # a substantial shrink. Keep this in sync with path_scale_usable in C.
    return required <= cached * (1.0 + 1e-12) and required >= cached * 0.25


def _pixel_scale(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Path rendering scale must be finite and non-negative")
    return value


def _mesh_scale(required):
    # Round upward so translation/rotation and small zoom changes reuse meshes.
    mantissa, exponent = math.frexp(required)
    return math.ldexp(1.0, exponent - (mantissa == 0.5))


class Path(Node):
    def __init__(self, commands=None, *, fill="#ffffff", stroke=None, stroke_width=0,
                 join="round", cap="round", miter_limit=4.0,
                 fill_rule="even_odd", curve_tolerance=0.75, curve_depth=10, **kw):
        super().__init__(**kw)
        self._commands = []
        self._flattened_subpaths = None
        self._flattened_tolerance = None
        self._local_bounds = None
        self._geometry_dirty = True
        self._raster_dirty = True
        self._stroke_dirty = True
        self._path_version = 1
        self._cached_mesh_scale = 0.0
        self._tex = None
        self._path_size = None
        self._path_center = None
        self._rscale = 1.0
        self._texture_version = -1
        self._texture_pixel_scale = 0.0
        self._texture_size = None
        self.fill = fill
        self.stroke = stroke
        self.stroke_width = float(stroke_width)
        self.join = join
        self.cap = cap
        self.miter_limit = float(miter_limit)
        self.fill_rule = fill_rule
        self.curve_tolerance = float(curve_tolerance)
        self.curve_depth = int(curve_depth)
        if commands:
            self.extend(commands)

    @property
    def fill(self):
        return self._fill

    @fill.setter
    def fill(self, value):
        self._fill = value
        self._mark_raster_dirty()

    @property
    def stroke(self):
        return self._stroke

    @stroke.setter
    def stroke(self, value):
        self._stroke = value
        self._mark_stroke_dirty()

    @property
    def stroke_width(self):
        return self._stroke_width

    @stroke_width.setter
    def stroke_width(self, value):
        self._stroke_width = max(0.0, float(value))
        self._mark_stroke_dirty()

    @property
    def join(self):
        return self._join

    @join.setter
    def join(self, value):
        self._join = str(value).lower()
        self._mark_stroke_dirty()

    @property
    def cap(self):
        return self._cap

    @cap.setter
    def cap(self, value):
        self._cap = str(value).lower()
        self._mark_stroke_dirty()

    @property
    def miter_limit(self):
        return self._miter_limit

    @miter_limit.setter
    def miter_limit(self, value):
        self._miter_limit = max(1.0, float(value))
        self._mark_stroke_dirty()

    @property
    def fill_rule(self):
        return self._fill_rule

    @fill_rule.setter
    def fill_rule(self, value):
        # FillRule._missing_ accepts "evenodd"/"even-odd"/"nonzero"/"non-zero" aliases
        self._fill_rule = str(FillRule(value))
        self._mark_geometry_dirty()

    @property
    def curve_tolerance(self):
        return self._curve_tolerance

    @curve_tolerance.setter
    def curve_tolerance(self, value):
        self._curve_tolerance = max(0.05, float(value))
        self._mark_geometry_dirty()

    @property
    def curve_depth(self):
        return self._curve_depth

    @curve_depth.setter
    def curve_depth(self, value):
        self._curve_depth = max(1, int(value))
        self._mark_geometry_dirty()

    @property
    def commands(self):
        return list(self._commands)

    def clear_path(self):
        self._commands.clear()
        self._mark_geometry_dirty()
        return self

    def extend(self, commands):
        for item in commands:
            if len(item) == 1:
                cmd, values = item[0], None
            else:
                cmd, values = item
            cmd = str(cmd).upper()
            vals = None if values is None else tuple(float(v) for v in values)
            if cmd not in ("M", "L", "Q", "C", "Z"):
                raise ValueError(f"Unsupported path command: {cmd!r}")
            self._commands.append((cmd, vals))
        self._mark_geometry_dirty()
        return self

    def move_to(self, x, y):
        self._commands.append(("M", (float(x), float(y))))
        self._mark_geometry_dirty()
        return self

    def line_to(self, x, y):
        self._commands.append(("L", (float(x), float(y))))
        self._mark_geometry_dirty()
        return self

    def quad_to(self, cx, cy, x, y):
        self._commands.append(("Q", (float(cx), float(cy), float(x), float(y))))
        self._mark_geometry_dirty()
        return self

    def cubic_to(self, c1x, c1y, c2x, c2y, x, y):
        self._commands.append(("C", (float(c1x), float(c1y), float(c2x), float(c2y), float(x), float(y))))
        self._mark_geometry_dirty()
        return self

    def close_path(self):
        self._commands.append(("Z", None))
        self._mark_geometry_dirty()
        return self

    def polygon(self, points, *, close=True):
        pts = [(float(p[0]), float(p[1])) for p in points]
        if not pts:
            return self
        self.move_to(*pts[0])
        for pt in pts[1:]:
            self.line_to(*pt)
        if close:
            self.close_path()
        return self

    def _mark_geometry_dirty(self):
        self._geometry_dirty = True
        self._raster_dirty = True
        self._stroke_dirty = True
        self._path_version += 1
        self._flattened_subpaths = None
        self._local_bounds = None
        self._cached_meshes = None
        self._cached_mesh_version = -1
        self._path_size = None
        self._path_center = None

    def _mark_stroke_dirty(self):
        self._stroke_dirty = True
        self._raster_dirty = True
        self._local_bounds = None
        self._path_version += 1
        self._cached_meshes = None
        self._cached_mesh_version = -1

    def _mark_raster_dirty(self):
        self._raster_dirty = True
        self._path_version += 1
        self._cached_meshes = None
        self._cached_mesh_version = -1

    def _ensure_flattened(self, pixel_scale=None):
        # Bounds/hit queries may precede rendering. They must neither lock in a
        # coarse approximation nor downgrade geometry already refined to pixels.
        tol = (self._flattened_tolerance if self._flattened_subpaths is not None
               else self.curve_tolerance)
        if pixel_scale is not None:
            tol = min(self.curve_tolerance, _CURVE_PIXEL_TOLERANCE / pixel_scale)
        if (self._flattened_subpaths is None or self._geometry_dirty or
                tol != self._flattened_tolerance):
            self._flattened_subpaths = flatten_commands(
                self._commands,
                tolerance=tol,
                max_depth=self.curve_depth,
            )
            self._flattened_tolerance = tol
            self._cached_meshes = None
            self._cached_mesh_version = -1
            self._geometry_dirty = False
        if self._local_bounds is None:
            if any(kind in ("Q", "C") for kind, _ in self._commands):
                bounds = command_bounds(self._commands)
                if bounds is not None:
                    # Layer measurement happens before rendering. Keep curve
                    # bounds independent of sampling, including bounded stroke
                    # extensions at any cap/join orientation or quality level.
                    pad = self.stroke_width * 0.5 if self.stroke is not None else 0.0
                    if pad > 0:
                        factor = self.miter_limit if self.join == "miter" else 1.0
                        if self.cap == "square":
                            factor = max(factor, math.sqrt(2.0))
                        pad *= factor
                    self._local_bounds = (bounds[0] - pad, bounds[1] - pad,
                                          bounds[2] + pad, bounds[3] + pad)
            else:
                self._local_bounds = path_bounds(
                    self._flattened_subpaths,
                    stroke_width=self.stroke_width if self.stroke is not None else 0.0,
                    join=self.join, cap=self.cap, miter_limit=self.miter_limit,
                )
        return self._flattened_subpaths

    def _quantized_rscale(self, world):
        ds = max(1.0, _pixel_scale(_max_scale(world)))
        if _usable_scale(self._rscale, ds):
            return self._rscale
        return math.ceil(ds * 4.0) / 4.0

    def _snap(self):
        return (
            self.x, self.y, self.rotation, self.scale, self.speed, self.opacity, self.z,
            self.fill, self.stroke, self.stroke_width, self.join, self.cap,
            self.miter_limit, self.fill_rule, self.curve_tolerance, self.curve_depth, self._path_version,
            commands_snapshot(self._commands),
            self._tex._handle if self._tex else None,
        )

    def _bounds(self):
        self._ensure_flattened()
        return self._local_bounds

    def contains_point(self, wx, wy):
        if not self._inside_clip(wx, wy):
            return False
        self._ensure_flattened()
        if not self._flattened_subpaths:
            return False
        lx, ly = self.convert_from_world(wx, wy)
        hit_fill = self.fill is not None and fill_contains_point(
            self._flattened_subpaths, lx, ly, fill_rule=self.fill_rule)
        if hit_fill:
            return True
        if self.stroke is not None and self.stroke_width > 0:
            return stroke_contains_point(
                self._flattened_subpaths, lx, ly,
                width=self.stroke_width, cap=self.cap)
        return False

    def _collider(self):
        b = self._bounds()
        if b is None:
            return None
        wt = self._world_transform
        cx_local = (b[0] + b[2]) * 0.5
        cy_local = (b[1] + b[3]) * 0.5
        cx, cy = _apply(wt, (cx_local, cy_local))
        sx = math.hypot(wt[0], wt[1])
        sy = math.hypot(wt[2], wt[3])
        return ('obb', cx, cy, (b[2] - b[0]) * sx / 2, (b[3] - b[1]) * sy / 2, _rot(wt))

    def _rebuild_texture(self, renderer, raster_scale):
        pixel_scale = _pixel_scale(renderer.screen_scale * raster_scale)
        meshes = self._get_meshes(renderer, pixel_scale=pixel_scale)
        bounds = self._local_bounds
        if not meshes or bounds is None:
            if self._tex is not None:
                self._tex.close()
                self._tex = None
            self._path_size = None
            self._path_center = None
            self._rscale = raster_scale
            self._texture_version = self._path_version
            self._texture_pixel_scale = pixel_scale
            self._texture_size = None
            self._raster_dirty = False
            self._stroke_dirty = False
            return

        dx, dy = -bounds[0] + 2.0, -bounds[1] + 2.0
        meshes = self._offset_meshes(meshes, dx, dy)

        width = max(1.0, bounds[2] - bounds[0] + 4.0)
        height = max(1.0, bounds[3] - bounds[1] + 4.0)
        pw = max(1, int(math.ceil(width * pixel_scale)))
        ph = max(1, int(math.ceil(height * pixel_scale)))
        target = self._tex
        if target is None or self._texture_size != (pw, ph):
            target = Texture.render_target(pw, ph)
        try:
            renderer.render_color_meshes(meshes, clear_color=(0, 0, 0, 0), target_texture=target, viewport=(width, height))
        except BaseException:
            if target is not self._tex:
                target.close()
            raise
        if target is not self._tex:
            if self._tex is not None:
                self._tex.close()
            self._tex = target
        self._path_size = (width, height)
        self._path_center = ((bounds[0] + bounds[2]) * 0.5, (bounds[1] + bounds[3]) * 0.5)
        self._rscale = raster_scale
        self._texture_version = self._path_version
        self._texture_pixel_scale = pixel_scale
        self._texture_size = (pw, ph)
        self._raster_dirty = False
        self._stroke_dirty = False

    @staticmethod
    def _offset_meshes(meshes, dx, dy):
        if not meshes:
            return []
        raw = [(m.vertex_bytes, m.vertex_count, m.index_bytes,
                m.index_count, m.index_type, m.color, m.blend) for m in meshes]
        shifted = _scene_accel.offset_meshes(raw, dx, dy)
        return [DrawMesh(*t, is_stroke=m.is_stroke, fill_rule=m.fill_rule)
                for t, m in zip(shifted, meshes)]

    def _emit(self, cmds, renderer, world, opacity, order):
        raster_scale = self._quantized_rscale(world)
        self._ensure_texture(renderer, raster_scale)
        if self._tex is None or self._path_size is None or self._path_center is None:
            return
        center = _apply(world, self._path_center)
        sx = math.hypot(world[0], world[1])
        sy = math.hypot(world[2], world[3])
        order[0] += 1
        cmds.append(Cmd(
            self.z, order[0], KIND_TEX,
            center[0], center[1],
            self._path_size[0] * sx / 2, self._path_size[1] * sy / 2,
            _rot(world),
            (KIND_TEX, 0, 0, 0), (0, 0, opacity, 0),
            (0, 0, 0, 0), (1, 1, 1, 1), self._tex,
        ))

    def _ensure_texture(self, renderer, raster_scale):
        pixel_scale = _pixel_scale(renderer.screen_scale * raster_scale)
        if (self._texture_version != self._path_version or
                not _usable_scale(self._texture_pixel_scale, pixel_scale)):
            self._rebuild_texture(renderer, raster_scale)
        return self._tex is not None and self._path_size is not None and self._path_center is not None

    def _get_meshes(self, renderer, *, pixel_scale=None):
        """Return list of DrawMesh in path-local space for direct rendering.

        Cache one quality level, refining for zoom, display DPI and capture size.
        """
        required = max(1.0, _pixel_scale(renderer.screen_scale * _max_scale(self._world_transform)
                                        if pixel_scale is None else pixel_scale))
        ver = self._path_version
        cached = getattr(self, '_cached_meshes', None)
        if (cached is not None and getattr(self, '_cached_mesh_version', -1) == ver and
                _usable_scale(self._cached_mesh_scale, required)):
            return cached
        quality = _mesh_scale(required)
        subpaths = self._ensure_flattened(quality)
        stroke_color = _color(self.stroke) if self.stroke is not None else None
        stroke_style = StrokeStyle(self.stroke_width, self.join, self.cap, self.miter_limit)
        fill_color = _color(self.fill) if self.fill is not None else None
        fill_meshes, stroke_meshes = build_path_meshes(
            subpaths, fill_color=fill_color, stroke_color=stroke_color,
            stroke_style=stroke_style, fill_rule=self.fill_rule,
            tolerance=_CURVE_PIXEL_TOLERANCE / quality,
        )
        self._raster_dirty = False
        self._stroke_dirty = False
        result = fill_meshes + stroke_meshes
        self._cached_meshes = result
        self._cached_mesh_version = ver
        self._cached_mesh_scale = quality
        return result

    def close(self):
        if self._tex is not None:
            self._tex.close()
            self._tex = None
        self._commands.clear()
        self._flattened_subpaths = None
        self._local_bounds = None
        self._cached_meshes = None
        self._cached_mesh_version = -1
        self._path_size = None
        self._path_center = None
        super().close()


class Polygon(Path):
    def __init__(self, points, *, fill="#ffffff", stroke=None, stroke_width=0,
                 join="round", fill_rule="even_odd", **kw):
        super().__init__(fill=fill, stroke=stroke, stroke_width=stroke_width,
                         join=join, cap="round", fill_rule=fill_rule, **kw)
        self.polygon(points, close=True)
