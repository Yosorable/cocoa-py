// PhysicsModule.mm — Python C bridge for Box2D v3 physics engine.
// Provides the _physics built-in module with World/Body/Shape/Joint management.

#import <Foundation/Foundation.h>
#include <Python.h>
#include "box2d/box2d.h"
#include "box2d/types.h"
#include "box2d/collision.h"
#include "box2d/math_functions.h"

// ────────────────────────────── Scale factor ──────────────────────────────
// Box2D uses meters; scene uses points. 1 meter = 100 points.
static const float kPTM = 100.0f;  // points-to-meters denominator
static inline float pt2m(float pt) { return pt / kPTM; }
static inline float m2pt(float m) { return m * kPTM; }

// ────────────────────────────── World management ──────────────────────────
// We support up to 16 simultaneous worlds (way more than needed).
#define MAX_WORLDS 16
static b2WorldId sWorlds[MAX_WORLDS];
static int sWorldCount = 0;

static int world_slot(b2WorldId wid) {
    for (int i = 0; i < sWorldCount; i++) {
        if (sWorlds[i].index1 == wid.index1 && sWorlds[i].generation == wid.generation)
            return i;
    }
    return -1;
}

// ────────────────────────────── Helper: pack/unpack IDs ──────────────────
// We store Box2D ids as Python integers (uint64 for body/shape/joint/chain, uint32 for world).

static PyObject *pack_world_id(b2WorldId wid) {
    return PyLong_FromUnsignedLong(b2StoreWorldId(wid));
}

static b2WorldId unpack_world_id(PyObject *obj) {
    uint32_t v = (uint32_t)PyLong_AsUnsignedLong(obj);
    return b2LoadWorldId(v);
}

static PyObject *pack_body_id(b2BodyId bid) {
    return PyLong_FromUnsignedLongLong(b2StoreBodyId(bid));
}

static b2BodyId unpack_body_id(PyObject *obj) {
    uint64_t v = PyLong_AsUnsignedLongLong(obj);
    return b2LoadBodyId(v);
}

static PyObject *pack_shape_id(b2ShapeId sid) {
    return PyLong_FromUnsignedLongLong(b2StoreShapeId(sid));
}

static b2ShapeId unpack_shape_id(PyObject *obj) {
    uint64_t v = PyLong_AsUnsignedLongLong(obj);
    return b2LoadShapeId(v);
}

static PyObject *pack_joint_id(b2JointId jid) {
    return PyLong_FromUnsignedLongLong(b2StoreJointId(jid));
}

static b2JointId unpack_joint_id(PyObject *obj) {
    uint64_t v = PyLong_AsUnsignedLongLong(obj);
    return b2LoadJointId(v);
}

static PyObject *pack_chain_id(b2ChainId cid) {
    return PyLong_FromUnsignedLongLong(b2StoreChainId(cid));
}

static b2ChainId unpack_chain_id(PyObject *obj) {
    uint64_t v = PyLong_AsUnsignedLongLong(obj);
    return b2LoadChainId(v);
}

// ────────────────────────────── World API ─────────────────────────────────

// create_world(gravity_x, gravity_y) -> world_id
static PyObject *py_create_world(PyObject *self, PyObject *args) {
    float gx, gy;
    if (!PyArg_ParseTuple(args, "ff", &gx, &gy)) return NULL;

    if (sWorldCount >= MAX_WORLDS) {
        PyErr_SetString(PyExc_RuntimeError, "max physics worlds reached");
        return NULL;
    }

    b2WorldDef def = b2DefaultWorldDef();
    def.gravity = (b2Vec2){pt2m(gx), pt2m(gy)};
    b2WorldId wid = b2CreateWorld(&def);
    sWorlds[sWorldCount++] = wid;
    return pack_world_id(wid);
}

// destroy_world(world_id)
static PyObject *py_destroy_world(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    if (!PyArg_ParseTuple(args, "O", &wid_obj)) return NULL;
    b2WorldId wid = unpack_world_id(wid_obj);

    int slot = world_slot(wid);
    if (slot >= 0) {
        b2DestroyWorld(wid);
        sWorlds[slot] = sWorlds[--sWorldCount];
    }
    Py_RETURN_NONE;
}

// world_step(world_id, time_step, sub_steps)
static PyObject *py_world_step(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    float ts;
    int sub;
    if (!PyArg_ParseTuple(args, "Ofi", &wid_obj, &ts, &sub)) return NULL;
    b2WorldId wid = unpack_world_id(wid_obj);
    b2World_Step(wid, ts, sub);
    Py_RETURN_NONE;
}

// world_set_gravity(world_id, gx, gy)
static PyObject *py_world_set_gravity(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    float gx, gy;
    if (!PyArg_ParseTuple(args, "Off", &wid_obj, &gx, &gy)) return NULL;
    b2WorldId wid = unpack_world_id(wid_obj);
    b2World_SetGravity(wid, (b2Vec2){pt2m(gx), pt2m(gy)});
    Py_RETURN_NONE;
}

// world_get_gravity(world_id) -> (gx, gy)
static PyObject *py_world_get_gravity(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    if (!PyArg_ParseTuple(args, "O", &wid_obj)) return NULL;
    b2WorldId wid = unpack_world_id(wid_obj);
    b2Vec2 g = b2World_GetGravity(wid);
    return Py_BuildValue("(ff)", m2pt(g.x), m2pt(g.y));
}

// ────────────────────────────── Body API ──────────────────────────────────

// create_body(world_id, type, x, y, rotation, linear_damping, angular_damping,
//             gravity_scale, is_bullet, allow_sleep) -> body_id
// type: 0=static, 1=kinematic, 2=dynamic
static PyObject *py_create_body(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    int btype;
    float x, y, rot, ld, ad, gs;
    int bullet, allow_sleep;
    if (!PyArg_ParseTuple(args, "Oiffffffii", &wid_obj, &btype, &x, &y, &rot,
                          &ld, &ad, &gs, &bullet, &allow_sleep))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2BodyDef def = b2DefaultBodyDef();
    def.type = (b2BodyType)btype;
    def.position = (b2Vec2){pt2m(x), pt2m(y)};
    def.rotation = b2MakeRot(rot);
    def.linearDamping = ld;
    def.angularDamping = ad;
    def.gravityScale = gs;
    def.isBullet = (bool)bullet;
    def.enableSleep = (bool)allow_sleep;
    def.isAwake = true;

    b2BodyId bid = b2CreateBody(wid, &def);
    return pack_body_id(bid);
}

// destroy_body(body_id)
static PyObject *py_destroy_body(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    b2BodyId bid = unpack_body_id(bid_obj);
    if (b2Body_IsValid(bid)) {
        b2DestroyBody(bid);
    }
    Py_RETURN_NONE;
}

// body_get_transform(body_id) -> (x, y, rotation)
static PyObject *py_body_get_transform(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    b2BodyId bid = unpack_body_id(bid_obj);
    b2Vec2 pos = b2Body_GetPosition(bid);
    b2Rot rot = b2Body_GetRotation(bid);
    float angle = b2Rot_GetAngle(rot);
    return Py_BuildValue("(fff)", m2pt(pos.x), m2pt(pos.y), angle);
}

// body_set_transform(body_id, x, y, rotation)
static PyObject *py_body_set_transform(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float x, y, rot;
    if (!PyArg_ParseTuple(args, "Offf", &bid_obj, &x, &y, &rot)) return NULL;
    b2BodyId bid = unpack_body_id(bid_obj);
    b2Body_SetTransform(bid, (b2Vec2){pt2m(x), pt2m(y)}, b2MakeRot(rot));
    Py_RETURN_NONE;
}

// body_get_velocity(body_id) -> (vx, vy, omega)
static PyObject *py_body_get_velocity(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    b2BodyId bid = unpack_body_id(bid_obj);
    b2Vec2 lv = b2Body_GetLinearVelocity(bid);
    float av = b2Body_GetAngularVelocity(bid);
    return Py_BuildValue("(fff)", m2pt(lv.x), m2pt(lv.y), av);
}

// body_set_velocity(body_id, vx, vy, omega)
static PyObject *py_body_set_velocity(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float vx, vy, omega;
    if (!PyArg_ParseTuple(args, "Offf", &bid_obj, &vx, &vy, &omega)) return NULL;
    b2BodyId bid = unpack_body_id(bid_obj);
    b2Body_SetLinearVelocity(bid, (b2Vec2){pt2m(vx), pt2m(vy)});
    b2Body_SetAngularVelocity(bid, omega);
    Py_RETURN_NONE;
}

// body_apply_force(body_id, fx, fy, px, py)
static PyObject *py_body_apply_force(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float fx, fy, px, py;
    if (!PyArg_ParseTuple(args, "Offff", &bid_obj, &fx, &fy, &px, &py)) return NULL;
    b2BodyId bid = unpack_body_id(bid_obj);
    // Force: N = kg*m/s^2. Scale from pt to m for the point, force stays in "pt-space" scaled.
    // We treat forces in pt-units: F_meters = F_pt / kPTM
    b2Body_ApplyForce(bid, (b2Vec2){fx / kPTM, fy / kPTM}, (b2Vec2){pt2m(px), pt2m(py)}, true);
    Py_RETURN_NONE;
}

// body_apply_force_center(body_id, fx, fy)
static PyObject *py_body_apply_force_center(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float fx, fy;
    if (!PyArg_ParseTuple(args, "Off", &bid_obj, &fx, &fy)) return NULL;
    b2BodyId bid = unpack_body_id(bid_obj);
    b2Body_ApplyForceToCenter(bid, (b2Vec2){fx / kPTM, fy / kPTM}, true);
    Py_RETURN_NONE;
}

// body_apply_impulse(body_id, ix, iy, px, py)
static PyObject *py_body_apply_impulse(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float ix, iy, px, py;
    if (!PyArg_ParseTuple(args, "Offff", &bid_obj, &ix, &iy, &px, &py)) return NULL;
    b2BodyId bid = unpack_body_id(bid_obj);
    b2Body_ApplyLinearImpulse(bid, (b2Vec2){ix / kPTM, iy / kPTM},
                              (b2Vec2){pt2m(px), pt2m(py)}, true);
    Py_RETURN_NONE;
}

// body_apply_impulse_center(body_id, ix, iy)
static PyObject *py_body_apply_impulse_center(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float ix, iy;
    if (!PyArg_ParseTuple(args, "Off", &bid_obj, &ix, &iy)) return NULL;
    b2BodyId bid = unpack_body_id(bid_obj);
    b2Body_ApplyLinearImpulseToCenter(bid, (b2Vec2){ix / kPTM, iy / kPTM}, true);
    Py_RETURN_NONE;
}

// body_apply_torque(body_id, torque)
static PyObject *py_body_apply_torque(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float torque;
    if (!PyArg_ParseTuple(args, "Of", &bid_obj, &torque)) return NULL;
    b2BodyId bid = unpack_body_id(bid_obj);
    // Torque: N*m. Scale by 1/kPTM^2 from pt^2 to m^2
    b2Body_ApplyTorque(bid, torque / (kPTM * kPTM), true);
    Py_RETURN_NONE;
}

// body_get_mass(body_id) -> mass
static PyObject *py_body_get_mass(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    b2BodyId bid = unpack_body_id(bid_obj);
    return PyFloat_FromDouble(b2Body_GetMass(bid));
}

// body_set_type(body_id, type)
static PyObject *py_body_set_type(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    int btype;
    if (!PyArg_ParseTuple(args, "Oi", &bid_obj, &btype)) return NULL;
    b2BodyId bid = unpack_body_id(bid_obj);
    b2Body_SetType(bid, (b2BodyType)btype);
    Py_RETURN_NONE;
}

// body_set_linear_damping(body_id, damping)
static PyObject *py_body_set_linear_damping(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float d;
    if (!PyArg_ParseTuple(args, "Of", &bid_obj, &d)) return NULL;
    b2Body_SetLinearDamping(unpack_body_id(bid_obj), d);
    Py_RETURN_NONE;
}

// body_set_angular_damping(body_id, damping)
static PyObject *py_body_set_angular_damping(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float d;
    if (!PyArg_ParseTuple(args, "Of", &bid_obj, &d)) return NULL;
    b2Body_SetAngularDamping(unpack_body_id(bid_obj), d);
    Py_RETURN_NONE;
}

// body_set_gravity_scale(body_id, scale)
static PyObject *py_body_set_gravity_scale(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float s;
    if (!PyArg_ParseTuple(args, "Of", &bid_obj, &s)) return NULL;
    b2Body_SetGravityScale(unpack_body_id(bid_obj), s);
    Py_RETURN_NONE;
}

// ────────────────────────────── Shape API ─────────────────────────────────

// Helper to set up common ShapeDef fields
static void setup_shape_def(b2ShapeDef *def, float density, float friction,
                            float restitution, uint64_t category, uint64_t mask,
                            int is_sensor, int enable_contact_events) {
    def->density = density;
    def->material.friction = friction;
    def->material.restitution = restitution;
    def->filter.categoryBits = category;
    def->filter.maskBits = mask;
    def->isSensor = (bool)is_sensor;
    def->enableContactEvents = (bool)enable_contact_events;
    def->enableHitEvents = (bool)enable_contact_events;
    // Both sensor and visitor shapes need this flag for sensor events to fire
    def->enableSensorEvents = true;
}

