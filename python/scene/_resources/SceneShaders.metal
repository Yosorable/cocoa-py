// Scene module built-in shaders — compiled at build time into default.metallib.
// Eliminates ~200ms runtime MSL compilation on first frame.

#include <metal_stdlib>
using namespace metal;

// ─────────────────────────────────────────────────────────────────────────────
// Quad rendering (shapes + textures)
// ─────────────────────────────────────────────────────────────────────────────

struct VI { float2 position; float2 uv; };
struct VO { float4 position [[position]]; float2 uv; uint qid [[flat]]; };
struct U  { float2 resolution; float scale; float pad; };
struct QD { float4 params; float4 style; float4 fill; float4 extra; };

vertex VO quad_vertex(const device VI* v [[buffer(0)]], constant U& u [[buffer(1)]], uint vid [[vertex_id]]) {
    VI i = v[vid];
    VO o;
    o.position = float4((i.position.x / u.resolution.x) * 2.0 - 1.0,
                         1.0 - (i.position.y / u.resolution.y) * 2.0, 0, 1);
    o.uv = i.uv;
    o.qid = vid / 6;
    return o;
}

float sd_circle(float2 p, float r) { return length(p) - r; }
float sd_rrect(float2 p, float2 b, float r) {
    float2 q = abs(p) - b + float2(r);
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}
float sd_seg(float2 p, float2 a, float2 b) {
    float2 pa = p - a, ba = b - a;
    return length(pa - ba * clamp(dot(pa, ba) / max(dot(ba, ba), 1e-4), 0.0, 1.0));
}

float4 shade_shape(VO in, const device QD* q) {
    QD d = q[in.qid];
    int k = int(d.params.x + 0.5);
    float2 h = float2(d.params.y, d.params.z);
    float2 l = (in.uv - 0.5) * 2.0 * h;
    float f = d.style.y, dist = 1e5;
    if (k == 1) dist = sd_circle(l, d.params.w);
    else if (k == 2) dist = abs(sd_circle(l, d.params.w)) - d.style.x * 0.5;
    else if (k == 3) { float2 s = h - (f + 1.0); dist = sd_rrect(l, s, min(d.params.w, min(s.x, s.y))); }
    else if (k == 4) { float2 s = h - (f + 1.0); float cr = min(d.params.w, min(s.x, s.y));
        dist = max(sd_rrect(l, s, cr), -sd_rrect(l, s - d.style.x, max(0.0, cr - d.style.x))); }
    else if (k == 5) dist = sd_seg(l, d.extra.xy, d.extra.zw) - d.style.x * 0.5;
    float3 col = d.fill.rgb;
    float gt = d.style.w;
    if (gt > 0.5) {
        float t;
        if (gt < 1.5) {
            // Linear gradient
            float angle = d.extra.w;
            float2 dir = float2(cos(angle), sin(angle));
            t = dot(dir, l / h) * 0.5 + 0.5;
        } else {
            // Radial gradient
            t = length(l / h);
        }
        col = mix(col, d.extra.rgb, clamp(t, 0.0, 1.0));
    }
    float a = smoothstep(f, -f, dist) * d.fill.a * d.style.z;
    return float4(col * a, a);
}

float4 shade_texture(VO in, const device QD* q, texture2d<float> t) {
    QD d = q[in.qid];
    constexpr sampler s(address::clamp_to_edge, filter::linear);
    float4 c = t.sample(s, in.uv);
    float f = d.extra.a * d.style.z;
    return float4(c.rgb * d.extra.rgb * f, c.a * f);
}

// Separate entry points keep the public low-level pipelines independent of
// Scene clipping. Clip geometry never occupies the path renderer's stencil.
struct ClipRegion { float4 linear; float4 offset_origin; float4 size_radius; };
struct ClipInfo { uint count; float scale_x; float scale_y; uint pad; };

