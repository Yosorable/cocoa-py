/*
 * SceneAccelModule.mm — C-accelerated scene graph collect + encode
 *
 * Replaces the Python _collect → sort → encode pipeline with a single C
 * function that walks the node tree, computes transforms, emits per-shape
 * GPU data, sorts by (z, order), and returns packed vertex/quad bytes +
 * batch info ready for GPU submission.
 *
 * Built-in types (Circle, Rect, Line, Label, Image, Group, Layer) are
 * handled natively in C.  Unknown types fall back to Python _emit().
 */

#include <Python.h>
#include <algorithm>
#include <array>
#include <ctype.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <vector>

#include "SceneAccelModule.h"

/* ─────────────────────────────────────────────────────────────────────── */
/* Constants                                                               */
/* ─────────────────────────────────────────────────────────────────────── */

enum {
    KIND_TEX = 0,
    KIND_CIRCLE = 1,
    KIND_RING = 2,
    KIND_RRECT = 3,
    KIND_STROKE_RRECT = 4,
    KIND_LINE = 5,
    KIND_MESH = 6,
    KIND_PARTICLE = 7,
};

#define COLL_CIRCLE 0
#define COLL_OBB    1

enum {
    NTYPE_GROUP   = 0,
    NTYPE_CIRCLE  = 1,
    NTYPE_RECT    = 2,
    NTYPE_LINE    = 3,
    NTYPE_LABEL   = 4,
    NTYPE_IMAGE   = 5,
    NTYPE_LAYER   = 6,
    NTYPE_SPRITE      = 7,
    NTYPE_SHADERNODE  = 8,
    NTYPE_NINESLICE   = 9,
    NTYPE_PATH        = 10,
    NTYPE_PARTICLE    = 11,
    NTYPE_UNKNOWN = -1,
};

/* ─────────────────────────────────────────────────────────────────────── */
/* Interned attribute name strings (initialized on first use)              */
/* ─────────────────────────────────────────────────────────────────────── */

static PyObject *s_x, *s_y, *s_rotation, *s_scale, *s_opacity, *s_z;
static PyObject *s_visible, *s_children, *s__c_cache;
static PyObject *s_radius, *s_fill, *s_stroke, *s_stroke_width;
static PyObject *s_width, *s_height, *s_color;
static PyObject *s_start, *s_end;
static PyObject *s_text, *s_size, *s_font;
static PyObject *s_texture, *s_img_size;
static PyObject *s__handle;
/* Sprite */
static PyObject *s_sprite_size, *s_anchor, *s_flip_x, *s_flip_y, *s_tint, *s__uv_rect;
/* World transform export */
static PyObject *s__world_transform, *s__world_opacity;
/* Interactive / bounds */
static PyObject *s_interactive, *s__bounds, *s__rendered_size;
/* For fallback path */
static PyObject *s__emit, *s__snap, *s_kind, *s_cx, *s_cy, *s_hw, *s_hh, *s_rot;
static PyObject *s_params, *s_style, *s_extra;
static PyObject *s__tex, *s__lsize, *s__lcenter, *s__dirty, *s__rscale, *s__shader_size;
static PyObject *s__capture_center;
static PyObject *s__path_size, *s__path_center, *s__path_version, *s__ensure_texture, *s__raster_dirty, *s__get_meshes;
static PyObject *s__cached_mesh_scale;
static PyObject *s__rebuild, *s_text_texture;
static PyObject *s_parent;
/* NineSlice */
static PyObject *s_nine_size, *s_insets;
/* Collision / hit-test */
static PyObject *s_collision_category, *s_collision_mask, *s_passthrough;
static PyObject *s__collider, *s_contains_point;

/* Lazy-init type objects */
static PyObject *SceneType, *GroupType, *LayerType;
static PyObject *CircleType, *RectType, *LineType, *LabelType, *ImageType, *SpriteType, *ShaderNodeType;
static PyObject *NineSliceType, *PathType, *PolygonType, *ParticleEmitterType;
static int types_ready = 0;

static int intern_strings(void) {
    #define INTERN(var, name) var = PyUnicode_InternFromString(name); if (!var) return -1
    INTERN(s_x, "x"); INTERN(s_y, "y"); INTERN(s_rotation, "rotation");
    INTERN(s_scale, "scale"); INTERN(s_opacity, "opacity"); INTERN(s_z, "z");
    INTERN(s_visible, "visible"); INTERN(s_children, "children"); INTERN(s__c_cache, "_c_cache");
    INTERN(s_radius, "radius"); INTERN(s_fill, "fill"); INTERN(s_stroke, "stroke");
    INTERN(s_stroke_width, "stroke_width");
    INTERN(s_width, "width"); INTERN(s_height, "height"); INTERN(s_color, "color");
    INTERN(s_start, "start"); INTERN(s_end, "end");
    INTERN(s_text, "text"); INTERN(s_size, "size"); INTERN(s_font, "font");
    INTERN(s_texture, "texture"); INTERN(s_img_size, "img_size");
    INTERN(s__handle, "_handle");
    INTERN(s__emit, "_emit"); INTERN(s__snap, "_snap"); INTERN(s_kind, "kind");
    INTERN(s_cx, "cx"); INTERN(s_cy, "cy"); INTERN(s_hw, "hw"); INTERN(s_hh, "hh");
    INTERN(s_rot, "rot"); INTERN(s_params, "params"); INTERN(s_style, "style");
    INTERN(s_extra, "extra");
    INTERN(s__tex, "_tex"); INTERN(s__shader_size, "_shader_size");
    INTERN(s__path_size, "_path_size"); INTERN(s__path_center, "_path_center");
    INTERN(s__path_version, "_path_version"); INTERN(s__ensure_texture, "_ensure_texture"); INTERN(s__get_meshes, "_get_meshes");
    INTERN(s__cached_mesh_scale, "_cached_mesh_scale");
    INTERN(s__raster_dirty, "_raster_dirty");
    INTERN(s__lsize, "_lsize"); INTERN(s__lcenter, "_lcenter");
    INTERN(s__capture_center, "_capture_center");
    INTERN(s__dirty, "_dirty"); INTERN(s__rscale, "_rscale"); INTERN(s__rebuild, "_rebuild");
    INTERN(s_text_texture, "text_texture"); INTERN(s_parent, "parent");
    INTERN(s_sprite_size, "sprite_size"); INTERN(s_anchor, "anchor");
    INTERN(s_flip_x, "flip_x"); INTERN(s_flip_y, "flip_y");
    INTERN(s_tint, "tint"); INTERN(s__uv_rect, "_uv_rect");
    INTERN(s__world_transform, "_world_transform"); INTERN(s__world_opacity, "_world_opacity");
    INTERN(s_interactive, "interactive"); INTERN(s__bounds, "_bounds"); INTERN(s__rendered_size, "_rendered_size");
    INTERN(s_nine_size, "nine_size"); INTERN(s_insets, "insets");
    INTERN(s_collision_category, "collision_category"); INTERN(s_collision_mask, "collision_mask");
    INTERN(s_passthrough, "passthrough");
    INTERN(s__collider, "_collider"); INTERN(s_contains_point, "contains_point");
    #undef INTERN
    return 0;
}