// add_circle_shape(body_id, radius, offset_x, offset_y,
//                  density, friction, restitution, category, mask, is_sensor, enable_contacts)
// -> shape_id
static PyObject *py_add_circle_shape(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float radius, ox, oy, density, friction, restitution;
    unsigned long long category, mask;
    int is_sensor, enable_contacts;
    if (!PyArg_ParseTuple(args, "OffffffKKii", &bid_obj, &radius, &ox, &oy,
                          &density, &friction, &restitution,
                          &category, &mask, &is_sensor, &enable_contacts))
        return NULL;

    b2BodyId bid = unpack_body_id(bid_obj);
    b2ShapeDef sdef = b2DefaultShapeDef();
    setup_shape_def(&sdef, density, friction, restitution, category, mask,
                    is_sensor, enable_contacts);

    b2Circle circle = {{pt2m(ox), pt2m(oy)}, pt2m(radius)};
    b2ShapeId sid = b2CreateCircleShape(bid, &sdef, &circle);
    return pack_shape_id(sid);
}

// add_box_shape(body_id, half_w, half_h, center_x, center_y, rotation,
//               density, friction, restitution, category, mask, is_sensor, enable_contacts)
// -> shape_id
static PyObject *py_add_box_shape(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float hw, hh, cx, cy, rot, density, friction, restitution;
    unsigned long long category, mask;
    int is_sensor, enable_contacts;
    if (!PyArg_ParseTuple(args, "OffffffffKKii", &bid_obj, &hw, &hh, &cx, &cy, &rot,
                          &density, &friction, &restitution,
                          &category, &mask, &is_sensor, &enable_contacts))
        return NULL;

    b2BodyId bid = unpack_body_id(bid_obj);
    b2ShapeDef sdef = b2DefaultShapeDef();
    setup_shape_def(&sdef, density, friction, restitution, category, mask,
                    is_sensor, enable_contacts);

    b2Polygon box = b2MakeOffsetBox(pt2m(hw), pt2m(hh),
                                     (b2Vec2){pt2m(cx), pt2m(cy)}, b2MakeRot(rot));
    b2ShapeId sid = b2CreatePolygonShape(bid, &sdef, &box);
    return pack_shape_id(sid);
}

// add_polygon_shape(body_id, points_flat, density, friction, restitution,
//                   category, mask, is_sensor, enable_contacts)
// points_flat: flat list [x0,y0, x1,y1, ...] (max 8 vertices, Box2D limit)
// -> shape_id
static PyObject *py_add_polygon_shape(PyObject *self, PyObject *args) {
    PyObject *bid_obj, *pts_obj;
    float density, friction, restitution;
    unsigned long long category, mask;
    int is_sensor, enable_contacts;
    if (!PyArg_ParseTuple(args, "OOfffKKii", &bid_obj, &pts_obj,
                          &density, &friction, &restitution,
                          &category, &mask, &is_sensor, &enable_contacts))
        return NULL;

    PyObject *seq = PySequence_Fast(pts_obj, "points must be a sequence");
    if (!seq) return NULL;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(seq);
    if (n < 6 || n > 16 || (n & 1)) {
        Py_DECREF(seq);
        PyErr_SetString(PyExc_ValueError, "need 3-8 vertices (6-16 floats)");
        return NULL;
    }

    int vc = (int)(n / 2);
    b2Vec2 verts[8];
    for (int i = 0; i < vc; i++) {
        float vx = (float)PyFloat_AsDouble(PySequence_Fast_GET_ITEM(seq, i * 2));
        float vy = (float)PyFloat_AsDouble(PySequence_Fast_GET_ITEM(seq, i * 2 + 1));
        verts[i] = (b2Vec2){pt2m(vx), pt2m(vy)};
    }
    Py_DECREF(seq);

    b2Hull hull = b2ComputeHull(verts, vc);
    if (hull.count == 0) {
        PyErr_SetString(PyExc_ValueError, "degenerate polygon");
        return NULL;
    }

    b2BodyId bid = unpack_body_id(bid_obj);
    b2ShapeDef sdef = b2DefaultShapeDef();
    setup_shape_def(&sdef, density, friction, restitution, category, mask,
                    is_sensor, enable_contacts);

    b2Polygon poly = b2MakePolygon(&hull, 0.0f);
    b2ShapeId sid = b2CreatePolygonShape(bid, &sdef, &poly);
    return pack_shape_id(sid);
}

// add_capsule_shape(body_id, x1, y1, x2, y2, radius,
//                   density, friction, restitution, category, mask, is_sensor, enable_contacts)
// -> shape_id
static PyObject *py_add_capsule_shape(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float x1, y1, x2, y2, radius, density, friction, restitution;
    unsigned long long category, mask;
    int is_sensor, enable_contacts;
    if (!PyArg_ParseTuple(args, "OffffffffKKii", &bid_obj, &x1, &y1, &x2, &y2, &radius,
                          &density, &friction, &restitution,
                          &category, &mask, &is_sensor, &enable_contacts))
        return NULL;

    b2BodyId bid = unpack_body_id(bid_obj);
    b2ShapeDef sdef = b2DefaultShapeDef();
    setup_shape_def(&sdef, density, friction, restitution, category, mask,
                    is_sensor, enable_contacts);

    b2Capsule cap = {{pt2m(x1), pt2m(y1)}, {pt2m(x2), pt2m(y2)}, pt2m(radius)};
    b2ShapeId sid = b2CreateCapsuleShape(bid, &sdef, &cap);
    return pack_shape_id(sid);
}

// destroy_shape(shape_id)
static PyObject *py_destroy_shape(PyObject *self, PyObject *args) {
    PyObject *sid_obj;
    if (!PyArg_ParseTuple(args, "O", &sid_obj)) return NULL;
    b2ShapeId sid = unpack_shape_id(sid_obj);
    if (b2Shape_IsValid(sid)) {
        b2DestroyShape(sid, true);
    }
    Py_RETURN_NONE;
}

// shape_set_filter(shape_id, category, mask)
static PyObject *py_shape_set_filter(PyObject *self, PyObject *args) {
    PyObject *sid_obj;
    unsigned long long category, mask;
    if (!PyArg_ParseTuple(args, "OKK", &sid_obj, &category, &mask)) return NULL;
    b2ShapeId sid = unpack_shape_id(sid_obj);
    b2Filter f = b2DefaultFilter();
    f.categoryBits = category;
    f.maskBits = mask;
    b2Shape_SetFilter(sid, f);
    Py_RETURN_NONE;
}

// shape_set_density(shape_id, density)
static PyObject *py_shape_set_density(PyObject *self, PyObject *args) {
    PyObject *sid_obj;
    float d;
    if (!PyArg_ParseTuple(args, "Of", &sid_obj, &d)) return NULL;
    b2Shape_SetDensity(unpack_shape_id(sid_obj), d, true);
    Py_RETURN_NONE;
}

// shape_set_friction(shape_id, friction)
static PyObject *py_shape_set_friction(PyObject *self, PyObject *args) {
    PyObject *sid_obj;
    float f;
    if (!PyArg_ParseTuple(args, "Of", &sid_obj, &f)) return NULL;
    b2Shape_SetFriction(unpack_shape_id(sid_obj), f);
    Py_RETURN_NONE;
}

// shape_set_restitution(shape_id, restitution)
static PyObject *py_shape_set_restitution(PyObject *self, PyObject *args) {
    PyObject *sid_obj;
    float r;
    if (!PyArg_ParseTuple(args, "Of", &sid_obj, &r)) return NULL;
    b2Shape_SetRestitution(unpack_shape_id(sid_obj), r);
    Py_RETURN_NONE;
}

// ────────────────────────────── Joint API ─────────────────────────────────

// Helper: compute localFrame for a body given a world-space anchor point.
// localFrame.p = local anchor, localFrame.q = identity (no extra rotation).
static b2Transform local_frame_from_anchor(b2BodyId body, b2Vec2 worldAnchor) {
    b2Transform frame;
    frame.p = b2Body_GetLocalPoint(body, worldAnchor);
    frame.q = b2Rot_identity;
    return frame;
}

// Helper: compute localFrame with an axis direction encoded in the rotation.
static b2Transform local_frame_from_anchor_axis(b2BodyId body, b2Vec2 worldAnchor,
                                                 b2Vec2 worldAxis) {
    b2Transform frame;
    frame.p = b2Body_GetLocalPoint(body, worldAnchor);
    // Encode axis direction as local rotation
    b2Vec2 localAxis = b2Body_GetLocalVector(body, worldAxis);
    frame.q = (b2Rot){localAxis.x, localAxis.y};  // (cos, sin)
    return frame;
}

// create_revolute_joint(world_id, body_a_id, body_b_id, anchor_x, anchor_y,
//                       enable_limit, lower, upper, enable_motor, motor_speed, max_torque)
// -> joint_id
static PyObject *py_create_revolute_joint(PyObject *self, PyObject *args) {
    PyObject *wid_obj, *ba_obj, *bb_obj;
    float ax, ay, lower, upper, motor_speed, max_torque;
    int enable_limit, enable_motor;
    if (!PyArg_ParseTuple(args, "OOOffiffiff", &wid_obj, &ba_obj, &bb_obj,
                          &ax, &ay, &enable_limit, &lower, &upper,
                          &enable_motor, &motor_speed, &max_torque))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2BodyId ba = unpack_body_id(ba_obj);
    b2BodyId bb = unpack_body_id(bb_obj);

    b2Vec2 anchor = {pt2m(ax), pt2m(ay)};
    b2RevoluteJointDef def = b2DefaultRevoluteJointDef();
    def.base.bodyIdA = ba;
    def.base.bodyIdB = bb;
    def.base.localFrameA = local_frame_from_anchor(ba, anchor);
    def.base.localFrameB = local_frame_from_anchor(bb, anchor);
    def.enableLimit = (bool)enable_limit;
    def.lowerAngle = lower;
    def.upperAngle = upper;
    def.enableMotor = (bool)enable_motor;
    def.motorSpeed = motor_speed;
    def.maxMotorTorque = max_torque / (kPTM * kPTM);

    b2JointId jid = b2CreateRevoluteJoint(wid, &def);
    return pack_joint_id(jid);
}

// create_distance_joint(world_id, body_a_id, body_b_id,
//                       anchor_a_x, anchor_a_y, anchor_b_x, anchor_b_y,
//                       length, min_length, max_length, hertz, damping_ratio)
// -> joint_id
static PyObject *py_create_distance_joint(PyObject *self, PyObject *args) {
    PyObject *wid_obj, *ba_obj, *bb_obj;
    float aax, aay, abx, aby, length, min_len, max_len, hertz, damping;
    if (!PyArg_ParseTuple(args, "OOOfffffffff", &wid_obj, &ba_obj, &bb_obj,
                          &aax, &aay, &abx, &aby,
                          &length, &min_len, &max_len, &hertz, &damping))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2BodyId ba = unpack_body_id(ba_obj);
    b2BodyId bb = unpack_body_id(bb_obj);

    b2DistanceJointDef def = b2DefaultDistanceJointDef();
    def.base.bodyIdA = ba;
    def.base.bodyIdB = bb;
    def.base.localFrameA = local_frame_from_anchor(ba, (b2Vec2){pt2m(aax), pt2m(aay)});
    def.base.localFrameB = local_frame_from_anchor(bb, (b2Vec2){pt2m(abx), pt2m(aby)});
    def.length = pt2m(length);
    def.enableSpring = (hertz > 0);
    if (min_len >= 0 || max_len >= 0) {
        def.enableLimit = true;
        if (min_len >= 0) def.minLength = pt2m(min_len);
        if (max_len >= 0) def.maxLength = pt2m(max_len);
    }
    def.hertz = hertz;
    def.dampingRatio = damping;

    b2JointId jid = b2CreateDistanceJoint(wid, &def);
    return pack_joint_id(jid);
}

// create_weld_joint(world_id, body_a_id, body_b_id, anchor_x, anchor_y,
//                   hertz, damping_ratio)
// -> joint_id
static PyObject *py_create_weld_joint(PyObject *self, PyObject *args) {
    PyObject *wid_obj, *ba_obj, *bb_obj;
    float ax, ay, hertz, damping;
    if (!PyArg_ParseTuple(args, "OOOffff", &wid_obj, &ba_obj, &bb_obj,
                          &ax, &ay, &hertz, &damping))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2BodyId ba = unpack_body_id(ba_obj);
    b2BodyId bb = unpack_body_id(bb_obj);

    b2Vec2 anchor = {pt2m(ax), pt2m(ay)};
    b2WeldJointDef def = b2DefaultWeldJointDef();
    def.base.bodyIdA = ba;
    def.base.bodyIdB = bb;
    def.base.localFrameA = local_frame_from_anchor(ba, anchor);
    def.base.localFrameB = local_frame_from_anchor(bb, anchor);
    def.linearHertz = hertz;
    def.linearDampingRatio = damping;
    def.angularHertz = hertz;
    def.angularDampingRatio = damping;

    b2JointId jid = b2CreateWeldJoint(wid, &def);
    return pack_joint_id(jid);
}