float clip_coverage(float2 position, const device ClipRegion* regions, constant ClipInfo& info) {
    float2 point = position / float2(info.scale_x, info.scale_y);
    float coverage = 1.0;
    for (uint i = 0; i < info.count; ++i) {
        ClipRegion c = regions[i];
        if (any(c.size_radius.xy <= 0.0)) return 0.0;
        float2 local = float2(dot(c.linear.xz, point), dot(c.linear.yw, point));
        local += c.offset_origin.xy - c.offset_origin.zw - c.size_radius.xy * 0.5;
        float distance = sd_rrect(local, c.size_radius.xy * 0.5, c.size_radius.z);
        float feather = max(fwidth(distance), 1e-6);
        coverage = min(coverage, clamp(0.5 - distance / feather, 0.0, 1.0));
    }
    return coverage;
}

fragment float4 shape_frag(VO in [[stage_in]], const device QD* q [[buffer(0)]]) {
    return shade_shape(in, q);
}

fragment float4 shape_frag_clipped(VO in [[stage_in]], const device QD* q [[buffer(0)]],
    const device ClipRegion* clips [[buffer(1)]], constant ClipInfo& info [[buffer(2)]]) {
    float coverage = clip_coverage(in.position.xy, clips, info);
    if (coverage <= 0.0) discard_fragment();
    return shade_shape(in, q) * coverage;
}

fragment float4 tex_frag(VO in [[stage_in]], const device QD* q [[buffer(0)]], texture2d<float> t [[texture(0)]]) {
    return shade_texture(in, q, t);
}

fragment float4 tex_frag_clipped(VO in [[stage_in]], const device QD* q [[buffer(0)]], texture2d<float> t [[texture(0)]],
    const device ClipRegion* clips [[buffer(1)]], constant ClipInfo& info [[buffer(2)]]) {
    float coverage = clip_coverage(in.position.xy, clips, info);
    if (coverage <= 0.0) discard_fragment();
    return shade_texture(in, q, t) * coverage;
}

// ─────────────────────────────────────────────────────────────────────────────
// Vector rendering (Path/Polygon mesh with per-vertex alpha for fringe AA)
// ─────────────────────────────────────────────────────────────────────────────

struct VIn { packed_float2 position; float alpha; };
struct VOut { float4 position [[position]]; float alpha; };
struct Res { float2 resolution; float2 pad; };
struct Col { float4 color; };

vertex VOut vector_vertex(const device VIn* v [[buffer(0)]], constant Res& r [[buffer(1)]], uint vid [[vertex_id]]) {
    VOut o;
    float2 p = v[vid].position;
    o.position = float4((p.x / r.resolution.x) * 2.0 - 1.0,
                        1.0 - (p.y / r.resolution.y) * 2.0,
                        0.0, 1.0);
    o.alpha = v[vid].alpha;
    return o;
}

fragment float4 vector_frag(VOut in [[stage_in]], constant Col& c [[buffer(0)]]) {
    float a = c.color.a * in.alpha;
    return float4(c.color.rgb * (a / max(c.color.a, 1e-5)), a);
}

fragment float4 vector_frag_clipped(VOut in [[stage_in]], constant Col& c [[buffer(0)]],
    const device ClipRegion* clips [[buffer(1)]], constant ClipInfo& info [[buffer(2)]]) {
    float coverage = clip_coverage(in.position.xy, clips, info);
    if (coverage <= 0.0) discard_fragment();
    float a = c.color.a * in.alpha;
    return float4(c.color.rgb * (a / max(c.color.a, 1e-5)), a) * coverage;
}

// ─────────────────────────────────────────────────────────────────────────────
// SDF text rendering
// ─────────────────────────────────────────────────────────────────────────────

vertex VO sdf_text_vertex(const device VI* v [[buffer(0)]], constant U& u [[buffer(1)]], uint vid [[vertex_id]]) {
    VI i = v[vid];
    VO o;
    o.position = float4((i.position.x / u.resolution.x) * 2.0 - 1.0,
                         1.0 - (i.position.y / u.resolution.y) * 2.0, 0, 1);
    o.uv = i.uv;
    o.qid = vid / 6;
    return o;
}