static int ensure_types(void) {
    if (types_ready) return 0;
    PyObject *mod = PyImport_ImportModule("scene");
    if (!mod) return -1;
    #define LOAD(var, name) var = PyObject_GetAttrString(mod, name); if (!var) { Py_DECREF(mod); return -1; }
    LOAD(SceneType, "Scene"); LOAD(GroupType, "Group"); LOAD(LayerType, "Layer");
    LOAD(CircleType, "Circle"); LOAD(RectType, "Rect"); LOAD(LineType, "Line");
    LOAD(LabelType, "Label"); LOAD(ImageType, "Image"); LOAD(SpriteType, "Sprite");
    LOAD(ShaderNodeType, "ShaderNode"); LOAD(NineSliceType, "NineSlice");
    LOAD(PathType, "Path"); LOAD(PolygonType, "Polygon");
    LOAD(ParticleEmitterType, "ParticleEmitter");
    #undef LOAD
    Py_DECREF(mod);
    types_ready = 1;
    return 0;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Data structures                                                         */
/* ─────────────────────────────────────────────────────────────────────── */

typedef struct {
    double z;
    int order;
    int kind;
    uint8_t vb[96];        /* 6 vertices × 4 floats × 4 bytes */
    uint8_t qb[64];        /* 4 × float4 × 4 bytes */
    PyObject *texture;     /* borrowed ref for current frame */
    int mesh_batch_idx;    /* index into mesh_batches for KIND_MESH, -1 otherwise */
    int particle_idx;      /* index into particle_emitters for KIND_PARTICLE, -1 otherwise */
} CCmd;

typedef struct {
    double base[7];        /* x, y, rotation, sx, sy, opacity, z */
    double shape[12];      /* type-specific params */
    int shape_len;
    PyObject *colors[3];   /* strong refs to color objects */
    int color_count;
    double parent_tf[6];
    double parent_op;
    double screen_scale;
    double world[6];
    double world_op;
    int cmd_count;
    CCmd cmds[2];          /* for built-in types (always <= 2 cmds) */
    CCmd *dyn_cmds;        /* for unknown types with >2 cmds */
    int dyn_count;
    int dyn_capacity;
    PyObject *snap_cache;  /* _snap() result for unknown type cache validation */
    int valid;
    int type_id;
    /* Collider cache (computed during collect) */
    int coll_type;         /* COLL_CIRCLE(0), COLL_OBB(1), -1=none */
    double coll[6];        /* cx, cy, p0, p1, p2, 0 */
    /* Contains-point cache (local-space) */
    double local_bounds[4]; /* x0, y0, x1, y1 */
    int has_bounds;
    /* Line-specific for contains_point */
    double line_start[2], line_end[2], line_hw;
    /* Path local-space mesh cache */
    PyObject *cached_mesh_list;  /* strong ref to _get_meshes() result */
    double cached_path_version;  /* _path_version when meshes were cached */
    double cached_mesh_scale;   /* pixel scale covered by this mesh list */
} CNodeCache;

typedef struct {
    PyObject *node;
    double z;
    int order;
} InteractiveEntry;

/* Per-mesh-batch info for direct path rendering */
typedef struct {
    int vert_offset;    /* float offset in mesh_verts (= vertex_index * 3) */
    int idx_offset;     /* byte offset in mesh_idx */
    int idx_count;
    int idx_is_32;      /* 0 = uint16, 1 = uint32 */
    float color[4];
    int blend;
    int is_stroke;      /* 1 = use stencil two-pass rendering */
    int fill_rule;      /* 0 = not a fill, 1 = even-odd, 2 = non-zero */
} MeshBatch;

typedef struct {
    CCmd *cmds;
    int count;
    int capacity;
    int order;
    int any_miss;
    int requires_continuous_render;
    unsigned long long fingerprint;
    double screen_scale;
    PyObject *renderer;
    PyObject *render_error; /* owned exception from a Python rendering callback */
    InteractiveEntry *interactive;
    int interactive_count;
    int interactive_capacity;
    PyObject *root;  /* root node (Scene), excluded from interactive */
    /* Direct mesh rendering data */
    float *mesh_verts;
    int mesh_vert_count;
    int mesh_vert_capacity;
    uint8_t *mesh_idx;
    int mesh_idx_bytes;
    int mesh_idx_capacity;
    MeshBatch *mesh_batches;
    int mesh_batch_count;
    int mesh_batch_capacity;
    /* Particle emitters (Python refs stored for render_packed) */
    PyObject **particle_emitters;
    int particle_count;
    int particle_capacity;
} CollectState;

static inline unsigned long long fingerprint_mix(unsigned long long h, unsigned long long v) {
    h ^= v + 0x9e3779b97f4a7c15ULL + (h << 6) + (h >> 2);
    return h;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Helper: read attributes                                                 */
/* ─────────────────────────────────────────────────────────────────────── */

static inline double read_double(PyObject *obj, PyObject *attr) {
    PyObject *v = PyObject_GetAttr(obj, attr);
    if (!v) { PyErr_Clear(); return 0.0; }
    double r = PyFloat_AsDouble(v);
    if (r == -1.0 && PyErr_Occurred()) { PyErr_Clear(); r = 0.0; }
    Py_DECREF(v);
    return r;
}

static inline int read_bool(PyObject *obj, PyObject *attr) {
    PyObject *v = PyObject_GetAttr(obj, attr);
    if (!v) { PyErr_Clear(); return 0; }
    int r = PyObject_IsTrue(v);
    Py_DECREF(v);
    return r > 0;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Math (pure C, no Python objects)                                        */
/* ─────────────────────────────────────────────────────────────────────── */

static void c_matrix(double x, double y, double rot, double sx, double sy, double out[6]) {
    double c = cos(rot), s = sin(rot);
    out[0] = c*sx; out[1] = s*sx; out[2] = -s*sy; out[3] = c*sy; out[4] = x; out[5] = y;
}

static void c_mul(const double a[6], const double b[6], double out[6]) {
    out[0] = a[0]*b[0] + a[2]*b[1];
    out[1] = a[1]*b[0] + a[3]*b[1];
    out[2] = a[0]*b[2] + a[2]*b[3];
    out[3] = a[1]*b[2] + a[3]*b[3];
    out[4] = a[0]*b[4] + a[2]*b[5] + a[4];
    out[5] = a[1]*b[4] + a[3]*b[5] + a[5];
}

static inline void c_apply(const double m[6], double px, double py, double *ox, double *oy) {
    *ox = m[0]*px + m[2]*py + m[4];
    *oy = m[1]*px + m[3]*py + m[5];
}

static inline double c_avg_scale(const double m[6]) {
    return 0.5 * (hypot(m[0], m[1]) + hypot(m[2], m[3]));
}

static double c_max_scale(const double m[6]) {
    double peak = std::max({fabs(m[0]), fabs(m[1]), fabs(m[2]), fabs(m[3])});
    if (peak == 0.0 || !isfinite(peak)) return peak;
    double a = m[0] / peak, b = m[1] / peak, c = m[2] / peak, d = m[3] / peak;
    return peak * (0.5 * hypot(a + d, b - c) + 0.5 * hypot(a - d, b + c));
}

static bool path_scale_usable(double cached, double required) {
    /* Match _usable_scale in _path_node.py, including shrink hysteresis. */
    return isfinite(required) && required <= cached * (1.0 + 1e-12) && required >= cached * 0.25;
}

static inline double c_rot(const double m[6]) {
    return atan2(m[1], m[0]);
}

static inline double c_feather(double s) {
    double mx = s > 1.0 ? s : 1.0;
    double v = 0.95 / mx;
    return v > 0.45 ? v : 0.45;
}

static inline void c_invert(const double m[6], double out[6]) {
    double det = m[0]*m[3] - m[1]*m[2];
    if (fabs(det) < 1e-12) {
        out[0]=1; out[1]=0; out[2]=0; out[3]=1; out[4]=0; out[5]=0;
        return;
    }
    double inv = 1.0 / det;
    out[0] = m[3]*inv;  out[1] = -m[1]*inv;
    out[2] = -m[2]*inv; out[3] = m[0]*inv;
    out[4] = (m[2]*m[5] - m[3]*m[4])*inv;
    out[5] = (m[1]*m[4] - m[0]*m[5])*inv;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Color parsing (C reimplementation of gpu.normalize_color)               */
/* ─────────────────────────────────────────────────────────────────────── */

static int hex_val(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* Parse color into float RGBA [0-1].  Returns 1 on success, 0 on failure. */
static int c_parse_color(PyObject *obj, float rgba[4]) {
    if (obj == Py_None) return 0;

    if (PyUnicode_Check(obj)) {
        const char *s = PyUnicode_AsUTF8(obj);
        if (!s) { PyErr_Clear(); return 0; }
        /* skip '#' and whitespace */
        while (*s == ' ' || *s == '#') s++;
        int len = (int)strlen(s);
        int r, g, b, a = 255;
        if (len == 3) {
            r = hex_val(s[0]); g = hex_val(s[1]); b = hex_val(s[2]);
            if (r < 0 || g < 0 || b < 0) return 0;
            r = r*16+r; g = g*16+g; b = b*16+b;
        } else if (len == 4) {
            r = hex_val(s[0]); g = hex_val(s[1]); b = hex_val(s[2]); int a1 = hex_val(s[3]);
            if (r < 0 || g < 0 || b < 0 || a1 < 0) return 0;
            r = r*16+r; g = g*16+g; b = b*16+b; a = a1*16+a1;
        } else if (len == 6) {
            int r1=hex_val(s[0]),r2=hex_val(s[1]),g1=hex_val(s[2]),g2=hex_val(s[3]),b1=hex_val(s[4]),b2=hex_val(s[5]);
            if (r1<0||r2<0||g1<0||g2<0||b1<0||b2<0) return 0;
            r=r1*16+r2; g=g1*16+g2; b=b1*16+b2;
        } else if (len == 8) {
            int r1=hex_val(s[0]),r2=hex_val(s[1]),g1=hex_val(s[2]),g2=hex_val(s[3]);
            int b1=hex_val(s[4]),b2=hex_val(s[5]),a1=hex_val(s[6]),a2=hex_val(s[7]);
            if (r1<0||r2<0||g1<0||g2<0||b1<0||b2<0||a1<0||a2<0) return 0;
            r=r1*16+r2; g=g1*16+g2; b=b1*16+b2; a=a1*16+a2;
        } else {
            return 0;
        }
        rgba[0] = r / 255.0f; rgba[1] = g / 255.0f; rgba[2] = b / 255.0f; rgba[3] = a / 255.0f;
        return 1;
    }

    /* tuple/list (r,g,b) or (r,g,b,a) in 0-1 range */
    Py_ssize_t n = PySequence_Size(obj);
    if (n < 3) return 0;
    auto clamp = [](double v) -> float { return v < 0.0 ? 0.0f : (v > 1.0 ? 1.0f : (float)v); };
    PyObject *items[4];
    for (Py_ssize_t i = 0; i < (n >= 4 ? 4 : 3); i++) {
        items[i] = PySequence_GetItem(obj, i);
        if (!items[i]) { PyErr_Clear(); return 0; }
    }
    rgba[0] = clamp(PyFloat_AsDouble(items[0]));
    rgba[1] = clamp(PyFloat_AsDouble(items[1]));
    rgba[2] = clamp(PyFloat_AsDouble(items[2]));
    rgba[3] = n >= 4 ? clamp(PyFloat_AsDouble(items[3])) : 1.0f;
    for (Py_ssize_t i = 0; i < (n >= 4 ? 4 : 3); i++) Py_DECREF(items[i]);
    if (PyErr_Occurred()) { PyErr_Clear(); return 0; }
    return 1;
}

/* Pack color into uint32 for fast comparison */
static inline uint32_t color_pack(const float rgba[4]) {
    return ((uint32_t)(rgba[0]*255.0f+0.5f) << 24) |
           ((uint32_t)(rgba[1]*255.0f+0.5f) << 16) |
           ((uint32_t)(rgba[2]*255.0f+0.5f) << 8)  |
           ((uint32_t)(rgba[3]*255.0f+0.5f));
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Path geometry helpers                                                  */
/* ─────────────────────────────────────────────────────────────────────── */

struct PathPoint {
    double x;
    double y;
};

struct PathSubpath {
    std::vector<PathPoint> points;
    bool closed = false;
};

struct PathMesh {
    std::vector<float> vertices;
    std::vector<uint32_t> indices;
    float color[4];
    bool blend = true;
};

static inline double path_dist_sq(const PathPoint &a, const PathPoint &b) {
    double dx = a.x - b.x, dy = a.y - b.y;
    return dx * dx + dy * dy;
}

static inline PathPoint path_mid(const PathPoint &a, const PathPoint &b) {
    return {(a.x + b.x) * 0.5, (a.y + b.y) * 0.5};
}

static inline double path_cross(const PathPoint &a, const PathPoint &b, const PathPoint &c) {
    return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
}

static double path_distance_to_chord(const PathPoint &p, const PathPoint &a, const PathPoint &b) {
    double dx = b.x - a.x, dy = b.y - a.y;
    double ln = hypot(dx, dy);
    if (ln < 1e-12) return hypot(p.x - a.x, p.y - a.y);
    double ux = dx / ln, uy = dy / ln;
    double projection = (p.x - a.x) * ux + (p.y - a.y) * uy;
    if (projection <= 0.0) return hypot(p.x - a.x, p.y - a.y);
    if (projection >= ln) return hypot(p.x - b.x, p.y - b.y);
    return fabs((p.x - a.x) * uy - (p.y - a.y) * ux);
}

static inline bool quad_flat_enough(const PathPoint &p0, const PathPoint &p1, const PathPoint &p2, double tol) {
    return path_distance_to_chord(p1, p0, p2) <= tol;
}

static inline bool cubic_flat_enough(const PathPoint &p0, const PathPoint &p1, const PathPoint &p2, const PathPoint &p3, double tol) {
    return std::max(path_distance_to_chord(p1, p0, p3), path_distance_to_chord(p2, p0, p3)) <= tol;
}

static void flatten_quad(const PathPoint &p0, const PathPoint &p1, const PathPoint &p2, double tol, int depth, std::vector<PathPoint> &out) {
    if (depth <= 0 || quad_flat_enough(p0, p1, p2, tol)) {
        out.push_back(p2);
        return;
    }
    PathPoint p01 = path_mid(p0, p1);
    PathPoint p12 = path_mid(p1, p2);
    PathPoint p012 = path_mid(p01, p12);
    flatten_quad(p0, p01, p012, tol, depth - 1, out);
    flatten_quad(p012, p12, p2, tol, depth - 1, out);
}

static void flatten_cubic(const PathPoint &p0, const PathPoint &p1, const PathPoint &p2, const PathPoint &p3,
                          double tol, int depth, std::vector<PathPoint> &out) {
    if (depth <= 0 || cubic_flat_enough(p0, p1, p2, p3, tol)) {
        out.push_back(p3);
        return;
    }
    PathPoint p01 = path_mid(p0, p1);
    PathPoint p12 = path_mid(p1, p2);
    PathPoint p23 = path_mid(p2, p3);
    PathPoint p012 = path_mid(p01, p12);
    PathPoint p123 = path_mid(p12, p23);
    PathPoint p0123 = path_mid(p012, p123);
    flatten_cubic(p0, p01, p012, p0123, tol, depth - 1, out);
    flatten_cubic(p0123, p123, p23, p3, tol, depth - 1, out);
}

static std::vector<PathPoint> dedupe_points(const std::vector<PathPoint> &points) {
    std::vector<PathPoint> out;
    out.reserve(points.size());
    for (const PathPoint &pt : points) {
        if (out.empty() || path_dist_sq(out.back(), pt) > 1e-12) out.push_back(pt);
    }
    if (out.size() > 1 && path_dist_sq(out.front(), out.back()) <= 1e-12) out.back() = out.front();
    return out;
}

static bool point_in_polygon(const std::vector<PathPoint> &poly, double x, double y) {
    bool inside = false;
    const size_t n = poly.size();
    for (size_t i = 0; i < n; i++) {
        const PathPoint &a = poly[i];
        const PathPoint &b = poly[(i + 1) % n];
        if ((a.y > y) != (b.y > y)) {
            double xint = a.x + (y - a.y) * (b.x - a.x) / (b.y - a.y);
            if (xint > x) inside = !inside;
        }
    }
    return inside;
}

static int polygon_winding(const std::vector<PathPoint> &poly, double x, double y) {
    int winding = 0;
    const size_t n = poly.size();
    for (size_t i = 0; i < n; i++) {
        const PathPoint &a = poly[i];
        const PathPoint &b = poly[(i + 1) % n];
        if (a.y <= y) {
            if (b.y > y && path_cross(a, b, {x, y}) > 0) winding += 1;
        } else if (b.y <= y && path_cross(a, b, {x, y}) < 0) {
            winding -= 1;
        }
    }
    return winding;
}

static inline int normalize_fill_rule(const char *s) {
    if (!s) return 0;
    return strcmp(s, "non_zero") == 0 ? 1 : 0; /* 0 = even_odd */
}

static void append_triangle(std::vector<std::array<PathPoint, 3>> &tris, const PathPoint &a, const PathPoint &b, const PathPoint &c) {
    tris.push_back({a, b, c});
}

static PathPoint line_intersection(const PathPoint &a0, const PathPoint &a1, const PathPoint &b0, const PathPoint &b1, bool *ok) {
    double dax = a1.x - a0.x, day = a1.y - a0.y;
    double dbx = b1.x - b0.x, dby = b1.y - b0.y;
    double den = dax * dby - day * dbx;
    if (fabs(den) < 1e-12) {
        *ok = false;
        return {0.0, 0.0};
    }
    double dx = b0.x - a0.x, dy = b0.y - a0.y;
    double t = (dx * dby - dy * dbx) / den;
    *ok = true;
    return {a0.x + t * dax, a0.y + t * day};
}

static int path_arc_steps(double span, double radius, int minimum, double tolerance) {
    double angle = M_PI / 10.0;
    if (tolerance > 0.0 && radius > 0.0) {
        /* Sagitta bound, evaluated without subtracting nearly equal numbers. */
        angle = std::min(angle, 4.0 * asin(sqrt(std::min(1.0, tolerance / radius) * 0.5)));
    }
    if (angle <= fabs(span) / 4096.0) return 4096;
    return std::max(minimum, (int)ceil(fabs(span) / angle));
}

static void append_round_join(std::vector<std::array<PathPoint, 3>> &tris, const PathPoint &center,
                              const PathPoint &start_outer, const PathPoint &end_outer, double radius, bool clockwise,
                              double tolerance) {
    double a0 = atan2(start_outer.y - center.y, start_outer.x - center.x);
    double a1 = atan2(end_outer.y - center.y, end_outer.x - center.x);
    if (clockwise) while (a1 > a0) a1 -= M_PI * 2.0;
    else while (a1 < a0) a1 += M_PI * 2.0;
    double span = a1 - a0;
    if (fabs(span) < 1e-6) return;
    int steps = path_arc_steps(span, radius, 3, tolerance);
    PathPoint prev = start_outer;
    for (int i = 1; i <= steps; i++) {
        PathPoint cur;
        if (i == steps) {
            cur = end_outer;
        } else {
            double ang = a0 + span * ((double)i / (double)steps);
            cur = {center.x + cos(ang) * radius, center.y + sin(ang) * radius};
        }
        append_triangle(tris, center, prev, cur);
        prev = cur;
    }
}

static void append_cap(std::vector<std::array<PathPoint, 3>> &tris, const PathPoint &point, const PathPoint &direction, double hw,
                       const PathPoint &left, const PathPoint &right, const char *cap, bool start, double tolerance) {
    if (strcmp(cap, "butt") == 0) return;
    if (strcmp(cap, "square") == 0) {
        double sign = start ? -1.0 : 1.0;
        double ex = direction.x * hw * sign, ey = direction.y * hw * sign;
        PathPoint l2{left.x + ex, left.y + ey}, r2{right.x + ex, right.y + ey};
        append_triangle(tris, left, l2, right);
        append_triangle(tris, right, l2, r2);
        return;
    }
    double a0 = atan2(right.y - point.y, right.x - point.x);
    double a1 = atan2(left.y - point.y, left.x - point.x);
    if (start) while (a1 > a0) a1 -= M_PI * 2.0;
    else while (a1 < a0) a1 += M_PI * 2.0;
    double span = a1 - a0;
    int steps = path_arc_steps(span, hw, 6, tolerance);
    PathPoint prev = right;
    for (int i = 1; i <= steps; i++) {
        PathPoint cur;
        if (i == steps) {
            cur = left;
        } else {
            double ang = a0 + span * ((double)i / (double)steps);
            cur = {point.x + cos(ang) * hw, point.y + sin(ang) * hw};
        }
        append_triangle(tris, point, prev, cur);
        prev = cur;
    }
}

static const char *normalize_join_c(const char *join) {
    if (!join) return "round";
    if (strcmp(join, "miter") == 0 || strcmp(join, "bevel") == 0 || strcmp(join, "round") == 0) return join;
    return "round";
}

static const char *normalize_cap_c(const char *cap) {
    if (!cap) return "round";
    if (strcmp(cap, "butt") == 0 || strcmp(cap, "square") == 0 || strcmp(cap, "round") == 0) return cap;
    return "round";
}

static std::vector<std::array<PathPoint, 3>> stroke_polyline_c(const std::vector<PathPoint> &src_points, bool closed, double width,
                                                               const char *join, const char *cap, double miter_limit,
                                                               double tolerance = 0.0) {
    std::vector<std::array<PathPoint, 3>> triangles;
    if (src_points.size() < 2 || width <= 0.0) return triangles;
    join = normalize_join_c(join);
    cap = normalize_cap_c(cap);
    double hw = width * 0.5;
    std::vector<PathPoint> points = src_points;
    int n = (int)points.size();
    if (closed && n > 2 && path_dist_sq(points.front(), points.back()) <= 1e-12) {
        points.pop_back();
        n = (int)points.size();
    }
    if (n < 2) return triangles;

    std::vector<PathPoint> dirs;
    std::vector<PathPoint> norms;
    dirs.reserve(closed ? n : n - 1);
    norms.reserve(closed ? n : n - 1);
    int seg_count = closed ? n : n - 1;
    for (int i = 0; i < seg_count; i++) {
        PathPoint p0 = points[i], p1 = points[(i + 1) % n];
        double dx = p1.x - p0.x, dy = p1.y - p0.y;
        double ln = hypot(dx, dy);
        PathPoint dir{1.0, 0.0}, norm{0.0, 1.0};
        if (ln >= 1e-12) {
            dir = {dx / ln, dy / ln};
            norm = {-dir.y, dir.x};
        }
        dirs.push_back(dir);
        norms.push_back(norm);
        PathPoint l0{p0.x - dir.y * hw, p0.y + dir.x * hw};
        PathPoint r0{p0.x + dir.y * hw, p0.y - dir.x * hw};
        PathPoint l1{p1.x - dir.y * hw, p1.y + dir.x * hw};
        PathPoint r1{p1.x + dir.y * hw, p1.y - dir.x * hw};
        append_triangle(triangles, l0, l1, r0);
        append_triangle(triangles, r0, l1, r1);
    }

    for (int i = closed ? 0 : 1; i < (closed ? n : n - 1); i++) {
        int prev_i = (i - 1 + seg_count) % seg_count;
        int next_i = i % seg_count;
        PathPoint prev_dir = dirs[prev_i], next_dir = dirs[next_i];
        double turn = prev_dir.x * next_dir.y - prev_dir.y * next_dir.x;
        if (fabs(turn) < 1e-8) continue;
        PathPoint point = points[i];
        PathPoint prev_norm = norms[prev_i], next_norm = norms[next_i];
        /* Segment quads overlap on the inside; only the outside needs a join. */
        if (turn > 0) {
            /* Outer gap on - normal side */
            PathPoint outer_prev{point.x - prev_norm.x * hw, point.y - prev_norm.y * hw};
            PathPoint outer_next{point.x - next_norm.x * hw, point.y - next_norm.y * hw};
            if (strcmp(join, "miter") == 0) {
                /* Cover the base wedge as well as the optional miter extension. */
                append_triangle(triangles, point, outer_prev, outer_next);
                bool ok = false;
                PathPoint inter = line_intersection(outer_prev, {outer_prev.x + prev_dir.x, outer_prev.y + prev_dir.y},
                                                    outer_next, {outer_next.x + next_dir.x, outer_next.y + next_dir.y}, &ok);
                if (ok && hypot(inter.x - point.x, inter.y - point.y) <= hw * std::max(1.0, miter_limit))
                    append_triangle(triangles, outer_prev, inter, outer_next);
            } else if (strcmp(join, "round") == 0) {
                append_round_join(triangles, point, outer_prev, outer_next, hw, false, tolerance);
            } else {
                append_triangle(triangles, point, outer_prev, outer_next);
            }
        } else {
            /* Outer gap on + normal side */
            PathPoint outer_prev{point.x + prev_norm.x * hw, point.y + prev_norm.y * hw};
            PathPoint outer_next{point.x + next_norm.x * hw, point.y + next_norm.y * hw};
            if (strcmp(join, "miter") == 0) {
                append_triangle(triangles, point, outer_next, outer_prev);
                bool ok = false;
                PathPoint inter = line_intersection(outer_prev, {outer_prev.x + prev_dir.x, outer_prev.y + prev_dir.y},
                                                    outer_next, {outer_next.x + next_dir.x, outer_next.y + next_dir.y}, &ok);
                if (ok && hypot(inter.x - point.x, inter.y - point.y) <= hw * std::max(1.0, miter_limit))
                    append_triangle(triangles, outer_prev, outer_next, inter);
            } else if (strcmp(join, "round") == 0) {
                append_round_join(triangles, point, outer_prev, outer_next, hw, true, tolerance);
            } else {
                append_triangle(triangles, point, outer_next, outer_prev);
            }
        }
    }

    if (!closed) {
        PathPoint first_norm = norms.front(), last_norm = norms.back();
        PathPoint left0{points.front().x + first_norm.x * hw, points.front().y + first_norm.y * hw};
        PathPoint right0{points.front().x - first_norm.x * hw, points.front().y - first_norm.y * hw};
        PathPoint left1{points.back().x + last_norm.x * hw, points.back().y + last_norm.y * hw};
        PathPoint right1{points.back().x - last_norm.x * hw, points.back().y - last_norm.y * hw};
        append_cap(triangles, points.front(), dirs.front(), hw, left0, right0, cap, true, tolerance);
        append_cap(triangles, points.back(), dirs.back(), hw, left1, right1, cap, false, tolerance);
    }

    return triangles;
}

static double distance_sq_to_segment(const PathPoint &p, const PathPoint &a, const PathPoint &b) {
    double dx = b.x - a.x, dy = b.y - a.y;
    double ls = dx * dx + dy * dy;
    if (ls < 1e-12) return path_dist_sq(p, a);
    double t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / ls;
    if (t < 0.0) t = 0.0;
    else if (t > 1.0) t = 1.0;
    PathPoint q{a.x + dx * t, a.y + dy * t};
    return path_dist_sq(p, q);
}

static bool polyline_hit(const std::vector<PathPoint> &points, double x, double y, double rr, bool closed, const char *cap) {
    int n = (int)points.size();
    int seg_count = closed ? n : n - 1;
    PathPoint p{x, y};
    for (int i = 0; i < seg_count; i++) {
        const PathPoint &a = points[i];
        const PathPoint &b = points[(i + 1) % n];
        if (distance_sq_to_segment(p, a, b) <= rr) return true;
    }
    if (closed) return false;
    if (strcmp(cap, "round") == 0 || strcmp(cap, "square") == 0) {
        if (path_dist_sq(p, points.front()) <= rr || path_dist_sq(p, points.back()) <= rr) return true;
    }
    return false;
}

static bool parse_path_subpaths(PyObject *obj, std::vector<PathSubpath> &out) {
    PyObject *seq = PySequence_Fast(obj, "subpaths must be a sequence");
    if (!seq) return false;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(seq);
    PyObject **items = PySequence_Fast_ITEMS(seq);
    out.clear();
    out.reserve((size_t)n);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *sub = items[i];
        PyObject *pts_obj = NULL;
        PyObject *closed_obj = NULL;
        if (PyDict_Check(sub)) {
            pts_obj = PyDict_GetItemString(sub, "points");
            closed_obj = PyDict_GetItemString(sub, "closed");
            Py_XINCREF(pts_obj);
            Py_XINCREF(closed_obj);
        } else if (PyTuple_Check(sub) && PyTuple_GET_SIZE(sub) >= 2) {
            pts_obj = PyTuple_GET_ITEM(sub, 0); Py_INCREF(pts_obj);
            closed_obj = PyTuple_GET_ITEM(sub, 1); Py_INCREF(closed_obj);
        }
        if (!pts_obj || !closed_obj) {
            Py_XDECREF(pts_obj); Py_XDECREF(closed_obj); Py_DECREF(seq);
            PyErr_SetString(PyExc_TypeError, "subpath must contain 'points' and 'closed'");
            return false;
        }
        PyObject *pts_fast = PySequence_Fast(pts_obj, "points must be a sequence");
        if (!pts_fast) { Py_DECREF(pts_obj); Py_DECREF(closed_obj); Py_DECREF(seq); return false; }
        PathSubpath parsed;
        parsed.closed = PyObject_IsTrue(closed_obj) > 0;
        Py_ssize_t pn = PySequence_Fast_GET_SIZE(pts_fast);
        PyObject **pitems = PySequence_Fast_ITEMS(pts_fast);
        parsed.points.reserve((size_t)pn);
        for (Py_ssize_t j = 0; j < pn; j++) {
            PyObject *pt = pitems[j];
            if (!PySequence_Check(pt) || PySequence_Size(pt) < 2) {
                Py_DECREF(pts_fast); Py_DECREF(pts_obj); Py_DECREF(closed_obj); Py_DECREF(seq);
                PyErr_SetString(PyExc_TypeError, "path point must be a 2-tuple");
                return false;
            }
            PyObject *x = PySequence_GetItem(pt, 0);
            PyObject *y = PySequence_GetItem(pt, 1);
            double px = PyFloat_AsDouble(x);
            double py = PyFloat_AsDouble(y);
            Py_XDECREF(x); Py_XDECREF(y);
            if (PyErr_Occurred()) {
                Py_DECREF(pts_fast); Py_DECREF(pts_obj); Py_DECREF(closed_obj); Py_DECREF(seq);
                return false;
            }
            parsed.points.push_back({px, py});
        }
        out.push_back(std::move(parsed));
        Py_DECREF(pts_fast);
        Py_DECREF(pts_obj);
        Py_DECREF(closed_obj);
    }
    Py_DECREF(seq);
    return true;
}

static PyObject *build_subpaths_py(const std::vector<PathSubpath> &subpaths) {
    PyObject *list = PyList_New((Py_ssize_t)subpaths.size());
    if (!list) return NULL;
    for (Py_ssize_t i = 0; i < (Py_ssize_t)subpaths.size(); i++) {
        const auto &sub = subpaths[(size_t)i];
        PyObject *dict = PyDict_New();
        PyObject *pts = PyList_New((Py_ssize_t)sub.points.size());
        PyObject *closed = PyBool_FromLong(sub.closed ? 1 : 0);
        if (!dict || !pts || !closed) { Py_XDECREF(dict); Py_XDECREF(pts); Py_XDECREF(closed); Py_DECREF(list); return NULL; }
        for (Py_ssize_t j = 0; j < (Py_ssize_t)sub.points.size(); j++) {
            const auto &p = sub.points[(size_t)j];
            PyObject *pt = Py_BuildValue("(dd)", p.x, p.y);
            if (!pt) { Py_DECREF(dict); Py_DECREF(pts); Py_DECREF(closed); Py_DECREF(list); return NULL; }
            PyList_SET_ITEM(pts, j, pt);
        }
        PyDict_SetItemString(dict, "points", pts);
        PyDict_SetItemString(dict, "closed", closed);
        Py_DECREF(pts);
        Py_DECREF(closed);
        PyList_SET_ITEM(list, i, dict);
    }
    return list;
}

static PyObject *build_meshes_py(const std::vector<PathMesh> &meshes) {
    PyObject *list = PyList_New((Py_ssize_t)meshes.size());
    if (!list) return NULL;
    for (Py_ssize_t i = 0; i < (Py_ssize_t)meshes.size(); i++) {
        const auto &mesh = meshes[(size_t)i];
        PyObject *vbytes = PyBytes_FromStringAndSize((const char *)mesh.vertices.data(), (Py_ssize_t)(mesh.vertices.size() * sizeof(float)));
        Py_ssize_t isize = mesh.indices.size() <= 0xFFFF ? (Py_ssize_t)(mesh.indices.size() * sizeof(uint16_t)) : (Py_ssize_t)(mesh.indices.size() * sizeof(uint32_t));
        PyObject *ibytes = PyBytes_FromStringAndSize(NULL, isize);
        PyObject *color = Py_BuildValue("(ffff)", mesh.color[0], mesh.color[1], mesh.color[2], mesh.color[3]);
        PyObject *itype = PyUnicode_FromString(mesh.indices.size() <= 0xFFFF ? "uint16" : "uint32");
        PyObject *blend = PyBool_FromLong(mesh.blend ? 1 : 0);
        if (!vbytes || !ibytes || !color || !itype || !blend) {
            Py_XDECREF(vbytes); Py_XDECREF(ibytes); Py_XDECREF(color); Py_XDECREF(itype); Py_XDECREF(blend); Py_DECREF(list); return NULL;
        }
        if (mesh.indices.size() <= 0xFFFF) {
            uint16_t *dst = (uint16_t *)PyBytes_AS_STRING(ibytes);
            for (size_t j = 0; j < mesh.indices.size(); j++) dst[j] = (uint16_t)mesh.indices[j];
        } else {
            memcpy(PyBytes_AS_STRING(ibytes), mesh.indices.data(), mesh.indices.size() * sizeof(uint32_t));
        }
        PyObject *item = PyTuple_New(7);
        if (!item) {
            Py_DECREF(vbytes); Py_DECREF(ibytes); Py_DECREF(itype); Py_DECREF(color); Py_DECREF(blend); Py_DECREF(list); return NULL;
        }
        PyTuple_SET_ITEM(item, 0, vbytes);
        PyTuple_SET_ITEM(item, 1, PyLong_FromSsize_t((Py_ssize_t)(mesh.vertices.size() / 3)));
        PyTuple_SET_ITEM(item, 2, ibytes);
        PyTuple_SET_ITEM(item, 3, PyLong_FromSsize_t((Py_ssize_t)mesh.indices.size()));
        PyTuple_SET_ITEM(item, 4, itype);
        PyTuple_SET_ITEM(item, 5, color);
        PyTuple_SET_ITEM(item, 6, blend);
        for (int j = 0; j < 7; j++) {
            if (PyTuple_GET_ITEM(item, j) == NULL) { Py_DECREF(item); Py_DECREF(list); return NULL; }
        }
        PyList_SET_ITEM(list, i, item);
    }
    return list;
}

static void triangles_to_mesh(const std::vector<std::array<PathPoint, 3>> &tris, const float color[4], bool blend, PathMesh &out,
                              float ox = 0.0f, float oy = 0.0f) {
    out.vertices.clear();
    out.indices.clear();
    out.blend = blend;
    memcpy(out.color, color, sizeof(float) * 4);
    out.vertices.reserve(tris.size() * 9);  /* 3 floats × 3 verts per tri */
    out.indices.reserve(tris.size() * 3);
    uint32_t index = 0;
    for (const auto &tri : tris) {
        for (int k = 0; k < 3; k++) {
            out.vertices.push_back((float)tri[k].x + ox);
            out.vertices.push_back((float)tri[k].y + oy);
            out.vertices.push_back(1.0f);  /* alpha = 1.0 for interior */
        }
        out.indices.push_back(index++);
        out.indices.push_back(index++);
        out.indices.push_back(index++);
    }
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Vertex / quad data packing                                              */
/* ─────────────────────────────────────────────────────────────────────── */

static void pack_verts(float cx, float cy, float hw_, float hh_, float rot, uint8_t out[96]) {
    float c = cosf(rot), s = sinf(rot);
    float v[24];
    /* tl */ v[0]=cx+c*(-hw_)-s*(-hh_); v[1]=cy+s*(-hw_)+c*(-hh_); v[2]=0; v[3]=0;
    /* tr */ v[4]=cx+c*(hw_)-s*(-hh_);  v[5]=cy+s*(hw_)+c*(-hh_);  v[6]=1; v[7]=0;
    /* bl */ v[8]=cx+c*(-hw_)-s*(hh_);  v[9]=cy+s*(-hw_)+c*(hh_); v[10]=0;v[11]=1;
    /* tr */ v[12]=v[4]; v[13]=v[5]; v[14]=1; v[15]=0;
    /* br */ v[16]=cx+c*(hw_)-s*(hh_); v[17]=cy+s*(hw_)+c*(hh_); v[18]=1; v[19]=1;
    /* bl */ v[20]=v[8]; v[21]=v[9]; v[22]=0; v[23]=1;
    memcpy(out, v, 96);
}

static void pack_quad(const float params[4], const float style[4],
                      const float fill_[4], const float extra[4], uint8_t out[64]) {
    memcpy(out,      params, 16);
    memcpy(out + 16, style,  16);
    memcpy(out + 32, fill_,  16);
    memcpy(out + 48, extra,  16);
}

/* ─────────────────────────────────────────────────────────────────────── */
/* CollectState                                                            */
/* ─────────────────────────────────────────────────────────────────────── */

static void state_init(CollectState *st, double screen_scale, PyObject *renderer, PyObject *root) {
    st->capacity = 256;
    st->cmds = (CCmd *)malloc(sizeof(CCmd) * st->capacity);
    st->count = 0;
    st->order = 0;
    st->any_miss = 0;
    st->requires_continuous_render = 0;
    st->fingerprint = 0xcbf29ce484222325ULL;
    st->screen_scale = screen_scale;
    st->renderer = renderer;
    st->render_error = NULL;
    st->interactive_capacity = 64;
    st->interactive = (InteractiveEntry *)malloc(sizeof(InteractiveEntry) * st->interactive_capacity);
    st->interactive_count = 0;
    st->root = root;
    st->mesh_vert_capacity = 1024;
    st->mesh_verts = (float *)malloc(sizeof(float) * st->mesh_vert_capacity);
    st->mesh_vert_count = 0;
    st->mesh_idx_capacity = 4096;
    st->mesh_idx = (uint8_t *)malloc(st->mesh_idx_capacity);
    st->mesh_idx_bytes = 0;
    st->mesh_batch_capacity = 64;
    st->mesh_batches = (MeshBatch *)malloc(sizeof(MeshBatch) * st->mesh_batch_capacity);
    st->mesh_batch_count = 0;
    st->particle_capacity = 16;
    st->particle_emitters = (PyObject **)malloc(sizeof(PyObject *) * st->particle_capacity);
    st->particle_count = 0;
}

static CCmd *state_append(CollectState *st) {
    if (st->count >= st->capacity) {
        st->capacity *= 2;
        st->cmds = (CCmd *)realloc(st->cmds, sizeof(CCmd) * st->capacity);
    }
    return &st->cmds[st->count++];
}

static void state_destroy(CollectState *st) {
    Py_XDECREF(st->render_error);
    st->render_error = NULL;
    free(st->cmds);
    st->cmds = NULL;
    free(st->interactive);
    st->interactive = NULL;
    free(st->mesh_verts);
    st->mesh_verts = NULL;
    free(st->mesh_idx);
    st->mesh_idx = NULL;
    free(st->mesh_batches);
    st->mesh_batches = NULL;
    free(st->particle_emitters);
    st->particle_emitters = NULL;
}

static bool state_take_render_error(CollectState *st) {
    if (!PyErr_Occurred()) return false;
    if (!st->render_error) st->render_error = PyErr_GetRaisedException();
    else PyErr_Clear();
    return true;
}

/* Append a direct mesh (screen-space vertices, 3 floats per vertex: x, y, alpha) to CollectState */
static void state_append_mesh(CollectState *st,
                              const float *verts, int vert_count,
                              const uint8_t *idx_bytes, int idx_byte_count,
                              int idx_count, int idx_is_32,
                              const float color[4], int blend, int is_stroke, int fill_rule) {
    /* Grow vertex buffer — 3 floats per vertex (x, y, alpha) */
    int need_floats = vert_count * 3;
    while (st->mesh_vert_count + need_floats > st->mesh_vert_capacity) {
        st->mesh_vert_capacity *= 2;
        st->mesh_verts = (float *)realloc(st->mesh_verts, sizeof(float) * st->mesh_vert_capacity);
    }
    int vert_float_offset = st->mesh_vert_count;
    memcpy(st->mesh_verts + vert_float_offset, verts, need_floats * sizeof(float));
    st->mesh_vert_count += need_floats;

    /* Grow index buffer */
    while (st->mesh_idx_bytes + idx_byte_count > st->mesh_idx_capacity) {
        st->mesh_idx_capacity *= 2;
        st->mesh_idx = (uint8_t *)realloc(st->mesh_idx, st->mesh_idx_capacity);
    }
    int idx_byte_offset = st->mesh_idx_bytes;

    /* Copy indices with vertex offset adjustment (3 floats per vertex) */
    int base_vertex = vert_float_offset / 3;
    if (idx_is_32) {
        uint32_t *dst = (uint32_t *)(st->mesh_idx + idx_byte_offset);
        const uint32_t *src = (const uint32_t *)idx_bytes;
        for (int i = 0; i < idx_count; i++) dst[i] = src[i] + (uint32_t)base_vertex;
    } else {
        /* uint16 input → output as uint32 for uniform handling */
        int out_bytes = idx_count * sizeof(uint32_t);
        while (st->mesh_idx_bytes + out_bytes > st->mesh_idx_capacity) {
            st->mesh_idx_capacity *= 2;
            st->mesh_idx = (uint8_t *)realloc(st->mesh_idx, st->mesh_idx_capacity);
        }
        uint32_t *dst = (uint32_t *)(st->mesh_idx + idx_byte_offset);
        const uint16_t *src = (const uint16_t *)idx_bytes;
        for (int i = 0; i < idx_count; i++) dst[i] = (uint32_t)src[i] + (uint32_t)base_vertex;
        idx_byte_count = out_bytes;
        idx_is_32 = 1;
    }
    st->mesh_idx_bytes += idx_byte_count;

    /* Add batch */
    if (st->mesh_batch_count >= st->mesh_batch_capacity) {
        st->mesh_batch_capacity *= 2;
        st->mesh_batches = (MeshBatch *)realloc(st->mesh_batches, sizeof(MeshBatch) * st->mesh_batch_capacity);
    }
    MeshBatch *mb = &st->mesh_batches[st->mesh_batch_count++];
    mb->vert_offset = vert_float_offset;
    mb->idx_offset = idx_byte_offset;
    mb->idx_count = idx_count;
    mb->idx_is_32 = idx_is_32;
    memcpy(mb->color, color, sizeof(float) * 4);
    mb->blend = blend;
    mb->is_stroke = is_stroke;
    mb->fill_rule = fill_rule;
}

static void state_add_interactive(CollectState *st, PyObject *node, double z, int order) {
    if (st->interactive_count >= st->interactive_capacity) {
        st->interactive_capacity *= 2;
        st->interactive = (InteractiveEntry *)realloc(st->interactive,
            sizeof(InteractiveEntry) * st->interactive_capacity);
    }
    InteractiveEntry *e = &st->interactive[st->interactive_count++];
    e->node = node;
    e->z = z;
    e->order = order;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* CNodeCache (stored as PyCapsule on node._c_cache)                       */
/* ─────────────────────────────────────────────────────────────────────── */

static const char *CAPSULE_NAME = "_scene_accel.cache";

static void cache_destructor(PyObject *capsule) {
    CNodeCache *c = (CNodeCache *)PyCapsule_GetPointer(capsule, CAPSULE_NAME);
    if (c) {
        for (int i = 0; i < c->color_count; i++) Py_XDECREF(c->colors[i]);
        for (int i = 0; i < c->cmd_count; i++) Py_XDECREF(c->cmds[i].texture);
        if (c->dyn_cmds) {
            for (int i = 0; i < c->dyn_count; i++) Py_XDECREF(c->dyn_cmds[i].texture);
            free(c->dyn_cmds);
        }
        Py_XDECREF(c->snap_cache);
        Py_XDECREF(c->cached_mesh_list);
        free(c);
    }
}

static CNodeCache *get_cache(PyObject *node) {
    PyObject *cap = PyObject_GetAttr(node, s__c_cache);
    if (cap && PyCapsule_IsValid(cap, CAPSULE_NAME)) {
        CNodeCache *c = (CNodeCache *)PyCapsule_GetPointer(cap, CAPSULE_NAME);
        Py_DECREF(cap);
        return c;
    }
    Py_XDECREF(cap);
    PyErr_Clear();

    CNodeCache *c = (CNodeCache *)calloc(1, sizeof(CNodeCache));
    c->type_id = NTYPE_UNKNOWN;
    PyObject *new_cap = PyCapsule_New(c, CAPSULE_NAME, cache_destructor);
    if (!new_cap) { free(c); return NULL; }
    PyObject_SetAttr(node, s__c_cache, new_cap);
    Py_DECREF(new_cap);
    return c;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Node type identification                                                */
/* ─────────────────────────────────────────────────────────────────────── */

static int identify_type(PyObject *node) {
    PyObject *tp = (PyObject *)Py_TYPE(node);
    if (tp == CircleType) return NTYPE_CIRCLE;
    if (tp == RectType)   return NTYPE_RECT;
    if (tp == LineType)   return NTYPE_LINE;
    if (tp == LabelType)  return NTYPE_LABEL;
    if (tp == ImageType)  return NTYPE_IMAGE;
    if (tp == SpriteType) return NTYPE_SPRITE;
    if (tp == ShaderNodeType) return NTYPE_SHADERNODE;
    if (tp == NineSliceType) return NTYPE_NINESLICE;
    if (tp == ParticleEmitterType) return NTYPE_PARTICLE;
    if (tp == PathType || tp == PolygonType) return NTYPE_PATH;
    if (tp == LayerType)  return NTYPE_LAYER;
    if (tp == GroupType || tp == SceneType) return NTYPE_GROUP;
    /* Check subclass of known container types */
    if (PyObject_IsInstance(node, GroupType) || PyObject_IsInstance(node, SceneType))
        return NTYPE_GROUP;
    if (PyObject_IsInstance(node, PathType))
        return NTYPE_PATH;
    return NTYPE_UNKNOWN;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Read base attributes (x, y, rotation, sx, sy, opacity, z)               */
/* ─────────────────────────────────────────────────────────────────────── */

static void read_base(PyObject *node, double base[7]) {
    base[0] = read_double(node, s_x);
    base[1] = read_double(node, s_y);
    base[2] = read_double(node, s_rotation);

    /* scale can be float or (sx, sy) tuple */
    PyObject *sc = PyObject_GetAttr(node, s_scale);
    if (sc) {
        if (PyTuple_Check(sc) && PyTuple_GET_SIZE(sc) >= 2) {
            base[3] = PyFloat_AsDouble(PyTuple_GET_ITEM(sc, 0));
            base[4] = PyFloat_AsDouble(PyTuple_GET_ITEM(sc, 1));
        } else {
            base[3] = base[4] = PyFloat_AsDouble(sc);
        }
        if (PyErr_Occurred()) { PyErr_Clear(); base[3] = base[4] = 1.0; }
        Py_DECREF(sc);
    } else {
        PyErr_Clear(); base[3] = base[4] = 1.0;
    }

    base[5] = read_double(node, s_opacity);
    base[6] = read_double(node, s_z);
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Cache validation                                                        */
/* ─────────────────────────────────────────────────────────────────────── */

static int cache_valid(CNodeCache *c, const double base[7], const double shape[], int shape_len,
                       const double ptf[6], double pop, double sscale) {
    if (!c->valid) return 0;
    if (memcmp(base, c->base, 7 * sizeof(double)) != 0) return 0;
    if (shape_len > 0 && memcmp(shape, c->shape, shape_len * sizeof(double)) != 0) return 0;
    if (memcmp(ptf, c->parent_tf, 6 * sizeof(double)) != 0) return 0;
    if (pop != c->parent_op) return 0;
    if (sscale != c->screen_scale) return 0;
    return 1;
}

static void cache_store_context(CNodeCache *c, const double base[7], const double shape[], int shape_len,
                                const double ptf[6], double pop, double sscale,
                                const double world[6], double wop) {
    memcpy(c->base, base, 7 * sizeof(double));
    if (shape_len > 0) memcpy(c->shape, shape, shape_len * sizeof(double));
    c->shape_len = shape_len;
    memcpy(c->parent_tf, ptf, 6 * sizeof(double));
    c->parent_op = pop;
    c->screen_scale = sscale;
    memcpy(c->world, world, 6 * sizeof(double));
    c->world_op = wop;
    c->valid = 1;
}

/* Store a texture ref in cache cmd, handling refcount properly */
static void cache_set_tex(CCmd *ccmd, PyObject *tex) {
    if (ccmd->texture != tex) {
        Py_XDECREF(ccmd->texture);
        ccmd->texture = tex;
        Py_XINCREF(tex);
    }
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Emit functions for each built-in type                                   */
/* ─────────────────────────────────────────────────────────────────────── */

static void emit_circle(PyObject *node, CNodeCache *cache, CollectState *st,
                        const double world[6], double op) {
    double radius = read_double(node, s_radius);
    PyObject *fill_obj = PyObject_GetAttr(node, s_fill);
    PyObject *stroke_obj = PyObject_GetAttr(node, s_stroke);
    double sw = read_double(node, s_stroke_width);

    /* shape snapshot for cache comparison: radius, stroke_width */
    double shape[2] = { radius, sw };

    /* Free old color refs if changing, store new ones */
    int old_cmd_count = cache->cmd_count;
    cache->cmd_count = 0;

    double cx_, cy_;
    c_apply(world, 0, 0, &cx_, &cy_);
    double s = c_avg_scale(world);
    double r = radius * s;
    double f = c_feather(s);
    double m = f + 1.0;

    /* Fill */
    float fill_rgba[4];
    if (fill_obj && fill_obj != Py_None && c_parse_color(fill_obj, fill_rgba)) {
        CCmd *cmd = &cache->cmds[cache->cmd_count];
        st->order++;
        cmd->z = cache->base[6]; /* z */
        cmd->order = st->order;
        cmd->kind = KIND_CIRCLE;
        float hw = (float)(r + m), hh = (float)(r + m);
        pack_verts((float)cx_, (float)cy_, hw, hh, 0, cmd->vb);
        float p[4] = {(float)KIND_CIRCLE, hw, hh, (float)r};
        float sty[4] = {0, (float)f, (float)op, 0};
        float ext[4] = {0, 0, 0, 0};
        pack_quad(p, sty, fill_rgba, ext, cmd->qb);
        cache_set_tex(cmd, NULL);
        cache->cmd_count++;
    }

    /* Stroke */
    float stroke_rgba[4];
    if (stroke_obj && stroke_obj != Py_None && sw > 0 && c_parse_color(stroke_obj, stroke_rgba)) {
        CCmd *cmd = &cache->cmds[cache->cmd_count];
        double ssw = sw * s;
        st->order++;
        cmd->z = cache->base[6] + 0.0001;
        cmd->order = st->order;
        cmd->kind = KIND_RING;
        float hw = (float)(r + ssw * 0.5 + m), hh = hw;
        pack_verts((float)cx_, (float)cy_, hw, hh, 0, cmd->vb);
        float p[4] = {(float)KIND_RING, hw, hh, (float)r};
        float sty[4] = {(float)ssw, (float)f, (float)op, 0};
        float ext[4] = {0, 0, 0, 0};
        pack_quad(p, sty, stroke_rgba, ext, cmd->qb);
        cache_set_tex(cmd, NULL);
        cache->cmd_count++;
    }

    /* Release excess cached texture refs */
    for (int i = cache->cmd_count; i < old_cmd_count; i++) {
        Py_XDECREF(cache->cmds[i].texture);
        cache->cmds[i].texture = NULL;
    }

    /* Update color refs in cache */
    for (int i = 0; i < cache->color_count; i++) Py_XDECREF(cache->colors[i]);
    cache->colors[0] = fill_obj;    Py_XINCREF(fill_obj);
    cache->colors[1] = stroke_obj;  Py_XINCREF(stroke_obj);
    cache->color_count = 2;

    memcpy(cache->shape, shape, sizeof(shape));
    cache->shape_len = 2;

    Py_XDECREF(fill_obj);
    Py_XDECREF(stroke_obj);
}

static void emit_rect(PyObject *node, CNodeCache *cache, CollectState *st,
                      const double world[6], double op) {
    double w = read_double(node, s_width);
    double h = read_double(node, s_height);
    double cr = read_double(node, s_radius);
    double sw = read_double(node, s_stroke_width);
    PyObject *fill_obj = PyObject_GetAttr(node, s_fill);
    PyObject *stroke_obj = PyObject_GetAttr(node, s_stroke);

    int old_cmd_count = cache->cmd_count;
    cache->cmd_count = 0;

    double cx_, cy_;
    c_apply(world, 0, 0, &cx_, &cy_);
    double sx = hypot(world[0], world[1]);
    double sy = hypot(world[2], world[3]);
    double rw = w * sx, rh = h * sy;
    double corner = cr * fmin(sx, sy);
    double f = c_feather((sx + sy) * 0.5);
    double m = f + 1.0;
    float rot = (float)c_rot(world);

    /* Fill */
    float fill_rgba[4];
    if (fill_obj && fill_obj != Py_None && c_parse_color(fill_obj, fill_rgba)) {
        CCmd *cmd = &cache->cmds[cache->cmd_count];
        st->order++;
        cmd->z = cache->base[6];
        cmd->order = st->order;
        cmd->kind = KIND_RRECT;
        float hw = (float)(rw/2 + m), hh = (float)(rh/2 + m);
        pack_verts((float)cx_, (float)cy_, hw, hh, rot, cmd->vb);
        float p[4] = {(float)KIND_RRECT, hw, hh, (float)corner};
        float sty[4] = {0, (float)f, (float)op, 0};
        float ext[4] = {0, 0, 0, 0};
        pack_quad(p, sty, fill_rgba, ext, cmd->qb);
        cache_set_tex(cmd, NULL);
        cache->cmd_count++;
    }

    /* Stroke */
    float stroke_rgba[4];
    if (stroke_obj && stroke_obj != Py_None && sw > 0 && c_parse_color(stroke_obj, stroke_rgba)) {
        CCmd *cmd = &cache->cmds[cache->cmd_count];
        double ssw = sw * fmin(sx, sy);
        st->order++;
        cmd->z = cache->base[6] + 0.0001;
        cmd->order = st->order;
        cmd->kind = KIND_STROKE_RRECT;
        float hw = (float)(rw/2 + ssw/2 + m), hh = (float)(rh/2 + ssw/2 + m);
        pack_verts((float)cx_, (float)cy_, hw, hh, rot, cmd->vb);
        float p[4] = {(float)KIND_STROKE_RRECT, hw, hh, (float)corner};
        float sty[4] = {(float)ssw, (float)f, (float)op, 0};
        float ext[4] = {0, 0, 0, 0};
        pack_quad(p, sty, stroke_rgba, ext, cmd->qb);
        cache_set_tex(cmd, NULL);
        cache->cmd_count++;
    }

    for (int i = cache->cmd_count; i < old_cmd_count; i++) {
        Py_XDECREF(cache->cmds[i].texture); cache->cmds[i].texture = NULL;
    }
    for (int i = 0; i < cache->color_count; i++) Py_XDECREF(cache->colors[i]);
    cache->colors[0] = fill_obj;    Py_XINCREF(fill_obj);
    cache->colors[1] = stroke_obj;  Py_XINCREF(stroke_obj);
    cache->color_count = 2;

    double shape[4] = { w, h, cr, sw };
    memcpy(cache->shape, shape, sizeof(shape));
    cache->shape_len = 4;

    Py_XDECREF(fill_obj);
    Py_XDECREF(stroke_obj);
}

static void emit_line(PyObject *node, CNodeCache *cache, CollectState *st,
                      const double world[6], double op) {
    PyObject *start_obj = PyObject_GetAttr(node, s_start);
    PyObject *end_obj = PyObject_GetAttr(node, s_end);
    double lw = read_double(node, s_width);
    PyObject *color_obj = PyObject_GetAttr(node, s_color);

    double stx = 0, sty = 0, enx = 0, eny = 0;
    if (start_obj && PyTuple_Check(start_obj) && PyTuple_GET_SIZE(start_obj) >= 2) {
        stx = PyFloat_AsDouble(PyTuple_GET_ITEM(start_obj, 0));
        sty = PyFloat_AsDouble(PyTuple_GET_ITEM(start_obj, 1));
    }
    if (end_obj && PyTuple_Check(end_obj) && PyTuple_GET_SIZE(end_obj) >= 2) {
        enx = PyFloat_AsDouble(PyTuple_GET_ITEM(end_obj, 0));
        eny = PyFloat_AsDouble(PyTuple_GET_ITEM(end_obj, 1));
    }
    if (PyErr_Occurred()) PyErr_Clear();

    int old_cmd_count = cache->cmd_count;
    cache->cmd_count = 0;

    float color_rgba[4];
    if (color_obj && c_parse_color(color_obj, color_rgba)) {
        double wsx, wsy, wex, wey;
        c_apply(world, stx, sty, &wsx, &wsy);
        c_apply(world, enx, eny, &wex, &wey);
        double s = c_avg_scale(world);
        double slw = lw * s;
        double f = c_feather(s);
        double m = slw/2 + f + 1.0;
        double cx_ = (wsx + wex) / 2, cy_ = (wsy + wey) / 2;
        float hw = (float)(fabs(wsx - wex) / 2 + m);
        float hh = (float)(fabs(wsy - wey) / 2 + m);

        CCmd *cmd = &cache->cmds[0];
        st->order++;
        cmd->z = cache->base[6];
        cmd->order = st->order;
        cmd->kind = KIND_LINE;
        pack_verts((float)cx_, (float)cy_, hw, hh, 0, cmd->vb);
        float p[4] = {(float)KIND_LINE, hw, hh, 0};
        float sty_[4] = {(float)slw, (float)f, (float)op, 0};
        float ext[4] = {(float)(wsx-cx_), (float)(wsy-cy_), (float)(wex-cx_), (float)(wey-cy_)};
        pack_quad(p, sty_, color_rgba, ext, cmd->qb);
        cache_set_tex(cmd, NULL);
        cache->cmd_count = 1;
    }

    for (int i = cache->cmd_count; i < old_cmd_count; i++) {
        Py_XDECREF(cache->cmds[i].texture); cache->cmds[i].texture = NULL;
    }
    for (int i = 0; i < cache->color_count; i++) Py_XDECREF(cache->colors[i]);
    cache->colors[0] = color_obj; Py_XINCREF(color_obj);
    cache->color_count = 1;

    double shape[5] = { stx, sty, enx, eny, lw };
    memcpy(cache->shape, shape, sizeof(shape));
    cache->shape_len = 5;

    Py_XDECREF(start_obj); Py_XDECREF(end_obj); Py_XDECREF(color_obj);
}

static void emit_label(PyObject *node, CNodeCache *cache, CollectState *st,
                       const double world[6], double op) {
    PyObject *text_obj = PyObject_GetAttr(node, s_text);
    double font_size = read_double(node, s_size);
    PyObject *font_obj = PyObject_GetAttr(node, s_font);
    PyObject *color_obj = PyObject_GetAttr(node, s_color);

    double requested_size = round(font_size * st->screen_scale);
    if (!isfinite(requested_size) || requested_size > 16384) {
        PyErr_SetString(PyExc_ValueError, "Label raster font size must be finite and at most 16384 pixels.");
        state_take_render_error(st);
        Py_XDECREF(text_obj); Py_XDECREF(font_obj); Py_XDECREF(color_obj);
        cache->valid = 0;
        return;
    }
    int pfs = (int)fmax(10, requested_size);

    int old_cmd_count = cache->cmd_count;
    cache->cmd_count = 0;

    if (!text_obj || !PyUnicode_Check(text_obj) || PyUnicode_GET_LENGTH(text_obj) == 0) {
        PyObject *cached_text = (text_obj && PyUnicode_Check(text_obj)) ? text_obj : Py_None;
        for (int i = 0; i < old_cmd_count; i++) { Py_XDECREF(cache->cmds[i].texture); cache->cmds[i].texture = NULL; }
        for (int i = 0; i < cache->color_count; i++) Py_XDECREF(cache->colors[i]);
        cache->colors[0] = color_obj; Py_XINCREF(color_obj);
        cache->colors[1] = cached_text; Py_INCREF(cached_text);
        cache->colors[2] = font_obj;  Py_XINCREF(font_obj);
        cache->color_count = 3;
        double shape[2] = { font_size, (double)pfs };
        memcpy(cache->shape, shape, sizeof(shape));
        cache->shape_len = 2;
        Py_XDECREF(text_obj);
        Py_XDECREF(font_obj);
        Py_XDECREF(color_obj);
        return;
    }

    double s = c_avg_scale(world);
    if (s < 0.001) s = 0.001;
    /* Render texture at base size (no world scale) so it stays cached
       during scale animations.  GPU quad handles the scaling. */

    /* Call renderer.text_texture(text, font, pixel_size) */
    PyObject *pfs_obj = PyLong_FromLong(pfs);
    PyObject *tex = PyObject_CallMethodObjArgs(st->renderer, s_text_texture,
                                                text_obj, font_obj, pfs_obj, NULL);
    Py_XDECREF(pfs_obj);
    if (!tex && state_take_render_error(st)) {
        Py_XDECREF(text_obj); Py_XDECREF(font_obj); Py_XDECREF(color_obj);
        cache->cmd_count = old_cmd_count;
        cache->valid = 0;
        return;
    }

    if (tex && tex != Py_None) {
        PyObject *tsz = PyObject_GetAttr(tex, s_size);
        double tw = 0, th = 0;
        if (tsz && PyTuple_Check(tsz) && PyTuple_GET_SIZE(tsz) >= 2) {
            tw = PyFloat_AsDouble(PyTuple_GET_ITEM(tsz, 0));
            th = PyFloat_AsDouble(PyTuple_GET_ITEM(tsz, 1));
        }
        Py_XDECREF(tsz);
        if (PyErr_Occurred()) PyErr_Clear();

        /* A minimum raster font size must not enlarge the logical label. */
        double raster_scale = font_size > 0 ? pfs / font_size : st->screen_scale;
        double dw = tw / raster_scale * s;
        double dh = th / raster_scale * s;
        /* Cache rendered size for accurate _bounds / _collider */
        PyObject *rsz = Py_BuildValue("(dd)", dw, dh);
        if (rsz) { PyObject_SetAttr(node, s__rendered_size, rsz); Py_DECREF(rsz); }
        if (PyErr_Occurred()) PyErr_Clear();
        double cx_, cy_;
        c_apply(world, 0, 0, &cx_, &cy_);
        float rot = (float)c_rot(world);

        float color_rgba[4] = {1,1,1,1};
        c_parse_color(color_obj, color_rgba);

        CCmd *cmd = &cache->cmds[0];
        st->order++;
        cmd->z = cache->base[6];
        cmd->order = st->order;
        cmd->kind = KIND_TEX;
        pack_verts((float)cx_, (float)cy_, (float)(dw/2), (float)(dh/2), rot, cmd->vb);
        float p[4] = {(float)KIND_TEX, 0, 0, 0};
        float sty_[4] = {0, 0, (float)op, 0};
        float fill_[4] = {0, 0, 0, 0};
        float ext[4] = {color_rgba[0], color_rgba[1], color_rgba[2], color_rgba[3]};
        pack_quad(p, sty_, fill_, ext, cmd->qb);
        /* Store texture ref — steal ref from CallMethodObjArgs */
        Py_XDECREF(cache->cmds[0].texture);
        cache->cmds[0].texture = tex;  /* steal ref */
        tex = NULL;  /* prevent double decref */
        cache->cmd_count = 1;
    }

    for (int i = cache->cmd_count; i < old_cmd_count; i++) {
        Py_XDECREF(cache->cmds[i].texture); cache->cmds[i].texture = NULL;
    }
    for (int i = 0; i < cache->color_count; i++) Py_XDECREF(cache->colors[i]);
    cache->colors[0] = color_obj; Py_XINCREF(color_obj);
    cache->colors[1] = text_obj;  Py_XINCREF(text_obj);
    cache->colors[2] = font_obj;  Py_XINCREF(font_obj);
    cache->color_count = 3;

    double shape[2] = { font_size, (double)pfs };
    memcpy(cache->shape, shape, sizeof(shape));
    cache->shape_len = 2;

    Py_XDECREF(text_obj); Py_XDECREF(font_obj); Py_XDECREF(color_obj);
    Py_XDECREF(tex);
}

static void emit_image(PyObject *node, CNodeCache *cache, CollectState *st,
                       const double world[6], double op) {
    PyObject *tex_obj = PyObject_GetAttr(node, s_texture);
    if (!tex_obj || tex_obj == Py_None) {
        Py_XDECREF(tex_obj);
        int old = cache->cmd_count; cache->cmd_count = 0;
        for (int i = 0; i < old; i++) { Py_XDECREF(cache->cmds[i].texture); cache->cmds[i].texture = NULL; }
        return;
    }

    PyObject *isz = PyObject_GetAttr(node, s_img_size);
    double iw = 0, ih = 0;
    if (isz && PyTuple_Check(isz) && PyTuple_GET_SIZE(isz) >= 2) {
        iw = PyFloat_AsDouble(PyTuple_GET_ITEM(isz, 0));
        ih = PyFloat_AsDouble(PyTuple_GET_ITEM(isz, 1));
    }
    Py_XDECREF(isz);
    if (PyErr_Occurred()) PyErr_Clear();

    int old_cmd_count = cache->cmd_count;
    cache->cmd_count = 0;

    double sx = hypot(world[0], world[1]);
    double sy = hypot(world[2], world[3]);
    double cx_, cy_;
    c_apply(world, 0, 0, &cx_, &cy_);
    float rot = (float)c_rot(world);

    CCmd *cmd = &cache->cmds[0];
    st->order++;
    cmd->z = cache->base[6];
    cmd->order = st->order;
    cmd->kind = KIND_TEX;
    pack_verts((float)cx_, (float)cy_, (float)(iw*sx/2), (float)(ih*sy/2), rot, cmd->vb);
    float p[4] = {(float)KIND_TEX, 0, 0, 0};
    float sty_[4] = {0, 0, (float)op, 0};
    float fill_[4] = {0, 0, 0, 0};
    float ext[4] = {1, 1, 1, 1};
    pack_quad(p, sty_, fill_, ext, cmd->qb);

    Py_XDECREF(cache->cmds[0].texture);
    cache->cmds[0].texture = tex_obj; /* steal ref */
    tex_obj = NULL;
    cache->cmd_count = 1;

    for (int i = cache->cmd_count; i < old_cmd_count; i++) {
        Py_XDECREF(cache->cmds[i].texture); cache->cmds[i].texture = NULL;
    }
    cache->color_count = 0;

    double shape[2] = { iw, ih };
    memcpy(cache->shape, shape, sizeof(shape));
    cache->shape_len = 2;

    Py_XDECREF(tex_obj);
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Sprite                                                                  */
/* ─────────────────────────────────────────────────────────────────────── */

static void pack_verts_uv(float cx, float cy, float hw_, float hh_, float rot,
                           float u0, float v0_, float u1, float v1_, uint8_t out[96]) {
    float c = cosf(rot), s = sinf(rot);
    float v[24];
    v[0]=cx+c*(-hw_)-s*(-hh_); v[1]=cy+s*(-hw_)+c*(-hh_); v[2]=u0;  v[3]=v0_;
    v[4]=cx+c*(hw_)-s*(-hh_);  v[5]=cy+s*(hw_)+c*(-hh_);  v[6]=u1;  v[7]=v0_;
    v[8]=cx+c*(-hw_)-s*(hh_);  v[9]=cy+s*(-hw_)+c*(hh_); v[10]=u0; v[11]=v1_;
    v[12]=v[4]; v[13]=v[5]; v[14]=u1; v[15]=v0_;
    v[16]=cx+c*(hw_)-s*(hh_); v[17]=cy+s*(hw_)+c*(hh_); v[18]=u1; v[19]=v1_;
    v[20]=v[8]; v[21]=v[9]; v[22]=u0; v[23]=v1_;
    memcpy(out, v, 96);
}

static void emit_sprite(PyObject *node, CNodeCache *cache, CollectState *st,
                         const double world[6], double op) {
    PyObject *tex_obj = PyObject_GetAttr(node, s_texture);
    if (!tex_obj || tex_obj == Py_None) {
        Py_XDECREF(tex_obj);
        int old = cache->cmd_count; cache->cmd_count = 0;
        for (int i = 0; i < old; i++) { Py_XDECREF(cache->cmds[i].texture); cache->cmds[i].texture = NULL; }
        return;
    }

    /* Read sprite attributes */
    PyObject *ssz = PyObject_GetAttr(node, s_sprite_size);
    double sw_ = 0, sh_ = 0;
    if (ssz && PyTuple_Check(ssz) && PyTuple_GET_SIZE(ssz) >= 2) {
        sw_ = PyFloat_AsDouble(PyTuple_GET_ITEM(ssz, 0));
        sh_ = PyFloat_AsDouble(PyTuple_GET_ITEM(ssz, 1));
    }
    Py_XDECREF(ssz);

    PyObject *anch = PyObject_GetAttr(node, s_anchor);
    double ax = 0.5, ay = 0.5;
    if (anch && PyTuple_Check(anch) && PyTuple_GET_SIZE(anch) >= 2) {
        ax = PyFloat_AsDouble(PyTuple_GET_ITEM(anch, 0));
        ay = PyFloat_AsDouble(PyTuple_GET_ITEM(anch, 1));
    }
    Py_XDECREF(anch);

    int fx = read_bool(node, s_flip_x);
    int fy = read_bool(node, s_flip_y);

    PyObject *tint_obj = PyObject_GetAttr(node, s_tint);
    float tint[4] = {1,1,1,1};
    if (tint_obj) c_parse_color(tint_obj, tint);

    PyObject *uvr = PyObject_GetAttr(node, s__uv_rect);
    float u0 = 0, v0_ = 0, u1 = 1, v1_ = 1;
    if (uvr && PyTuple_Check(uvr) && PyTuple_GET_SIZE(uvr) >= 4) {
        u0 = (float)PyFloat_AsDouble(PyTuple_GET_ITEM(uvr, 0));
        v0_ = (float)PyFloat_AsDouble(PyTuple_GET_ITEM(uvr, 1));
        u1 = (float)PyFloat_AsDouble(PyTuple_GET_ITEM(uvr, 2));
        v1_ = (float)PyFloat_AsDouble(PyTuple_GET_ITEM(uvr, 3));
    }
    Py_XDECREF(uvr);
    if (PyErr_Occurred()) PyErr_Clear();

    if (fx) { float tmp = u0; u0 = u1; u1 = tmp; }
    if (fy) { float tmp = v0_; v0_ = v1_; v1_ = tmp; }

    double off_x = (0.5 - ax) * sw_;
    double off_y = (0.5 - ay) * sh_;
    double cx_, cy_;
    c_apply(world, off_x, off_y, &cx_, &cy_);
    double sx = hypot(world[0], world[1]);
    double sy = hypot(world[2], world[3]);
    float hw = (float)(sw_ * sx / 2);
    float hh = (float)(sh_ * sy / 2);
    float rot = (float)c_rot(world);

    int old_cmd_count = cache->cmd_count;
    cache->cmd_count = 0;

    CCmd *cmd = &cache->cmds[0];
    st->order++;
    cmd->z = cache->base[6];
    cmd->order = st->order;
    cmd->kind = KIND_TEX;
    pack_verts_uv((float)cx_, (float)cy_, hw, hh, rot, u0, v0_, u1, v1_, cmd->vb);
    float p[4] = {(float)KIND_TEX, 0, 0, 0};
    float sty[4] = {0, 0, (float)op, 0};
    float fill_[4] = {0, 0, 0, 0};
    pack_quad(p, sty, fill_, tint, cmd->qb);

    Py_XDECREF(cache->cmds[0].texture);
    cache->cmds[0].texture = tex_obj; /* steal ref */
    tex_obj = NULL;
    cache->cmd_count = 1;

    for (int i = cache->cmd_count; i < old_cmd_count; i++) {
        Py_XDECREF(cache->cmds[i].texture); cache->cmds[i].texture = NULL;
    }

    /* Update color refs in cache: [0]=texture, [1]=tint */
    for (int i = 0; i < cache->color_count; i++) Py_XDECREF(cache->colors[i]);
    cache->colors[0] = cache->cmds[0].texture; Py_XINCREF(cache->cmds[0].texture);
    cache->colors[1] = tint_obj; Py_XINCREF(tint_obj);
    cache->color_count = 2;

    /* shape: sprite_size[2], anchor[2], flip[2], uv_rect[4] = 10 doubles */
    double shape[10] = { sw_, sh_, ax, ay, (double)fx, (double)fy, (double)u0, (double)v0_, (double)u1, (double)v1_ };
    memcpy(cache->shape, shape, sizeof(shape));
    cache->shape_len = 10;

    Py_XDECREF(tex_obj);
    Py_XDECREF(tint_obj);
}

/* ─────────────────────────────────────────────────────────────────────── */
/* NineSlice — 9-patch stretchable texture                                 */
/* ─────────────────────────────────────────────────────────────────────── */

static void emit_nineslice(PyObject *node, CNodeCache *cache, CollectState *st,
                            const double world[6], double op) {
    PyObject *tex_obj = PyObject_GetAttr(node, s_texture);
    if (!tex_obj || tex_obj == Py_None) {
        Py_XDECREF(tex_obj);
        /* Clear old cmds */
        if (cache->dyn_cmds) {
            for (int i = 0; i < cache->dyn_count; i++) Py_XDECREF(cache->dyn_cmds[i].texture);
        }
        cache->cmd_count = 0; cache->dyn_count = 0;
        return;
    }

    /* Read texture size */
    PyObject *tsz = PyObject_GetAttr(tex_obj, s_size);
    double tw = 1, th = 1;
    if (tsz && PyTuple_Check(tsz) && PyTuple_GET_SIZE(tsz) >= 2) {
        tw = PyFloat_AsDouble(PyTuple_GET_ITEM(tsz, 0));
        th = PyFloat_AsDouble(PyTuple_GET_ITEM(tsz, 1));
    }
    Py_XDECREF(tsz);
    if (tw < 1) tw = 1;
    if (th < 1) th = 1;

    /* Read insets (left, top, right, bottom) */
    PyObject *ins = PyObject_GetAttr(node, s_insets);
    double il = 0, it = 0, ir = 0, ib = 0;
    if (ins && PyTuple_Check(ins) && PyTuple_GET_SIZE(ins) >= 4) {
        il = PyFloat_AsDouble(PyTuple_GET_ITEM(ins, 0));
        it = PyFloat_AsDouble(PyTuple_GET_ITEM(ins, 1));
        ir = PyFloat_AsDouble(PyTuple_GET_ITEM(ins, 2));
        ib = PyFloat_AsDouble(PyTuple_GET_ITEM(ins, 3));
    }
    Py_XDECREF(ins);

    /* Read nine_size (width, height) */
    PyObject *nsz = PyObject_GetAttr(node, s_nine_size);
    double nw = 0, nh = 0;
    if (nsz && PyTuple_Check(nsz) && PyTuple_GET_SIZE(nsz) >= 2) {
        nw = PyFloat_AsDouble(PyTuple_GET_ITEM(nsz, 0));
        nh = PyFloat_AsDouble(PyTuple_GET_ITEM(nsz, 1));
    }
    Py_XDECREF(nsz);

    /* Read tint */
    PyObject *tint_obj = PyObject_GetAttr(node, s_tint);
    float tint[4] = {1, 1, 1, 1};
    if (tint_obj) c_parse_color(tint_obj, tint);

    if (PyErr_Occurred()) PyErr_Clear();

    /* World transform components */
    double sx = hypot(world[0], world[1]);
    double sy = hypot(world[2], world[3]);
    float rot = (float)c_rot(world);

    /* UV boundaries */
    double ul = il / tw, ur = 1.0 - ir / tw;
    double vt = it / th, vb = 1.0 - ib / th;

    /* Display sizes for border */
    double dl = il, dr = ir, dt = it, db = ib;
    double cw = nw - dl - dr;  if (cw < 0) cw = 0;
    double ch = nh - dt - db;  if (ch < 0) ch = 0;

    /* 9 patches: (lx, ly, pw, ph, u0, v0, u1, v1) */
    double x0 = -nw / 2, x1 = x0 + dl, x2 = x1 + cw;
    double y0 = -nh / 2, y1 = y0 + dt, y2 = y1 + ch;
    struct Patch { double lx, ly, pw, ph, u0, v0, u1, v1; };
    Patch patches[9] = {
        /* row 0 (top) */
        {x0, y0, dl,  dt, 0,  0,  ul, vt},  /* TL */
        {x1, y0, cw,  dt, ul, 0,  ur, vt},  /* TC */
        {x2, y0, dr,  dt, ur, 0,  1,  vt},  /* TR */
        /* row 1 (mid) */
        {x0, y1, dl,  ch, 0,  vt, ul, vb},  /* ML */
        {x1, y1, cw,  ch, ul, vt, ur, vb},  /* MC */
        {x2, y1, dr,  ch, ur, vt, 1,  vb},  /* MR */
        /* row 2 (bot) */
        {x0, y2, dl,  db, 0,  vb, ul, 1},   /* BL */
        {x1, y2, cw,  db, ul, vb, ur, 1},   /* BC */
        {x2, y2, dr,  db, ur, vb, 1,  1},   /* BR */
    };

    /* Free old dynamic cmds */
    if (cache->dyn_cmds) {
        for (int i = 0; i < cache->dyn_count; i++) Py_XDECREF(cache->dyn_cmds[i].texture);
    }
    cache->cmd_count = 0;

    /* Ensure capacity for up to 9 cmds */
    if (cache->dyn_capacity < 9) {
        cache->dyn_cmds = (CCmd *)realloc(cache->dyn_cmds, sizeof(CCmd) * 9);
        cache->dyn_capacity = 9;
    }
    cache->dyn_count = 0;

    float params[4] = {(float)KIND_TEX, 0, 0, 0};
    float style[4] = {0, 0, (float)op, 0};
    float fill_[4] = {0, 0, 0, 0};

    for (int i = 0; i < 9; i++) {
        Patch *p = &patches[i];
        if (p->pw <= 0 || p->ph <= 0) continue;

        double pcx = p->lx + p->pw / 2;
        double pcy = p->ly + p->ph / 2;
        double wcx, wcy;
        c_apply(world, pcx, pcy, &wcx, &wcy);
        float hw = (float)(p->pw * sx / 2);
        float hh = (float)(p->ph * sy / 2);

        CCmd *cmd = &cache->dyn_cmds[cache->dyn_count];
        st->order++;
        cmd->z = cache->base[6];
        cmd->order = st->order;
        cmd->kind = KIND_TEX;
        pack_verts_uv((float)wcx, (float)wcy, hw, hh, rot,
                      (float)p->u0, (float)p->v0, (float)p->u1, (float)p->v1, cmd->vb);
        pack_quad(params, style, fill_, tint, cmd->qb);
        cmd->texture = tex_obj;  /* borrowed; will incref below */
        cache->dyn_count++;
    }

    /* Incref texture for each dyn_cmd */
    for (int i = 0; i < cache->dyn_count; i++) {
        Py_INCREF(cache->dyn_cmds[i].texture);
    }

    /* Update color refs: [0]=texture, [1]=tint */
    for (int i = 0; i < cache->color_count; i++) Py_XDECREF(cache->colors[i]);
    cache->colors[0] = tex_obj; Py_INCREF(tex_obj);
    cache->colors[1] = tint_obj; Py_XINCREF(tint_obj);
    cache->color_count = 2;

    /* shape: nine_size[2], insets[4], tint[4] + tex_handle = 11 doubles */
    PyObject *hobj = PyObject_GetAttr(tex_obj, s__handle);
    double tex_h = hobj ? (double)PyLong_AsLongLong(hobj) : 0;
    Py_XDECREF(hobj);
    if (PyErr_Occurred()) PyErr_Clear();
    double shape[11] = { nw, nh, il, it, ir, ib, tint[0], tint[1], tint[2], tint[3], tex_h };
    memcpy(cache->shape, shape, sizeof(shape));
    cache->shape_len = 11;

    Py_DECREF(tex_obj);
    Py_XDECREF(tint_obj);
}

/* Forward declaration (defined after Layer section) */
static void handle_unknown_emit(PyObject *node, CNodeCache *cache, CollectState *st,
                                const double world[6], double op);

/* ─────────────────────────────────────────────────────────────────────── */
/* ShaderNode — emit KIND_TEX quad for the off-screen shader texture       */
/* ─────────────────────────────────────────────────────────────────────── */

static void emit_shadernode(PyObject *node, CNodeCache *cache, CollectState *st,
                            const double world[6], double op) {
    PyObject *tex_obj = PyObject_GetAttr(node, s__tex);
    if (!tex_obj || tex_obj == Py_None) {
        Py_XDECREF(tex_obj);
        /* Texture not yet created — fall back to Python _emit() which runs _init_gpu.
           Invalidate cache so next frame re-emits via the C path once _tex is ready. */
        handle_unknown_emit(node, cache, st, world, op);
        cache->valid = 0;
        return;
    }

    PyObject *ssz = PyObject_GetAttr(node, s__shader_size);
    double sw = 0, sh = 0;
    if (ssz && PyTuple_Check(ssz) && PyTuple_GET_SIZE(ssz) >= 2) {
        sw = PyFloat_AsDouble(PyTuple_GET_ITEM(ssz, 0));
        sh = PyFloat_AsDouble(PyTuple_GET_ITEM(ssz, 1));
    }
    Py_XDECREF(ssz);
    if (PyErr_Occurred()) PyErr_Clear();

    int old_cmd_count = cache->cmd_count;
    cache->cmd_count = 0;

    double sx = hypot(world[0], world[1]);
    double sy = hypot(world[2], world[3]);
    double cx_, cy_;
    c_apply(world, 0, 0, &cx_, &cy_);
    float rot = (float)c_rot(world);

    CCmd *cmd = &cache->cmds[0];
    st->order++;
    cmd->z = cache->base[6];
    cmd->order = st->order;
    cmd->kind = KIND_TEX;
    pack_verts((float)cx_, (float)cy_, (float)(sw * sx / 2), (float)(sh * sy / 2), rot, cmd->vb);
    float p[4] = {(float)KIND_TEX, 0, 0, 0};
    float sty_[4] = {0, 0, (float)op, 0};
    float fill_[4] = {0, 0, 0, 0};
    float ext[4] = {1, 1, 1, 1};
    pack_quad(p, sty_, fill_, ext, cmd->qb);

    Py_XDECREF(cache->cmds[0].texture);
    cache->cmds[0].texture = tex_obj; /* steal ref */
    tex_obj = NULL;
    cache->cmd_count = 1;

    for (int i = cache->cmd_count; i < old_cmd_count; i++) {
        Py_XDECREF(cache->cmds[i].texture); cache->cmds[i].texture = NULL;
    }

    /* Store texture in colors[0] for cache validation (pointer compare) */
    for (int i = 0; i < cache->color_count; i++) Py_XDECREF(cache->colors[i]);
    cache->colors[0] = cache->cmds[0].texture;
    Py_XINCREF(cache->cmds[0].texture);
    cache->color_count = 1;

    double shape[2] = { sw, sh };
    memcpy(cache->shape, shape, sizeof(shape));
    cache->shape_len = 2;

    Py_XDECREF(tex_obj);
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Path / Polygon — emit cached off-screen vector texture                 */
/* ─────────────────────────────────────────────────────────────────────── */

static void emit_path(PyObject *node, CNodeCache *cache, CollectState *st,
                      const double world[6], double op) {
    /* Geometry depends on projected size as well as the logical path version. */
    double version = read_double(node, s__path_version);
    double required = c_max_scale(world) * st->screen_scale;
    if (required < 1.0) required = 1.0;
    PyObject *mesh_list = NULL;
    if (cache->cached_mesh_list != NULL && cache->cached_path_version == version &&
            path_scale_usable(cache->cached_mesh_scale, required)) {
        mesh_list = cache->cached_mesh_list;
        Py_INCREF(mesh_list);
    } else {
        /* Call Python _get_meshes() to build/cache mesh data */
        mesh_list = PyObject_CallMethodObjArgs(node, s__get_meshes, st->renderer, NULL);
        if (mesh_list && PyList_Check(mesh_list)) {
            Py_XDECREF(cache->cached_mesh_list);
            cache->cached_mesh_list = mesh_list;
            Py_INCREF(mesh_list);
            cache->cached_path_version = version;
            cache->cached_mesh_scale = read_double(node, s__cached_mesh_scale);
        }
    }
    if (!mesh_list || mesh_list == Py_None || !PyList_Check(mesh_list) || PyList_GET_SIZE(mesh_list) == 0) {
        Py_XDECREF(mesh_list);
        state_take_render_error(st);
        cache->cmd_count = 0;
        cache->valid = 0;
        return;
    }

    /* Transform each mesh's vertices to screen space and append to mesh buffer */
    Py_ssize_t n = PyList_GET_SIZE(mesh_list);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *mesh = PyList_GET_ITEM(mesh_list, i);
        /* mesh is DrawMesh, including stroke/fill stencil metadata. */
        PyObject *vb_obj = PyObject_GetAttrString(mesh, "vertex_bytes");
        PyObject *vc_obj = PyObject_GetAttrString(mesh, "vertex_count");
        PyObject *ib_obj = PyObject_GetAttrString(mesh, "index_bytes");
        PyObject *ic_obj = PyObject_GetAttrString(mesh, "index_count");
        PyObject *it_obj = PyObject_GetAttrString(mesh, "index_type");
        PyObject *cl_obj = PyObject_GetAttrString(mesh, "color");
        PyObject *bl_obj = PyObject_GetAttrString(mesh, "blend");
        PyObject *is_obj = PyObject_GetAttrString(mesh, "is_stroke");
        PyObject *fr_obj = PyObject_GetAttrString(mesh, "fill_rule");
        if (!vb_obj || !vc_obj || !ib_obj || !ic_obj || !it_obj || !cl_obj || !bl_obj || !is_obj || !fr_obj) {
            Py_XDECREF(vb_obj); Py_XDECREF(vc_obj); Py_XDECREF(ib_obj);
            Py_XDECREF(ic_obj); Py_XDECREF(it_obj); Py_XDECREF(cl_obj); Py_XDECREF(bl_obj);
            Py_XDECREF(is_obj);
            Py_XDECREF(fr_obj);
            if (PyErr_Occurred()) PyErr_Clear();
            continue;
        }

        int vert_count = (int)PyLong_AsLong(vc_obj);
        int idx_count = (int)PyLong_AsLong(ic_obj);
        const char *idx_type_str = PyUnicode_AsUTF8(it_obj);
        int idx_is_32 = idx_type_str && strcmp(idx_type_str, "uint32") == 0;
        int blend = PyObject_IsTrue(bl_obj);
        int is_stroke = PyObject_IsTrue(is_obj);
        int fill_rule = 0;
        if (PyUnicode_Check(fr_obj)) {
            const char *rule = PyUnicode_AsUTF8(fr_obj);
            if (rule) fill_rule = normalize_fill_rule(rule) + 1;
        }

        float color[4] = {0, 0, 0, 0};
        if (PyTuple_Check(cl_obj) && PyTuple_GET_SIZE(cl_obj) >= 4) {
            for (int k = 0; k < 4; k++)
                color[k] = (float)PyFloat_AsDouble(PyTuple_GET_ITEM(cl_obj, k));
        }
        /* Apply opacity and premultiply alpha */
        color[3] *= (float)op;
        color[0] *= color[3];
        color[1] *= color[3];
        color[2] *= color[3];

        /* Transform vertices to screen space (3 floats per vertex: x, y, alpha) */
        const float *src = (const float *)PyBytes_AS_STRING(vb_obj);
        Py_ssize_t vb_size = PyBytes_GET_SIZE(vb_obj);
        int floats_per_vert = (vert_count > 0) ? (int)(vb_size / (sizeof(float) * vert_count)) : 2;
        int has_alpha = (floats_per_vert >= 3);
        /* Output always has 3 floats per vertex (x, y, alpha) for fringe support */
        float *transformed = (float *)malloc(sizeof(float) * vert_count * 3);
        for (int v = 0; v < vert_count; v++) {
            double lx = src[v * floats_per_vert], ly = src[v * floats_per_vert + 1];
            transformed[v * 3]     = (float)(world[0] * lx + world[2] * ly + world[4]);
            transformed[v * 3 + 1] = (float)(world[1] * lx + world[3] * ly + world[5]);
            transformed[v * 3 + 2] = has_alpha ? src[v * floats_per_vert + 2] : 1.0f;
        }

        int mb_idx = st->mesh_batch_count;
        state_append_mesh(st, transformed, vert_count,
                          (const uint8_t *)PyBytes_AS_STRING(ib_obj),
                          (int)PyBytes_GET_SIZE(ib_obj),
                          idx_count, idx_is_32, color, blend, is_stroke, fill_rule);
        free(transformed);

        /* Emit a KIND_MESH placeholder CCmd for z-order sorting */
        CCmd *cmd = state_append(st);
        cmd->z = cache->base[6];
        st->order++;
        cmd->order = st->order;
        cmd->kind = KIND_MESH;
        cmd->texture = NULL;
        cmd->mesh_batch_idx = mb_idx;

        Py_DECREF(vb_obj); Py_DECREF(vc_obj); Py_DECREF(ib_obj);
        Py_DECREF(ic_obj); Py_DECREF(it_obj); Py_DECREF(cl_obj); Py_DECREF(bl_obj);
        Py_DECREF(is_obj);
        Py_DECREF(fr_obj);
    }
    Py_DECREF(mesh_list);

    /* No CCmd emitted — path renders via mesh buffer */
    int old_cmd_count = cache->cmd_count;
    cache->cmd_count = 0;
    for (int i = 0; i < old_cmd_count; i++) {
        Py_XDECREF(cache->cmds[i].texture); cache->cmds[i].texture = NULL;
    }
    for (int i = 0; i < cache->color_count; i++) Py_XDECREF(cache->colors[i]);
    cache->color_count = 0;

    double shape[1] = { version };
    memcpy(cache->shape, shape, sizeof(shape));
    cache->shape_len = 1;
    cache->valid = 0; /* must re-emit: mesh vertices depend on world transform */
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Layer handling                                                          */
/* ─────────────────────────────────────────────────────────────────────── */

static void handle_layer(PyObject *node, CNodeCache *cache, CollectState *st,
                         const double world[6], double op) {
    PyObject *children = PyObject_GetAttr(node, s_children);
    Py_ssize_t n = children ? PyList_Size(children) : 0;
    Py_XDECREF(children);
    if (n <= 0) {
        cache->cmd_count = 0;
        return;
    }

    double ds = c_avg_scale(world);
    double rs = ds > 1.0 ? ds : 1.0;

    /* Check if rebuild is needed */
    PyObject *tex = PyObject_GetAttr(node, s__tex);
    int dirty = read_bool(node, s__dirty);
    double old_rscale = read_double(node, s__rscale);
    int need_rebuild = (!tex || tex == Py_None || dirty || fabs(rs - old_rscale) > 1e-3);
    Py_XDECREF(tex);

    if (need_rebuild) {
        /* Call node._rebuild(renderer, rs) */
        PyObject *rs_obj = PyFloat_FromDouble(rs);
        PyObject *result = PyObject_CallMethodObjArgs(node, s__rebuild, st->renderer, rs_obj, NULL);
        Py_XDECREF(rs_obj);
        Py_XDECREF(result);
        if (state_take_render_error(st)) {
            cache->valid = 0;
            return;
        }
    }

    /* Read texture info after potential rebuild */
    tex = PyObject_GetAttr(node, s__tex);
    PyObject *lsize = PyObject_GetAttr(node, s__lsize);
    PyObject *lcenter = PyObject_GetAttr(node, s__lcenter);

    int old_cmd_count = cache->cmd_count;
    cache->cmd_count = 0;

    if (tex && tex != Py_None && lsize && lsize != Py_None && lcenter && lcenter != Py_None) {
        double lw = PyFloat_AsDouble(PyTuple_GET_ITEM(lsize, 0));
        double lh = PyFloat_AsDouble(PyTuple_GET_ITEM(lsize, 1));
        double lcx = PyFloat_AsDouble(PyTuple_GET_ITEM(lcenter, 0));
        double lcy = PyFloat_AsDouble(PyTuple_GET_ITEM(lcenter, 1));
        if (PyErr_Occurred()) PyErr_Clear();

        double wcx, wcy;
        c_apply(world, lcx, lcy, &wcx, &wcy);
        PyObject *capture_center = PyObject_GetAttr(node, s__capture_center);
        if (capture_center && capture_center != Py_None &&
            PyTuple_Check(capture_center) && PyTuple_GET_SIZE(capture_center) == 2) {
            wcx = PyFloat_AsDouble(PyTuple_GET_ITEM(capture_center, 0));
            wcy = PyFloat_AsDouble(PyTuple_GET_ITEM(capture_center, 1));
        }
        Py_XDECREF(capture_center);
        if (PyErr_Occurred()) PyErr_Clear();
        float rot = (float)c_rot(world);

        CCmd *cmd = &cache->cmds[0];
        st->order++;
        cmd->z = cache->base[6];
        cmd->order = st->order;
        cmd->kind = KIND_TEX;
        pack_verts((float)wcx, (float)wcy, (float)(lw * ds * 0.5), (float)(lh * ds * 0.5), rot, cmd->vb);
        float p[4] = {(float)KIND_TEX, 0, 0, 0};
        float sty_[4] = {0, 0, (float)op, 0};
        float fill_[4] = {0, 0, 0, 0};
        float ext[4] = {0, 0, 0, 0};  /* no tint for layer */
        /* Layer uses (0,0,0,0) fill and (1,1,1,1) extra in Python */
        ext[0] = 1; ext[1] = 1; ext[2] = 1; ext[3] = 1;
        pack_quad(p, sty_, fill_, ext, cmd->qb);

        Py_XDECREF(cache->cmds[0].texture);
        cache->cmds[0].texture = tex; Py_INCREF(tex);
        cache->cmd_count = 1;
    }

    for (int i = cache->cmd_count; i < old_cmd_count; i++) {
        Py_XDECREF(cache->cmds[i].texture); cache->cmds[i].texture = NULL;
    }

    Py_XDECREF(tex); Py_XDECREF(lsize); Py_XDECREF(lcenter);
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Unknown type fallback                                                   */
/* ─────────────────────────────────────────────────────────────────────── */

static void handle_unknown_emit(PyObject *node, CNodeCache *cache, CollectState *st,
                                const double world[6], double op) {
    /* Build Python args for node._emit(cmds, renderer, world, opacity, order) */
    PyObject *py_cmds = PyList_New(0);
    PyObject *world_tuple = Py_BuildValue("(dddddd)", world[0], world[1], world[2],
                                           world[3], world[4], world[5]);
    PyObject *py_order = PyList_New(1);
    PyList_SET_ITEM(py_order, 0, PyLong_FromLong(st->order));

    PyObject *py_op = PyFloat_FromDouble(op);
    PyObject *result = PyObject_CallMethodObjArgs(node, s__emit,
        py_cmds, st->renderer, world_tuple, py_op, py_order, NULL);
    Py_XDECREF(result);
    Py_XDECREF(py_op);
    if (state_take_render_error(st)) {
        Py_DECREF(py_cmds); Py_DECREF(world_tuple); Py_DECREF(py_order);
        cache->valid = 0;
        return;
    }

    /* Match Node._collect: cache the snapshot after _emit updates local state. */
    PyObject *snap = PyObject_CallMethodObjArgs(node, s__snap, NULL);
    Py_XSETREF(cache->snap_cache, snap);
    if (PyErr_Occurred()) PyErr_Clear();

    st->order = (int)PyLong_AsLong(PyList_GET_ITEM(py_order, 0));
    if (PyErr_Occurred()) PyErr_Clear();

    /* Convert Python Cmd objects to CCmd — store ALL in dyn_cmds for caching */
    Py_ssize_t n = PyList_GET_SIZE(py_cmds);

    /* Helper lambda to parse one Python Cmd into a CCmd */
    auto parse_cmd = [&](PyObject *cmd, CCmd *ccmd) {
        ccmd->z = read_double(cmd, s_z);
        st->order++;
        ccmd->order = st->order;
        ccmd->kind = (int)read_double(cmd, s_kind);
        float cx_ = (float)read_double(cmd, s_cx);
        float cy_ = (float)read_double(cmd, s_cy);
        float hw = (float)read_double(cmd, s_hw);
        float hh = (float)read_double(cmd, s_hh);
        float rot = (float)read_double(cmd, s_rot);

        /* Check for pre-packed _vb on the Cmd (Sprite/NineSlice set this) */
        static PyObject *s__vb = NULL;
        if (!s__vb) s__vb = PyUnicode_InternFromString("_vb");
        PyObject *vb_obj = PyObject_GetAttr(cmd, s__vb);
        if (vb_obj && vb_obj != Py_None && PyBytes_Check(vb_obj) && PyBytes_GET_SIZE(vb_obj) == 96) {
            memcpy(ccmd->vb, PyBytes_AS_STRING(vb_obj), 96);
        } else {
            pack_verts(cx_, cy_, hw, hh, rot, ccmd->vb);
        }
        Py_XDECREF(vb_obj);
        if (PyErr_Occurred()) PyErr_Clear();

        PyObject *pt = PyObject_GetAttr(cmd, s_params);
        PyObject *st_ = PyObject_GetAttr(cmd, s_style);
        PyObject *fl = PyObject_GetAttr(cmd, s_fill);
        PyObject *ex = PyObject_GetAttr(cmd, s_extra);
        float p[4]={0}, sy_[4]={0}, fi[4]={0}, e[4]={0};
        if (pt && PyTuple_Check(pt)) for (int j=0;j<4;j++) p[j]=(float)PyFloat_AsDouble(PyTuple_GET_ITEM(pt,j));
        if (st_ && PyTuple_Check(st_)) for (int j=0;j<4;j++) sy_[j]=(float)PyFloat_AsDouble(PyTuple_GET_ITEM(st_,j));
        if (fl && PyTuple_Check(fl)) for (int j=0;j<4;j++) fi[j]=(float)PyFloat_AsDouble(PyTuple_GET_ITEM(fl,j));
        if (ex && PyTuple_Check(ex)) for (int j=0;j<4;j++) e[j]=(float)PyFloat_AsDouble(PyTuple_GET_ITEM(ex,j));
        if (PyErr_Occurred()) PyErr_Clear();
        pack_quad(p, sy_, fi, e, ccmd->qb);
        Py_XDECREF(pt); Py_XDECREF(st_); Py_XDECREF(fl); Py_XDECREF(ex);

        PyObject *tex = PyObject_GetAttr(cmd, s_texture);
        ccmd->texture = (tex && tex != Py_None) ? tex : NULL;
        if (tex == Py_None) Py_DECREF(tex);
    };

    /* Free old dynamic cmds */
    if (cache->dyn_cmds) {
        for (int i = 0; i < cache->dyn_count; i++) Py_XDECREF(cache->dyn_cmds[i].texture);
    }
    int old_cmd_count = cache->cmd_count;

    if (n <= 2) {
        /* Small: store in fixed cmds[2] */
        cache->cmd_count = 0;
        for (Py_ssize_t i = 0; i < n; i++) {
            parse_cmd(PyList_GET_ITEM(py_cmds, i), &cache->cmds[cache->cmd_count]);
            Py_XINCREF(cache->cmds[cache->cmd_count].texture);
            cache->cmd_count++;
        }
        /* Clear dyn */
        if (cache->dyn_cmds) { free(cache->dyn_cmds); cache->dyn_cmds = NULL; }
        cache->dyn_count = 0; cache->dyn_capacity = 0;
    } else {
        /* Large: store ALL in dyn_cmds */
        cache->cmd_count = 0;
        if (cache->dyn_capacity < (int)n) {
            cache->dyn_cmds = (CCmd *)realloc(cache->dyn_cmds, sizeof(CCmd) * n);
            cache->dyn_capacity = (int)n;
        }
        cache->dyn_count = (int)n;
        for (Py_ssize_t i = 0; i < n; i++) {
            parse_cmd(PyList_GET_ITEM(py_cmds, i), &cache->dyn_cmds[i]);
            Py_XINCREF(cache->dyn_cmds[i].texture);
        }
    }

    for (int i = cache->cmd_count; i < old_cmd_count; i++) {
        Py_XDECREF(cache->cmds[i].texture); cache->cmds[i].texture = NULL;
    }

    Py_DECREF(py_cmds); Py_DECREF(world_tuple); Py_DECREF(py_order);
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Core recursive collect                                                  */
/* ─────────────────────────────────────────────────────────────────────── */

static void collect_recursive(PyObject *node, const double ptf[6], double pop,
                              CollectState *st) {
    if (st->render_error) return;
    /* Visibility check */
    if (!read_bool(node, s_visible)) return;
    if (pop <= 0.001) return;

    st->fingerprint = fingerprint_mix(st->fingerprint, (unsigned long long)(uintptr_t)node);

    /* Get/create cache */
    CNodeCache *cache = get_cache(node);
    if (!cache) { PyErr_Clear(); return; }

    /* Determine type (cached after first identification) */
    if (!cache->valid || cache->type_id == NTYPE_UNKNOWN) {
        cache->type_id = identify_type(node);
    }
    st->fingerprint = fingerprint_mix(st->fingerprint, (unsigned long long)(cache->type_id + 17));
    if (cache->type_id == NTYPE_PARTICLE || cache->type_id == NTYPE_SHADERNODE) {
        st->requires_continuous_render = 1;
    }

    /* Read base attributes */
    double base[7];
    read_base(node, base);

    /* Cache comparison: base + context */
    /* For shape params, we compare on cache miss side since they differ per type */
    int hit = cache->valid
        && memcmp(base, cache->base, 7 * sizeof(double)) == 0
        && memcmp(ptf, cache->parent_tf, 6 * sizeof(double)) == 0
        && pop == cache->parent_op
        && st->screen_scale == cache->screen_scale;

    /* Layer content can change without changing the layer transform.  For now
       re-emit layer quads whenever a layer is present rather than treating the
       texture wrapper as a fully static cache entry. */
    if (hit && cache->type_id == NTYPE_LAYER) {
        hit = 0;
    }

    /* Validate shape attributes and custom snapshots even when transforms match.
       NTYPE_UNKNOWN is negative, so it must not be excluded by an ordering check. */
    if (hit && cache->type_id != NTYPE_GROUP && cache->type_id != NTYPE_LAYER) {
        /* Read shape-specific color objects and compare pointers */
        if (cache->type_id == NTYPE_CIRCLE || cache->type_id == NTYPE_RECT) {
            PyObject *f = PyObject_GetAttr(node, s_fill);
            PyObject *sk = PyObject_GetAttr(node, s_stroke);
            if (f != cache->colors[0] || sk != cache->colors[1]) hit = 0;
            /* Also compare shape params */
            if (hit) {
                if (cache->type_id == NTYPE_CIRCLE) {
                    double r = read_double(node, s_radius);
                    double sw = read_double(node, s_stroke_width);
                    double sh[2] = {r, sw};
                    if (memcmp(sh, cache->shape, 2*sizeof(double)) != 0) hit = 0;
                } else { /* RECT */
                    double w = read_double(node, s_width);
                    double h = read_double(node, s_height);
                    double cr = read_double(node, s_radius);
                    double sw = read_double(node, s_stroke_width);
                    double sh[4] = {w, h, cr, sw};
                    if (memcmp(sh, cache->shape, 4*sizeof(double)) != 0) hit = 0;
                }
            }
            Py_XDECREF(f); Py_XDECREF(sk);
        } else if (cache->type_id == NTYPE_LINE) {
            PyObject *c = PyObject_GetAttr(node, s_color);
            if (c != cache->colors[0]) hit = 0;
            if (hit) {
                PyObject *st_obj = PyObject_GetAttr(node, s_start);
                PyObject *en_obj = PyObject_GetAttr(node, s_end);
                double lw = read_double(node, s_width);
                double sx_=0,sy_=0,ex_=0,ey_=0;
                if (st_obj && PyTuple_Check(st_obj)) { sx_=PyFloat_AsDouble(PyTuple_GET_ITEM(st_obj,0)); sy_=PyFloat_AsDouble(PyTuple_GET_ITEM(st_obj,1)); }
                if (en_obj && PyTuple_Check(en_obj)) { ex_=PyFloat_AsDouble(PyTuple_GET_ITEM(en_obj,0)); ey_=PyFloat_AsDouble(PyTuple_GET_ITEM(en_obj,1)); }
                if (PyErr_Occurred()) PyErr_Clear();
                double sh[5] = {sx_, sy_, ex_, ey_, lw};
                if (memcmp(sh, cache->shape, 5*sizeof(double)) != 0) hit = 0;
                Py_XDECREF(st_obj); Py_XDECREF(en_obj);
            }
            Py_XDECREF(c);
        } else if (cache->type_id == NTYPE_LABEL) {
            PyObject *c = PyObject_GetAttr(node, s_color);
            PyObject *t = PyObject_GetAttr(node, s_text);
            PyObject *fo = PyObject_GetAttr(node, s_font);
            if (c != cache->colors[0]) hit = 0;
            /* For text, compare content not just pointer */
            if (hit && t && cache->colors[1]) {
                int eq = PyObject_RichCompareBool(t, cache->colors[1], Py_EQ);
                if (eq != 1) hit = 0;
            } else if (t != cache->colors[1]) {
                hit = 0;
            }
            if (hit && fo != cache->colors[2]) {
                int eq = (fo && cache->colors[2]) ? PyObject_RichCompareBool(fo, cache->colors[2], Py_EQ) : 0;
                if (eq != 1) hit = 0;
            }
            if (hit) {
                double fs = read_double(node, s_size);
                double pfs = round(fs * st->screen_scale);
                if (pfs < 10) pfs = 10;
                double sh[2] = {fs, (double)pfs};
                if (memcmp(sh, cache->shape, 2*sizeof(double)) != 0) hit = 0;
            }
            Py_XDECREF(c); Py_XDECREF(t); Py_XDECREF(fo);
        } else if (cache->type_id == NTYPE_IMAGE) {
            PyObject *t = PyObject_GetAttr(node, s_texture);
            PyObject *isz = PyObject_GetAttr(node, s_img_size);
            long long h1 = 0;
            if (t && t != Py_None) {
                PyObject *hobj = PyObject_GetAttr(t, s__handle);
                if (hobj) { h1 = PyLong_AsLongLong(hobj); Py_DECREF(hobj); }
                if (PyErr_Occurred()) { PyErr_Clear(); h1 = 0; }
            }
            if (t != cache->colors[0]) hit = 0;
            if (hit) {
                double iw=0, ih=0;
                if (isz && PyTuple_Check(isz)) {
                    iw = PyFloat_AsDouble(PyTuple_GET_ITEM(isz, 0));
                    ih = PyFloat_AsDouble(PyTuple_GET_ITEM(isz, 1));
                }
                if (PyErr_Occurred()) PyErr_Clear();
                double sh[2] = {iw, ih};
                if (memcmp(sh, cache->shape, 2*sizeof(double)) != 0) hit = 0;
            }
            Py_XDECREF(t); Py_XDECREF(isz);
        } else if (cache->type_id == NTYPE_SPRITE) {
            PyObject *t = PyObject_GetAttr(node, s_texture);
            PyObject *tn = PyObject_GetAttr(node, s_tint);
            /* colors[0]=texture, colors[1]=tint */
            if (t != cache->colors[0] || tn != cache->colors[1]) hit = 0;
            if (hit) {
                PyObject *ssz = PyObject_GetAttr(node, s_sprite_size);
                PyObject *anch = PyObject_GetAttr(node, s_anchor);
                double sw_=0,sh_=0,ax_=0.5,ay_=0.5;
                if (ssz && PyTuple_Check(ssz) && PyTuple_GET_SIZE(ssz)>=2) {
                    sw_ = PyFloat_AsDouble(PyTuple_GET_ITEM(ssz,0));
                    sh_ = PyFloat_AsDouble(PyTuple_GET_ITEM(ssz,1));
                }
                if (anch && PyTuple_Check(anch) && PyTuple_GET_SIZE(anch)>=2) {
                    ax_ = PyFloat_AsDouble(PyTuple_GET_ITEM(anch,0));
                    ay_ = PyFloat_AsDouble(PyTuple_GET_ITEM(anch,1));
                }
                int fx = read_bool(node, s_flip_x);
                int fy = read_bool(node, s_flip_y);
                PyObject *uvr = PyObject_GetAttr(node, s__uv_rect);
                double u0=0,v0_=0,u1=1,v1_=1;
                if (uvr && PyTuple_Check(uvr) && PyTuple_GET_SIZE(uvr)>=4) {
                    u0 = PyFloat_AsDouble(PyTuple_GET_ITEM(uvr,0));
                    v0_ = PyFloat_AsDouble(PyTuple_GET_ITEM(uvr,1));
                    u1 = PyFloat_AsDouble(PyTuple_GET_ITEM(uvr,2));
                    v1_ = PyFloat_AsDouble(PyTuple_GET_ITEM(uvr,3));
                }
                if (PyErr_Occurred()) PyErr_Clear();
                double sh[10] = {sw_,sh_,ax_,ay_,(double)fx,(double)fy,u0,v0_,u1,v1_};
                if (memcmp(sh, cache->shape, 10*sizeof(double)) != 0) hit = 0;
                Py_XDECREF(ssz); Py_XDECREF(anch); Py_XDECREF(uvr);
            }
            Py_XDECREF(t); Py_XDECREF(tn);
        } else if (cache->type_id == NTYPE_SHADERNODE) {
            PyObject *t = PyObject_GetAttr(node, s__tex);
            PyObject *ssz = PyObject_GetAttr(node, s__shader_size);
            if (t != cache->colors[0]) hit = 0;
            if (hit) {
                double sw=0, sh=0;
                if (ssz && PyTuple_Check(ssz) && PyTuple_GET_SIZE(ssz)>=2) {
                    sw = PyFloat_AsDouble(PyTuple_GET_ITEM(ssz, 0));
                    sh = PyFloat_AsDouble(PyTuple_GET_ITEM(ssz, 1));
                }
                if (PyErr_Occurred()) PyErr_Clear();
                double shape[2] = {sw, sh};
                if (memcmp(shape, cache->shape, 2*sizeof(double)) != 0) hit = 0;
            }
            Py_XDECREF(t); Py_XDECREF(ssz);
        } else if (cache->type_id == NTYPE_PATH) {
            PyObject *t = PyObject_GetAttr(node, s__tex);
            if (t != cache->colors[0]) hit = 0;
            if (hit) {
                PyObject *psz = PyObject_GetAttr(node, s__path_size);
                PyObject *pcenter = PyObject_GetAttr(node, s__path_center);
                double sw = 0, sh = 0, pcx = 0, pcy = 0;
                if (psz && PyTuple_Check(psz) && PyTuple_GET_SIZE(psz) >= 2) {
                    sw = PyFloat_AsDouble(PyTuple_GET_ITEM(psz, 0));
                    sh = PyFloat_AsDouble(PyTuple_GET_ITEM(psz, 1));
                }
                if (pcenter && PyTuple_Check(pcenter) && PyTuple_GET_SIZE(pcenter) >= 2) {
                    pcx = PyFloat_AsDouble(PyTuple_GET_ITEM(pcenter, 0));
                    pcy = PyFloat_AsDouble(PyTuple_GET_ITEM(pcenter, 1));
                }
                double version = read_double(node, s__path_version);
                if (PyErr_Occurred()) PyErr_Clear();
                double shv[5] = { sw, sh, pcx, pcy, version };
                if (memcmp(shv, cache->shape, 5*sizeof(double)) != 0) hit = 0;
                Py_XDECREF(psz); Py_XDECREF(pcenter);
            }
            Py_XDECREF(t);
        } else if (cache->type_id == NTYPE_NINESLICE) {
            PyObject *t = PyObject_GetAttr(node, s_texture);
            PyObject *tn = PyObject_GetAttr(node, s_tint);
            if (t != cache->colors[0] || tn != cache->colors[1]) hit = 0;
            if (hit) {
                PyObject *nsz = PyObject_GetAttr(node, s_nine_size);
                PyObject *ins = PyObject_GetAttr(node, s_insets);
                double nw=0,nh=0, il=0,it_=0,ir=0,ib=0;
                if (nsz && PyTuple_Check(nsz) && PyTuple_GET_SIZE(nsz)>=2) {
                    nw = PyFloat_AsDouble(PyTuple_GET_ITEM(nsz,0));
                    nh = PyFloat_AsDouble(PyTuple_GET_ITEM(nsz,1));
                }
                if (ins && PyTuple_Check(ins) && PyTuple_GET_SIZE(ins)>=4) {
                    il = PyFloat_AsDouble(PyTuple_GET_ITEM(ins,0));
                    it_ = PyFloat_AsDouble(PyTuple_GET_ITEM(ins,1));
                    ir = PyFloat_AsDouble(PyTuple_GET_ITEM(ins,2));
                    ib = PyFloat_AsDouble(PyTuple_GET_ITEM(ins,3));
                }
                if (PyErr_Occurred()) PyErr_Clear();
                float tnt[4] = {1,1,1,1};
                if (tn) c_parse_color(tn, tnt);
                PyObject *hobj = t ? PyObject_GetAttr(t, s__handle) : NULL;
                double tex_h = hobj ? (double)PyLong_AsLongLong(hobj) : 0;
                Py_XDECREF(hobj);
                if (PyErr_Occurred()) PyErr_Clear();
                double sh[11] = { nw,nh, il,it_,ir,ib, tnt[0],tnt[1],tnt[2],tnt[3], tex_h };
                if (memcmp(sh, cache->shape, 11*sizeof(double)) != 0) hit = 0;
                Py_XDECREF(nsz); Py_XDECREF(ins);
            }
            Py_XDECREF(t); Py_XDECREF(tn);
        } else if (cache->type_id == NTYPE_UNKNOWN) {
            /* For unknown types, compare Python _snap() */
            PyObject *snap = PyObject_CallMethodObjArgs(node, s__snap, NULL);
            int eq = (snap && cache->snap_cache)
                ? PyObject_RichCompareBool(snap, cache->snap_cache, Py_EQ) : 0;
            if (eq != 1) hit = 0;
            Py_XDECREF(snap);
            if (PyErr_Occurred()) PyErr_Clear();
        }
    }

    if (!hit) {
        st->any_miss = 1;
    }

    if (hit) {
        /* CACHE HIT: reuse cached cmds, just update order */
        /* For unknown types, use dyn_cmds if available */
        if (cache->dyn_cmds && cache->dyn_count > 0) {
            for (int i = 0; i < cache->dyn_count; i++) {
                CCmd *dst = state_append(st);
                memcpy(dst, &cache->dyn_cmds[i], sizeof(CCmd));
                st->order++;
                dst->order = st->order;
                if (dst->kind == KIND_PARTICLE) {
                    if (st->particle_count >= st->particle_capacity) {
                        st->particle_capacity *= 2;
                        st->particle_emitters = (PyObject **)realloc(st->particle_emitters,
                            sizeof(PyObject *) * st->particle_capacity);
                    }
                    dst->particle_idx = st->particle_count;
                    st->particle_emitters[st->particle_count++] = node;
                }
            }
        } else {
            for (int i = 0; i < cache->cmd_count; i++) {
                CCmd *src = &cache->cmds[i];
                CCmd *dst = state_append(st);
                memcpy(dst, src, sizeof(CCmd));
                st->order++;
                dst->order = st->order;
                /* Particle cmds: re-register emitter for this frame */
                if (dst->kind == KIND_PARTICLE) {
                    if (st->particle_count >= st->particle_capacity) {
                        st->particle_capacity *= 2;
                        st->particle_emitters = (PyObject **)realloc(st->particle_emitters,
                            sizeof(PyObject *) * st->particle_capacity);
                    }
                    dst->particle_idx = st->particle_count;
                    st->particle_emitters[st->particle_count++] = node;
                }
            }
        }
    } else {
        /* CACHE MISS: compute world transform and emit */
        double local[6], world[6];
        c_matrix(base[0], base[1], base[2], base[3], base[4], local);
        c_mul(ptf, local, world);
        double wop = pop * base[5]; /* opacity */

        memcpy(cache->base, base, sizeof(base));
        memcpy(cache->parent_tf, ptf, 6 * sizeof(double));
        cache->parent_op = pop;
        cache->screen_scale = st->screen_scale;
        memcpy(cache->world, world, sizeof(world));
        cache->world_op = wop;

        /* Export world transform to Python for hit testing / collision detection */
        {
            PyObject *wt = Py_BuildValue("(dddddd)", world[0], world[1], world[2],
                                          world[3], world[4], world[5]);
            if (wt) { PyObject_SetAttr(node, s__world_transform, wt); Py_DECREF(wt); }
            PyObject *wo = PyFloat_FromDouble(wop);
            if (wo) { PyObject_SetAttr(node, s__world_opacity, wo); Py_DECREF(wo); }
            if (PyErr_Occurred()) PyErr_Clear();
        }

        switch (cache->type_id) {
            case NTYPE_GROUP:
                cache->cmd_count = 0;
                break;
            case NTYPE_CIRCLE:
                emit_circle(node, cache, st, world, wop);
                break;
            case NTYPE_RECT:
                emit_rect(node, cache, st, world, wop);
                break;
            case NTYPE_LINE:
                emit_line(node, cache, st, world, wop);
                break;
            case NTYPE_LABEL:
                emit_label(node, cache, st, world, wop);
                break;
            case NTYPE_IMAGE:
                emit_image(node, cache, st, world, wop);
                break;
            case NTYPE_SPRITE:
                emit_sprite(node, cache, st, world, wop);
                break;
            case NTYPE_SHADERNODE:
                emit_shadernode(node, cache, st, world, wop);
                break;
            case NTYPE_PATH:
                emit_path(node, cache, st, world, wop);
                break;
            case NTYPE_NINESLICE:
                emit_nineslice(node, cache, st, world, wop);
                break;
            case NTYPE_PARTICLE: {
                /* Emit a KIND_PARTICLE placeholder CCmd for z-order sorting */
                cache->cmd_count = 1;
                CCmd *cmd = &cache->cmds[0];
                cmd->z = cache->base[6];  /* node.z */
                st->order++;
                cmd->order = st->order;
                cmd->kind = KIND_PARTICLE;
                cmd->texture = NULL;
                cmd->mesh_batch_idx = -1;
                /* Store emitter ref */
                if (st->particle_count >= st->particle_capacity) {
                    st->particle_capacity *= 2;
                    st->particle_emitters = (PyObject **)realloc(st->particle_emitters,
                        sizeof(PyObject *) * st->particle_capacity);
                }
                cmd->particle_idx = st->particle_count;
                st->particle_emitters[st->particle_count++] = node;  /* borrowed ref, valid for frame */
                /* Call Python _emit to trigger _init_gpu on first frame */
                {
                    static PyObject *s__pbuf = NULL;
                    if (!s__pbuf) s__pbuf = PyUnicode_InternFromString("_pbuf");
                    PyObject *pbuf = PyObject_GetAttr(node, s__pbuf);
                    int need_init = (!pbuf || pbuf == Py_None);
                    Py_XDECREF(pbuf);
                    if (need_init) {
                        PyObject *cmds_list = PyList_New(0);
                        PyObject *world_tuple = Py_BuildValue("(dddddd)", world[0], world[1], world[2], world[3], world[4], world[5]);
                        PyObject *py_op = PyFloat_FromDouble(wop);
                        PyObject *py_order = PyList_New(1);
                        PyList_SET_ITEM(py_order, 0, PyLong_FromLong(st->order));
                        PyObject *result = PyObject_CallMethodObjArgs(node, s__emit,
                            cmds_list, st->renderer, world_tuple, py_op, py_order, NULL);
                        Py_XDECREF(result);
                        Py_XDECREF(py_op);
                        Py_DECREF(world_tuple);
                        Py_DECREF(cmds_list);
                        Py_DECREF(py_order);
                        state_take_render_error(st);
                    }
                }
                break;
            }
            case NTYPE_LAYER:
                handle_layer(node, cache, st, world, wop);
                if (st->render_error) return;
                /* Append Layer cmds to state, then return (no child recursion) */
                for (int i = 0; i < cache->cmd_count; i++) {
                    CCmd *dst = state_append(st);
                    memcpy(dst, &cache->cmds[i], sizeof(CCmd));
                }
                cache->valid = 1;
                return;
            default:
                handle_unknown_emit(node, cache, st, world, wop);
                break;
        }

        /* Append newly emitted cmds to state */
        if (st->render_error) {
            cache->valid = 0;
            return;
        }
        if (cache->dyn_cmds && cache->dyn_count > 0) {
            for (int i = 0; i < cache->dyn_count; i++) {
                CCmd *dst = state_append(st);
                memcpy(dst, &cache->dyn_cmds[i], sizeof(CCmd));
            }
        } else {
            for (int i = 0; i < cache->cmd_count; i++) {
                CCmd *dst = state_append(st);
                memcpy(dst, &cache->cmds[i], sizeof(CCmd));
            }
        }

        /* ── Compute collider & bounds from world + shape cache ── */
        {
            double sxw = hypot(world[0], world[1]);
            double syw = hypot(world[2], world[3]);
            double rotw = c_rot(world);
            double wcx, wcy;

            cache->coll_type = -1;
            cache->has_bounds = 0;

            switch (cache->type_id) {
            case NTYPE_CIRCLE: {
                /* shape: [0]=radius, [1]=stroke_width */
                double r = cache->shape[0];
                double sw = cache->shape[1];
                c_apply(world, 0, 0, &wcx, &wcy);
                cache->coll_type = COLL_CIRCLE;
                cache->coll[0] = wcx; cache->coll[1] = wcy;
                cache->coll[2] = r * c_avg_scale(world);
                double e = r + sw * 0.5;
                cache->local_bounds[0] = -e; cache->local_bounds[1] = -e;
                cache->local_bounds[2] = e;  cache->local_bounds[3] = e;
                cache->has_bounds = 1;
                break;
            }
            case NTYPE_RECT: {
                /* shape: [0]=width, [1]=height, [2]=corner_radius, [3]=stroke_width */
                double w = cache->shape[0], h = cache->shape[1], sw = cache->shape[3];
                double p = sw * 0.5;
                c_apply(world, 0, 0, &wcx, &wcy);
                cache->coll_type = COLL_OBB;
                cache->coll[0] = wcx; cache->coll[1] = wcy;
                cache->coll[2] = w * sxw / 2; cache->coll[3] = h * syw / 2;
                cache->coll[4] = rotw;
                cache->local_bounds[0] = -w/2-p; cache->local_bounds[1] = -h/2-p;
                cache->local_bounds[2] = w/2+p;  cache->local_bounds[3] = h/2+p;
                cache->has_bounds = 1;
                break;
            }
            case NTYPE_LINE: {
                /* shape: [0]=sx, [1]=sy, [2]=ex, [3]=ey, [4]=line_width */
                double lsx = cache->shape[0], lsy = cache->shape[1];
                double lex = cache->shape[2], ley = cache->shape[3];
                double lw = cache->shape[4];
                double wsx, wsy, wex, wey;
                c_apply(world, lsx, lsy, &wsx, &wsy);
                c_apply(world, lex, ley, &wex, &wey);
                double s = c_avg_scale(world);
                double cx_ = (wsx + wex) / 2, cy_ = (wsy + wey) / 2;
                double angle = atan2(wey - wsy, wex - wsx);
                double half_len = hypot(wex - wsx, wey - wsy) / 2;
                cache->coll_type = COLL_OBB;
                cache->coll[0] = cx_; cache->coll[1] = cy_;
                cache->coll[2] = half_len + lw * s / 2;
                cache->coll[3] = lw * s / 2;
                cache->coll[4] = angle;
                /* Local bounds */
                double xmin = fmin(lsx, lex), xmax = fmax(lsx, lex);
                double ymin = fmin(lsy, ley), ymax = fmax(lsy, ley);
                double p = lw * 0.5;
                cache->local_bounds[0] = xmin-p; cache->local_bounds[1] = ymin-p;
                cache->local_bounds[2] = xmax+p; cache->local_bounds[3] = ymax+p;
                cache->has_bounds = 1;
                /* For line-specific contains_point */
                cache->line_start[0] = lsx; cache->line_start[1] = lsy;
                cache->line_end[0] = lex;   cache->line_end[1] = ley;
                cache->line_hw = lw * 0.5;
                break;
            }
            case NTYPE_LABEL: {
                /* shape: [0]=font_size, [1]=pixel_font_size */
                double fs = cache->shape[0];
                c_apply(world, 0, 0, &wcx, &wcy);
                /* Try _rendered_size, fall back to estimate */
                PyObject *rsz = PyObject_GetAttr(node, s__rendered_size);
                double rw = 0, rh = 0;
                if (rsz && PyTuple_Check(rsz) && PyTuple_GET_SIZE(rsz) >= 2) {
                    rw = PyFloat_AsDouble(PyTuple_GET_ITEM(rsz, 0));
                    rh = PyFloat_AsDouble(PyTuple_GET_ITEM(rsz, 1));
                }
                Py_XDECREF(rsz);
                if (rw <= 0 || rh <= 0) {
                    /* Estimate from font size and text length */
                    PyObject *txt = PyObject_GetAttr(node, s_text);
                    Py_ssize_t tlen = (txt && PyUnicode_Check(txt)) ? PyUnicode_GET_LENGTH(txt) : 1;
                    Py_XDECREF(txt);
                    rw = fmax(fs * 0.6 * fmax(tlen, 1), fs * 0.6);
                    rh = fs * 1.2;
                }
                if (PyErr_Occurred()) PyErr_Clear();
                cache->coll_type = COLL_OBB;
                cache->coll[0] = wcx; cache->coll[1] = wcy;
                cache->coll[2] = rw * sxw / 2; cache->coll[3] = rh * syw / 2;
                cache->coll[4] = rotw;
                cache->local_bounds[0] = -rw/2; cache->local_bounds[1] = -rh/2;
                cache->local_bounds[2] = rw/2;  cache->local_bounds[3] = rh/2;
                cache->has_bounds = 1;
                break;
            }
            case NTYPE_IMAGE: {
                /* shape: [0]=img_w, [1]=img_h */
                double iw = cache->shape[0], ih = cache->shape[1];
                c_apply(world, 0, 0, &wcx, &wcy);
                cache->coll_type = COLL_OBB;
                cache->coll[0] = wcx; cache->coll[1] = wcy;
                cache->coll[2] = iw * sxw / 2; cache->coll[3] = ih * syw / 2;
                cache->coll[4] = rotw;
                cache->local_bounds[0] = -iw/2; cache->local_bounds[1] = -ih/2;
                cache->local_bounds[2] = iw/2;  cache->local_bounds[3] = ih/2;
                cache->has_bounds = 1;
                break;
            }
            case NTYPE_SPRITE: {
                /* shape: [0]=sw, [1]=sh, [2]=ax, [3]=ay, ... */
                double sw_ = cache->shape[0], sh_ = cache->shape[1];
                double ax = cache->shape[2], ay = cache->shape[3];
                double off_x = (0.5 - ax) * sw_;
                double off_y = (0.5 - ay) * sh_;
                c_apply(world, off_x, off_y, &wcx, &wcy);
                cache->coll_type = COLL_OBB;
                cache->coll[0] = wcx; cache->coll[1] = wcy;
                cache->coll[2] = sw_ * sxw / 2; cache->coll[3] = sh_ * syw / 2;
                cache->coll[4] = rotw;
                double x0 = -ax * sw_, y0 = -ay * sh_;
                cache->local_bounds[0] = x0; cache->local_bounds[1] = y0;
                cache->local_bounds[2] = x0 + sw_; cache->local_bounds[3] = y0 + sh_;
                cache->has_bounds = 1;
                break;
            }
            case NTYPE_NINESLICE: {
                /* shape: [0]=nw, [1]=nh, ... */
                double nw = cache->shape[0], nh = cache->shape[1];
                c_apply(world, 0, 0, &wcx, &wcy);
                cache->coll_type = COLL_OBB;
                cache->coll[0] = wcx; cache->coll[1] = wcy;
                cache->coll[2] = nw * sxw / 2; cache->coll[3] = nh * syw / 2;
                cache->coll[4] = rotw;
                cache->local_bounds[0] = -nw/2; cache->local_bounds[1] = -nh/2;
                cache->local_bounds[2] = nw/2;  cache->local_bounds[3] = nh/2;
                cache->has_bounds = 1;
                break;
            }
            case NTYPE_SHADERNODE: {
                /* shape: [0]=sw, [1]=sh */
                double sw_ = cache->shape[0], sh_ = cache->shape[1];
                c_apply(world, 0, 0, &wcx, &wcy);
                cache->coll_type = COLL_OBB;
                cache->coll[0] = wcx; cache->coll[1] = wcy;
                cache->coll[2] = sw_ * sxw / 2; cache->coll[3] = sh_ * syw / 2;
                cache->coll[4] = rotw;
                cache->local_bounds[0] = -sw_/2; cache->local_bounds[1] = -sh_/2;
                cache->local_bounds[2] = sw_/2;  cache->local_bounds[3] = sh_/2;
                cache->has_bounds = 1;
                break;
            }
            case NTYPE_PATH: {
                PyObject *bounds = PyObject_CallMethodObjArgs(node, s__bounds, NULL);
                if (bounds && PyTuple_Check(bounds) && PyTuple_GET_SIZE(bounds) >= 4) {
                    double x0 = PyFloat_AsDouble(PyTuple_GET_ITEM(bounds, 0));
                    double y0 = PyFloat_AsDouble(PyTuple_GET_ITEM(bounds, 1));
                    double x1 = PyFloat_AsDouble(PyTuple_GET_ITEM(bounds, 2));
                    double y1 = PyFloat_AsDouble(PyTuple_GET_ITEM(bounds, 3));
                    if (!PyErr_Occurred()) {
                        double cx_ = (x0 + x1) * 0.5;
                        double cy_ = (y0 + y1) * 0.5;
                        c_apply(world, cx_, cy_, &wcx, &wcy);
                        cache->coll_type = COLL_OBB;
                        cache->coll[0] = wcx; cache->coll[1] = wcy;
                        cache->coll[2] = (x1 - x0) * sxw / 2; cache->coll[3] = (y1 - y0) * syw / 2;
                        cache->coll[4] = rotw;
                        cache->local_bounds[0] = x0; cache->local_bounds[1] = y0;
                        cache->local_bounds[2] = x1; cache->local_bounds[3] = y1;
                        cache->has_bounds = 1;
                    } else {
                        PyErr_Clear();
                    }
                }
                Py_XDECREF(bounds);
                break;
            }
            default:
                /* GROUP, LAYER, UNKNOWN: no C-side collider */
                cache->coll_type = -1;
                cache->has_bounds = 0;
                break;
            }
        }

        cache->valid = 1;
    }

    /* Collect interactive nodes (for hit testing) — use cached bounds */
    if (node != st->root && cache->type_id != NTYPE_GROUP) {
        int is_interactive = read_bool(node, s_interactive);
        st->fingerprint = fingerprint_mix(st->fingerprint, is_interactive ? 0xa11ceULL : 0x51a7eULL);
        if (is_interactive) {
            if (cache->has_bounds) {
                state_add_interactive(st, node, cache->base[6], st->order);
            } else if (cache->type_id == NTYPE_UNKNOWN) {
                /* Fallback: call Python _bounds() */
                PyObject *bounds = PyObject_CallMethodObjArgs(node, s__bounds, NULL);
                if (bounds && bounds != Py_None) {
                    state_add_interactive(st, node, cache->base[6], st->order);
                }
                Py_XDECREF(bounds);
                if (PyErr_Occurred()) PyErr_Clear();
            }
        }
    }

    /* Recurse into children */
    PyObject *children = PyObject_GetAttr(node, s_children);
    if (children && PyList_Check(children)) {
        const double *w = cache->world;
        double wop = cache->world_op;
        Py_ssize_t n = PyList_GET_SIZE(children);
        st->fingerprint = fingerprint_mix(st->fingerprint, (unsigned long long)n);
        for (Py_ssize_t i = 0; i < n; i++) {
            collect_recursive(PyList_GET_ITEM(children, i), w, wop, st);
        }
    } else {
        st->fingerprint = fingerprint_mix(st->fingerprint, 0ULL);
    }
    Py_XDECREF(children);
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Sort comparator                                                         */
/* ─────────────────────────────────────────────────────────────────────── */

static int cmp_ccmd(const void *a, const void *b) {
    const CCmd *ca = (const CCmd *)a;
    const CCmd *cb = (const CCmd *)b;
    if (ca->z < cb->z) return -1;
    if (ca->z > cb->z) return 1;
    return (ca->order > cb->order) - (ca->order < cb->order);
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Main entry: accel_collect                                               */
/* ─────────────────────────────────────────────────────────────────────── */

static PyObject *accel_collect(PyObject *self, PyObject *args) {
    PyObject *root, *tf_tuple, *renderer;
    double opacity, screen_scale;
    unsigned long long previous_fingerprint = 0;

    if (!PyArg_ParseTuple(args, "OOddO|K", &root, &tf_tuple, &opacity, &screen_scale, &renderer,
                          &previous_fingerprint))
        return NULL;

    if (ensure_types() < 0) return NULL;

    /* Parse transform tuple */
    double tf[6];
    if (!PyTuple_Check(tf_tuple) || PyTuple_GET_SIZE(tf_tuple) < 6) {
        PyErr_SetString(PyExc_ValueError, "transform must be a 6-tuple");
        return NULL;
    }
    for (int i = 0; i < 6; i++)
        tf[i] = PyFloat_AsDouble(PyTuple_GET_ITEM(tf_tuple, i));
    if (PyErr_Occurred()) return NULL;

    /* Collect (with autorelease pool for Python-thread Metal calls) */
    CollectState st;
    @autoreleasepool {
    state_init(&st, screen_scale, renderer, root);
    collect_recursive(root, tf, opacity, &st);
    }
    if (st.render_error) {
        PyObject *error = st.render_error;
        st.render_error = NULL;
        state_destroy(&st);
        PyErr_SetRaisedException(error);
        return NULL;
    }

    /* Sort by (z, order) */
    if (st.count > 1)
        qsort(st.cmds, st.count, sizeof(CCmd), cmp_ccmd);

    if (previous_fingerprint != 0
            && !st.any_miss
            && !st.requires_continuous_render
            && st.fingerprint == previous_fingerprint) {
        unsigned long long fingerprint = st.fingerprint;
        state_destroy(&st);
        return Py_BuildValue("(OK)", Py_None, fingerprint);
    }

    /* Pack quad cmds into bytes (skip KIND_MESH entries) and build unified batch list */
    int n = st.count;
    /* Count quad cmds for buffer sizing (skip mesh + particle placeholders) */
    int quad_count = 0;
    for (int i = 0; i < n; i++) {
        if (st.cmds[i].kind != KIND_MESH && st.cmds[i].kind != KIND_PARTICLE) quad_count++;
    }
    PyObject *vb_bytes = PyBytes_FromStringAndSize(NULL, quad_count * 96);
    PyObject *qb_bytes = PyBytes_FromStringAndSize(NULL, quad_count * 64);
    if (!vb_bytes || !qb_bytes) {
        Py_XDECREF(vb_bytes); Py_XDECREF(qb_bytes);
        state_destroy(&st);
        return NULL;
    }
    uint8_t *vb = (uint8_t *)PyBytes_AS_STRING(vb_bytes);
    uint8_t *qb = (uint8_t *)PyBytes_AS_STRING(qb_bytes);

    /* Remap: pack quads densely, track new indices */
    int *quad_idx = (int *)malloc(sizeof(int) * n);
    int qi = 0;
    for (int i = 0; i < n; i++) {
        if (st.cmds[i].kind != KIND_MESH && st.cmds[i].kind != KIND_PARTICLE) {
            memcpy(vb + qi * 96, st.cmds[i].vb, 96);
            memcpy(qb + qi * 64, st.cmds[i].qb, 64);
            quad_idx[i] = qi++;
        } else {
            quad_idx[i] = -1;
        }
    }

    /* Build unified batch list: quad batches and mesh entries interleaved by z-order.
     * Quad batch: (start, end, is_tex, texture)  — start/end in packed quad indices
     * Mesh entry: (-1, mesh_batch_idx, False, None) — mesh_batch_idx references mesh_batches
     */
    PyObject *batches = PyList_New(0);
    if (n > 0) {
        int bs = -1, pk_tex = -1;
        PyObject *pk_tobj = NULL;
        for (int i = 0; i <= n; i++) {
            int is_mesh = (i < n && st.cmds[i].kind == KIND_MESH);
            int is_particle = (i < n && st.cmds[i].kind == KIND_PARTICLE);
            if (is_mesh || is_particle || i == n) {
                /* Flush pending quad batch */
                if (bs >= 0 && pk_tex >= 0) {
                    PyObject *tex_py = pk_tobj ? pk_tobj : Py_None;
                    PyObject *item = Py_BuildValue("(iiOO)", quad_idx[bs], qi > 0 ? quad_idx[i - 1] + 1 : 0,
                        pk_tex ? Py_True : Py_False, tex_py);
                    PyList_Append(batches, item);
                    Py_DECREF(item);
                    bs = -1; pk_tex = -1; pk_tobj = NULL;
                }
                if (is_mesh) {
                    PyObject *item = Py_BuildValue("(iiOO)", -1, st.cmds[i].mesh_batch_idx,
                        Py_False, Py_None);
                    PyList_Append(batches, item);
                    Py_DECREF(item);
                } else if (is_particle) {
                    /* Particle batch entry: (-2, emitter_ref, False, None) */
                    PyObject *emitter = st.particle_emitters[st.cmds[i].particle_idx];
                    Py_INCREF(emitter);
                    PyObject *item = Py_BuildValue("(iNOO)", -2, emitter, Py_False, Py_None);
                    PyList_Append(batches, item);
                    Py_DECREF(item);
                }
            } else {
                /* Quad cmd */
                int cur_tex = (st.cmds[i].kind == KIND_TEX);
                PyObject *cur_tobj = st.cmds[i].texture;
                if (bs < 0) {
                    bs = i; pk_tex = cur_tex; pk_tobj = cur_tobj;
                } else if (cur_tex != pk_tex || cur_tobj != pk_tobj) {
                    /* Flush and start new quad batch */
                    PyObject *tex_py = pk_tobj ? pk_tobj : Py_None;
                    PyObject *item = Py_BuildValue("(iiOO)", quad_idx[bs], quad_idx[i - 1] + 1,
                        pk_tex ? Py_True : Py_False, tex_py);
                    PyList_Append(batches, item);
                    Py_DECREF(item);
                    bs = i; pk_tex = cur_tex; pk_tobj = cur_tobj;
                }
            }
        }
    }
    free(quad_idx);

    /* Build interactive nodes list sorted (z, order) descending (front-to-back) */
    /* Sort interactive entries */
    if (st.interactive_count > 1) {
        qsort(st.interactive, st.interactive_count, sizeof(InteractiveEntry),
            [](const void *a, const void *b) -> int {
                auto *ea = (const InteractiveEntry *)a;
                auto *eb = (const InteractiveEntry *)b;
                if (ea->z != eb->z) return ea->z > eb->z ? -1 : 1;
                return eb->order - ea->order;
            });
    }
    PyObject *interactive_list = PyList_New(st.interactive_count);
    for (int i = 0; i < st.interactive_count; i++) {
        Py_INCREF(st.interactive[i].node);
        PyList_SET_ITEM(interactive_list, i, st.interactive[i].node);
    }

    if (st.mesh_batch_count == 0) {
        unsigned long long fingerprint = st.fingerprint;
        state_destroy(&st);
        PyObject *result = Py_BuildValue("(OOiOOK)", vb_bytes, qb_bytes, quad_count, batches, interactive_list,
                                         fingerprint);
        Py_DECREF(vb_bytes);
        Py_DECREF(qb_bytes);
        Py_DECREF(batches);
        Py_DECREF(interactive_list);
        return result;
    }

    /* Pack mesh data for direct path rendering */
    PyObject *mesh_vb = PyBytes_FromStringAndSize((const char *)st.mesh_verts, st.mesh_vert_count * sizeof(float));
    PyObject *mesh_ib = PyBytes_FromStringAndSize((const char *)st.mesh_idx, st.mesh_idx_bytes);
    PyObject *mesh_batch_list = PyList_New(st.mesh_batch_count);
    for (int i = 0; i < st.mesh_batch_count; i++) {
        MeshBatch *mb = &st.mesh_batches[i];
        const char *fill_rule = mb->fill_rule == 1 ? "even_odd" : (mb->fill_rule == 2 ? "non_zero" : nullptr);
        PyObject *item = Py_BuildValue("(iii(ffff)iiz)",
            mb->idx_offset, mb->idx_count, mb->idx_is_32,
            mb->color[0], mb->color[1], mb->color[2], mb->color[3],
            mb->blend, mb->is_stroke, fill_rule);
        PyList_SET_ITEM(mesh_batch_list, i, item);
    }

    unsigned long long fingerprint = st.fingerprint;
    state_destroy(&st);

    PyObject *result = Py_BuildValue("(OOiOOOOOK)", vb_bytes, qb_bytes, quad_count, batches, interactive_list,
                                     mesh_vb, mesh_ib, mesh_batch_list, fingerprint);
    Py_DECREF(vb_bytes);
    Py_DECREF(qb_bytes);
    Py_DECREF(batches);
    Py_DECREF(interactive_list);
    Py_DECREF(mesh_vb);
    Py_DECREF(mesh_ib);
    Py_DECREF(mesh_batch_list);
    return result;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Python-exposed math functions (for Layer._rebuild etc.)                  */
/* ─────────────────────────────────────────────────────────────────────── */

/* matrix(pos_tuple, rot, scale) — matches Python _matrix(pos, rot, scale) */
static PyObject *py_matrix(PyObject *self, PyObject *args) {
    PyObject *pos, *scale_obj;
    double rot;
    if (!PyArg_ParseTuple(args, "OdO", &pos, &rot, &scale_obj))
        return NULL;
    if (!PyTuple_Check(pos) || PyTuple_GET_SIZE(pos) < 2) {
        PyErr_SetString(PyExc_TypeError, "pos must be (x,y)");
        return NULL;
    }
    double x = PyFloat_AsDouble(PyTuple_GET_ITEM(pos, 0));
    double y = PyFloat_AsDouble(PyTuple_GET_ITEM(pos, 1));
    double sx, sy;
    if (PyTuple_Check(scale_obj) && PyTuple_GET_SIZE(scale_obj) >= 2) {
        sx = PyFloat_AsDouble(PyTuple_GET_ITEM(scale_obj, 0));
        sy = PyFloat_AsDouble(PyTuple_GET_ITEM(scale_obj, 1));
    } else {
        sx = sy = PyFloat_AsDouble(scale_obj);
    }
    if (PyErr_Occurred()) return NULL;
    double out[6];
    c_matrix(x, y, rot, sx, sy, out);
    return Py_BuildValue("(dddddd)", out[0], out[1], out[2], out[3], out[4], out[5]);
}

static PyObject *py_mul(PyObject *self, PyObject *args) {
    PyObject *a, *b;
    if (!PyArg_ParseTuple(args, "OO", &a, &b)) return NULL;
    double ad[6], bd[6], out[6];
    for (int i = 0; i < 6; i++) {
        ad[i] = PyFloat_AsDouble(PyTuple_GET_ITEM(a, i));
        bd[i] = PyFloat_AsDouble(PyTuple_GET_ITEM(b, i));
    }
    if (PyErr_Occurred()) return NULL;
    c_mul(ad, bd, out);
    return Py_BuildValue("(dddddd)", out[0], out[1], out[2], out[3], out[4], out[5]);
}

/* apply(m, px, py) — C version takes separate x,y; Python wrapper adapts */
static PyObject *py_apply(PyObject *self, PyObject *args) {
    PyObject *m;
    double px, py_;
    if (!PyArg_ParseTuple(args, "Odd", &m, &px, &py_)) return NULL;
    if (!PyTuple_Check(m) || PyTuple_GET_SIZE(m) < 6) {
        PyErr_SetString(PyExc_TypeError, "m must be a 6-tuple");
        return NULL;
    }
    double md[6];
    for (int i = 0; i < 6; i++) md[i] = PyFloat_AsDouble(PyTuple_GET_ITEM(m, i));
    if (PyErr_Occurred()) return NULL;
    double ox, oy;
    c_apply(md, px, py_, &ox, &oy);
    return Py_BuildValue("(dd)", ox, oy);
}

static PyObject *py_avg_scale(PyObject *self, PyObject *args) {
    PyObject *m;
    if (!PyArg_ParseTuple(args, "O", &m)) return NULL;
    double md[6];
    for (int i = 0; i < 6; i++) md[i] = PyFloat_AsDouble(PyTuple_GET_ITEM(m, i));
    if (PyErr_Occurred()) return NULL;
    return PyFloat_FromDouble(c_avg_scale(md));
}

static PyObject *py_rot(PyObject *self, PyObject *args) {
    PyObject *m;
    if (!PyArg_ParseTuple(args, "O", &m)) return NULL;
    double md[6];
    for (int i = 0; i < 6; i++) md[i] = PyFloat_AsDouble(PyTuple_GET_ITEM(m, i));
    if (PyErr_Occurred()) return NULL;
    return PyFloat_FromDouble(c_rot(md));
}

static PyObject *py_feather(PyObject *self, PyObject *args) {
    double s;
    if (!PyArg_ParseTuple(args, "d", &s)) return NULL;
    return PyFloat_FromDouble(c_feather(s));
}

static PyObject *py_invert(PyObject *self, PyObject *args) {
    PyObject *m;
    if (!PyArg_ParseTuple(args, "O", &m)) return NULL;
    if (!PyTuple_Check(m) || PyTuple_GET_SIZE(m) < 6) {
        PyErr_SetString(PyExc_TypeError, "m must be a 6-tuple");
        return NULL;
    }
    double md[6];
    for (int i = 0; i < 6; i++) md[i] = PyFloat_AsDouble(PyTuple_GET_ITEM(m, i));
    if (PyErr_Occurred()) return NULL;
    double det = md[0]*md[3] - md[1]*md[2];
    if (fabs(det) < 1e-12)
        return Py_BuildValue("(dddddd)", 1.0, 0.0, 0.0, 1.0, 0.0, 0.0);
    double inv = 1.0 / det;
    return Py_BuildValue("(dddddd)",
        md[3]*inv, -md[1]*inv, -md[2]*inv, md[0]*inv,
        (md[2]*md[5]-md[3]*md[4])*inv, (md[1]*md[4]-md[0]*md[5])*inv);
}

/* ─────────────────────────────────────────────────────────────────────── */
/* C Collision Detection                                                   */
/* ─────────────────────────────────────────────────────────────────────── */

/* Circle vs Circle → 0=no hit, 1=hit.  out=[nx,ny,depth,px,py] */
static int c_collide_cc(double ax, double ay, double ar,
                        double bx, double by, double br, double out[5]) {
    double dx = bx - ax, dy = by - ay;
    double dist = hypot(dx, dy);
    double md = ar + br;
    if (dist >= md) return 0;
    if (dist < 1e-12) {
        out[0] = 1; out[1] = 0; out[2] = md; out[3] = ax; out[4] = ay;
    } else {
        double inv = 1.0 / dist;
        out[0] = dx * inv; out[1] = dy * inv;
        out[2] = md - dist;
        out[3] = ax + out[0] * ar; out[4] = ay + out[1] * ar;
    }
    return 1;
}

/* Circle vs OBB → 0=no hit, 1=hit.  out=[nx,ny,depth,px,py] */
static int c_collide_co(double ccx, double ccy, double cr,
                        double ox, double oy, double hw, double hh, double angle,
                        double out[5]) {
    double dx = ccx - ox, dy = ccy - oy;
    double ca = cos(-angle), sa = sin(-angle);
    double lx = ca * dx - sa * dy;
    double ly = sa * dx + ca * dy;
    double clx = lx < -hw ? -hw : (lx > hw ? hw : lx);
    double cly = ly < -hh ? -hh : (ly > hh ? hh : ly);
    double ddx = lx - clx, ddy = ly - cly;
    double dsq = ddx * ddx + ddy * ddy;
    if (dsq >= cr * cr) return 0;
    double nlx, nly, depth;
    if (dsq < 1e-12) {
        double fd[4] = { hw - lx, lx + hw, hh - ly, ly + hh };
        int mi = 0;
        for (int i = 1; i < 4; i++) if (fd[i] < fd[mi]) mi = i;
        double normals[4][2] = {{1,0},{-1,0},{0,1},{0,-1}};
        nlx = normals[mi][0]; nly = normals[mi][1];
        depth = fd[mi] + cr;
    } else {
        double d = sqrt(dsq);
        nlx = ddx / d; nly = ddy / d;
        depth = cr - d;
    }
    double ca2 = cos(angle), sa2 = sin(angle);
    out[0] = ca2 * nlx - sa2 * nly;
    out[1] = sa2 * nlx + ca2 * nly;
    out[2] = depth;
    out[3] = ox + ca2 * clx - sa2 * cly;
    out[4] = oy + sa2 * clx + ca2 * cly;
    return 1;
}

/* OBB vs OBB (SAT + edge clipping contact point) → 0=no hit, 1=hit */
static int c_collide_oo(double ax, double ay, double ahw, double ahh, double aa,
                        double bx, double by, double bhw, double bhh, double ba,
                        double out[5]) {
    /* Compute axes and corners */
    double ca_a = cos(aa), sa_a = sin(aa);
    double ca_b = cos(ba), sa_b = sin(ba);
    double axes[4][2] = {
        { ca_a,  sa_a}, {-sa_a, ca_a},
        { ca_b,  sa_b}, {-sa_b, ca_b}
    };
    /* Corners: +hw+hh, -hw+hh, -hw-hh, +hw-hh */
    double ca[4][2], cb[4][2];
    {
        double ux = ca_a, uy = sa_a, vx = -sa_a, vy = ca_a;
        ca[0][0]=ax+ux*ahw+vx*ahh; ca[0][1]=ay+uy*ahw+vy*ahh;
        ca[1][0]=ax-ux*ahw+vx*ahh; ca[1][1]=ay-uy*ahw+vy*ahh;
        ca[2][0]=ax-ux*ahw-vx*ahh; ca[2][1]=ay-uy*ahw-vy*ahh;
        ca[3][0]=ax+ux*ahw-vx*ahh; ca[3][1]=ay+uy*ahw-vy*ahh;
    }
    {
        double ux = ca_b, uy = sa_b, vx = -sa_b, vy = ca_b;
        cb[0][0]=bx+ux*bhw+vx*bhh; cb[0][1]=by+uy*bhw+vy*bhh;
        cb[1][0]=bx-ux*bhw+vx*bhh; cb[1][1]=by-uy*bhw+vy*bhh;
        cb[2][0]=bx-ux*bhw-vx*bhh; cb[2][1]=by-uy*bhw-vy*bhh;
        cb[3][0]=bx+ux*bhw-vx*bhh; cb[3][1]=by+uy*bhw-vy*bhh;
    }

    double min_depth = 1e18;
    double min_axis[2] = {0, 0};
    int min_axis_idx = 0;

    for (int i = 0; i < 4; i++) {
        double axi = axes[i][0], ayi = axes[i][1];
        double min_a = 1e18, max_a = -1e18;
        double min_b = 1e18, max_b = -1e18;
        for (int j = 0; j < 4; j++) {
            double pa = ca[j][0]*axi + ca[j][1]*ayi;
            double pb = cb[j][0]*axi + cb[j][1]*ayi;
            if (pa < min_a) min_a = pa; if (pa > max_a) max_a = pa;
            if (pb < min_b) min_b = pb; if (pb > max_b) max_b = pb;
        }
        double overlap = fmin(max_a, max_b) - fmax(min_a, min_b);
        if (overlap <= 0) return 0;
        if (overlap < min_depth) {
            min_depth = overlap;
            min_axis[0] = axi; min_axis[1] = ayi;
            min_axis_idx = i;
        }
    }

    /* Ensure normal points A→B */
    double dx = bx - ax, dy = by - ay;
    if (dx * min_axis[0] + dy * min_axis[1] < 0) {
        min_axis[0] = -min_axis[0]; min_axis[1] = -min_axis[1];
    }

    out[0] = min_axis[0]; out[1] = min_axis[1]; out[2] = min_depth;

    /* --- Edge clipping for accurate contact point --- */
    /* Reference face: the face of the OBB that contributed min_axis */
    /* Incident face: the face of the other OBB most anti-aligned with min_axis */
    double (*ref_corners)[2] = (min_axis_idx < 2) ? ca : cb;
    double (*inc_corners)[2] = (min_axis_idx < 2) ? cb : ca;

    /* Find incident edge: edge whose outward normal is most anti-aligned with min_axis */
    double inc_axes[4][2];
    if (min_axis_idx < 2) {
        /* ref=A, inc=B */
        inc_axes[0][0]= ca_b; inc_axes[0][1]= sa_b;
        inc_axes[1][0]=-sa_b; inc_axes[1][1]= ca_b;
        inc_axes[2][0]=-ca_b; inc_axes[2][1]=-sa_b;
        inc_axes[3][0]= sa_b; inc_axes[3][1]=-ca_b;
    } else {
        inc_axes[0][0]= ca_a; inc_axes[0][1]= sa_a;
        inc_axes[1][0]=-sa_a; inc_axes[1][1]= ca_a;
        inc_axes[2][0]=-ca_a; inc_axes[2][1]=-sa_a;
        inc_axes[3][0]= sa_a; inc_axes[3][1]=-ca_a;
    }

    int inc_edge = 0;
    double min_dot = 1e18;
    for (int i = 0; i < 4; i++) {
        double d = inc_axes[i][0] * min_axis[0] + inc_axes[i][1] * min_axis[1];
        if (d < min_dot) { min_dot = d; inc_edge = i; }
    }

    /* Incident edge endpoints */
    double ie0[2] = { inc_corners[inc_edge][0], inc_corners[inc_edge][1] };
    double ie1[2] = { inc_corners[(inc_edge+1)%4][0], inc_corners[(inc_edge+1)%4][1] };

    /* Reference face: project onto min_axis */
    double ref_proj = 0;
    for (int j = 0; j < 4; j++) {
        double p = ref_corners[j][0]*min_axis[0] + ref_corners[j][1]*min_axis[1];
        ref_proj += p;
    }
    ref_proj /= 4.0;
    double ref_max = ref_proj + min_depth / 2.0;

    /* Keep incident points behind reference face */
    double cp[2][2];
    int cp_count = 0;
    double pts[2][2] = {{ie0[0], ie0[1]}, {ie1[0], ie1[1]}};
    for (int i = 0; i < 2; i++) {
        double proj = pts[i][0]*min_axis[0] + pts[i][1]*min_axis[1];
        if (proj <= ref_max) {
            cp[cp_count][0] = pts[i][0]; cp[cp_count][1] = pts[i][1];
            cp_count++;
        }
    }
    if (cp_count > 0) {
        out[3] = out[4] = 0;
        for (int i = 0; i < cp_count; i++) {
            out[3] += cp[i][0]; out[4] += cp[i][1];
        }
        out[3] /= cp_count; out[4] /= cp_count;
    } else {
        out[3] = (ax + bx) / 2; out[4] = (ay + by) / 2;
    }
    return 1;
}

/* Python: test_colliders(ta, ax, ay, ap0, ap1, ap2, tb, bx, by, bp0, bp1, bp2) */
static PyObject *py_test_colliders(PyObject *self, PyObject *args) {
    int ta, tb;
    double ax, ay, ap0, ap1, ap2, bxx, by, bp0, bp1, bp2;
    if (!PyArg_ParseTuple(args, "idddddiddddd",
            &ta, &ax, &ay, &ap0, &ap1, &ap2,
            &tb, &bxx, &by, &bp0, &bp1, &bp2))
        return NULL;

    double out[5];
    int hit = 0;

    if (ta == COLL_CIRCLE && tb == COLL_CIRCLE) {
        hit = c_collide_cc(ax, ay, ap0, bxx, by, bp0, out);
    } else if (ta == COLL_CIRCLE && tb == COLL_OBB) {
        hit = c_collide_co(ax, ay, ap0, bxx, by, bp0, bp1, bp2, out);
    } else if (ta == COLL_OBB && tb == COLL_CIRCLE) {
        hit = c_collide_co(bxx, by, bp0, ax, ay, ap0, ap1, ap2, out);
        if (hit) { out[0] = -out[0]; out[1] = -out[1]; }
    } else if (ta == COLL_OBB && tb == COLL_OBB) {
        hit = c_collide_oo(ax, ay, ap0, ap1, ap2, bxx, by, bp0, bp1, bp2, out);
    }

    if (!hit) Py_RETURN_NONE;
    return Py_BuildValue("(ddddd)", out[0], out[1], out[2], out[3], out[4]);
}

/* Python: batch_collisions(colliders_a, colliders_b) → [(ia, ib, nx, ny, depth, px, py)] */
/* Each collider: (type, cx, cy, p0, p1, p2, category, mask) */
static PyObject *py_batch_collisions(PyObject *self, PyObject *args) {
    PyObject *list_a, *list_b;
    if (!PyArg_ParseTuple(args, "OO", &list_a, &list_b)) return NULL;
    if (!PyList_Check(list_a) || !PyList_Check(list_b)) {
        PyErr_SetString(PyExc_TypeError, "args must be lists"); return NULL;
    }

    Py_ssize_t na = PyList_GET_SIZE(list_a);
    Py_ssize_t nb = PyList_GET_SIZE(list_b);

    /* Parse colliders */
    typedef struct { int type; double cx, cy, p0, p1, p2; uint32_t cat, mask; } Coll;
    Coll *ca = (Coll *)malloc(sizeof(Coll) * (na > 0 ? na : 1));
    Coll *cb_ = (Coll *)malloc(sizeof(Coll) * (nb > 0 ? nb : 1));

    auto parse_coll = [](PyObject *t, Coll *c) -> int {
        if (!PyTuple_Check(t) || PyTuple_GET_SIZE(t) < 8) return 0;
        c->type = (int)PyLong_AsLong(PyTuple_GET_ITEM(t, 0));
        c->cx   = PyFloat_AsDouble(PyTuple_GET_ITEM(t, 1));
        c->cy   = PyFloat_AsDouble(PyTuple_GET_ITEM(t, 2));
        c->p0   = PyFloat_AsDouble(PyTuple_GET_ITEM(t, 3));
        c->p1   = PyFloat_AsDouble(PyTuple_GET_ITEM(t, 4));
        c->p2   = PyFloat_AsDouble(PyTuple_GET_ITEM(t, 5));
        c->cat  = (uint32_t)PyLong_AsUnsignedLong(PyTuple_GET_ITEM(t, 6));
        c->mask = (uint32_t)PyLong_AsUnsignedLong(PyTuple_GET_ITEM(t, 7));
        if (PyErr_Occurred()) { PyErr_Clear(); return 0; }
        return 1;
    };

    for (Py_ssize_t i = 0; i < na; i++)
        if (!parse_coll(PyList_GET_ITEM(list_a, i), &ca[i])) { ca[i].type = -1; }
    for (Py_ssize_t i = 0; i < nb; i++)
        if (!parse_coll(PyList_GET_ITEM(list_b, i), &cb_[i])) { cb_[i].type = -1; }

    /* Spatial hash */
    double max_r = 0;
    for (Py_ssize_t i = 0; i < na; i++) {
        if (ca[i].type < 0) continue;
        double r = ca[i].type == 0 ? ca[i].p0 : hypot(ca[i].p0, ca[i].p1);
        if (r > max_r) max_r = r;
    }
    for (Py_ssize_t i = 0; i < nb; i++) {
        if (cb_[i].type < 0) continue;
        double r = cb_[i].type == 0 ? cb_[i].p0 : hypot(cb_[i].p0, cb_[i].p1);
        if (r > max_r) max_r = r;
    }
    double cs = fmax(max_r * 2, 64);
    double inv_cs = 1.0 / cs;

    /* Simple approach: for each pair in grid, test */
    /* Use brute force for small counts, spatial hash for large */
    PyObject *result = PyList_New(0);

    if ((na * nb) <= 400) {
        /* Brute force for small sets */
        for (Py_ssize_t i = 0; i < na; i++) {
            if (ca[i].type < 0) continue;
            for (Py_ssize_t j = 0; j < nb; j++) {
                if (cb_[j].type < 0) continue;
                if (!(ca[i].cat & cb_[j].mask) || !(cb_[j].cat & ca[i].mask)) continue;
                /* AABB quick reject */
                double ra = ca[i].type == 0 ? ca[i].p0 : hypot(ca[i].p0, ca[i].p1);
                double rb = cb_[j].type == 0 ? cb_[j].p0 : hypot(cb_[j].p0, cb_[j].p1);
                double ddx = ca[i].cx - cb_[j].cx, ddy = ca[i].cy - cb_[j].cy;
                if (ddx*ddx + ddy*ddy > (ra+rb)*(ra+rb)) continue;
                /* Narrow phase */
                double out[5];
                int hit = 0;
                int ta = ca[i].type, tb = cb_[j].type;
                if (ta == 0 && tb == 0) hit = c_collide_cc(ca[i].cx, ca[i].cy, ca[i].p0, cb_[j].cx, cb_[j].cy, cb_[j].p0, out);
                else if (ta == 0 && tb == 1) hit = c_collide_co(ca[i].cx, ca[i].cy, ca[i].p0, cb_[j].cx, cb_[j].cy, cb_[j].p0, cb_[j].p1, cb_[j].p2, out);
                else if (ta == 1 && tb == 0) { hit = c_collide_co(cb_[j].cx, cb_[j].cy, cb_[j].p0, ca[i].cx, ca[i].cy, ca[i].p0, ca[i].p1, ca[i].p2, out); if (hit) { out[0]=-out[0]; out[1]=-out[1]; } }
                else if (ta == 1 && tb == 1) hit = c_collide_oo(ca[i].cx, ca[i].cy, ca[i].p0, ca[i].p1, ca[i].p2, cb_[j].cx, cb_[j].cy, cb_[j].p0, cb_[j].p1, cb_[j].p2, out);
                if (hit) {
                    PyObject *item = Py_BuildValue("(nnddddd)", i, j, out[0], out[1], out[2], out[3], out[4]);
                    PyList_Append(result, item); Py_DECREF(item);
                }
            }
        }
    } else {
        /* Spatial hash for large sets */
        typedef struct { int set; Py_ssize_t idx; int gx, gy; } HashEntry;
        int total = (int)(na + nb);
        HashEntry *entries = (HashEntry *)malloc(sizeof(HashEntry) * total * 4); /* oversized */
        int entry_count = 0;

        auto insert_hash = [&](Coll *c, Py_ssize_t idx, int set) {
            if (c->type < 0) return;
            double r = c->type == 0 ? c->p0 : hypot(c->p0, c->p1);
            int x0 = (int)floor((c->cx - r) * inv_cs);
            int y0 = (int)floor((c->cy - r) * inv_cs);
            int x1 = (int)floor((c->cx + r) * inv_cs);
            int y1 = (int)floor((c->cy + r) * inv_cs);
            for (int gx = x0; gx <= x1; gx++)
                for (int gy = y0; gy <= y1; gy++) {
                    entries[entry_count++] = {set, idx, gx, gy};
                }
        };

        for (Py_ssize_t i = 0; i < na; i++) insert_hash(&ca[i], i, 0);
        for (Py_ssize_t i = 0; i < nb; i++) insert_hash(&cb_[i], i, 1);

        /* Sort by cell */
        qsort(entries, entry_count, sizeof(HashEntry), [](const void *a, const void *b) -> int {
            auto *ea = (const HashEntry *)a, *eb = (const HashEntry *)b;
            if (ea->gx != eb->gx) return ea->gx - eb->gx;
            return ea->gy - eb->gy;
        });

        /* Scan cells, test cross-set pairs */
        /* Use a seen set via sorted pair checking */
        /* Simple approach: track with a flat hash */
        int cell_start = 0;
        for (int i = 0; i <= entry_count; i++) {
            if (i < entry_count && (i == 0 || (entries[i].gx == entries[i-1].gx && entries[i].gy == entries[i-1].gy)))
                continue;
            /* Process cell [cell_start, i) */
            for (int a_idx = cell_start; a_idx < i; a_idx++) {
                if (entries[a_idx].set != 0) continue;
                Py_ssize_t ia = entries[a_idx].idx;
                for (int b_idx = cell_start; b_idx < i; b_idx++) {
                    if (entries[b_idx].set != 1) continue;
                    Py_ssize_t ib = entries[b_idx].idx;
                    if (!(ca[ia].cat & cb_[ib].mask) || !(cb_[ib].cat & ca[ia].mask)) continue;
                    double out[5]; int hit = 0;
                    int tta = ca[ia].type, ttb = cb_[ib].type;
                    if (tta == 0 && ttb == 0) hit = c_collide_cc(ca[ia].cx, ca[ia].cy, ca[ia].p0, cb_[ib].cx, cb_[ib].cy, cb_[ib].p0, out);
                    else if (tta == 0 && ttb == 1) hit = c_collide_co(ca[ia].cx, ca[ia].cy, ca[ia].p0, cb_[ib].cx, cb_[ib].cy, cb_[ib].p0, cb_[ib].p1, cb_[ib].p2, out);
                    else if (tta == 1 && ttb == 0) { hit = c_collide_co(cb_[ib].cx, cb_[ib].cy, cb_[ib].p0, ca[ia].cx, ca[ia].cy, ca[ia].p0, ca[ia].p1, ca[ia].p2, out); if (hit){out[0]=-out[0];out[1]=-out[1];} }
                    else if (tta == 1 && ttb == 1) hit = c_collide_oo(ca[ia].cx, ca[ia].cy, ca[ia].p0, ca[ia].p1, ca[ia].p2, cb_[ib].cx, cb_[ib].cy, cb_[ib].p0, cb_[ib].p1, cb_[ib].p2, out);
                    if (hit) {
                        PyObject *item = Py_BuildValue("(nnddddd)", ia, ib, out[0], out[1], out[2], out[3], out[4]);
                        PyList_Append(result, item); Py_DECREF(item);
                    }
                }
            }
            cell_start = i;
        }
        free(entries);
        /* Deduplicate: sort and remove dup (ia,ib) pairs */
        Py_ssize_t rn = PyList_GET_SIZE(result);
        if (rn > 1) {
            /* Simple dedup via set */
            PyObject *deduped = PyList_New(0);
            PyObject *seen = PySet_New(NULL);
            for (Py_ssize_t i = 0; i < rn; i++) {
                PyObject *item = PyList_GET_ITEM(result, i);
                PyObject *key = Py_BuildValue("(nn)",
                    PyLong_AsSsize_t(PyTuple_GET_ITEM(item, 0)),
                    PyLong_AsSsize_t(PyTuple_GET_ITEM(item, 1)));
                if (!PySet_Contains(seen, key)) {
                    PySet_Add(seen, key);
                    PyList_Append(deduped, item);
                }
                Py_DECREF(key);
            }
            Py_DECREF(seen);
            Py_DECREF(result);
            result = deduped;
        }
    }

    free(ca); free(cb_);
    return result;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* C-accelerated contains_point (internal helper)                          */
/* ─────────────────────────────────────────────────────────────────────── */

static int c_contains_point(CNodeCache *cache, double wx, double wy) {
    if (!cache->has_bounds) return -1; /* unknown — need Python fallback */

    /* Transform world → local */
    double inv[6];
    c_invert(cache->world, inv);
    double lx, ly;
    c_apply(inv, wx, wy, &lx, &ly);

    switch (cache->type_id) {
    case NTYPE_CIRCLE: {
        /* Distance check: lx² + ly² ≤ r² where r = radius + stroke_width/2 */
        double e = cache->shape[0] + cache->shape[1] * 0.5;
        return (lx*lx + ly*ly <= e*e) ? 1 : 0;
    }
    case NTYPE_LINE: {
        /* Point-to-segment distance */
        double sx = cache->line_start[0], sy = cache->line_start[1];
        double ex = cache->line_end[0],   ey = cache->line_end[1];
        double dx = ex - sx, dy = ey - sy;
        double len_sq = dx*dx + dy*dy;
        double t;
        if (len_sq < 1e-12) {
            return (hypot(lx - sx, ly - sy) <= cache->line_hw) ? 1 : 0;
        }
        t = ((lx - sx)*dx + (ly - sy)*dy) / len_sq;
        if (t < 0) t = 0; else if (t > 1) t = 1;
        double px = sx + t*dx, py = sy + t*dy;
        return (hypot(lx - px, ly - py) <= cache->line_hw) ? 1 : 0;
    }
    case NTYPE_PATH:
        return -1; /* precise path hit-test stays in Python for now */
    default:
        /* AABB check in local space */
        return (lx >= cache->local_bounds[0] && lx <= cache->local_bounds[2] &&
                ly >= cache->local_bounds[1] && ly <= cache->local_bounds[3]) ? 1 : 0;
    }
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Python: contains_point(node, wx, wy) → bool                            */
/* ─────────────────────────────────────────────────────────────────────── */

static PyObject *accel_contains_point(PyObject *self, PyObject *args) {
    PyObject *node;
    double wx, wy;
    if (!PyArg_ParseTuple(args, "Odd", &node, &wx, &wy)) return NULL;

    CNodeCache *cache = get_cache(node);
    if (!cache || !cache->valid) {
        /* No cache — fallback to Python */
        PyObject *wxo = PyFloat_FromDouble(wx);
        PyObject *wyo = PyFloat_FromDouble(wy);
        PyObject *res = PyObject_CallMethodObjArgs(node, s_contains_point, wxo, wyo, NULL);
        Py_DECREF(wxo); Py_DECREF(wyo);
        return res;
    }

    int r = c_contains_point(cache, wx, wy);
    if (r < 0) {
        /* UNKNOWN type — fallback to Python */
        PyObject *wxo = PyFloat_FromDouble(wx);
        PyObject *wyo = PyFloat_FromDouble(wy);
        PyObject *res = PyObject_CallMethodObjArgs(node, s_contains_point, wxo, wyo, NULL);
        Py_DECREF(wxo); Py_DECREF(wyo);
        return res;
    }
    return PyBool_FromLong(r);
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Python: hit_test(interactive_list, x, y) → node or None                 */
/* ─────────────────────────────────────────────────────────────────────── */

static PyObject *accel_hit_test(PyObject *self, PyObject *args) {
    PyObject *list;
    double x, y;
    if (!PyArg_ParseTuple(args, "Odd", &list, &x, &y)) return NULL;
    if (!PyList_Check(list)) { PyErr_SetString(PyExc_TypeError, "arg must be list"); return NULL; }

    Py_ssize_t n = PyList_GET_SIZE(list);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *node = PyList_GET_ITEM(list, i);
        CNodeCache *cache = get_cache(node);
        int hit;

        if (cache && cache->valid && cache->has_bounds) {
            hit = c_contains_point(cache, x, y);
        } else {
            /* Fallback to Python */
            PyObject *xo = PyFloat_FromDouble(x);
            PyObject *yo = PyFloat_FromDouble(y);
            PyObject *res = PyObject_CallMethodObjArgs(node, s_contains_point, xo, yo, NULL);
            Py_DECREF(xo); Py_DECREF(yo);
            hit = (res && PyObject_IsTrue(res)) ? 1 : 0;
            Py_XDECREF(res);
            if (PyErr_Occurred()) { PyErr_Clear(); continue; }
        }

        if (hit == 1) {
            int pt = read_bool(node, s_passthrough);
            if (!pt) {
                Py_INCREF(node);
                return node;
            }
        }
    }
    Py_RETURN_NONE;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Python: hit_test_all(interactive_list, x, y) → [node, ...]              */
/* ─────────────────────────────────────────────────────────────────────── */

static PyObject *accel_hit_test_all(PyObject *self, PyObject *args) {
    PyObject *list;
    double x, y;
    if (!PyArg_ParseTuple(args, "Odd", &list, &x, &y)) return NULL;
    if (!PyList_Check(list)) { PyErr_SetString(PyExc_TypeError, "arg must be list"); return NULL; }

    PyObject *result = PyList_New(0);
    Py_ssize_t n = PyList_GET_SIZE(list);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *node = PyList_GET_ITEM(list, i);
        CNodeCache *cache = get_cache(node);
        int hit;

        if (cache && cache->valid && cache->has_bounds) {
            hit = c_contains_point(cache, x, y);
        } else {
            PyObject *xo = PyFloat_FromDouble(x);
            PyObject *yo = PyFloat_FromDouble(y);
            PyObject *res = PyObject_CallMethodObjArgs(node, s_contains_point, xo, yo, NULL);
            Py_DECREF(xo); Py_DECREF(yo);
            hit = (res && PyObject_IsTrue(res)) ? 1 : 0;
            Py_XDECREF(res);
            if (PyErr_Occurred()) { PyErr_Clear(); continue; }
        }

        if (hit == 1) {
            PyList_Append(result, node);
        }
    }
    return result;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Python: collisions(nodes_a, nodes_b) → [(node_a, node_b, info_tuple)]   */
/* ─────────────────────────────────────────────────────────────────────── */

/* Helper: get collider from cache or Python fallback. Returns 1 on success. */
static int get_node_collider(PyObject *node, int *type, double coll[6],
                             uint32_t *cat, uint32_t *mask) {
    CNodeCache *cache = get_cache(node);
    if (cache && cache->valid && cache->coll_type >= 0) {
        *type = cache->coll_type;
        memcpy(coll, cache->coll, 5 * sizeof(double));
        coll[5] = 0;
    } else {
        /* Fallback: call Python _collider() */
        PyObject *c = PyObject_CallMethodObjArgs(node, s__collider, NULL);
        if (!c || c == Py_None) { Py_XDECREF(c); return 0; }
        if (!PyTuple_Check(c) || PyTuple_GET_SIZE(c) < 4) { Py_DECREF(c); return 0; }
        PyObject *kind = PyTuple_GET_ITEM(c, 0);
        const char *ks = PyUnicode_AsUTF8(kind);
        if (!ks) { PyErr_Clear(); Py_DECREF(c); return 0; }
        if (strcmp(ks, "circle") == 0) {
            *type = COLL_CIRCLE;
            coll[0] = PyFloat_AsDouble(PyTuple_GET_ITEM(c, 1));
            coll[1] = PyFloat_AsDouble(PyTuple_GET_ITEM(c, 2));
            coll[2] = PyFloat_AsDouble(PyTuple_GET_ITEM(c, 3));
            coll[3] = 0; coll[4] = 0; coll[5] = 0;
        } else if (PyTuple_GET_SIZE(c) >= 6) {
            *type = COLL_OBB;
            coll[0] = PyFloat_AsDouble(PyTuple_GET_ITEM(c, 1));
            coll[1] = PyFloat_AsDouble(PyTuple_GET_ITEM(c, 2));
            coll[2] = PyFloat_AsDouble(PyTuple_GET_ITEM(c, 3));
            coll[3] = PyFloat_AsDouble(PyTuple_GET_ITEM(c, 4));
            coll[4] = PyFloat_AsDouble(PyTuple_GET_ITEM(c, 5));
            coll[5] = 0;
        } else {
            Py_DECREF(c); return 0;
        }
        Py_DECREF(c);
        if (PyErr_Occurred()) { PyErr_Clear(); return 0; }
    }

    /* Read collision_category and collision_mask */
    PyObject *pcat = PyObject_GetAttr(node, s_collision_category);
    PyObject *pmask = PyObject_GetAttr(node, s_collision_mask);
    *cat = pcat ? (uint32_t)PyLong_AsUnsignedLong(pcat) : 0xFFFFFFFF;
    *mask = pmask ? (uint32_t)PyLong_AsUnsignedLong(pmask) : 0xFFFFFFFF;
    Py_XDECREF(pcat); Py_XDECREF(pmask);
    if (PyErr_Occurred()) { PyErr_Clear(); *cat = 0xFFFFFFFF; *mask = 0xFFFFFFFF; }
    return 1;
}

static PyObject *accel_collisions(PyObject *self, PyObject *args) {
    PyObject *list_a, *list_b;
    if (!PyArg_ParseTuple(args, "OO", &list_a, &list_b)) return NULL;
    if (!PyList_Check(list_a) || !PyList_Check(list_b)) {
        PyErr_SetString(PyExc_TypeError, "args must be lists"); return NULL;
    }

    Py_ssize_t na = PyList_GET_SIZE(list_a);
    Py_ssize_t nb = PyList_GET_SIZE(list_b);

    typedef struct { int type; double cx, cy, p0, p1, p2; uint32_t cat, mask; } Coll;
    Coll *ca = (Coll *)malloc(sizeof(Coll) * (na > 0 ? na : 1));
    Coll *cb_ = (Coll *)malloc(sizeof(Coll) * (nb > 0 ? nb : 1));
    PyObject **nodes_a = (PyObject **)malloc(sizeof(PyObject*) * (na > 0 ? na : 1));
    PyObject **nodes_b = (PyObject **)malloc(sizeof(PyObject*) * (nb > 0 ? nb : 1));
    Py_ssize_t real_na = 0, real_nb = 0;

    /* Build collider arrays from cache or Python fallback */
    for (Py_ssize_t i = 0; i < na; i++) {
        PyObject *node = PyList_GET_ITEM(list_a, i);
        int type; double coll[6]; uint32_t cat, mask;
        if (get_node_collider(node, &type, coll, &cat, &mask)) {
            Coll *c = &ca[real_na];
            c->type = type; c->cx = coll[0]; c->cy = coll[1];
            c->p0 = coll[2]; c->p1 = coll[3]; c->p2 = coll[4];
            c->cat = cat; c->mask = mask;
            nodes_a[real_na] = node;
            real_na++;
        }
    }
    for (Py_ssize_t i = 0; i < nb; i++) {
        PyObject *node = PyList_GET_ITEM(list_b, i);
        int type; double coll[6]; uint32_t cat, mask;
        if (get_node_collider(node, &type, coll, &cat, &mask)) {
            Coll *c = &cb_[real_nb];
            c->type = type; c->cx = coll[0]; c->cy = coll[1];
            c->p0 = coll[2]; c->p1 = coll[3]; c->p2 = coll[4];
            c->cat = cat; c->mask = mask;
            nodes_b[real_nb] = node;
            real_nb++;
        }
    }

    /* Run collision detection (same logic as py_batch_collisions) */
    PyObject *result = PyList_New(0);

    /* Import CollisionInfo for building results */
    static PyObject *CollisionInfoType = NULL;
    if (!CollisionInfoType) {
        PyObject *mod = PyImport_ImportModule("scene");
        if (mod) { CollisionInfoType = PyObject_GetAttrString(mod, "CollisionInfo"); Py_DECREF(mod); }
        if (!CollisionInfoType) { PyErr_Clear(); }
    }

    auto emit_result = [&](Py_ssize_t ia, Py_ssize_t ib, double out[5]) {
        PyObject *info;
        if (CollisionInfoType) {
            PyObject *normal = Py_BuildValue("(dd)", out[0], out[1]);
            PyObject *point = Py_BuildValue("(dd)", out[3], out[4]);
            info = PyObject_CallFunction(CollisionInfoType, "OOdO",
                Py_True, normal, out[2], point);
            Py_DECREF(normal); Py_DECREF(point);
        } else {
            info = Py_BuildValue("(ddddd)", out[0], out[1], out[2], out[3], out[4]);
        }
        PyObject *item = PyTuple_New(3);
        Py_INCREF(nodes_a[ia]); PyTuple_SET_ITEM(item, 0, nodes_a[ia]);
        Py_INCREF(nodes_b[ib]); PyTuple_SET_ITEM(item, 1, nodes_b[ib]);
        PyTuple_SET_ITEM(item, 2, info);
        PyList_Append(result, item);
        Py_DECREF(item);
    };

    if ((real_na * real_nb) <= 400) {
        /* Brute force */
        for (Py_ssize_t i = 0; i < real_na; i++) {
            for (Py_ssize_t j = 0; j < real_nb; j++) {
                if (!(ca[i].cat & cb_[j].mask) || !(cb_[j].cat & ca[i].mask)) continue;
                double ra = ca[i].type == 0 ? ca[i].p0 : hypot(ca[i].p0, ca[i].p1);
                double rb = cb_[j].type == 0 ? cb_[j].p0 : hypot(cb_[j].p0, cb_[j].p1);
                double ddx = ca[i].cx - cb_[j].cx, ddy = ca[i].cy - cb_[j].cy;
                if (ddx*ddx + ddy*ddy > (ra+rb)*(ra+rb)) continue;
                double out[5]; int hit = 0;
                int ta = ca[i].type, tb = cb_[j].type;
                if (ta==0 && tb==0) hit = c_collide_cc(ca[i].cx,ca[i].cy,ca[i].p0,cb_[j].cx,cb_[j].cy,cb_[j].p0,out);
                else if (ta==0 && tb==1) hit = c_collide_co(ca[i].cx,ca[i].cy,ca[i].p0,cb_[j].cx,cb_[j].cy,cb_[j].p0,cb_[j].p1,cb_[j].p2,out);
                else if (ta==1 && tb==0) { hit = c_collide_co(cb_[j].cx,cb_[j].cy,cb_[j].p0,ca[i].cx,ca[i].cy,ca[i].p0,ca[i].p1,ca[i].p2,out); if(hit){out[0]=-out[0];out[1]=-out[1];} }
                else if (ta==1 && tb==1) hit = c_collide_oo(ca[i].cx,ca[i].cy,ca[i].p0,ca[i].p1,ca[i].p2,cb_[j].cx,cb_[j].cy,cb_[j].p0,cb_[j].p1,cb_[j].p2,out);
                if (hit) emit_result(i, j, out);
            }
        }
    } else {
        /* Spatial hash for large sets */
        double max_r = 0;
        for (Py_ssize_t i = 0; i < real_na; i++) {
            double r = ca[i].type == 0 ? ca[i].p0 : hypot(ca[i].p0, ca[i].p1);
            if (r > max_r) max_r = r;
        }
        for (Py_ssize_t i = 0; i < real_nb; i++) {
            double r = cb_[i].type == 0 ? cb_[i].p0 : hypot(cb_[i].p0, cb_[i].p1);
            if (r > max_r) max_r = r;
        }
        double cs = fmax(max_r * 2, 64);
        double inv_cs = 1.0 / cs;

        typedef struct { int set; Py_ssize_t idx; int gx, gy; } HashEntry;
        int total = (int)(real_na + real_nb);
        HashEntry *entries = (HashEntry *)malloc(sizeof(HashEntry) * total * 4);
        int entry_count = 0;

        auto insert_hash = [&](Coll *c, Py_ssize_t idx, int set) {
            double r = c->type == 0 ? c->p0 : hypot(c->p0, c->p1);
            int x0 = (int)floor((c->cx - r) * inv_cs);
            int y0 = (int)floor((c->cy - r) * inv_cs);
            int x1 = (int)floor((c->cx + r) * inv_cs);
            int y1 = (int)floor((c->cy + r) * inv_cs);
            for (int gx = x0; gx <= x1; gx++)
                for (int gy = y0; gy <= y1; gy++)
                    entries[entry_count++] = {set, idx, gx, gy};
        };
        for (Py_ssize_t i = 0; i < real_na; i++) insert_hash(&ca[i], i, 0);
        for (Py_ssize_t i = 0; i < real_nb; i++) insert_hash(&cb_[i], i, 1);

        qsort(entries, entry_count, sizeof(HashEntry), [](const void *a, const void *b) -> int {
            auto *ea = (const HashEntry *)a, *eb = (const HashEntry *)b;
            if (ea->gx != eb->gx) return ea->gx - eb->gx;
            return ea->gy - eb->gy;
        });

        PyObject *seen = PySet_New(NULL);
        int cell_start = 0;
        for (int i = 0; i <= entry_count; i++) {
            if (i < entry_count && (i == 0 || (entries[i].gx == entries[i-1].gx && entries[i].gy == entries[i-1].gy)))
                continue;
            for (int a_idx = cell_start; a_idx < i; a_idx++) {
                if (entries[a_idx].set != 0) continue;
                Py_ssize_t ia = entries[a_idx].idx;
                for (int b_idx = cell_start; b_idx < i; b_idx++) {
                    if (entries[b_idx].set != 1) continue;
                    Py_ssize_t ib = entries[b_idx].idx;
                    if (!(ca[ia].cat & cb_[ib].mask) || !(cb_[ib].cat & ca[ia].mask)) continue;
                    /* Dedup */
                    PyObject *key = Py_BuildValue("(nn)", ia, ib);
                    if (PySet_Contains(seen, key)) { Py_DECREF(key); continue; }
                    PySet_Add(seen, key); Py_DECREF(key);
                    double out[5]; int hit = 0;
                    int tta = ca[ia].type, ttb = cb_[ib].type;
                    if (tta==0&&ttb==0) hit=c_collide_cc(ca[ia].cx,ca[ia].cy,ca[ia].p0,cb_[ib].cx,cb_[ib].cy,cb_[ib].p0,out);
                    else if (tta==0&&ttb==1) hit=c_collide_co(ca[ia].cx,ca[ia].cy,ca[ia].p0,cb_[ib].cx,cb_[ib].cy,cb_[ib].p0,cb_[ib].p1,cb_[ib].p2,out);
                    else if (tta==1&&ttb==0) { hit=c_collide_co(cb_[ib].cx,cb_[ib].cy,cb_[ib].p0,ca[ia].cx,ca[ia].cy,ca[ia].p0,ca[ia].p1,ca[ia].p2,out); if(hit){out[0]=-out[0];out[1]=-out[1];} }
                    else if (tta==1&&ttb==1) hit=c_collide_oo(ca[ia].cx,ca[ia].cy,ca[ia].p0,ca[ia].p1,ca[ia].p2,cb_[ib].cx,cb_[ib].cy,cb_[ib].p0,cb_[ib].p1,cb_[ib].p2,out);
                    if (hit) emit_result(ia, ib, out);
                }
            }
            cell_start = i;
        }
        Py_DECREF(seen);
        free(entries);
    }

    free(ca); free(cb_); free(nodes_a); free(nodes_b);
    return result;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Path geometry exports                                                  */
/* ─────────────────────────────────────────────────────────────────────── */

static int parse_path_commands(PyObject *commands, double tol, int depth, std::vector<PathSubpath> &subpaths) {
    PyObject *seq = PySequence_Fast(commands, "commands must be a sequence");
    if (!seq) return 0;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(seq);
    PyObject **items = PySequence_Fast_ITEMS(seq);

    std::vector<PathPoint> current;
    bool closed = false;
    bool has_cursor = false;
    PathPoint start{0.0, 0.0};
    PathPoint cursor{0.0, 0.0};

    auto finish = [&](bool force_closed) {
        if (!current.empty()) {
            std::vector<PathPoint> pts = dedupe_points(current);
            if (pts.size() >= 2) {
                PathSubpath sub;
                sub.points = std::move(pts);
                sub.closed = closed || force_closed;
                subpaths.push_back(std::move(sub));
            }
        }
        current.clear();
        closed = false;
        has_cursor = false;
        start = {0.0, 0.0};
        cursor = {0.0, 0.0};
    };

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *item = items[i];
        if (!PySequence_Check(item) || PySequence_Size(item) < 1) {
            Py_DECREF(seq);
            PyErr_SetString(PyExc_TypeError, "path command must be a tuple");
            return 0;
        }
        PyObject *cmd_obj = PySequence_GetItem(item, 0);
        PyObject *vals_obj = PySequence_Size(item) >= 2 ? PySequence_GetItem(item, 1) : NULL;
        if (!cmd_obj) { Py_XDECREF(vals_obj); Py_DECREF(seq); return 0; }
        const char *cmd = PyUnicode_Check(cmd_obj) ? PyUnicode_AsUTF8(cmd_obj) : NULL;
        if (!cmd || !cmd[0]) {
            Py_DECREF(cmd_obj); Py_XDECREF(vals_obj); Py_DECREF(seq);
            PyErr_SetString(PyExc_TypeError, "path command name must be a string");
            return 0;
        }
        char c = (char)toupper((unsigned char)cmd[0]);
        auto read_vals = [&](int count, double *out) -> bool {
            if (!vals_obj) return false;
            PyObject *vf = PySequence_Fast(vals_obj, "path command values must be a sequence");
            if (!vf) return false;
            if (PySequence_Fast_GET_SIZE(vf) < count) {
                Py_DECREF(vf);
                PyErr_SetString(PyExc_ValueError, "path command has too few values");
                return false;
            }
            for (int j = 0; j < count; j++) {
                out[j] = PyFloat_AsDouble(PySequence_Fast_GET_ITEM(vf, j));
                if (PyErr_Occurred()) { Py_DECREF(vf); return false; }
            }
            Py_DECREF(vf);
            return true;
        };
        if (c == 'M') {
            double v[2];
            if (!read_vals(2, v)) { Py_DECREF(cmd_obj); Py_XDECREF(vals_obj); Py_DECREF(seq); return 0; }
            finish(false);
            PathPoint pt{v[0], v[1]};
            current.push_back(pt);
            start = pt;
            cursor = pt;
            has_cursor = true;
        } else if (c == 'L') {
            double v[2];
            if (!has_cursor) {
                Py_DECREF(cmd_obj); Py_XDECREF(vals_obj); Py_DECREF(seq);
                PyErr_SetString(PyExc_ValueError, "line_to() requires an active subpath");
                return 0;
            }
            if (!read_vals(2, v)) { Py_DECREF(cmd_obj); Py_XDECREF(vals_obj); Py_DECREF(seq); return 0; }
            cursor = {v[0], v[1]};
            current.push_back(cursor);
        } else if (c == 'Q') {
            double v[4];
            if (!has_cursor) {
                Py_DECREF(cmd_obj); Py_XDECREF(vals_obj); Py_DECREF(seq);
                PyErr_SetString(PyExc_ValueError, "quad_to() requires an active subpath");
                return 0;
            }
            if (!read_vals(4, v)) { Py_DECREF(cmd_obj); Py_XDECREF(vals_obj); Py_DECREF(seq); return 0; }
            std::vector<PathPoint> out;
            flatten_quad(cursor, {v[0], v[1]}, {v[2], v[3]}, tol, depth, out);
            current.insert(current.end(), out.begin(), out.end());
            cursor = {v[2], v[3]};
        } else if (c == 'C') {
            double v[6];
            if (!has_cursor) {
                Py_DECREF(cmd_obj); Py_XDECREF(vals_obj); Py_DECREF(seq);
                PyErr_SetString(PyExc_ValueError, "cubic_to() requires an active subpath");
                return 0;
            }
            if (!read_vals(6, v)) { Py_DECREF(cmd_obj); Py_XDECREF(vals_obj); Py_DECREF(seq); return 0; }
            std::vector<PathPoint> out;
            flatten_cubic(cursor, {v[0], v[1]}, {v[2], v[3]}, {v[4], v[5]}, tol, depth, out);
            current.insert(current.end(), out.begin(), out.end());
            cursor = {v[4], v[5]};
        } else if (c == 'Z') {
            if (has_cursor) {
                closed = true;
                if (path_dist_sq(cursor, start) > 1e-12) current.push_back(start);
                finish(true);
            }
        } else {
            Py_DECREF(cmd_obj); Py_XDECREF(vals_obj); Py_DECREF(seq);
            PyErr_SetString(PyExc_ValueError, "unsupported path command");
            return 0;
        }
        Py_DECREF(cmd_obj);
        Py_XDECREF(vals_obj);
    }
    finish(false);
    Py_DECREF(seq);
    return 1;
}

static PyObject *py_path_flatten(PyObject *self, PyObject *args) {
    PyObject *commands;
    double tolerance;
    int max_depth;
    if (!PyArg_ParseTuple(args, "Odi", &commands, &tolerance, &max_depth)) return NULL;
    std::vector<PathSubpath> subpaths;
    if (!isfinite(tolerance) || tolerance <= 0.0) {
        PyErr_SetString(PyExc_ValueError, "Path tolerance must be finite and positive");
        return NULL;
    }
    if (!parse_path_commands(commands, tolerance, max_depth < 1 ? 1 : max_depth, subpaths)) return NULL;
    return build_subpaths_py(subpaths);
}

static PyObject *py_path_bounds(PyObject *self, PyObject *args) {
    PyObject *subpaths_obj;
    double stroke_width;
    const char *join = "round", *cap = "round";
    double miter_limit = 4.0;
    if (!PyArg_ParseTuple(args, "Od|ssd", &subpaths_obj, &stroke_width, &join, &cap, &miter_limit)) return NULL;
    std::vector<PathSubpath> subpaths;
    if (!parse_path_subpaths(subpaths_obj, subpaths)) return NULL;
    bool has = false;
    double x0 = 0.0, y0 = 0.0, x1 = 0.0, y1 = 0.0;
    for (const auto &sub : subpaths) {
        for (const auto &p : sub.points) {
            if (!has) {
                x0 = x1 = p.x; y0 = y1 = p.y; has = true;
            } else {
                x0 = std::min(x0, p.x); y0 = std::min(y0, p.y);
                x1 = std::max(x1, p.x); y1 = std::max(y1, p.y);
            }
        }
    }
    if (!has) Py_RETURN_NONE;
    double pad = std::max(0.0, stroke_width) * 0.5;
    x0 -= pad; y0 -= pad; x1 += pad; y1 += pad;
    /* Round/bevel joins and round/butt caps fit within half-width padding.
     * Reuse the rendered geometry for extensions, including the miter limit. */
    if (pad > 0.0 && (strcmp(normalize_join_c(join), "miter") == 0 || strcmp(normalize_cap_c(cap), "square") == 0)) {
        for (const auto &sub : subpaths) {
            auto triangles = stroke_polyline_c(sub.points, sub.closed, stroke_width, join, cap, miter_limit);
            for (const auto &triangle : triangles) {
                for (const auto &p : triangle) {
                    x0 = std::min(x0, p.x); y0 = std::min(y0, p.y);
                    x1 = std::max(x1, p.x); y1 = std::max(y1, p.y);
                }
            }
        }
    }
    return Py_BuildValue("(dddd)", x0, y0, x1, y1);
}

static PyObject *py_path_fill_contains(PyObject *self, PyObject *args) {
    PyObject *subpaths_obj;
    double x, y;
    const char *fill_rule;
    if (!PyArg_ParseTuple(args, "Odds", &subpaths_obj, &x, &y, &fill_rule)) return NULL;
    std::vector<PathSubpath> subpaths;
    if (!parse_path_subpaths(subpaths_obj, subpaths)) return NULL;
    bool even_odd = normalize_fill_rule(fill_rule) == 0;
    int winding = 0;
    bool inside = false;
    for (const auto &sub : subpaths) {
        if (!sub.closed || sub.points.size() < 3) continue;
        std::vector<PathPoint> poly = (path_dist_sq(sub.points.front(), sub.points.back()) <= 1e-12)
            ? std::vector<PathPoint>(sub.points.begin(), sub.points.end() - 1)
            : sub.points;
        if (even_odd) {
            if (point_in_polygon(poly, x, y)) inside = !inside;
        } else {
            winding += polygon_winding(poly, x, y);
        }
    }
    return PyBool_FromLong(even_odd ? inside : (winding != 0));
}

static PyObject *py_path_stroke_contains(PyObject *self, PyObject *args) {
    PyObject *subpaths_obj;
    double x, y, width;
    const char *cap;
    if (!PyArg_ParseTuple(args, "Oddds", &subpaths_obj, &x, &y, &width, &cap)) return NULL;
    std::vector<PathSubpath> subpaths;
    if (!parse_path_subpaths(subpaths_obj, subpaths)) return NULL;
    double radius = width * 0.5;
    if (radius <= 0.0) Py_RETURN_FALSE;
    double rr = radius * radius;
    const char *norm_cap = normalize_cap_c(cap);
    for (const auto &sub : subpaths) {
        if (sub.points.size() < 2) continue;
        std::vector<PathPoint> poly = (sub.closed && path_dist_sq(sub.points.front(), sub.points.back()) <= 1e-12)
            ? std::vector<PathPoint>(sub.points.begin(), sub.points.end() - 1)
            : sub.points;
        if (polyline_hit(poly, x, y, rr, sub.closed, norm_cap)) Py_RETURN_TRUE;
    }
    Py_RETURN_FALSE;
}

static PyObject *py_build_path_meshes(PyObject *self, PyObject *args) {
    PyObject *subpaths_obj, *fill_color_obj, *stroke_color_obj;
    double stroke_width, miter_limit;
    double offset_x = 0.0, offset_y = 0.0, tolerance = 0.0;
    const char *join, *cap, *fill_rule;
    if (!PyArg_ParseTuple(args, "OOOdssds|ddd", &subpaths_obj, &fill_color_obj, &stroke_color_obj,
                          &stroke_width, &join, &cap, &miter_limit, &fill_rule,
                          &offset_x, &offset_y, &tolerance)) return NULL;
    if (!isfinite(tolerance) || tolerance < 0.0) {
        PyErr_SetString(PyExc_ValueError, "Stroke tolerance must be finite and non-negative");
        return NULL;
    }
    float ox = (float)offset_x, oy = (float)offset_y;
    std::vector<PathSubpath> subpaths;
    if (!parse_path_subpaths(subpaths_obj, subpaths)) return NULL;

    std::vector<PathMesh> fill_meshes;
    std::vector<PathMesh> stroke_meshes;
    std::vector<std::vector<PathPoint>> closed_contours;
    std::vector<std::pair<std::vector<PathPoint>, bool>> stroke_paths;
    for (const auto &sub : subpaths) {
        if (sub.points.size() < 2) continue;
        bool is_closed = sub.closed;
        std::vector<PathPoint> poly = (is_closed && sub.points.size() > 2 && path_dist_sq(sub.points.front(), sub.points.back()) <= 1e-12)
            ? std::vector<PathPoint>(sub.points.begin(), sub.points.end() - 1)
            : sub.points;
        if (is_closed && poly.size() >= 3) closed_contours.push_back(poly);
        stroke_paths.push_back({poly, is_closed});
    }

    float fill_color[4];
    if (fill_color_obj != Py_None && !closed_contours.empty() && c_parse_color(fill_color_obj, fill_color)) {
        /* Preserve each contour's direction. Stencil resolves overlapping fans
         * using parity or winding before the color pass blends each sample once.
         * Holes never write color, so previously drawn content stays intact. */
        std::vector<std::array<PathPoint, 3>> tris;
        for (const auto &contour : closed_contours) {
            for (size_t i = 1; i + 1 < contour.size(); i++) {
                append_triangle(tris, contour.front(), contour[i], contour[i + 1]);
            }
        }
        PathMesh mesh;
        triangles_to_mesh(tris, fill_color, true, mesh, ox, oy);
        fill_meshes.push_back(std::move(mesh));
        /* MSAA handles fill edges without a separate overlapping fringe. */
    }

    float stroke_color[4];
    if (stroke_color_obj != Py_None && stroke_width > 0.0 && c_parse_color(stroke_color_obj, stroke_color)) {
        std::vector<std::array<PathPoint, 3>> tris;
        for (const auto &item : stroke_paths) {
            if (item.first.size() < 2) continue;
            auto part = stroke_polyline_c(item.first, item.second, stroke_width, join, cap, miter_limit, tolerance);
            tris.insert(tris.end(), part.begin(), part.end());
        }
        if (!tris.empty()) {
            PathMesh mesh;
            triangles_to_mesh(tris, stroke_color, true, mesh, ox, oy);
            stroke_meshes.push_back(std::move(mesh));
            /* Stroke fringe AA is handled by MSAA; no software fringe needed. */
        }
    }

    PyObject *fills = build_meshes_py(fill_meshes);
    PyObject *strokes = build_meshes_py(stroke_meshes);
    if (!fills || !strokes) { Py_XDECREF(fills); Py_XDECREF(strokes); return NULL; }
    PyObject *ret = Py_BuildValue("(NN)", fills, strokes);
    return ret;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* offset_meshes — shift all vertex positions by (dx, dy) in-place bytes   */
/* ─────────────────────────────────────────────────────────────────────── */

/*
 * py_offset_meshes(mesh_list, dx, dy)
 *
 * Each mesh is a tuple: (vertex_bytes, vertex_count, index_bytes,
 *                        index_count, index_type, color, blend)
 *
 * vertex_bytes is packed float pairs: x0 y0 x1 y1 ...
 * Returns a new list of tuples with offset vertex_bytes.
 */
static PyObject *py_offset_meshes(PyObject *self, PyObject *args) {
    PyObject *mesh_list;
    double dx, dy;
    if (!PyArg_ParseTuple(args, "Odd", &mesh_list, &dx, &dy))
        return NULL;

    Py_ssize_t n = PyList_GET_SIZE(mesh_list);
    PyObject *result = PyList_New(n);
    if (!result) return NULL;

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *mesh = PyList_GET_ITEM(mesh_list, i);
        /* mesh is (vertex_bytes, vertex_count, index_bytes,
                    index_count, index_type, color, blend) */
        PyObject *vb_obj = PyTuple_GET_ITEM(mesh, 0);
        Py_ssize_t vb_len = PyBytes_GET_SIZE(vb_obj);

        /* Create mutable copy of vertex bytes */
        PyObject *new_vb = PyBytes_FromStringAndSize(PyBytes_AS_STRING(vb_obj), vb_len);
        if (!new_vb) { Py_DECREF(result); return NULL; }

        float *floats = (float *)PyBytes_AS_STRING(new_vb);
        Py_ssize_t float_count = vb_len / sizeof(float);
        /* vertex_count is item[1] — deduce floats_per_vert */
        int vc = (int)PyLong_AsLong(PyTuple_GET_ITEM(mesh, 1));
        int fpv = (vc > 0) ? (int)(float_count / vc) : 2;  /* 3 for fringe-aware, 2 for legacy */
        float fdx = (float)dx, fdy = (float)dy;
        for (Py_ssize_t j = 0; j + 1 < float_count; j += fpv) {
            floats[j]     += fdx;
            floats[j + 1] += fdy;
        }

        /* Build new tuple: (new_vb, vc, ib, ic, it, color, blend) */
        PyObject *new_mesh = PyTuple_New(7);
        PyTuple_SET_ITEM(new_mesh, 0, new_vb);  /* steals ref */
        for (int k = 1; k < 7; k++) {
            PyObject *item = PyTuple_GET_ITEM(mesh, k);
            Py_INCREF(item);
            PyTuple_SET_ITEM(new_mesh, k, item);
        }
        PyList_SET_ITEM(result, i, new_mesh);  /* steals ref */
    }
    return result;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Module definition                                                       */
/* ─────────────────────────────────────────────────────────────────────── */

static PyMethodDef methods[] = {
    {"collect", accel_collect, METH_VARARGS, "Collect scene graph and return packed GPU data."},
    {"matrix",    py_matrix,    METH_VARARGS, "Compute 2D affine matrix."},
    {"mul",       py_mul,       METH_VARARGS, "Multiply two 2D affine matrices."},
    {"apply",     py_apply,     METH_VARARGS, "Apply 2D affine matrix to a point."},
    {"avg_scale", py_avg_scale, METH_VARARGS, "Average scale factor of a matrix."},
    {"rot",       py_rot,       METH_VARARGS, "Rotation angle of a matrix."},
    {"feather",   py_feather,   METH_VARARGS, "Compute feather value for SDF antialiasing."},
    {"invert",    py_invert,    METH_VARARGS, "Invert a 2D affine matrix."},
    {"test_colliders", py_test_colliders, METH_VARARGS, "Test two colliders. Returns (nx,ny,depth,px,py) or None."},
    {"batch_collisions", py_batch_collisions, METH_VARARGS, "Batch collision test with spatial hash."},
    {"collisions", accel_collisions, METH_VARARGS, "Batch collision using cached colliders from collect."},
    {"contains_point", accel_contains_point, METH_VARARGS, "C-accelerated contains_point for a node."},
    {"hit_test", accel_hit_test, METH_VARARGS, "C-accelerated hit_test on interactive list."},
    {"hit_test_all", accel_hit_test_all, METH_VARARGS, "C-accelerated hit_test_all on interactive list."},
    {"path_flatten", py_path_flatten, METH_VARARGS, "Flatten path commands into subpaths."},
    {"path_bounds", py_path_bounds, METH_VARARGS, "Compute flattened path bounds."},
    {"path_fill_contains", py_path_fill_contains, METH_VARARGS, "Fill hit-test for flattened path subpaths."},
    {"path_stroke_contains", py_path_stroke_contains, METH_VARARGS, "Stroke hit-test for flattened path subpaths."},
    {"build_path_meshes", py_build_path_meshes, METH_VARARGS, "Build fill and stroke meshes for flattened path subpaths."},
    {"offset_meshes", py_offset_meshes, METH_VARARGS, "Offset vertex positions in mesh list by (dx, dy)."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module_def = {
    PyModuleDef_HEAD_INIT,
    "_cocoa._scene_accel",
    "C-accelerated scene graph collect and encode.",
    -1,
    methods
};

PyMODINIT_FUNC PyInit__scene_accel(void) {
    if (intern_strings() < 0) return NULL;
    return PyModule_Create(&module_def);
}

void registerSceneAccelModule(void) {
    PyImport_AppendInittab("_cocoa._scene_accel", PyInit__scene_accel);
}