// create_prismatic_joint(world_id, body_a_id, body_b_id, anchor_x, anchor_y,
//                        axis_x, axis_y, enable_limit, lower, upper,
//                        enable_motor, motor_speed, max_force)
// -> joint_id
static PyObject *py_create_prismatic_joint(PyObject *self, PyObject *args) {
    PyObject *wid_obj, *ba_obj, *bb_obj;
    float ax, ay, axis_x, axis_y, lower, upper, motor_speed, max_force;
    int enable_limit, enable_motor;
    if (!PyArg_ParseTuple(args, "OOOffffiffiff", &wid_obj, &ba_obj, &bb_obj,
                          &ax, &ay, &axis_x, &axis_y,
                          &enable_limit, &lower, &upper,
                          &enable_motor, &motor_speed, &max_force))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2BodyId ba = unpack_body_id(ba_obj);
    b2BodyId bb = unpack_body_id(bb_obj);

    b2Vec2 anchor = {pt2m(ax), pt2m(ay)};
    // Normalize axis
    float len = sqrtf(axis_x * axis_x + axis_y * axis_y);
    if (len <= 0) {
        PyErr_SetString(PyExc_ValueError, "axis must be non-zero");
        return NULL;
    }
    axis_x /= len; axis_y /= len;
    b2Vec2 worldAxis = {axis_x, axis_y};

    b2PrismaticJointDef def = b2DefaultPrismaticJointDef();
    def.base.bodyIdA = ba;
    def.base.bodyIdB = bb;
    def.base.localFrameA = local_frame_from_anchor_axis(ba, anchor, worldAxis);
    def.base.localFrameB = local_frame_from_anchor(bb, anchor);
    def.enableLimit = (bool)enable_limit;
    def.lowerTranslation = pt2m(lower);
    def.upperTranslation = pt2m(upper);
    def.enableMotor = (bool)enable_motor;
    def.motorSpeed = pt2m(motor_speed);
    def.maxMotorForce = max_force / kPTM;

    b2JointId jid = b2CreatePrismaticJoint(wid, &def);
    return pack_joint_id(jid);
}

// create_wheel_joint(world_id, body_a_id, body_b_id, anchor_x, anchor_y,
//                    axis_x, axis_y, hertz, damping_ratio,
//                    enable_limit, lower, upper,
//                    enable_motor, motor_speed, max_torque)
// -> joint_id
static PyObject *py_create_wheel_joint(PyObject *self, PyObject *args) {
    PyObject *wid_obj, *ba_obj, *bb_obj;
    float ax, ay, axis_x, axis_y, hertz, damping;
    float lower, upper, motor_speed, max_torque;
    int enable_limit, enable_motor;
    if (!PyArg_ParseTuple(args, "OOOffffffiffiff", &wid_obj, &ba_obj, &bb_obj,
                          &ax, &ay, &axis_x, &axis_y, &hertz, &damping,
                          &enable_limit, &lower, &upper,
                          &enable_motor, &motor_speed, &max_torque))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2BodyId ba = unpack_body_id(ba_obj);
    b2BodyId bb = unpack_body_id(bb_obj);

    b2Vec2 anchor = {pt2m(ax), pt2m(ay)};
    float len = sqrtf(axis_x * axis_x + axis_y * axis_y);
    if (len <= 0) {
        PyErr_SetString(PyExc_ValueError, "axis must be non-zero");
        return NULL;
    }
    axis_x /= len; axis_y /= len;
    b2Vec2 worldAxis = {axis_x, axis_y};

    b2WheelJointDef def = b2DefaultWheelJointDef();
    def.base.bodyIdA = ba;
    def.base.bodyIdB = bb;
    def.base.localFrameA = local_frame_from_anchor_axis(ba, anchor, worldAxis);
    def.base.localFrameB = local_frame_from_anchor(bb, anchor);
    def.enableSpring = true;
    def.hertz = hertz;
    def.dampingRatio = damping;
    def.enableLimit = (bool)enable_limit;
    def.lowerTranslation = pt2m(lower);
    def.upperTranslation = pt2m(upper);
    def.enableMotor = (bool)enable_motor;
    def.motorSpeed = motor_speed;
    def.maxMotorTorque = max_torque / (kPTM * kPTM);

    b2JointId jid = b2CreateWheelJoint(wid, &def);
    return pack_joint_id(jid);
}

// destroy_joint(joint_id)
static PyObject *py_destroy_joint(PyObject *self, PyObject *args) {
    PyObject *jid_obj;
    if (!PyArg_ParseTuple(args, "O", &jid_obj)) return NULL;
    b2JointId jid = unpack_joint_id(jid_obj);
    if (b2Joint_IsValid(jid)) {
        b2DestroyJoint(jid, true);
    }
    Py_RETURN_NONE;
}

// ────────────────────────────── Contact events ───────────────────────────

// get_contact_events(world_id) -> list of (body_a_id, body_b_id, type)
// type: 0=begin, 1=end, 2=hit
static PyObject *py_get_contact_events(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    if (!PyArg_ParseTuple(args, "O", &wid_obj)) return NULL;
    b2WorldId wid = unpack_world_id(wid_obj);

    b2ContactEvents events = b2World_GetContactEvents(wid);

    // Use a growable list since we skip invalid shapes
    PyObject *list = PyList_New(0);
    if (!list) return NULL;

    // Begin contacts
    for (int i = 0; i < events.beginCount; i++) {
        b2ContactBeginTouchEvent *e = &events.beginEvents[i];
        if (!b2Shape_IsValid(e->shapeIdA) || !b2Shape_IsValid(e->shapeIdB)) continue;
        b2BodyId ba = b2Shape_GetBody(e->shapeIdA);
        b2BodyId bb = b2Shape_GetBody(e->shapeIdB);
        PyObject *t = Py_BuildValue("(NNi)", pack_body_id(ba), pack_body_id(bb), 0);
        PyList_Append(list, t);
        Py_DECREF(t);
    }
    // End contacts
    for (int i = 0; i < events.endCount; i++) {
        b2ContactEndTouchEvent *e = &events.endEvents[i];
        if (!b2Shape_IsValid(e->shapeIdA) || !b2Shape_IsValid(e->shapeIdB)) continue;
        b2BodyId ba = b2Shape_GetBody(e->shapeIdA);
        b2BodyId bb = b2Shape_GetBody(e->shapeIdB);
        PyObject *t = Py_BuildValue("(NNi)", pack_body_id(ba), pack_body_id(bb), 1);
        PyList_Append(list, t);
        Py_DECREF(t);
    }
    // Hit events
    for (int i = 0; i < events.hitCount; i++) {
        b2ContactHitEvent *e = &events.hitEvents[i];
        if (!b2Shape_IsValid(e->shapeIdA) || !b2Shape_IsValid(e->shapeIdB)) continue;
        b2BodyId ba = b2Shape_GetBody(e->shapeIdA);
        b2BodyId bb = b2Shape_GetBody(e->shapeIdB);
        PyObject *t = Py_BuildValue("(NNi)", pack_body_id(ba), pack_body_id(bb), 2);
        PyList_Append(list, t);
        Py_DECREF(t);
    }
    return list;
}

// ────────────────────────────── Ray cast ──────────────────────────────────

// ray_cast_closest(world_id, origin_x, origin_y, dir_x, dir_y, category, mask)
// -> (hit, body_id, point_x, point_y, normal_x, normal_y, fraction) or None
static PyObject *py_ray_cast_closest(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    float ox, oy, dx, dy;
    unsigned long long category, mask;
    if (!PyArg_ParseTuple(args, "OffffKK", &wid_obj, &ox, &oy, &dx, &dy,
                          &category, &mask))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2QueryFilter filter = b2DefaultQueryFilter();
    filter.categoryBits = category;
    filter.maskBits = mask;

    b2RayResult result = b2World_CastRayClosest(wid,
                                                 (b2Vec2){pt2m(ox), pt2m(oy)},
                                                 (b2Vec2){pt2m(dx), pt2m(dy)},
                                                 filter);
    if (!result.hit) {
        Py_RETURN_NONE;
    }

    b2BodyId bid = b2Shape_GetBody(result.shapeId);
    return Py_BuildValue("(Nfffff)",
                         pack_body_id(bid),
                         m2pt(result.point.x), m2pt(result.point.y),
                         result.normal.x, result.normal.y,
                         result.fraction);
}

// ────────────────────────────── Batch sync ────────────────────────────────

// sync_bodies(list_of_(body_id,)) -> list of (x, y, rotation)
// Batch-read all body transforms in one C call (avoids N Python→C round-trips).
static PyObject *py_sync_bodies(PyObject *self, PyObject *args) {
    PyObject *body_list;
    if (!PyArg_ParseTuple(args, "O", &body_list)) return NULL;

    PyObject *seq = PySequence_Fast(body_list, "expected sequence");
    if (!seq) return NULL;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(seq);

    PyObject *result = PyList_New(n);
    if (!result) { Py_DECREF(seq); return NULL; }

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *bid_obj = PySequence_Fast_GET_ITEM(seq, i);
        b2BodyId bid = unpack_body_id(bid_obj);
        PyObject *t;
        if (b2Body_IsValid(bid)) {
            b2Vec2 pos = b2Body_GetPosition(bid);
            b2Rot rot = b2Body_GetRotation(bid);
            float angle = b2Rot_GetAngle(rot);
            t = Py_BuildValue("(fff)", m2pt(pos.x), m2pt(pos.y), angle);
        } else {
            t = Py_BuildValue("(fff)", 0.0f, 0.0f, 0.0f);
        }
        PyList_SET_ITEM(result, i, t);
    }
    Py_DECREF(seq);
    return result;
}

// ────────────────────────────── Body State Control ────────────────────────

static PyObject *py_body_is_valid(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    return PyBool_FromLong(b2Body_IsValid(unpack_body_id(bid_obj)));
}

static PyObject *py_body_is_enabled(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    return PyBool_FromLong(b2Body_IsEnabled(unpack_body_id(bid_obj)));
}

static PyObject *py_body_enable(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    b2Body_Enable(unpack_body_id(bid_obj));
    Py_RETURN_NONE;
}

static PyObject *py_body_disable(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    b2Body_Disable(unpack_body_id(bid_obj));
    Py_RETURN_NONE;
}

static PyObject *py_body_is_awake(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    return PyBool_FromLong(b2Body_IsAwake(unpack_body_id(bid_obj)));
}

static PyObject *py_body_set_awake(PyObject *self, PyObject *args) {
    PyObject *bid_obj; int flag;
    if (!PyArg_ParseTuple(args, "Oi", &bid_obj, &flag)) return NULL;
    b2Body_SetAwake(unpack_body_id(bid_obj), (bool)flag);
    Py_RETURN_NONE;
}

static PyObject *py_body_enable_sleep(PyObject *self, PyObject *args) {
    PyObject *bid_obj; int flag;
    if (!PyArg_ParseTuple(args, "Oi", &bid_obj, &flag)) return NULL;
    b2Body_EnableSleep(unpack_body_id(bid_obj), (bool)flag);
    Py_RETURN_NONE;
}

static PyObject *py_body_is_sleep_enabled(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    return PyBool_FromLong(b2Body_IsSleepEnabled(unpack_body_id(bid_obj)));
}

static PyObject *py_body_set_bullet(PyObject *self, PyObject *args) {
    PyObject *bid_obj; int flag;
    if (!PyArg_ParseTuple(args, "Oi", &bid_obj, &flag)) return NULL;
    b2Body_SetBullet(unpack_body_id(bid_obj), (bool)flag);
    Py_RETURN_NONE;
}

static PyObject *py_body_is_bullet(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    return PyBool_FromLong(b2Body_IsBullet(unpack_body_id(bid_obj)));
}

static PyObject *py_body_set_motion_locks(PyObject *self, PyObject *args) {
    PyObject *bid_obj; int lx, ly, la;
    if (!PyArg_ParseTuple(args, "Oiii", &bid_obj, &lx, &ly, &la)) return NULL;
    b2MotionLocks locks;
    locks.linearX = (bool)lx;
    locks.linearY = (bool)ly;
    locks.angularZ = (bool)la;
    b2Body_SetMotionLocks(unpack_body_id(bid_obj), locks);
    Py_RETURN_NONE;
}

static PyObject *py_body_get_motion_locks(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    b2MotionLocks locks = b2Body_GetMotionLocks(unpack_body_id(bid_obj));
    return Py_BuildValue("(iii)", (int)locks.linearX, (int)locks.linearY, (int)locks.angularZ);
}

// ────────────────────────────── Body Coordinates ─────────────────────────

static PyObject *py_body_get_local_point(PyObject *self, PyObject *args) {
    PyObject *bid_obj; float wx, wy;
    if (!PyArg_ParseTuple(args, "Off", &bid_obj, &wx, &wy)) return NULL;
    b2Vec2 lp = b2Body_GetLocalPoint(unpack_body_id(bid_obj), (b2Vec2){pt2m(wx), pt2m(wy)});
    return Py_BuildValue("(ff)", m2pt(lp.x), m2pt(lp.y));
}

static PyObject *py_body_get_world_point(PyObject *self, PyObject *args) {
    PyObject *bid_obj; float lx, ly;
    if (!PyArg_ParseTuple(args, "Off", &bid_obj, &lx, &ly)) return NULL;
    b2Vec2 wp = b2Body_GetWorldPoint(unpack_body_id(bid_obj), (b2Vec2){pt2m(lx), pt2m(ly)});
    return Py_BuildValue("(ff)", m2pt(wp.x), m2pt(wp.y));
}

