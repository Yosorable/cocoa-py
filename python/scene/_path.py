from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from ._enums import FillRule, LineCap, LineJoin

try:
    from _cocoa import _scene_accel as _PATH_ACCEL
except ImportError:
    _PATH_ACCEL = None


_F32 = struct.Struct("<f")
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")


@dataclass(slots=True)
class StrokeStyle:
    width: float
    join: str
    cap: str
    miter_limit: float


@dataclass(slots=True)
class DrawMesh:
    vertex_bytes: bytes
    vertex_count: int
    index_bytes: bytes
    index_count: int
    index_type: str
    color: tuple[float, float, float, float]
    blend: bool = True
    is_stroke: bool = False
    fill_rule: str | None = None


def commands_snapshot(commands):
    snap = []
    for cmd, values in commands:
        if values is None:
            snap.append((cmd, None))
        else:
            snap.append((cmd, tuple(float(v) for v in values)))
    return tuple(snap)


def _positive_tolerance(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Path tolerance must be finite and positive")
    return value


def flatten_commands(commands, *, tolerance=0.75, max_depth=10):
    tol = _positive_tolerance(tolerance)
    depth = max(1, int(max_depth))
    subpaths = []
    current = []
    closed = False
    start = None
    cursor = None

    def finish(force_closed=False):
        nonlocal current, closed, start, cursor
        if current:
            pts = _dedupe_points(current)
            if len(pts) >= 2:
                subpaths.append({
                    "points": pts,
                    "closed": closed or force_closed,
                })
        current = []
        closed = False
        start = None
        cursor = None

    for cmd, values in commands:
        if cmd == "M":
            finish()
            x, y = values
            pt = (float(x), float(y))
            current = [pt]
            start = pt
            cursor = pt
        elif cmd == "L":
            if cursor is None:
                raise ValueError("line_to() requires an active subpath")
            pt = (float(values[0]), float(values[1]))
            current.append(pt)
            cursor = pt
        elif cmd == "Q":
            if cursor is None:
                raise ValueError("quad_to() requires an active subpath")
            c = (float(values[0]), float(values[1]))
            end = (float(values[2]), float(values[3]))
            out = []
            _flatten_quad(cursor, c, end, tol, depth, out)
            current.extend(out)
            cursor = end
        elif cmd == "C":
            if cursor is None:
                raise ValueError("cubic_to() requires an active subpath")
            c1 = (float(values[0]), float(values[1]))
            c2 = (float(values[2]), float(values[3]))
            end = (float(values[4]), float(values[5]))
            out = []
            _flatten_cubic(cursor, c1, c2, end, tol, depth, out)
            current.extend(out)
            cursor = end
        elif cmd == "Z":
            if cursor is None:
                continue
            closed = True
            if start is not None and _dist_sq(cursor, start) > 1e-12:
                current.append(start)
            finish(force_closed=True)
        else:
            raise ValueError(f"Unsupported path command: {cmd!r}")

    finish()
    return subpaths


def _build_path_meshes_py(subpaths, *, fill_color, stroke_color, stroke_style, fill_rule="even_odd", offset=(0, 0), tolerance=None):
    fill_meshes = []
    stroke_meshes = []

    closed_contours = []
    stroke_paths = []
    for sub in subpaths:
        points = sub["points"]
        if len(points) < 2:
            continue
        is_closed = bool(sub["closed"])
        poly = points[:-1] if is_closed and len(points) > 2 and _dist_sq(points[0], points[-1]) <= 1e-12 else points[:]
        if is_closed and len(poly) >= 3:
            closed_contours.append(poly)
        stroke_paths.append((poly, is_closed))

    if fill_color is not None and closed_contours:
        # Directed fans accumulate parity/winding in stencil, including holes.
        # The color pass then paints the covered samples once, preserving the
        # background and avoiding repeated alpha blending where fans overlap.
        triangles = [
            (contour[0], contour[i], contour[i + 1])
            for contour in closed_contours
            for i in range(1, len(contour) - 1)
        ]
        fill_meshes.append(_triangles_to_mesh(
            triangles, fill_color, offset=offset,
            fill_rule=str(FillRule(fill_rule)),
        ))

    if stroke_color is not None and stroke_style.width > 0:
        stroke_tris = []
        for points, is_closed in stroke_paths:
            if len(points) >= 2:
                stroke_tris.extend(stroke_polyline(
                    points,
                    closed=is_closed,
                    width=stroke_style.width,
                    join=stroke_style.join,
                    cap=stroke_style.cap,
                    miter_limit=stroke_style.miter_limit,
                    tolerance=tolerance,
                ))
        if stroke_tris:
            stroke_meshes.append(_triangles_to_mesh(stroke_tris, stroke_color, is_stroke=True, offset=offset))
            # Both backends use renderer MSAA on the actual stroke boundary.

    return fill_meshes, stroke_meshes


def _convex_hull(points):
    """Compute convex hull using Andrew's monotone chain. Returns CCW hull."""
    pts = sorted(set(points), key=lambda p: (p[0], p[1]))
    if len(pts) <= 1:
        return pts
    lower = []
    for p in pts:
        while len(lower) >= 2 and _cross2(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and _cross2(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _cross2(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def path_bounds(subpaths, *, stroke_width=0.0, join="round", cap="round", miter_limit=4.0):
    pts = []
    for sub in subpaths:
        pts.extend(sub["points"])
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    pad = max(0.0, float(stroke_width)) * 0.5
    x0, y0, x1, y1 = min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad
    # Round/bevel joins and round/butt caps fit within half-width padding.
    # Reuse the rendered geometry for extensions, including the miter limit.
    if pad > 0 and (_normalize_join(join) == "miter" or _normalize_cap(cap) == "square"):
        for sub in subpaths:
            triangles = stroke_polyline(
                sub["points"], closed=sub["closed"], width=stroke_width,
                join=join, cap=cap, miter_limit=miter_limit,
            )
            for triangle in triangles:
                for x, y in triangle:
                    x0, y0 = min(x0, x), min(y0, y)
                    x1, y1 = max(x1, x), max(y1, y)
    return (x0, y0, x1, y1)


def _bezier_axis_extrema(values):
    """Evaluate interior extrema without depending on a flattening tolerance."""
    peak = max(abs(value) for value in values)
    if peak == 0:
        return []
    normalized = [value / peak for value in values]
    if len(values) == 3:
        p0, p1, p2 = normalized
        denominator = (p0 - p1) + (p2 - p1)
        roots = [] if denominator == 0 else [(p0 - p1) / denominator]
    else:
        p0, p1, p2, p3 = normalized
        d0, d1, d2 = p1 - p0, p2 - p1, p3 - p2
        a, b, c = d0 - 2 * d1 + d2, 2 * (d1 - d0), d0
        if a == 0:
            roots = [] if b == 0 else [-c / b]
        else:
            discriminant = b * b - 4 * a * c
            if discriminant < 0:
                roots = []
            else:
                q = -0.5 * (b + math.copysign(math.sqrt(discriminant), b))
                roots = [-b / (2 * a)] if q == 0 else [q / a, c / q]
    extrema = []
    for t in roots:
        if 0 < t < 1:
            work = values
            while len(work) > 1:
                work = [(1 - t) * x + t * y for x, y in zip(work, work[1:])]
            extrema.append(work[0])
    return extrema


def command_bounds(commands):
    """Centerline bounds of validated commands, independent of render quality."""
    xs, ys = [], []
    cursor = start = None
    for kind, values in commands:
        if kind == "M":
            cursor = start = values
        elif kind in ("L", "Q", "C"):
            controls = [cursor, *zip(values[::2], values[1::2])]
            # A move followed only by zero-length segments has no drawable bounds.
            if any(point != cursor for point in controls[1:]):
                x, y = list(zip(*controls))
                xs.extend((x[0], x[-1]))
                ys.extend((y[0], y[-1]))
                if kind != "L":
                    xs.extend(_bezier_axis_extrema(x))
                    ys.extend(_bezier_axis_extrema(y))
            cursor = controls[-1]
        elif kind == "Z":
            if cursor is not None and start is not None and cursor != start:
                xs.extend((cursor[0], start[0]))
                ys.extend((cursor[1], start[1]))
            cursor = start = None
    return None if not xs else (min(xs), min(ys), max(xs), max(ys))


def fill_contains_point(subpaths, x, y, *, fill_rule="even_odd"):
    winding = 0
    inside = False
    for sub in subpaths:
        pts = sub["points"]
        if not sub["closed"] or len(pts) < 3:
            continue
        poly = pts[:-1] if _dist_sq(pts[0], pts[-1]) <= 1e-12 else pts
        if fill_rule == "non_zero":
            winding += _polygon_winding(poly, x, y)
        else:
            if _point_in_polygon(poly, x, y):
                inside = not inside
    return winding != 0 if fill_rule == "non_zero" else inside


def stroke_contains_point(subpaths, x, y, *, width, cap="butt", closed_override=None):
    radius = float(width) * 0.5
    if radius <= 0:
        return False
    rr = radius * radius
    for sub in subpaths:
        pts = sub["points"]
        if len(pts) < 2:
            continue
        poly = pts[:-1] if sub["closed"] and _dist_sq(pts[0], pts[-1]) <= 1e-12 else pts
        closed = sub["closed"] if closed_override is None else closed_override
        if _polyline_hit(poly, x, y, rr, radius, closed=closed, cap=cap):
            return True
    return False


def contour_fill_layers(contours, fill_color, *, fill_rule: FillRule | str = FillRule.EVEN_ODD):
    fill_rule = FillRule(fill_rule)
    nodes = _build_contour_nodes(contours)
    if fill_rule is FillRule.NON_ZERO:
        _assign_winding(nodes)
        key_fn = lambda node: (node["depth"], abs(node["area"]))
        return [
            (_ensure_ccw(node["points"]), fill_color if node["winding"] != 0 else (0.0, 0.0, 0.0, 0.0))
            for node in sorted(nodes, key=key_fn)
        ]
    key_fn = lambda node: (node["depth"], abs(node["area"]))
    return [
        (_ensure_ccw(node["points"]), fill_color if node["depth"] % 2 == 0 else (0.0, 0.0, 0.0, 0.0))
        for node in sorted(nodes, key=key_fn)
    ]


def stroke_polyline(points, *, closed, width, join="round", cap="round", miter_limit=4.0, tolerance=None):
    if tolerance is not None:
        tolerance = _positive_tolerance(tolerance)
    if len(points) < 2 or width <= 0:
        return []
    join = _normalize_join(join)
    cap = _normalize_cap(cap)
    hw = float(width) * 0.5
    n = len(points)
    closed = bool(closed)
    if closed and n > 2 and _dist_sq(points[0], points[-1]) <= 1e-12:
        points = points[:-1]
        n = len(points)
    if n < 2:
        return []

    triangles = []
    dirs = []
    norms = []
    seg_count = n if closed else n - 1
    for i in range(seg_count):
        p0 = points[i]
        p1 = points[(i + 1) % n]
        dx = p1[0] - p0[0]
        dy = p1[1] - p0[1]
        ln = math.hypot(dx, dy)
        if ln < 1e-12:
            dirs.append((1.0, 0.0))
            norms.append((0.0, 1.0))
            continue
        ux, uy = dx / ln, dy / ln
        dirs.append((ux, uy))
        norms.append((-uy, ux))

        l0 = (p0[0] - uy * hw, p0[1] + ux * hw)
        r0 = (p0[0] + uy * hw, p0[1] - ux * hw)
        l1 = (p1[0] - uy * hw, p1[1] + ux * hw)
        r1 = (p1[0] + uy * hw, p1[1] - ux * hw)
        triangles.append((l0, l1, r0))
        triangles.append((r0, l1, r1))

    vertex_indices = range(n) if closed else range(1, n - 1)
    for i in vertex_indices:
        prev_i = (i - 1 + seg_count) % seg_count
        next_i = i % seg_count
        prev_dir = dirs[prev_i]
        next_dir = dirs[next_i]
        turn = prev_dir[0] * next_dir[1] - prev_dir[1] * next_dir[0]
        if abs(turn) < 1e-8:
            continue
        point = points[i]
        prev_norm = norms[prev_i]
        next_norm = norms[next_i]
        # Segment rectangles overlap on the inside; join the opposite side.
        if turn > 0:
            outer_prev = (point[0] - prev_norm[0] * hw, point[1] - prev_norm[1] * hw)
            outer_next = (point[0] - next_norm[0] * hw, point[1] - next_norm[1] * hw)
            if join == "miter":
                # The miter extension alone does not cover the base wedge.
                triangles.append((point, outer_prev, outer_next))
                inter = _line_intersection(
                    outer_prev, (outer_prev[0] + prev_dir[0], outer_prev[1] + prev_dir[1]),
                    outer_next, (outer_next[0] + next_dir[0], outer_next[1] + next_dir[1]),
                )
                if inter is not None and math.hypot(inter[0] - point[0], inter[1] - point[1]) <= hw * max(1.0, miter_limit):
                    triangles.append((outer_prev, inter, outer_next))
            elif join == "round":
                _append_round_join(triangles, point, outer_prev, outer_next, hw, clockwise=False, tolerance=tolerance)
            else:
                triangles.append((point, outer_prev, outer_next))
        else:
            outer_prev = (point[0] + prev_norm[0] * hw, point[1] + prev_norm[1] * hw)
            outer_next = (point[0] + next_norm[0] * hw, point[1] + next_norm[1] * hw)
            if join == "miter":
                triangles.append((point, outer_next, outer_prev))
                inter = _line_intersection(
                    outer_prev, (outer_prev[0] + prev_dir[0], outer_prev[1] + prev_dir[1]),
                    outer_next, (outer_next[0] + next_dir[0], outer_next[1] + next_dir[1]),
                )
                if inter is not None and math.hypot(inter[0] - point[0], inter[1] - point[1]) <= hw * max(1.0, miter_limit):
                    triangles.append((outer_prev, outer_next, inter))
            elif join == "round":
                _append_round_join(triangles, point, outer_prev, outer_next, hw, clockwise=True, tolerance=tolerance)
            else:
                triangles.append((point, outer_next, outer_prev))

    if not closed:
        first_norm = norms[0]
        last_norm = norms[-1]
        left0 = (points[0][0] + first_norm[0] * hw, points[0][1] + first_norm[1] * hw)
        right0 = (points[0][0] - first_norm[0] * hw, points[0][1] - first_norm[1] * hw)
        left1 = (points[-1][0] + last_norm[0] * hw, points[-1][1] + last_norm[1] * hw)
        right1 = (points[-1][0] - last_norm[0] * hw, points[-1][1] - last_norm[1] * hw)
        _append_cap(triangles, points[0], dirs[0], hw, left0, right0, cap, start=True, tolerance=tolerance)
        _append_cap(triangles, points[-1], dirs[-1], hw, left1, right1, cap, start=False, tolerance=tolerance)

    return triangles


def _triangles_to_mesh(triangles, color, *, blend=True, is_stroke=False, fill_rule=None, offset=(0, 0)):
    vertices = []
    indices = []
    index = 0
    ox, oy = offset
    for tri in triangles:
        for x, y in tri:
            vertices.extend((float(x) + ox, float(y) + oy, 1.0))
        indices.extend((index, index + 1, index + 2))
        index += 3
    vertex_bytes = struct.pack(f"<{len(vertices)}f", *vertices)
    if index <= 0xFFFF:
        index_bytes = struct.pack(f"<{len(indices)}H", *indices)
        index_type = "uint16"
    else:
        index_bytes = struct.pack(f"<{len(indices)}I", *indices)
        index_type = "uint32"
    return DrawMesh(vertex_bytes, index, index_bytes, len(indices), index_type,
                    tuple(float(c) for c in color), blend, is_stroke, fill_rule)


def _generate_fringe_strip(contour, *, fringe_width=0.5, closed=True, offset=(0, 0)):
    """Generate a fringe strip (alpha 1→0) around a contour for anti-aliasing.

    Returns list of (x, y, alpha) float triples and index list.
    """
    n = len(contour)
    if n < 2:
        return [], []
    ox, oy = offset
    hw = fringe_width

    # Compute per-edge outward normals
    normals = []
    for i in range(n):
        j = (i + 1) % n
        dx = contour[j][0] - contour[i][0]
        dy = contour[j][1] - contour[i][1]
        ln = math.hypot(dx, dy)
        if ln < 1e-12:
            normals.append((0.0, 1.0))
        else:
            normals.append((-dy / ln, dx / ln))

    # Average normals at each vertex
    avg_normals = []
    for i in range(n):
        if closed:
            prev = (i - 1) % n
        else:
            prev = max(0, i - 1)
        nx = (normals[prev][0] + normals[i % len(normals)][0]) * 0.5
        ny = (normals[prev][1] + normals[i % len(normals)][1]) * 0.5
        ln = math.hypot(nx, ny)
        if ln < 1e-12:
            avg_normals.append(normals[i % len(normals)])
        else:
            avg_normals.append((nx / ln, ny / ln))

    vertices = []
    indices = []
    seg_count = n if closed else n - 1
    for i in range(n):
        px, py = contour[i]
        anx, any_ = avg_normals[i]
        # Inner vertex (alpha=1)
        vertices.extend((px + ox, py + oy, 1.0))
        # Outer vertex (alpha=0)
        vertices.extend((px + anx * hw + ox, py + any_ * hw + oy, 0.0))

    # Build quad strip indices
    for i in range(seg_count):
        j = (i + 1) % n
        i0 = i * 2       # inner current
        i1 = i * 2 + 1   # outer current
        j0 = j * 2       # inner next
        j1 = j * 2 + 1   # outer next
        indices.extend((i0, j0, i1, i1, j0, j1))

    return vertices, indices


def _build_fringe_mesh(contour, color, *, fringe_width=0.5, closed=True, offset=(0, 0)):
    """Build a DrawMesh for the fringe strip around a contour."""
    verts, idxs = _generate_fringe_strip(contour, fringe_width=fringe_width, closed=closed, offset=offset)
    if not verts or not idxs:
        return None
    vertex_count = len(verts) // 3
    vertex_bytes = struct.pack(f"<{len(verts)}f", *verts)
    if vertex_count <= 0xFFFF:
        index_bytes = struct.pack(f"<{len(idxs)}H", *idxs)
        index_type = "uint16"
    else:
        index_bytes = struct.pack(f"<{len(idxs)}I", *idxs)
        index_type = "uint32"
    return DrawMesh(vertex_bytes, vertex_count, index_bytes, len(idxs), index_type,
                    tuple(float(c) for c in color), blend=True, is_stroke=False)


def _flatten_quad(p0, p1, p2, tol, depth, out):
    if depth <= 0 or _quad_flat_enough(p0, p1, p2, tol):
        out.append(p2)
        return
    p01 = _mid(p0, p1)
    p12 = _mid(p1, p2)
    p012 = _mid(p01, p12)
    _flatten_quad(p0, p01, p012, tol, depth - 1, out)
    _flatten_quad(p012, p12, p2, tol, depth - 1, out)


def _flatten_cubic(p0, p1, p2, p3, tol, depth, out):
    if depth <= 0 or _cubic_flat_enough(p0, p1, p2, p3, tol):
        out.append(p3)
        return
    p01 = _mid(p0, p1)
    p12 = _mid(p1, p2)
    p23 = _mid(p2, p3)
    p012 = _mid(p01, p12)
    p123 = _mid(p12, p23)
    p0123 = _mid(p012, p123)
    _flatten_cubic(p0, p01, p012, p0123, tol, depth - 1, out)
    _flatten_cubic(p0123, p123, p23, p3, tol, depth - 1, out)


def _dedupe_points(points):
    out = []
    for pt in points:
        if not out or _dist_sq(out[-1], pt) > 1e-12:
            out.append(pt)
    if len(out) > 1 and _dist_sq(out[0], out[-1]) <= 1e-12:
        out[-1] = out[0]
    return out


def _build_contour_nodes(contours):
    nodes = []
    cleaned = []
    for contour in contours:
        pts = _dedupe_points(contour)
        if len(pts) >= 3:
            cleaned.append(pts)

    for pts in cleaned:
        area = _signed_area(pts)
        node = {
            "points": pts,
            "area": area,
            "sign": 1 if area >= 0 else -1,
            "parent": None,
            "children": [],
            "depth": 0,
        }
        nodes.append(node)

    areas = [abs(n["area"]) for n in nodes]
    order = sorted(range(len(nodes)), key=lambda i: areas[i])
    for idx in order:
        node = nodes[idx]
        test_point = _contour_probe_point(node["points"])
        parent = None
        parent_area = None
        for candidate in nodes:
            if candidate is node:
                continue
            if abs(candidate["area"]) <= abs(node["area"]):
                continue
            if _point_in_polygon(candidate["points"], test_point[0], test_point[1]):
                ca = abs(candidate["area"])
                if parent is None or ca < parent_area:
                    parent = candidate
                    parent_area = ca
        node["parent"] = parent
        if parent is not None:
            parent["children"].append(node)
            node["depth"] = parent["depth"] + 1
    return nodes


def _assign_winding(nodes):
    def walk(node, winding):
        node["winding"] = winding + node["sign"]
        for child in node["children"]:
            walk(child, node["winding"])
    for node in nodes:
        if node["parent"] is None:
            walk(node, 0)


def _ear_clip(points):
    pts = _dedupe_ring(points)
    if len(pts) < 3:
        return []
    if _signed_area(pts) < 0:
        pts.reverse()
    idx = list(range(len(pts)))
    triangles = []
    guard = 0
    while len(idx) > 3 and guard < len(pts) * len(pts):
        ear_found = False
        m = len(idx)
        for i in range(m):
            i0 = idx[(i - 1) % m]
            i1 = idx[i]
            i2 = idx[(i + 1) % m]
            a = pts[i0]
            b = pts[i1]
            c = pts[i2]
            if _cross(a, b, c) <= 1e-10:
                continue
            if any(_point_in_triangle(pts[j], a, b, c) for j in idx if j not in (i0, i1, i2)):
                continue
            triangles.append((a, b, c))
            idx.pop(i)
            ear_found = True
            break
        if not ear_found:
            break
        guard += 1
    if len(idx) == 3:
        triangles.append((pts[idx[0]], pts[idx[1]], pts[idx[2]]))
    return triangles


def _arc_steps(span, radius, minimum, tolerance):
    angle = math.pi / 10
    if tolerance is not None and radius > 0:
        # Sagitta = radius * (1 - cos(angle / 2)); asin stays accurate for
        # tolerances much smaller than the radius. Bound work at extreme zoom.
        angle = min(angle, 4 * math.asin(math.sqrt(min(1.0, tolerance / radius) * 0.5)))
    if angle <= abs(span) / 4096:
        return 4096
    return max(minimum, math.ceil(abs(span) / angle))


def _append_round_join(triangles, center, start_outer, end_outer, radius, *, clockwise, tolerance=None):
    a0 = math.atan2(start_outer[1] - center[1], start_outer[0] - center[0])
    a1 = math.atan2(end_outer[1] - center[1], end_outer[0] - center[0])
    if clockwise:
        while a1 > a0:
            a1 -= math.tau
    else:
        while a1 < a0:
            a1 += math.tau
    span = a1 - a0
    if abs(span) < 1e-6:
        return
    steps = _arc_steps(span, radius, 3, tolerance)
    prev = start_outer
    for i in range(1, steps + 1):
        ang = a0 + span * (i / steps)
        cur = end_outer if i == steps else (center[0] + math.cos(ang) * radius, center[1] + math.sin(ang) * radius)
        triangles.append((center, prev, cur))
        prev = cur


def _append_cap(triangles, point, direction, hw, left, right, cap, *, start, tolerance=None):
    if cap == "butt":
        return
    if cap == "square":
        sign = -1.0 if start else 1.0
        ex = direction[0] * hw * sign
        ey = direction[1] * hw * sign
        l2 = (left[0] + ex, left[1] + ey)
        r2 = (right[0] + ex, right[1] + ey)
        triangles.append((left, l2, right))
        triangles.append((right, l2, r2))
        return

    a0 = math.atan2(right[1] - point[1], right[0] - point[0])
    a1 = math.atan2(left[1] - point[1], left[0] - point[0])
    if start:
        while a1 > a0:
            a1 -= math.tau
        span = a1 - a0
    else:
        while a1 < a0:
            a1 += math.tau
        span = a1 - a0
    steps = _arc_steps(span, hw, 6, tolerance)
    prev = right
    for i in range(1, steps + 1):
        ang = a0 + span * (i / steps)
        cur = left if i == steps else (point[0] + math.cos(ang) * hw, point[1] + math.sin(ang) * hw)
        triangles.append((point, prev, cur))
        prev = cur


def _polyline_hit(points, x, y, rr, radius, *, closed, cap):
    n = len(points)
    seg_count = n if closed else n - 1
    for i in range(seg_count):
        a = points[i]
        b = points[(i + 1) % n]
        if _distance_sq_to_segment((x, y), a, b) <= rr:
            return True
    if closed:
        return False
    if cap in ("round", "square"):
        if _dist_sq((x, y), points[0]) <= rr or _dist_sq((x, y), points[-1]) <= rr:
            return True
    return False


def _distance_sq_to_segment(p, a, b):
    ax, ay = a
    bx, by = b
    dx = bx - ax
    dy = by - ay
    ls = dx * dx + dy * dy
    if ls < 1e-12:
        return _dist_sq(p, a)
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / ls
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    qx = ax + dx * t
    qy = ay + dy * t
    return (p[0] - qx) ** 2 + (p[1] - qy) ** 2


def _line_intersection(a0, a1, b0, b1):
    dax = a1[0] - a0[0]
    day = a1[1] - a0[1]
    dbx = b1[0] - b0[0]
    dby = b1[1] - b0[1]
    den = dax * dby - day * dbx
    if abs(den) < 1e-12:
        return None
    dx = b0[0] - a0[0]
    dy = b0[1] - a0[1]
    t = (dx * dby - dy * dbx) / den
    return (a0[0] + t * dax, a0[1] + t * day)


def _normalize_join(join):
    return str(LineJoin(str(join).lower()))


def _normalize_cap(cap):
    return str(LineCap(str(cap).lower()))


def _ensure_ccw(points):
    return points if _signed_area(points) >= 0 else list(reversed(points))


def _ensure_cw(points):
    return points if _signed_area(points) <= 0 else list(reversed(points))


def _dedupe_ring(points):
    pts = _dedupe_points(points)
    if len(pts) > 1 and _dist_sq(pts[0], pts[-1]) <= 1e-12:
        pts = pts[:-1]
    return pts


def _contour_probe_point(points):
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    return (cx, cy)


def _quad_flat_enough(p0, p1, p2, tol):
    return _distance_to_chord(p1, p0, p2) <= tol


def _cubic_flat_enough(p0, p1, p2, p3, tol):
    return max(_distance_to_chord(p1, p0, p3), _distance_to_chord(p2, p0, p3)) <= tol


def _distance_to_chord(p, a, b):
    ax, ay = a
    bx, by = b
    dx = bx - ax
    dy = by - ay
    ln = math.hypot(dx, dy)
    if ln < 1e-12:
        return math.hypot(p[0] - ax, p[1] - ay)
    ux, uy = dx / ln, dy / ln
    projection = (p[0] - ax) * ux + (p[1] - ay) * uy
    if projection <= 0:
        return math.hypot(p[0] - ax, p[1] - ay)
    if projection >= ln:
        return math.hypot(p[0] - bx, p[1] - by)
    return abs((p[0] - ax) * uy - (p[1] - ay) * ux)


def _point_in_polygon(poly, x, y):
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if ((y0 > y) != (y1 > y)):
            xint = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if xint > x:
                inside = not inside
    return inside


def _polygon_winding(poly, x, y):
    winding = 0
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if y0 <= y:
            if y1 > y and _cross((x0, y0), (x1, y1), (x, y)) > 0:
                winding += 1
        else:
            if y1 <= y and _cross((x0, y0), (x1, y1), (x, y)) < 0:
                winding -= 1
    return winding


def _point_in_triangle(p, a, b, c):
    c1 = _cross(a, b, p)
    c2 = _cross(b, c, p)
    c3 = _cross(c, a, p)
    has_neg = (c1 < -1e-10) or (c2 < -1e-10) or (c3 < -1e-10)
    has_pos = (c1 > 1e-10) or (c2 > 1e-10) or (c3 > 1e-10)
    return not (has_neg and has_pos)


def _signed_area(points):
    area = 0.0
    n = len(points)
    for i in range(n):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    return area * 0.5


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _mid(a, b):
    return ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)


def _normalize(v, *, fallback=(1.0, 0.0)):
    ln = math.hypot(v[0], v[1])
    if ln < 1e-12:
        return fallback
    return (v[0] / ln, v[1] / ln)


def _dist_sq(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


_flatten_commands_py = flatten_commands
_path_bounds_py = path_bounds
_fill_contains_point_py = fill_contains_point
_stroke_contains_point_py = stroke_contains_point


def _accel_available(name):
    return _PATH_ACCEL is not None and hasattr(_PATH_ACCEL, name)


def _decode_meshes(items, is_stroke=False, fill_rule=None):
    return [
        DrawMesh(vertex_bytes, vertex_count, index_bytes, index_count, index_type,
                 tuple(color), bool(blend), is_stroke, fill_rule)
        for vertex_bytes, vertex_count, index_bytes, index_count, index_type, color, blend in items
    ]


def flatten_commands(commands, *, tolerance=0.75, max_depth=10):
    if _accel_available("path_flatten"):
        return _PATH_ACCEL.path_flatten(commands, _positive_tolerance(tolerance), max(1, int(max_depth)))
    return _flatten_commands_py(commands, tolerance=tolerance, max_depth=max_depth)


def build_path_meshes(subpaths, *, fill_color, stroke_color, stroke_style, fill_rule="even_odd", offset=(0, 0), tolerance=None):
    if tolerance is not None:
        tolerance = _positive_tolerance(tolerance)
    if _accel_available("build_path_meshes"):
        fill_meshes, stroke_meshes = _PATH_ACCEL.build_path_meshes(
            subpaths, fill_color, stroke_color,
            float(stroke_style.width), str(stroke_style.join),
            str(stroke_style.cap), float(stroke_style.miter_limit), str(fill_rule),
            float(offset[0]), float(offset[1]),
            0.0 if tolerance is None else tolerance,
        )
        return (_decode_meshes(fill_meshes, fill_rule=str(FillRule(fill_rule))),
                _decode_meshes(stroke_meshes, is_stroke=True))
    return _build_path_meshes_py(
        subpaths, fill_color=fill_color, stroke_color=stroke_color,
        stroke_style=stroke_style, fill_rule=fill_rule, offset=offset, tolerance=tolerance,
    )


def path_bounds(subpaths, *, stroke_width=0.0, join="round", cap="round", miter_limit=4.0):
    if _accel_available("path_bounds"):
        return _PATH_ACCEL.path_bounds(
            subpaths, float(stroke_width), str(join), str(cap), float(miter_limit),
        )
    return _path_bounds_py(
        subpaths, stroke_width=stroke_width, join=join, cap=cap, miter_limit=miter_limit,
    )


def fill_contains_point(subpaths, x, y, *, fill_rule="even_odd"):
    if _accel_available("path_fill_contains"):
        return bool(_PATH_ACCEL.path_fill_contains(subpaths, float(x), float(y), str(fill_rule)))
    return _fill_contains_point_py(subpaths, x, y, fill_rule=fill_rule)


def stroke_contains_point(subpaths, x, y, *, width, cap="butt", closed_override=None):
    if closed_override is None and _accel_available("path_stroke_contains"):
        return bool(_PATH_ACCEL.path_stroke_contains(subpaths, float(x), float(y), float(width), str(cap)))
    return _stroke_contains_point_py(subpaths, x, y, width=width, cap=cap, closed_override=closed_override)