fragment float4 sdf_text_frag(VO in [[stage_in]], const device QD* q [[buffer(0)]],
                              texture2d<float> t [[texture(0)]]) {
    QD d = q[in.qid];
    constexpr sampler s(address::clamp_to_edge, filter::linear);
    float sd = t.sample(s, in.uv).r;
    float edge = 0.5;
    // params.y = pre-computed smoothing half-width (scale-aware, not fwidth)
    float w = d.params.y;
    float alpha = smoothstep(edge - w, edge + w, sd);
    float f = d.extra.a * d.style.z * alpha;
    return float4(d.extra.rgb * f, f);
}

// ─────────────────────────────────────────────────────────────────────────────
// SDF compute (JFA distance transform)
// ─────────────────────────────────────────────────────────────────────────────

struct P { int width; int height; int step; int mode; };

kernel void sdf_seed(texture2d<float, access::read> src [[texture(0)]],
                     texture2d<float, access::write> dst [[texture(1)]],
                     constant P &p [[buffer(0)]],
                     uint2 gid [[thread_position_in_grid]]) {
    if ((int)gid.x >= p.width || (int)gid.y >= p.height) return;
    float a = src.read(gid).a;
    bool hit = (p.mode == 1) ? (a > 0.5) : (a <= 0.5);
    if (hit) dst.write(float4(float(gid.x), float(gid.y), 0, 1), gid);
    else     dst.write(float4(-1, -1, 0, 0), gid);
}

kernel void sdf_jfa(texture2d<float, access::read> src [[texture(0)]],
                    texture2d<float, access::write> dst [[texture(1)]],
                    constant P &p [[buffer(0)]],
                    uint2 gid [[thread_position_in_grid]]) {
    if ((int)gid.x >= p.width || (int)gid.y >= p.height) return;
    float best = 1e20;
    float2 seed = float2(-1);
    for (int dy = -1; dy <= 1; dy++) {
        for (int dx = -1; dx <= 1; dx++) {
            int2 np = int2(gid) + int2(dx, dy) * p.step;
            if (np.x < 0 || np.y < 0 || np.x >= p.width || np.y >= p.height) continue;
            float4 s = src.read(uint2(np));
            if (s.x < 0) continue;
            float d = length_squared(float2(gid) - s.xy);
            if (d < best) { best = d; seed = s.xy; }
        }
    }
    dst.write(float4(seed, 0, seed.x >= 0 ? 1 : 0), gid);
}

kernel void sdf_final(texture2d<float, access::read> inside [[texture(0)]],
                      texture2d<float, access::read> outside [[texture(1)]],
                      texture2d<float, access::write> out [[texture(2)]],
                      constant P &p [[buffer(0)]],
                      uint2 gid [[thread_position_in_grid]]) {
    if ((int)gid.x >= p.width || (int)gid.y >= p.height) return;
    float4 si = inside.read(gid);
    float4 so = outside.read(gid);
    float di = (si.x >= 0) ? length(float2(gid) - si.xy) : 1e4;
    float do_ = (so.x >= 0) ? length(float2(gid) - so.xy) : 1e4;
    float sd = do_ - di;
    float spread = float(p.step);
    float val = clamp(sd / (2.0 * spread) + 0.5, 0.0, 1.0);
    out.write(float4(val, 0, 0, 0), gid);  /* r16f: only R channel matters */
}

// ─────────────────────────────────────────────────────────────────────────────
// Particle system (instanced billboard quads, GPU physics)
// ─────────────────────────────────────────────────────────────────────────────

struct Particle {
    float2 pos;       // birth position (screen coords)
    float2 vel;       // initial velocity
    float  birth;     // birth time
    float  lifetime;  // total lifetime
    float  size0;     // initial size
    float  size1;     // end size (size_over_life)
    float4 color;     // RGBA
    float  opacity0;  // start opacity
    float  opacity1;  // end opacity
    float2 _pad;
};