static PyObject *py_body_get_local_vector(PyObject *self, PyObject *args) {
    PyObject *bid_obj; float wx, wy;
    if (!PyArg_ParseTuple(args, "Off", &bid_obj, &wx, &wy)) return NULL;
    b2Vec2 lv = b2Body_GetLocalVector(unpack_body_id(bid_obj), (b2Vec2){pt2m(wx), pt2m(wy)});
    return Py_BuildValue("(ff)", m2pt(lv.x), m2pt(lv.y));
}

static PyObject *py_body_get_world_vector(PyObject *self, PyObject *args) {
    PyObject *bid_obj; float lx, ly;
    if (!PyArg_ParseTuple(args, "Off", &bid_obj, &lx, &ly)) return NULL;
    b2Vec2 wv = b2Body_GetWorldVector(unpack_body_id(bid_obj), (b2Vec2){pt2m(lx), pt2m(ly)});
    return Py_BuildValue("(ff)", m2pt(wv.x), m2pt(wv.y));
}

static PyObject *py_body_set_target_transform(PyObject *self, PyObject *args) {
    PyObject *bid_obj; float x, y, angle, time_step;
    if (!PyArg_ParseTuple(args, "Offff", &bid_obj, &x, &y, &angle, &time_step)) return NULL;
    b2Transform target;
    target.p = (b2Vec2){pt2m(x), pt2m(y)};
    target.q = b2MakeRot(angle);
    b2Body_SetTargetTransform(unpack_body_id(bid_obj), target, time_step, true);
    Py_RETURN_NONE;
}

// ────────────────────────────── Body Mass ────────────────────────────────

static PyObject *py_body_get_inertia(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    return PyFloat_FromDouble(b2Body_GetRotationalInertia(unpack_body_id(bid_obj)));
}

static PyObject *py_body_get_center_of_mass(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    b2Vec2 c = b2Body_GetWorldCenterOfMass(unpack_body_id(bid_obj));
    return Py_BuildValue("(ff)", m2pt(c.x), m2pt(c.y));
}

static PyObject *py_body_set_mass_data(PyObject *self, PyObject *args) {
    PyObject *bid_obj; float mass, cx, cy, inertia;
    if (!PyArg_ParseTuple(args, "Offff", &bid_obj, &mass, &cx, &cy, &inertia)) return NULL;
    b2MassData md;
    md.mass = mass;
    md.center = (b2Vec2){pt2m(cx), pt2m(cy)};
    md.rotationalInertia = inertia;
    b2Body_SetMassData(unpack_body_id(bid_obj), md);
    Py_RETURN_NONE;
}

static PyObject *py_body_apply_mass_from_shapes(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    if (!PyArg_ParseTuple(args, "O", &bid_obj)) return NULL;
    b2Body_ApplyMassFromShapes(unpack_body_id(bid_obj));
    Py_RETURN_NONE;
}

// ────────────────────────────── Shape Queries ────────────────────────────

static PyObject *py_shape_test_point(PyObject *self, PyObject *args) {
    PyObject *sid_obj; float x, y;
    if (!PyArg_ParseTuple(args, "Off", &sid_obj, &x, &y)) return NULL;
    return PyBool_FromLong(b2Shape_TestPoint(unpack_shape_id(sid_obj), (b2Vec2){pt2m(x), pt2m(y)}));
}

static PyObject *py_shape_get_aabb(PyObject *self, PyObject *args) {
    PyObject *sid_obj;
    if (!PyArg_ParseTuple(args, "O", &sid_obj)) return NULL;
    b2AABB aabb = b2Shape_GetAABB(unpack_shape_id(sid_obj));
    return Py_BuildValue("(ffff)", m2pt(aabb.lowerBound.x), m2pt(aabb.lowerBound.y),
                         m2pt(aabb.upperBound.x), m2pt(aabb.upperBound.y));
}

static PyObject *py_shape_get_closest_point(PyObject *self, PyObject *args) {
    PyObject *sid_obj; float x, y;
    if (!PyArg_ParseTuple(args, "Off", &sid_obj, &x, &y)) return NULL;
    b2Vec2 p = b2Shape_GetClosestPoint(unpack_shape_id(sid_obj), (b2Vec2){pt2m(x), pt2m(y)});
    return Py_BuildValue("(ff)", m2pt(p.x), m2pt(p.y));
}

static PyObject *py_shape_get_type(PyObject *self, PyObject *args) {
    PyObject *sid_obj;
    if (!PyArg_ParseTuple(args, "O", &sid_obj)) return NULL;
    return PyLong_FromLong((int)b2Shape_GetType(unpack_shape_id(sid_obj)));
}

static PyObject *py_shape_is_sensor(PyObject *self, PyObject *args) {
    PyObject *sid_obj;
    if (!PyArg_ParseTuple(args, "O", &sid_obj)) return NULL;
    return PyBool_FromLong(b2Shape_IsSensor(unpack_shape_id(sid_obj)));
}

static PyObject *py_shape_get_body(PyObject *self, PyObject *args) {
    PyObject *sid_obj;
    if (!PyArg_ParseTuple(args, "O", &sid_obj)) return NULL;
    b2BodyId bid = b2Shape_GetBody(unpack_shape_id(sid_obj));
    return pack_body_id(bid);
}

// ────────────────────────────── Shape Geometry Access ────────────────────

static PyObject *py_shape_get_circle(PyObject *self, PyObject *args) {
    PyObject *sid_obj;
    if (!PyArg_ParseTuple(args, "O", &sid_obj)) return NULL;
    b2Circle c = b2Shape_GetCircle(unpack_shape_id(sid_obj));
    return Py_BuildValue("(fff)", m2pt(c.center.x), m2pt(c.center.y), m2pt(c.radius));
}

static PyObject *py_shape_get_capsule(PyObject *self, PyObject *args) {
    PyObject *sid_obj;
    if (!PyArg_ParseTuple(args, "O", &sid_obj)) return NULL;
    b2Capsule cap = b2Shape_GetCapsule(unpack_shape_id(sid_obj));
    return Py_BuildValue("(fffff)", m2pt(cap.center1.x), m2pt(cap.center1.y),
                         m2pt(cap.center2.x), m2pt(cap.center2.y), m2pt(cap.radius));
}

static PyObject *py_shape_get_polygon(PyObject *self, PyObject *args) {
    PyObject *sid_obj;
    if (!PyArg_ParseTuple(args, "O", &sid_obj)) return NULL;
    b2Polygon poly = b2Shape_GetPolygon(unpack_shape_id(sid_obj));
    PyObject *flat = PyList_New(poly.count * 2);
    if (!flat) return NULL;
    for (int i = 0; i < poly.count; i++) {
        PyList_SET_ITEM(flat, i * 2, PyFloat_FromDouble(m2pt(poly.vertices[i].x)));
        PyList_SET_ITEM(flat, i * 2 + 1, PyFloat_FromDouble(m2pt(poly.vertices[i].y)));
    }
    return Py_BuildValue("(iN)", poly.count, flat);
}

static PyObject *py_shape_set_circle(PyObject *self, PyObject *args) {
    PyObject *sid_obj; float cx, cy, radius;
    if (!PyArg_ParseTuple(args, "Offf", &sid_obj, &cx, &cy, &radius)) return NULL;
    b2Circle circle = {{pt2m(cx), pt2m(cy)}, pt2m(radius)};
    b2Shape_SetCircle(unpack_shape_id(sid_obj), &circle);
    Py_RETURN_NONE;
}

static PyObject *py_shape_set_capsule(PyObject *self, PyObject *args) {
    PyObject *sid_obj; float x1, y1, x2, y2, radius;
    if (!PyArg_ParseTuple(args, "Offfff", &sid_obj, &x1, &y1, &x2, &y2, &radius)) return NULL;
    b2Capsule cap = {{pt2m(x1), pt2m(y1)}, {pt2m(x2), pt2m(y2)}, pt2m(radius)};
    b2Shape_SetCapsule(unpack_shape_id(sid_obj), &cap);
    Py_RETURN_NONE;
}

static PyObject *py_shape_set_polygon(PyObject *self, PyObject *args) {
    PyObject *sid_obj, *pts_obj;
    if (!PyArg_ParseTuple(args, "OO", &sid_obj, &pts_obj)) return NULL;
    PyObject *seq = PySequence_Fast(pts_obj, "points must be a sequence");
    if (!seq) return NULL;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(seq);
    if (n < 6 || n > 16 || (n & 1)) {
        Py_DECREF(seq);
        PyErr_SetString(PyExc_ValueError, "need 3-8 vertices (6-16 floats)");
        return NULL;
    }
    int vc = (int)(n / 2);
    b2Vec2 verts[8];
    for (int i = 0; i < vc; i++) {
        float vx = (float)PyFloat_AsDouble(PySequence_Fast_GET_ITEM(seq, i * 2));
        float vy = (float)PyFloat_AsDouble(PySequence_Fast_GET_ITEM(seq, i * 2 + 1));
        verts[i] = (b2Vec2){pt2m(vx), pt2m(vy)};
    }
    Py_DECREF(seq);
    b2Hull hull = b2ComputeHull(verts, vc);
    if (hull.count == 0) {
        PyErr_SetString(PyExc_ValueError, "degenerate polygon");
        return NULL;
    }
    b2Polygon poly = b2MakePolygon(&hull, 0.0f);
    b2Shape_SetPolygon(unpack_shape_id(sid_obj), &poly);
    Py_RETURN_NONE;
}

// ────────────────────────────── Segment Shape ────────────────────────────

static PyObject *py_add_segment_shape(PyObject *self, PyObject *args) {
    PyObject *bid_obj;
    float x1, y1, x2, y2, density, friction, restitution;
    unsigned long long category, mask;
    int is_sensor, enable_contacts;
    if (!PyArg_ParseTuple(args, "OfffffffKKii", &bid_obj, &x1, &y1, &x2, &y2,
                          &density, &friction, &restitution,
                          &category, &mask, &is_sensor, &enable_contacts))
        return NULL;

    b2BodyId bid = unpack_body_id(bid_obj);
    b2ShapeDef sdef = b2DefaultShapeDef();
    setup_shape_def(&sdef, density, friction, restitution, category, mask,
                    is_sensor, enable_contacts);

    b2Segment seg = {{pt2m(x1), pt2m(y1)}, {pt2m(x2), pt2m(y2)}};
    b2ShapeId sid = b2CreateSegmentShape(bid, &sdef, &seg);
    return pack_shape_id(sid);
}

// ────────────────────────────── Chain Shape ──────────────────────────────

static PyObject *py_create_chain(PyObject *self, PyObject *args) {
    PyObject *bid_obj, *pts_obj;
    int is_loop;
    float friction, restitution;
    unsigned long long category, mask;
    if (!PyArg_ParseTuple(args, "OOiffKK", &bid_obj, &pts_obj, &is_loop,
                          &friction, &restitution, &category, &mask))
        return NULL;

    PyObject *seq = PySequence_Fast(pts_obj, "points must be a sequence");
    if (!seq) return NULL;
    Py_ssize_t n = PySequence_Fast_GET_SIZE(seq);
    if (n < 8 || (n & 1)) {
        Py_DECREF(seq);
        PyErr_SetString(PyExc_ValueError, "need at least 4 points (8 floats)");
        return NULL;
    }
    int pc = (int)(n / 2);

    // Use stack for small arrays, malloc for large
    b2Vec2 stack_pts[256];
    b2Vec2 *pts = (pc <= 256) ? stack_pts : (b2Vec2 *)malloc(sizeof(b2Vec2) * pc);
    if (!pts) {
        Py_DECREF(seq);
        PyErr_NoMemory();
        return NULL;
    }
    for (int i = 0; i < pc; i++) {
        float px = (float)PyFloat_AsDouble(PySequence_Fast_GET_ITEM(seq, i * 2));
        float py = (float)PyFloat_AsDouble(PySequence_Fast_GET_ITEM(seq, i * 2 + 1));
        pts[i] = (b2Vec2){pt2m(px), pt2m(py)};
    }
    Py_DECREF(seq);

    b2BodyId bid = unpack_body_id(bid_obj);
    b2ChainDef def = b2DefaultChainDef();
    def.points = pts;
    def.count = pc;
    def.isLoop = (bool)is_loop;
    b2SurfaceMaterial mat = b2DefaultSurfaceMaterial();
    mat.friction = friction;
    mat.restitution = restitution;
    def.materials = &mat;
    def.materialCount = 1;
    def.filter.categoryBits = category;
    def.filter.maskBits = mask;
    def.enableSensorEvents = true;

    b2ChainId cid = b2CreateChain(bid, &def);
    if (pts != stack_pts) free(pts);
    return pack_chain_id(cid);
}

static PyObject *py_destroy_chain(PyObject *self, PyObject *args) {
    PyObject *cid_obj;
    if (!PyArg_ParseTuple(args, "O", &cid_obj)) return NULL;
    b2ChainId cid = unpack_chain_id(cid_obj);
    if (b2Chain_IsValid(cid)) {
        b2DestroyChain(cid);
    }
    Py_RETURN_NONE;
}

// ────────────────────────────── Motor Joint ──────────────────────────────

static PyObject *py_create_motor_joint(PyObject *self, PyObject *args) {
    PyObject *wid_obj, *ba_obj, *bb_obj;
    float lvx, lvy, av, max_vf, max_vt;
    if (!PyArg_ParseTuple(args, "OOOfffff", &wid_obj, &ba_obj, &bb_obj,
                          &lvx, &lvy, &av, &max_vf, &max_vt))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2BodyId ba = unpack_body_id(ba_obj);
    b2BodyId bb = unpack_body_id(bb_obj);

    b2MotorJointDef def = b2DefaultMotorJointDef();
    def.base.bodyIdA = ba;
    def.base.bodyIdB = bb;
    def.linearVelocity = (b2Vec2){pt2m(lvx), pt2m(lvy)};
    def.angularVelocity = av;
    def.maxVelocityForce = max_vf / kPTM;
    def.maxVelocityTorque = max_vt / (kPTM * kPTM);

    b2JointId jid = b2CreateMotorJoint(wid, &def);
    return pack_joint_id(jid);
}

// ────────────────────────────── Filter Joint ─────────────────────────────

static PyObject *py_create_filter_joint(PyObject *self, PyObject *args) {
    PyObject *wid_obj, *ba_obj, *bb_obj;
    if (!PyArg_ParseTuple(args, "OOO", &wid_obj, &ba_obj, &bb_obj))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2BodyId ba = unpack_body_id(ba_obj);
    b2BodyId bb = unpack_body_id(bb_obj);

    b2FilterJointDef def = b2DefaultFilterJointDef();
    def.base.bodyIdA = ba;
    def.base.bodyIdB = bb;

    b2JointId jid = b2CreateFilterJoint(wid, &def);
    return pack_joint_id(jid);
}

// ────────────────────────────── Joint Base Queries ───────────────────────

static PyObject *py_joint_is_valid(PyObject *self, PyObject *args) {
    PyObject *j;
    if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return PyBool_FromLong(b2Joint_IsValid(unpack_joint_id(j)));
}

static PyObject *py_joint_get_type(PyObject *self, PyObject *args) {
    PyObject *j;
    if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return PyLong_FromLong((int)b2Joint_GetType(unpack_joint_id(j)));
}

static PyObject *py_joint_get_body_a(PyObject *self, PyObject *args) {
    PyObject *j;
    if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return pack_body_id(b2Joint_GetBodyA(unpack_joint_id(j)));
}

static PyObject *py_joint_get_body_b(PyObject *self, PyObject *args) {
    PyObject *j;
    if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return pack_body_id(b2Joint_GetBodyB(unpack_joint_id(j)));
}

static PyObject *py_joint_get_constraint_force(PyObject *self, PyObject *args) {
    PyObject *j;
    if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    b2Vec2 f = b2Joint_GetConstraintForce(unpack_joint_id(j));
    return Py_BuildValue("(ff)", f.x * kPTM, f.y * kPTM);
}

static PyObject *py_joint_get_constraint_torque(PyObject *self, PyObject *args) {
    PyObject *j;
    if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return PyFloat_FromDouble(b2Joint_GetConstraintTorque(unpack_joint_id(j)) * kPTM * kPTM);
}

static PyObject *py_joint_set_collide_connected(PyObject *self, PyObject *args) {
    PyObject *j; int flag;
    if (!PyArg_ParseTuple(args, "Oi", &j, &flag)) return NULL;
    b2Joint_SetCollideConnected(unpack_joint_id(j), (bool)flag);
    Py_RETURN_NONE;
}

static PyObject *py_joint_get_collide_connected(PyObject *self, PyObject *args) {
    PyObject *j;
    if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return PyBool_FromLong(b2Joint_GetCollideConnected(unpack_joint_id(j)));
}

static PyObject *py_joint_wake_bodies(PyObject *self, PyObject *args) {
    PyObject *j;
    if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    b2Joint_WakeBodies(unpack_joint_id(j));
    Py_RETURN_NONE;
}

// ────────────────────────── Joint Type-Specific Control ──────────────────

#define JFLOAT_GET(name, api) \
static PyObject *py_##name(PyObject *self, PyObject *args) { \
    PyObject *j; if (!PyArg_ParseTuple(args, "O", &j)) return NULL; \
    return PyFloat_FromDouble(api(unpack_joint_id(j))); }

#define JFLOAT_SET(name, api) \
static PyObject *py_##name(PyObject *self, PyObject *args) { \
    PyObject *j; float v; if (!PyArg_ParseTuple(args, "Of", &j, &v)) return NULL; \
    api(unpack_joint_id(j), v); Py_RETURN_NONE; }

#define JBOOL_GET(name, api) \
static PyObject *py_##name(PyObject *self, PyObject *args) { \
    PyObject *j; if (!PyArg_ParseTuple(args, "O", &j)) return NULL; \
    return PyBool_FromLong(api(unpack_joint_id(j))); }

#define JBOOL_SET(name, api) \
static PyObject *py_##name(PyObject *self, PyObject *args) { \
    PyObject *j; int v; if (!PyArg_ParseTuple(args, "Oi", &j, &v)) return NULL; \
    api(unpack_joint_id(j), (bool)v); Py_RETURN_NONE; }

#define JFLOAT_GET_M2PT(name, api) \
static PyObject *py_##name(PyObject *self, PyObject *args) { \
    PyObject *j; if (!PyArg_ParseTuple(args, "O", &j)) return NULL; \
    return PyFloat_FromDouble(m2pt(api(unpack_joint_id(j)))); }

#define JFLOAT_SET_PT2M(name, api) \
static PyObject *py_##name(PyObject *self, PyObject *args) { \
    PyObject *j; float v; if (!PyArg_ParseTuple(args, "Of", &j, &v)) return NULL; \
    api(unpack_joint_id(j), pt2m(v)); Py_RETURN_NONE; }

// ── Revolute Joint ──
JFLOAT_GET(revolute_get_angle, b2RevoluteJoint_GetAngle)
JFLOAT_SET(revolute_set_target_angle, b2RevoluteJoint_SetTargetAngle)
JFLOAT_GET(revolute_get_target_angle, b2RevoluteJoint_GetTargetAngle)
JBOOL_SET(revolute_enable_spring, b2RevoluteJoint_EnableSpring)
JFLOAT_SET(revolute_set_spring_hertz, b2RevoluteJoint_SetSpringHertz)
JFLOAT_GET(revolute_get_spring_hertz, b2RevoluteJoint_GetSpringHertz)
JFLOAT_SET(revolute_set_spring_damping, b2RevoluteJoint_SetSpringDampingRatio)
JFLOAT_GET(revolute_get_spring_damping, b2RevoluteJoint_GetSpringDampingRatio)
JBOOL_SET(revolute_enable_limit, b2RevoluteJoint_EnableLimit)
JBOOL_GET(revolute_is_limit_enabled, b2RevoluteJoint_IsLimitEnabled)
JFLOAT_GET(revolute_get_lower_limit, b2RevoluteJoint_GetLowerLimit)
JFLOAT_GET(revolute_get_upper_limit, b2RevoluteJoint_GetUpperLimit)
JBOOL_SET(revolute_enable_motor, b2RevoluteJoint_EnableMotor)
JBOOL_GET(revolute_is_motor_enabled, b2RevoluteJoint_IsMotorEnabled)
JFLOAT_SET(revolute_set_motor_speed, b2RevoluteJoint_SetMotorSpeed)
JFLOAT_GET(revolute_get_motor_speed, b2RevoluteJoint_GetMotorSpeed)

// revolute_get_motor_torque: scale by kPTM^2
static PyObject *py_revolute_get_motor_torque(PyObject *self, PyObject *args) {
    PyObject *j; if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return PyFloat_FromDouble(b2RevoluteJoint_GetMotorTorque(unpack_joint_id(j)) * kPTM * kPTM);
}

static PyObject *py_revolute_set_max_motor_torque(PyObject *self, PyObject *args) {
    PyObject *j; float v;
    if (!PyArg_ParseTuple(args, "Of", &j, &v)) return NULL;
    b2RevoluteJoint_SetMaxMotorTorque(unpack_joint_id(j), v / (kPTM * kPTM));
    Py_RETURN_NONE;
}

static PyObject *py_revolute_get_max_motor_torque(PyObject *self, PyObject *args) {
    PyObject *j; if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return PyFloat_FromDouble(b2RevoluteJoint_GetMaxMotorTorque(unpack_joint_id(j)) * kPTM * kPTM);
}

static PyObject *py_revolute_set_limits(PyObject *self, PyObject *args) {
    PyObject *j; float lo, hi;
    if (!PyArg_ParseTuple(args, "Off", &j, &lo, &hi)) return NULL;
    b2RevoluteJoint_SetLimits(unpack_joint_id(j), lo, hi);
    Py_RETURN_NONE;
}

// ── Distance Joint ──
JFLOAT_SET_PT2M(distance_set_length, b2DistanceJoint_SetLength)
JFLOAT_GET_M2PT(distance_get_length, b2DistanceJoint_GetLength)
JBOOL_SET(distance_enable_spring, b2DistanceJoint_EnableSpring)
JFLOAT_SET(distance_set_spring_hertz, b2DistanceJoint_SetSpringHertz)
JFLOAT_GET(distance_get_spring_hertz, b2DistanceJoint_GetSpringHertz)
JFLOAT_SET(distance_set_spring_damping, b2DistanceJoint_SetSpringDampingRatio)
JFLOAT_GET(distance_get_spring_damping, b2DistanceJoint_GetSpringDampingRatio)
JBOOL_SET(distance_enable_limit, b2DistanceJoint_EnableLimit)
JBOOL_GET(distance_is_limit_enabled, b2DistanceJoint_IsLimitEnabled)
JFLOAT_GET_M2PT(distance_get_min_length, b2DistanceJoint_GetMinLength)
JFLOAT_GET_M2PT(distance_get_max_length, b2DistanceJoint_GetMaxLength)
JBOOL_SET(distance_enable_motor, b2DistanceJoint_EnableMotor)
JBOOL_GET(distance_is_motor_enabled, b2DistanceJoint_IsMotorEnabled)
JFLOAT_SET_PT2M(distance_set_motor_speed, b2DistanceJoint_SetMotorSpeed)
JFLOAT_GET_M2PT(distance_get_motor_speed, b2DistanceJoint_GetMotorSpeed)

static PyObject *py_distance_set_length_range(PyObject *self, PyObject *args) {
    PyObject *j; float mn, mx;
    if (!PyArg_ParseTuple(args, "Off", &j, &mn, &mx)) return NULL;
    b2DistanceJoint_SetLengthRange(unpack_joint_id(j), pt2m(mn), pt2m(mx));
    Py_RETURN_NONE;
}

static PyObject *py_distance_set_max_motor_force(PyObject *self, PyObject *args) {
    PyObject *j; float v;
    if (!PyArg_ParseTuple(args, "Of", &j, &v)) return NULL;
    b2DistanceJoint_SetMaxMotorForce(unpack_joint_id(j), v / kPTM);
    Py_RETURN_NONE;
}

static PyObject *py_distance_get_max_motor_force(PyObject *self, PyObject *args) {
    PyObject *j; if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return PyFloat_FromDouble(b2DistanceJoint_GetMaxMotorForce(unpack_joint_id(j)) * kPTM);
}

// ── Prismatic Joint ──
JBOOL_SET(prismatic_enable_spring, b2PrismaticJoint_EnableSpring)
JFLOAT_SET(prismatic_set_spring_hertz, b2PrismaticJoint_SetSpringHertz)
JFLOAT_GET(prismatic_get_spring_hertz, b2PrismaticJoint_GetSpringHertz)
JFLOAT_SET(prismatic_set_spring_damping, b2PrismaticJoint_SetSpringDampingRatio)
JFLOAT_GET(prismatic_get_spring_damping, b2PrismaticJoint_GetSpringDampingRatio)
JBOOL_SET(prismatic_enable_limit, b2PrismaticJoint_EnableLimit)
JBOOL_GET(prismatic_is_limit_enabled, b2PrismaticJoint_IsLimitEnabled)
JFLOAT_GET_M2PT(prismatic_get_lower_limit, b2PrismaticJoint_GetLowerLimit)
JFLOAT_GET_M2PT(prismatic_get_upper_limit, b2PrismaticJoint_GetUpperLimit)
JBOOL_SET(prismatic_enable_motor, b2PrismaticJoint_EnableMotor)
JBOOL_GET(prismatic_is_motor_enabled, b2PrismaticJoint_IsMotorEnabled)
JFLOAT_SET_PT2M(prismatic_set_motor_speed, b2PrismaticJoint_SetMotorSpeed)
JFLOAT_GET_M2PT(prismatic_get_motor_speed, b2PrismaticJoint_GetMotorSpeed)
JFLOAT_GET_M2PT(prismatic_get_translation, b2PrismaticJoint_GetTranslation)
JFLOAT_GET_M2PT(prismatic_get_speed, b2PrismaticJoint_GetSpeed)

static PyObject *py_prismatic_set_limits(PyObject *self, PyObject *args) {
    PyObject *j; float lo, hi;
    if (!PyArg_ParseTuple(args, "Off", &j, &lo, &hi)) return NULL;
    b2PrismaticJoint_SetLimits(unpack_joint_id(j), pt2m(lo), pt2m(hi));
    Py_RETURN_NONE;
}

static PyObject *py_prismatic_set_max_motor_force(PyObject *self, PyObject *args) {
    PyObject *j; float v;
    if (!PyArg_ParseTuple(args, "Of", &j, &v)) return NULL;
    b2PrismaticJoint_SetMaxMotorForce(unpack_joint_id(j), v / kPTM);
    Py_RETURN_NONE;
}