struct ParticleUniforms {
    float2 resolution;
    float  time;
    float  gravity_x;
    float  gravity_y;
    float  node_opacity;
    float2 _pad;
    float4 image_linear;
    float2 image_translation;
    float2 _image_pad;
};

struct ParticleOut {
    float4 position [[position]];
    float2 uv;
    float4 color;
};

vertex ParticleOut particle_vs(uint vid [[vertex_id]],
                               uint iid [[instance_id]],
                               const device Particle* particles [[buffer(0)]],
                               constant ParticleUniforms& u [[buffer(1)]]) {
    Particle p = particles[iid];

    float t = u.time - p.birth;
    float alive = step(0.0, t) * step(t, p.lifetime);

    // Physics: pos + vel*t + 0.5*gravity*t²
    float2 pos = p.pos + p.vel * t + float2(u.gravity_x, u.gravity_y) * 0.5 * t * t;

    // Age [0, 1]
    float age = clamp(t / max(p.lifetime, 0.001), 0.0, 1.0);

    // Size decay
    float sz = mix(p.size0, p.size1, age) * alive;

    // Opacity decay
    float alpha = mix(p.opacity0, p.opacity1, age) * alive * u.node_opacity;

    // Billboard quad: 6 vertices → 2 triangles
    constexpr float2 corners[] = {
        {-1,-1}, {1,-1}, {-1,1},
        {1,-1},  {1,1},  {-1,1}
    };
    float2 corner = corners[vid];
    float2 sp = pos + corner * sz;
    sp = float2(u.image_linear.x * sp.x + u.image_linear.z * sp.y,
                u.image_linear.y * sp.x + u.image_linear.w * sp.y) + u.image_translation;

    // Screen coords → NDC
    float2 ndc = float2(sp.x / u.resolution.x * 2.0 - 1.0,
                        1.0 - sp.y / u.resolution.y * 2.0);

    ParticleOut o;
    o.position = float4(ndc, 0, 1);
    o.uv = corner;
    o.color = float4(p.color.rgb, p.color.a * alpha);
    return o;
}

float4 shade_particle(ParticleOut in) {
    // Soft circle: smoothstep falloff
    float d = length(in.uv);
    float a = smoothstep(1.0, 0.6, d) * in.color.a;
    return float4(in.color.rgb * a, a);  // pre-multiplied alpha
}

float4 shade_particle_texture(ParticleOut in, texture2d<float> tex) {
    constexpr sampler s(address::clamp_to_edge, filter::linear);
    float2 uv = in.uv * 0.5 + 0.5;       // [-1,1] → [0,1]
    float4 t = tex.sample(s, uv);
    float a = t.a * in.color.a;
    return float4(t.rgb * in.color.rgb * a, a);  // tint + pre-multiplied alpha
}

fragment float4 particle_fs(ParticleOut in [[stage_in]]) {
    return shade_particle(in);
}

fragment float4 particle_fs_clipped(ParticleOut in [[stage_in]],
    const device ClipRegion* clips [[buffer(1)]], constant ClipInfo& info [[buffer(2)]]) {
    float coverage = clip_coverage(in.position.xy, clips, info);
    if (coverage <= 0.0) discard_fragment();
    return shade_particle(in) * coverage;
}

fragment float4 particle_tex_fs(ParticleOut in [[stage_in]], texture2d<float> tex [[texture(0)]]) {
    return shade_particle_texture(in, tex);
}

fragment float4 particle_tex_fs_clipped(ParticleOut in [[stage_in]], texture2d<float> tex [[texture(0)]],
    const device ClipRegion* clips [[buffer(1)]], constant ClipInfo& info [[buffer(2)]]) {
    float coverage = clip_coverage(in.position.xy, clips, info);
    if (coverage <= 0.0) discard_fragment();
    return shade_particle_texture(in, tex) * coverage;
}