static PyObject *py_prismatic_get_max_motor_force(PyObject *self, PyObject *args) {
    PyObject *j; if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return PyFloat_FromDouble(b2PrismaticJoint_GetMaxMotorForce(unpack_joint_id(j)) * kPTM);
}

// ── Weld Joint ──
JFLOAT_SET(weld_set_linear_hertz, b2WeldJoint_SetLinearHertz)
JFLOAT_GET(weld_get_linear_hertz, b2WeldJoint_GetLinearHertz)
JFLOAT_SET(weld_set_linear_damping, b2WeldJoint_SetLinearDampingRatio)
JFLOAT_GET(weld_get_linear_damping, b2WeldJoint_GetLinearDampingRatio)
JFLOAT_SET(weld_set_angular_hertz, b2WeldJoint_SetAngularHertz)
JFLOAT_GET(weld_get_angular_hertz, b2WeldJoint_GetAngularHertz)
JFLOAT_SET(weld_set_angular_damping, b2WeldJoint_SetAngularDampingRatio)
JFLOAT_GET(weld_get_angular_damping, b2WeldJoint_GetAngularDampingRatio)

// ── Wheel Joint ──
JBOOL_SET(wheel_enable_spring, b2WheelJoint_EnableSpring)
JFLOAT_SET(wheel_set_spring_hertz, b2WheelJoint_SetSpringHertz)
JFLOAT_GET(wheel_get_spring_hertz, b2WheelJoint_GetSpringHertz)
JFLOAT_SET(wheel_set_spring_damping, b2WheelJoint_SetSpringDampingRatio)
JFLOAT_GET(wheel_get_spring_damping, b2WheelJoint_GetSpringDampingRatio)
JBOOL_SET(wheel_enable_limit, b2WheelJoint_EnableLimit)
JBOOL_GET(wheel_is_limit_enabled, b2WheelJoint_IsLimitEnabled)
JFLOAT_GET_M2PT(wheel_get_lower_limit, b2WheelJoint_GetLowerLimit)
JFLOAT_GET_M2PT(wheel_get_upper_limit, b2WheelJoint_GetUpperLimit)
JBOOL_SET(wheel_enable_motor, b2WheelJoint_EnableMotor)
JBOOL_GET(wheel_is_motor_enabled, b2WheelJoint_IsMotorEnabled)
JFLOAT_SET(wheel_set_motor_speed, b2WheelJoint_SetMotorSpeed)
JFLOAT_GET(wheel_get_motor_speed, b2WheelJoint_GetMotorSpeed)

static PyObject *py_wheel_set_limits(PyObject *self, PyObject *args) {
    PyObject *j; float lo, hi;
    if (!PyArg_ParseTuple(args, "Off", &j, &lo, &hi)) return NULL;
    b2WheelJoint_SetLimits(unpack_joint_id(j), pt2m(lo), pt2m(hi));
    Py_RETURN_NONE;
}

static PyObject *py_wheel_set_max_motor_torque(PyObject *self, PyObject *args) {
    PyObject *j; float v;
    if (!PyArg_ParseTuple(args, "Of", &j, &v)) return NULL;
    b2WheelJoint_SetMaxMotorTorque(unpack_joint_id(j), v / (kPTM * kPTM));
    Py_RETURN_NONE;
}

static PyObject *py_wheel_get_max_motor_torque(PyObject *self, PyObject *args) {
    PyObject *j; if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return PyFloat_FromDouble(b2WheelJoint_GetMaxMotorTorque(unpack_joint_id(j)) * kPTM * kPTM);
}

// ── Motor Joint runtime control ──
static PyObject *py_motor_joint_set_linear_velocity(PyObject *self, PyObject *args) {
    PyObject *j; float vx, vy;
    if (!PyArg_ParseTuple(args, "Off", &j, &vx, &vy)) return NULL;
    b2MotorJoint_SetLinearVelocity(unpack_joint_id(j), (b2Vec2){pt2m(vx), pt2m(vy)});
    Py_RETURN_NONE;
}

static PyObject *py_motor_joint_get_linear_velocity(PyObject *self, PyObject *args) {
    PyObject *j; if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    b2Vec2 v = b2MotorJoint_GetLinearVelocity(unpack_joint_id(j));
    return Py_BuildValue("(ff)", m2pt(v.x), m2pt(v.y));
}

static PyObject *py_motor_joint_set_angular_velocity(PyObject *self, PyObject *args) {
    PyObject *j; float v;
    if (!PyArg_ParseTuple(args, "Of", &j, &v)) return NULL;
    b2MotorJoint_SetAngularVelocity(unpack_joint_id(j), v);
    Py_RETURN_NONE;
}

static PyObject *py_motor_joint_get_angular_velocity(PyObject *self, PyObject *args) {
    PyObject *j; if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return PyFloat_FromDouble(b2MotorJoint_GetAngularVelocity(unpack_joint_id(j)));
}

static PyObject *py_motor_joint_set_max_force(PyObject *self, PyObject *args) {
    PyObject *j; float v;
    if (!PyArg_ParseTuple(args, "Of", &j, &v)) return NULL;
    b2MotorJoint_SetMaxVelocityForce(unpack_joint_id(j), v / kPTM);
    Py_RETURN_NONE;
}

static PyObject *py_motor_joint_get_max_force(PyObject *self, PyObject *args) {
    PyObject *j; if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return PyFloat_FromDouble(b2MotorJoint_GetMaxVelocityForce(unpack_joint_id(j)) * kPTM);
}

static PyObject *py_motor_joint_set_max_torque(PyObject *self, PyObject *args) {
    PyObject *j; float v;
    if (!PyArg_ParseTuple(args, "Of", &j, &v)) return NULL;
    b2MotorJoint_SetMaxVelocityTorque(unpack_joint_id(j), v / (kPTM * kPTM));
    Py_RETURN_NONE;
}

static PyObject *py_motor_joint_get_max_torque(PyObject *self, PyObject *args) {
    PyObject *j; if (!PyArg_ParseTuple(args, "O", &j)) return NULL;
    return PyFloat_FromDouble(b2MotorJoint_GetMaxVelocityTorque(unpack_joint_id(j)) * kPTM * kPTM);
}

// ────────────────────────────── Events ───────────────────────────────────

static PyObject *py_get_sensor_events(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    if (!PyArg_ParseTuple(args, "O", &wid_obj)) return NULL;
    b2WorldId wid = unpack_world_id(wid_obj);

    b2SensorEvents events = b2World_GetSensorEvents(wid);
    PyObject *list = PyList_New(0);
    if (!list) return NULL;

    for (int i = 0; i < events.beginCount; i++) {
        b2SensorBeginTouchEvent *e = &events.beginEvents[i];
        if (!b2Shape_IsValid(e->sensorShapeId) || !b2Shape_IsValid(e->visitorShapeId)) continue;
        b2BodyId sb = b2Shape_GetBody(e->sensorShapeId);
        b2BodyId vb = b2Shape_GetBody(e->visitorShapeId);
        PyObject *t = Py_BuildValue("(NNi)", pack_body_id(sb), pack_body_id(vb), 0);
        PyList_Append(list, t);
        Py_DECREF(t);
    }
    for (int i = 0; i < events.endCount; i++) {
        b2SensorEndTouchEvent *e = &events.endEvents[i];
        if (!b2Shape_IsValid(e->sensorShapeId) || !b2Shape_IsValid(e->visitorShapeId)) continue;
        b2BodyId sb = b2Shape_GetBody(e->sensorShapeId);
        b2BodyId vb = b2Shape_GetBody(e->visitorShapeId);
        PyObject *t = Py_BuildValue("(NNi)", pack_body_id(sb), pack_body_id(vb), 1);
        PyList_Append(list, t);
        Py_DECREF(t);
    }
    return list;
}

static PyObject *py_get_body_move_events(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    if (!PyArg_ParseTuple(args, "O", &wid_obj)) return NULL;
    b2WorldId wid = unpack_world_id(wid_obj);

    b2BodyEvents events = b2World_GetBodyEvents(wid);
    PyObject *list = PyList_New(events.moveCount);
    if (!list) return NULL;

    for (int i = 0; i < events.moveCount; i++) {
        b2BodyMoveEvent *e = &events.moveEvents[i];
        float angle = b2Rot_GetAngle(e->transform.q);
        PyObject *t = Py_BuildValue("(NfffO)",
            pack_body_id(e->bodyId),
            m2pt(e->transform.p.x), m2pt(e->transform.p.y), angle,
            e->fellAsleep ? Py_True : Py_False);
        PyList_SET_ITEM(list, i, t);
    }
    return list;
}

static PyObject *py_get_joint_events(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    if (!PyArg_ParseTuple(args, "O", &wid_obj)) return NULL;
    b2WorldId wid = unpack_world_id(wid_obj);

    b2JointEvents events = b2World_GetJointEvents(wid);
    PyObject *list = PyList_New(events.count);
    if (!list) return NULL;

    for (int i = 0; i < events.count; i++) {
        PyList_SET_ITEM(list, i, pack_joint_id(events.jointEvents[i].jointId));
    }
    return list;
}

// ────────────────────────────── World Queries ────────────────────────────

// ── overlap_aabb ──
typedef struct { PyObject *list; } OverlapCtx;

static bool overlap_aabb_cb(b2ShapeId shapeId, void *ctx) {
    if (!b2Shape_IsValid(shapeId)) return true;
    OverlapCtx *c = (OverlapCtx *)ctx;
    b2BodyId bid = b2Shape_GetBody(shapeId);
    PyObject *t = pack_body_id(bid);
    PyList_Append(c->list, t);
    Py_DECREF(t);
    return true;
}

static PyObject *py_overlap_aabb(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    float min_x, min_y, max_x, max_y;
    unsigned long long category, mask;
    if (!PyArg_ParseTuple(args, "OffffKK", &wid_obj, &min_x, &min_y, &max_x, &max_y,
                          &category, &mask))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2AABB aabb;
    aabb.lowerBound = (b2Vec2){pt2m(min_x), pt2m(min_y)};
    aabb.upperBound = (b2Vec2){pt2m(max_x), pt2m(max_y)};
    b2QueryFilter filter = b2DefaultQueryFilter();
    filter.categoryBits = category;
    filter.maskBits = mask;

    OverlapCtx ctx;
    ctx.list = PyList_New(0);
    if (!ctx.list) return NULL;
    b2World_OverlapAABB(wid, aabb, filter, overlap_aabb_cb, &ctx);
    return ctx.list;
}

// ── overlap_circle ──
static PyObject *py_overlap_circle(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    float cx, cy, radius;
    unsigned long long category, mask;
    if (!PyArg_ParseTuple(args, "OfffKK", &wid_obj, &cx, &cy, &radius,
                          &category, &mask))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2Vec2 center = {pt2m(cx), pt2m(cy)};
    b2ShapeProxy proxy = b2MakeProxy(&center, 1, pt2m(radius));
    b2QueryFilter filter = b2DefaultQueryFilter();
    filter.categoryBits = category;
    filter.maskBits = mask;

    OverlapCtx ctx;
    ctx.list = PyList_New(0);
    if (!ctx.list) return NULL;
    b2World_OverlapShape(wid, &proxy, filter, overlap_aabb_cb, &ctx);
    return ctx.list;
}

// ── cast_ray_all ──
typedef struct { PyObject *list; } RayCastCtx;

static float ray_cast_all_cb(b2ShapeId shapeId, b2Vec2 point, b2Vec2 normal, float fraction, void *ctx) {
    if (!b2Shape_IsValid(shapeId)) return 1.0f;
    RayCastCtx *c = (RayCastCtx *)ctx;
    b2BodyId bid = b2Shape_GetBody(shapeId);
    PyObject *t = Py_BuildValue("(Nfffff)", pack_body_id(bid),
                                m2pt(point.x), m2pt(point.y),
                                normal.x, normal.y, fraction);
    PyList_Append(c->list, t);
    Py_DECREF(t);
    return 1.0f;
}

static PyObject *py_cast_ray_all(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    float ox, oy, dx, dy;
    unsigned long long category, mask;
    if (!PyArg_ParseTuple(args, "OffffKK", &wid_obj, &ox, &oy, &dx, &dy,
                          &category, &mask))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2QueryFilter filter = b2DefaultQueryFilter();
    filter.categoryBits = category;
    filter.maskBits = mask;

    RayCastCtx ctx;
    ctx.list = PyList_New(0);
    if (!ctx.list) return NULL;
    b2World_CastRay(wid, (b2Vec2){pt2m(ox), pt2m(oy)}, (b2Vec2){pt2m(dx), pt2m(dy)},
                    filter, ray_cast_all_cb, &ctx);
    return ctx.list;
}

// ── cast_mover ──
static PyObject *py_cast_mover(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    float x1, y1, x2, y2, radius, tx, ty;
    unsigned long long category, mask;
    if (!PyArg_ParseTuple(args, "OfffffffKK", &wid_obj, &x1, &y1, &x2, &y2, &radius,
                          &tx, &ty, &category, &mask))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2Capsule mover = {{pt2m(x1), pt2m(y1)}, {pt2m(x2), pt2m(y2)}, pt2m(radius)};
    b2Vec2 translation = {pt2m(tx), pt2m(ty)};
    b2QueryFilter filter = b2DefaultQueryFilter();
    filter.categoryBits = category;
    filter.maskBits = mask;

    float fraction = b2World_CastMover(wid, &mover, translation, filter);
    return PyFloat_FromDouble(fraction);
}

// ── collide_mover ──
typedef struct { PyObject *list; } MoverCtx;

static bool mover_collide_cb(b2ShapeId shapeId, const b2PlaneResult *plane, void *ctx) {
    MoverCtx *c = (MoverCtx *)ctx;
    PyObject *t = Py_BuildValue("(fffffO)",
        plane->plane.normal.x, plane->plane.normal.y, m2pt(plane->plane.offset),
        m2pt(plane->point.x), m2pt(plane->point.y),
        plane->hit ? Py_True : Py_False);
    PyList_Append(c->list, t);
    Py_DECREF(t);
    return true;
}

static PyObject *py_collide_mover(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    float x1, y1, x2, y2, radius;
    unsigned long long category, mask;
    if (!PyArg_ParseTuple(args, "OfffffKK", &wid_obj, &x1, &y1, &x2, &y2, &radius,
                          &category, &mask))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2Capsule mover = {{pt2m(x1), pt2m(y1)}, {pt2m(x2), pt2m(y2)}, pt2m(radius)};
    b2QueryFilter filter = b2DefaultQueryFilter();
    filter.categoryBits = category;
    filter.maskBits = mask;

    MoverCtx ctx;
    ctx.list = PyList_New(0);
    if (!ctx.list) return NULL;
    b2World_CollideMover(wid, &mover, filter, mover_collide_cb, &ctx);
    return ctx.list;
}

// ── explode ──
static PyObject *py_explode(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    float x, y, radius, falloff, impulse_per_length;
    if (!PyArg_ParseTuple(args, "Offfff", &wid_obj, &x, &y, &radius, &falloff, &impulse_per_length))
        return NULL;

    b2WorldId wid = unpack_world_id(wid_obj);
    b2ExplosionDef def = b2DefaultExplosionDef();
    def.maskBits = UINT64_MAX;
    def.position = (b2Vec2){pt2m(x), pt2m(y)};
    def.radius = pt2m(radius);
    def.falloff = pt2m(falloff);
    def.impulsePerLength = impulse_per_length / kPTM;
    b2World_Explode(wid, &def);
    Py_RETURN_NONE;
}

// ────────────────────────────── World Tuning ─────────────────────────────

static PyObject *py_world_enable_sleeping(PyObject *self, PyObject *args) {
    PyObject *wid_obj; int flag;
    if (!PyArg_ParseTuple(args, "Oi", &wid_obj, &flag)) return NULL;
    b2World_EnableSleeping(unpack_world_id(wid_obj), (bool)flag);
    Py_RETURN_NONE;
}

static PyObject *py_world_is_sleeping_enabled(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    if (!PyArg_ParseTuple(args, "O", &wid_obj)) return NULL;
    return PyBool_FromLong(b2World_IsSleepingEnabled(unpack_world_id(wid_obj)));
}

static PyObject *py_world_enable_continuous(PyObject *self, PyObject *args) {
    PyObject *wid_obj; int flag;
    if (!PyArg_ParseTuple(args, "Oi", &wid_obj, &flag)) return NULL;
    b2World_EnableContinuous(unpack_world_id(wid_obj), (bool)flag);
    Py_RETURN_NONE;
}

static PyObject *py_world_is_continuous_enabled(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    if (!PyArg_ParseTuple(args, "O", &wid_obj)) return NULL;
    return PyBool_FromLong(b2World_IsContinuousEnabled(unpack_world_id(wid_obj)));
}

static PyObject *py_world_set_restitution_threshold(PyObject *self, PyObject *args) {
    PyObject *wid_obj; float v;
    if (!PyArg_ParseTuple(args, "Of", &wid_obj, &v)) return NULL;
    b2World_SetRestitutionThreshold(unpack_world_id(wid_obj), pt2m(v));
    Py_RETURN_NONE;
}

static PyObject *py_world_get_restitution_threshold(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    if (!PyArg_ParseTuple(args, "O", &wid_obj)) return NULL;
    return PyFloat_FromDouble(m2pt(b2World_GetRestitutionThreshold(unpack_world_id(wid_obj))));
}

static PyObject *py_world_set_hit_event_threshold(PyObject *self, PyObject *args) {
    PyObject *wid_obj; float v;
    if (!PyArg_ParseTuple(args, "Of", &wid_obj, &v)) return NULL;
    b2World_SetHitEventThreshold(unpack_world_id(wid_obj), pt2m(v));
    Py_RETURN_NONE;
}

static PyObject *py_world_get_hit_event_threshold(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    if (!PyArg_ParseTuple(args, "O", &wid_obj)) return NULL;
    return PyFloat_FromDouble(m2pt(b2World_GetHitEventThreshold(unpack_world_id(wid_obj))));
}

static PyObject *py_world_set_max_linear_speed(PyObject *self, PyObject *args) {
    PyObject *wid_obj; float v;
    if (!PyArg_ParseTuple(args, "Of", &wid_obj, &v)) return NULL;
    b2World_SetMaximumLinearSpeed(unpack_world_id(wid_obj), pt2m(v));
    Py_RETURN_NONE;
}

static PyObject *py_world_get_max_linear_speed(PyObject *self, PyObject *args) {
    PyObject *wid_obj;
    if (!PyArg_ParseTuple(args, "O", &wid_obj)) return NULL;
    return PyFloat_FromDouble(m2pt(b2World_GetMaximumLinearSpeed(unpack_world_id(wid_obj))));
}

// ────────────────────────────── Module definition ─────────────────────────

static PyMethodDef physics_methods[] = {
    // World
    {"create_world",         py_create_world,         METH_VARARGS, NULL},
    {"destroy_world",        py_destroy_world,        METH_VARARGS, NULL},
    {"world_step",           py_world_step,           METH_VARARGS, NULL},
    {"world_set_gravity",    py_world_set_gravity,    METH_VARARGS, NULL},
    {"world_get_gravity",    py_world_get_gravity,    METH_VARARGS, NULL},
    // Body
    {"create_body",          py_create_body,          METH_VARARGS, NULL},
    {"destroy_body",         py_destroy_body,         METH_VARARGS, NULL},
    {"body_get_transform",   py_body_get_transform,   METH_VARARGS, NULL},
    {"body_set_transform",   py_body_set_transform,   METH_VARARGS, NULL},
    {"body_get_velocity",    py_body_get_velocity,    METH_VARARGS, NULL},
    {"body_set_velocity",    py_body_set_velocity,    METH_VARARGS, NULL},
    {"body_apply_force",     py_body_apply_force,     METH_VARARGS, NULL},
    {"body_apply_force_center", py_body_apply_force_center, METH_VARARGS, NULL},
    {"body_apply_impulse",   py_body_apply_impulse,   METH_VARARGS, NULL},
    {"body_apply_impulse_center", py_body_apply_impulse_center, METH_VARARGS, NULL},
    {"body_apply_torque",    py_body_apply_torque,    METH_VARARGS, NULL},
    {"body_get_mass",        py_body_get_mass,        METH_VARARGS, NULL},
    {"body_set_type",        py_body_set_type,        METH_VARARGS, NULL},
    {"body_set_linear_damping",  py_body_set_linear_damping,  METH_VARARGS, NULL},
    {"body_set_angular_damping", py_body_set_angular_damping, METH_VARARGS, NULL},
    {"body_set_gravity_scale",   py_body_set_gravity_scale,   METH_VARARGS, NULL},
    // Body State Control
    {"body_is_valid",        py_body_is_valid,        METH_VARARGS, NULL},
    {"body_is_enabled",      py_body_is_enabled,      METH_VARARGS, NULL},
    {"body_enable",          py_body_enable,          METH_VARARGS, NULL},
    {"body_disable",         py_body_disable,         METH_VARARGS, NULL},
    {"body_is_awake",        py_body_is_awake,        METH_VARARGS, NULL},
    {"body_set_awake",       py_body_set_awake,       METH_VARARGS, NULL},
    {"body_enable_sleep",    py_body_enable_sleep,    METH_VARARGS, NULL},
    {"body_is_sleep_enabled", py_body_is_sleep_enabled, METH_VARARGS, NULL},
    {"body_set_bullet",      py_body_set_bullet,      METH_VARARGS, NULL},
    {"body_is_bullet",       py_body_is_bullet,       METH_VARARGS, NULL},
    {"body_set_motion_locks", py_body_set_motion_locks, METH_VARARGS, NULL},
    {"body_get_motion_locks", py_body_get_motion_locks, METH_VARARGS, NULL},
    // Body Coordinates
    {"body_get_local_point", py_body_get_local_point, METH_VARARGS, NULL},
    {"body_get_world_point", py_body_get_world_point, METH_VARARGS, NULL},
    {"body_get_local_vector", py_body_get_local_vector, METH_VARARGS, NULL},
    {"body_get_world_vector", py_body_get_world_vector, METH_VARARGS, NULL},
    {"body_set_target_transform", py_body_set_target_transform, METH_VARARGS, NULL},
    // Body Mass
    {"body_get_inertia",     py_body_get_inertia,     METH_VARARGS, NULL},
    {"body_get_center_of_mass", py_body_get_center_of_mass, METH_VARARGS, NULL},
    {"body_set_mass_data",   py_body_set_mass_data,   METH_VARARGS, NULL},
    {"body_apply_mass_from_shapes", py_body_apply_mass_from_shapes, METH_VARARGS, NULL},
    // Shape
    {"add_circle_shape",     py_add_circle_shape,     METH_VARARGS, NULL},
    {"add_box_shape",        py_add_box_shape,        METH_VARARGS, NULL},
    {"add_polygon_shape",    py_add_polygon_shape,    METH_VARARGS, NULL},
    {"add_capsule_shape",    py_add_capsule_shape,    METH_VARARGS, NULL},
    {"add_segment_shape",    py_add_segment_shape,    METH_VARARGS, NULL},
    {"destroy_shape",        py_destroy_shape,        METH_VARARGS, NULL},
    {"shape_set_filter",     py_shape_set_filter,     METH_VARARGS, NULL},
    {"shape_set_density",    py_shape_set_density,    METH_VARARGS, NULL},
    {"shape_set_friction",   py_shape_set_friction,   METH_VARARGS, NULL},
    {"shape_set_restitution", py_shape_set_restitution, METH_VARARGS, NULL},
    // Shape Queries
    {"shape_test_point",     py_shape_test_point,     METH_VARARGS, NULL},
    {"shape_get_aabb",       py_shape_get_aabb,       METH_VARARGS, NULL},
    {"shape_get_closest_point", py_shape_get_closest_point, METH_VARARGS, NULL},
    {"shape_get_type",       py_shape_get_type,       METH_VARARGS, NULL},
    {"shape_is_sensor",      py_shape_is_sensor,      METH_VARARGS, NULL},
    {"shape_get_body",       py_shape_get_body,       METH_VARARGS, NULL},
    // Shape Geometry Access
    {"shape_get_circle",     py_shape_get_circle,     METH_VARARGS, NULL},
    {"shape_get_capsule",    py_shape_get_capsule,    METH_VARARGS, NULL},
    {"shape_get_polygon",    py_shape_get_polygon,    METH_VARARGS, NULL},
    {"shape_set_circle",     py_shape_set_circle,     METH_VARARGS, NULL},
    {"shape_set_capsule",    py_shape_set_capsule,    METH_VARARGS, NULL},
    {"shape_set_polygon",    py_shape_set_polygon,    METH_VARARGS, NULL},
    // Chain Shape
    {"create_chain",         py_create_chain,         METH_VARARGS, NULL},
    {"destroy_chain",        py_destroy_chain,        METH_VARARGS, NULL},
    // Joint creation
    {"create_revolute_joint",   py_create_revolute_joint,   METH_VARARGS, NULL},
    {"create_distance_joint",   py_create_distance_joint,   METH_VARARGS, NULL},
    {"create_weld_joint",       py_create_weld_joint,       METH_VARARGS, NULL},
    {"create_prismatic_joint",  py_create_prismatic_joint,  METH_VARARGS, NULL},
    {"create_wheel_joint",      py_create_wheel_joint,      METH_VARARGS, NULL},
    {"create_motor_joint",      py_create_motor_joint,      METH_VARARGS, NULL},
    {"create_filter_joint",     py_create_filter_joint,     METH_VARARGS, NULL},
    {"destroy_joint",           py_destroy_joint,           METH_VARARGS, NULL},
    // Joint Base Queries
    {"joint_is_valid",       py_joint_is_valid,       METH_VARARGS, NULL},
    {"joint_get_type",       py_joint_get_type,       METH_VARARGS, NULL},
    {"joint_get_body_a",     py_joint_get_body_a,     METH_VARARGS, NULL},
    {"joint_get_body_b",     py_joint_get_body_b,     METH_VARARGS, NULL},
    {"joint_get_constraint_force",  py_joint_get_constraint_force,  METH_VARARGS, NULL},
    {"joint_get_constraint_torque", py_joint_get_constraint_torque, METH_VARARGS, NULL},
    {"joint_set_collide_connected", py_joint_set_collide_connected, METH_VARARGS, NULL},
    {"joint_get_collide_connected", py_joint_get_collide_connected, METH_VARARGS, NULL},
    {"joint_wake_bodies",    py_joint_wake_bodies,    METH_VARARGS, NULL},
    // Revolute Joint
    {"revolute_get_angle",   py_revolute_get_angle,   METH_VARARGS, NULL},
    {"revolute_set_target_angle", py_revolute_set_target_angle, METH_VARARGS, NULL},
    {"revolute_get_target_angle", py_revolute_get_target_angle, METH_VARARGS, NULL},
    {"revolute_enable_spring", py_revolute_enable_spring, METH_VARARGS, NULL},
    {"revolute_set_spring_hertz", py_revolute_set_spring_hertz, METH_VARARGS, NULL},
    {"revolute_get_spring_hertz", py_revolute_get_spring_hertz, METH_VARARGS, NULL},
    {"revolute_set_spring_damping", py_revolute_set_spring_damping, METH_VARARGS, NULL},
    {"revolute_get_spring_damping", py_revolute_get_spring_damping, METH_VARARGS, NULL},
    {"revolute_enable_limit", py_revolute_enable_limit, METH_VARARGS, NULL},
    {"revolute_is_limit_enabled", py_revolute_is_limit_enabled, METH_VARARGS, NULL},
    {"revolute_get_lower_limit", py_revolute_get_lower_limit, METH_VARARGS, NULL},
    {"revolute_get_upper_limit", py_revolute_get_upper_limit, METH_VARARGS, NULL},
    {"revolute_set_limits",  py_revolute_set_limits,  METH_VARARGS, NULL},
    {"revolute_enable_motor", py_revolute_enable_motor, METH_VARARGS, NULL},
    {"revolute_is_motor_enabled", py_revolute_is_motor_enabled, METH_VARARGS, NULL},
    {"revolute_set_motor_speed", py_revolute_set_motor_speed, METH_VARARGS, NULL},
    {"revolute_get_motor_speed", py_revolute_get_motor_speed, METH_VARARGS, NULL},
    {"revolute_get_motor_torque", py_revolute_get_motor_torque, METH_VARARGS, NULL},
    {"revolute_set_max_motor_torque", py_revolute_set_max_motor_torque, METH_VARARGS, NULL},
    {"revolute_get_max_motor_torque", py_revolute_get_max_motor_torque, METH_VARARGS, NULL},
    // Distance Joint
    {"distance_set_length",  py_distance_set_length,  METH_VARARGS, NULL},
    {"distance_get_length",  py_distance_get_length,  METH_VARARGS, NULL},
    {"distance_enable_spring", py_distance_enable_spring, METH_VARARGS, NULL},
    {"distance_set_spring_hertz", py_distance_set_spring_hertz, METH_VARARGS, NULL},
    {"distance_get_spring_hertz", py_distance_get_spring_hertz, METH_VARARGS, NULL},
    {"distance_set_spring_damping", py_distance_set_spring_damping, METH_VARARGS, NULL},
    {"distance_get_spring_damping", py_distance_get_spring_damping, METH_VARARGS, NULL},
    {"distance_enable_limit", py_distance_enable_limit, METH_VARARGS, NULL},
    {"distance_is_limit_enabled", py_distance_is_limit_enabled, METH_VARARGS, NULL},
    {"distance_set_length_range", py_distance_set_length_range, METH_VARARGS, NULL},
    {"distance_get_min_length", py_distance_get_min_length, METH_VARARGS, NULL},
    {"distance_get_max_length", py_distance_get_max_length, METH_VARARGS, NULL},
    {"distance_enable_motor", py_distance_enable_motor, METH_VARARGS, NULL},
    {"distance_is_motor_enabled", py_distance_is_motor_enabled, METH_VARARGS, NULL},
    {"distance_set_motor_speed", py_distance_set_motor_speed, METH_VARARGS, NULL},
    {"distance_get_motor_speed", py_distance_get_motor_speed, METH_VARARGS, NULL},
    {"distance_set_max_motor_force", py_distance_set_max_motor_force, METH_VARARGS, NULL},
    {"distance_get_max_motor_force", py_distance_get_max_motor_force, METH_VARARGS, NULL},
    // Prismatic Joint
    {"prismatic_enable_spring", py_prismatic_enable_spring, METH_VARARGS, NULL},
    {"prismatic_set_spring_hertz", py_prismatic_set_spring_hertz, METH_VARARGS, NULL},
    {"prismatic_get_spring_hertz", py_prismatic_get_spring_hertz, METH_VARARGS, NULL},
    {"prismatic_set_spring_damping", py_prismatic_set_spring_damping, METH_VARARGS, NULL},
    {"prismatic_get_spring_damping", py_prismatic_get_spring_damping, METH_VARARGS, NULL},
    {"prismatic_enable_limit", py_prismatic_enable_limit, METH_VARARGS, NULL},
    {"prismatic_is_limit_enabled", py_prismatic_is_limit_enabled, METH_VARARGS, NULL},
    {"prismatic_get_lower_limit", py_prismatic_get_lower_limit, METH_VARARGS, NULL},
    {"prismatic_get_upper_limit", py_prismatic_get_upper_limit, METH_VARARGS, NULL},
    {"prismatic_set_limits", py_prismatic_set_limits, METH_VARARGS, NULL},
    {"prismatic_enable_motor", py_prismatic_enable_motor, METH_VARARGS, NULL},
    {"prismatic_is_motor_enabled", py_prismatic_is_motor_enabled, METH_VARARGS, NULL},
    {"prismatic_set_motor_speed", py_prismatic_set_motor_speed, METH_VARARGS, NULL},
    {"prismatic_get_motor_speed", py_prismatic_get_motor_speed, METH_VARARGS, NULL},
    {"prismatic_set_max_motor_force", py_prismatic_set_max_motor_force, METH_VARARGS, NULL},
    {"prismatic_get_max_motor_force", py_prismatic_get_max_motor_force, METH_VARARGS, NULL},
    {"prismatic_get_translation", py_prismatic_get_translation, METH_VARARGS, NULL},
    {"prismatic_get_speed",  py_prismatic_get_speed,  METH_VARARGS, NULL},
    // Weld Joint
    {"weld_set_linear_hertz", py_weld_set_linear_hertz, METH_VARARGS, NULL},
    {"weld_get_linear_hertz", py_weld_get_linear_hertz, METH_VARARGS, NULL},
    {"weld_set_linear_damping", py_weld_set_linear_damping, METH_VARARGS, NULL},
    {"weld_get_linear_damping", py_weld_get_linear_damping, METH_VARARGS, NULL},
    {"weld_set_angular_hertz", py_weld_set_angular_hertz, METH_VARARGS, NULL},
    {"weld_get_angular_hertz", py_weld_get_angular_hertz, METH_VARARGS, NULL},
    {"weld_set_angular_damping", py_weld_set_angular_damping, METH_VARARGS, NULL},
    {"weld_get_angular_damping", py_weld_get_angular_damping, METH_VARARGS, NULL},
    // Wheel Joint
    {"wheel_enable_spring",  py_wheel_enable_spring,  METH_VARARGS, NULL},
    {"wheel_set_spring_hertz", py_wheel_set_spring_hertz, METH_VARARGS, NULL},
    {"wheel_get_spring_hertz", py_wheel_get_spring_hertz, METH_VARARGS, NULL},
    {"wheel_set_spring_damping", py_wheel_set_spring_damping, METH_VARARGS, NULL},
    {"wheel_get_spring_damping", py_wheel_get_spring_damping, METH_VARARGS, NULL},
    {"wheel_enable_limit",   py_wheel_enable_limit,   METH_VARARGS, NULL},
    {"wheel_is_limit_enabled", py_wheel_is_limit_enabled, METH_VARARGS, NULL},
    {"wheel_get_lower_limit", py_wheel_get_lower_limit, METH_VARARGS, NULL},
    {"wheel_get_upper_limit", py_wheel_get_upper_limit, METH_VARARGS, NULL},
    {"wheel_set_limits",     py_wheel_set_limits,     METH_VARARGS, NULL},
    {"wheel_enable_motor",   py_wheel_enable_motor,   METH_VARARGS, NULL},
    {"wheel_is_motor_enabled", py_wheel_is_motor_enabled, METH_VARARGS, NULL},
    {"wheel_set_motor_speed", py_wheel_set_motor_speed, METH_VARARGS, NULL},
    {"wheel_get_motor_speed", py_wheel_get_motor_speed, METH_VARARGS, NULL},
    {"wheel_set_max_motor_torque", py_wheel_set_max_motor_torque, METH_VARARGS, NULL},
    {"wheel_get_max_motor_torque", py_wheel_get_max_motor_torque, METH_VARARGS, NULL},
    // Motor Joint runtime
    {"motor_joint_set_linear_velocity", py_motor_joint_set_linear_velocity, METH_VARARGS, NULL},
    {"motor_joint_get_linear_velocity", py_motor_joint_get_linear_velocity, METH_VARARGS, NULL},
    {"motor_joint_set_angular_velocity", py_motor_joint_set_angular_velocity, METH_VARARGS, NULL},
    {"motor_joint_get_angular_velocity", py_motor_joint_get_angular_velocity, METH_VARARGS, NULL},
    {"motor_joint_set_max_force", py_motor_joint_set_max_force, METH_VARARGS, NULL},
    {"motor_joint_get_max_force", py_motor_joint_get_max_force, METH_VARARGS, NULL},
    {"motor_joint_set_max_torque", py_motor_joint_set_max_torque, METH_VARARGS, NULL},
    {"motor_joint_get_max_torque", py_motor_joint_get_max_torque, METH_VARARGS, NULL},
    // Events
    {"get_contact_events",   py_get_contact_events,   METH_VARARGS, NULL},
    {"get_sensor_events",    py_get_sensor_events,    METH_VARARGS, NULL},
    {"get_body_move_events", py_get_body_move_events, METH_VARARGS, NULL},
    {"get_joint_events",     py_get_joint_events,     METH_VARARGS, NULL},
    // World Queries
    {"overlap_aabb",         py_overlap_aabb,         METH_VARARGS, NULL},
    {"overlap_circle",       py_overlap_circle,       METH_VARARGS, NULL},
    {"cast_ray_all",         py_cast_ray_all,         METH_VARARGS, NULL},
    {"cast_mover",           py_cast_mover,           METH_VARARGS, NULL},
    {"collide_mover",        py_collide_mover,        METH_VARARGS, NULL},
    {"explode",              py_explode,              METH_VARARGS, NULL},
    // Ray cast
    {"ray_cast_closest",     py_ray_cast_closest,     METH_VARARGS, NULL},
    // World Tuning
    {"world_enable_sleeping", py_world_enable_sleeping, METH_VARARGS, NULL},
    {"world_is_sleeping_enabled", py_world_is_sleeping_enabled, METH_VARARGS, NULL},
    {"world_enable_continuous", py_world_enable_continuous, METH_VARARGS, NULL},
    {"world_is_continuous_enabled", py_world_is_continuous_enabled, METH_VARARGS, NULL},
    {"world_set_restitution_threshold", py_world_set_restitution_threshold, METH_VARARGS, NULL},
    {"world_get_restitution_threshold", py_world_get_restitution_threshold, METH_VARARGS, NULL},
    {"world_set_hit_event_threshold", py_world_set_hit_event_threshold, METH_VARARGS, NULL},
    {"world_get_hit_event_threshold", py_world_get_hit_event_threshold, METH_VARARGS, NULL},
    {"world_set_max_linear_speed", py_world_set_max_linear_speed, METH_VARARGS, NULL},
    {"world_get_max_linear_speed", py_world_get_max_linear_speed, METH_VARARGS, NULL},
    // Batch
    {"sync_bodies",          py_sync_bodies,          METH_VARARGS, NULL},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef physics_module_def = {
    PyModuleDef_HEAD_INIT,
    "_cocoa._physics",
    "Box2D v3 physics engine bridge.",
    -1,
    physics_methods
};

extern "C" {

PyMODINIT_FUNC PyInit__physics(void) {
    PyObject *m = PyModule_Create(&physics_module_def);
    if (!m) return NULL;
    // Export scale factor so Python side can use it
    PyModule_AddObject(m, "PTM_RATIO", PyFloat_FromDouble(kPTM));
    // Body types
    PyModule_AddIntConstant(m, "STATIC", b2_staticBody);
    PyModule_AddIntConstant(m, "KINEMATIC", b2_kinematicBody);
    PyModule_AddIntConstant(m, "DYNAMIC", b2_dynamicBody);
    // Joint types
    PyModule_AddIntConstant(m, "JOINT_DISTANCE", b2_distanceJoint);
    PyModule_AddIntConstant(m, "JOINT_MOTOR", b2_motorJoint);
    PyModule_AddIntConstant(m, "JOINT_PRISMATIC", b2_prismaticJoint);
    PyModule_AddIntConstant(m, "JOINT_REVOLUTE", b2_revoluteJoint);
    PyModule_AddIntConstant(m, "JOINT_WELD", b2_weldJoint);
    PyModule_AddIntConstant(m, "JOINT_WHEEL", b2_wheelJoint);
    PyModule_AddIntConstant(m, "JOINT_FILTER", b2_filterJoint);
    // Shape types
    PyModule_AddIntConstant(m, "SHAPE_CIRCLE", b2_circleShape);
    PyModule_AddIntConstant(m, "SHAPE_CAPSULE", b2_capsuleShape);
    PyModule_AddIntConstant(m, "SHAPE_SEGMENT", b2_segmentShape);
    PyModule_AddIntConstant(m, "SHAPE_POLYGON", b2_polygonShape);
    return m;
}

void registerPhysicsModule(void) {
    PyImport_AppendInittab("_cocoa._physics", PyInit__physics);
}

} // extern "C"
