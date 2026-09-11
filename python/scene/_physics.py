"""scene._physics — Box2D physics integration (PhysicsWorld, PhysicsBody, PhysicsJoint)."""
from __future__ import annotations

from _cocoa import _physics


# ────────────────────────────── PhysicsBody ───────────────────────────────

class PhysicsBody:
    """A rigid-body attached to a Node. Created via class methods, then assigned
    to ``node.physics_body``.

    Example::

        player.physics_body = PhysicsBody.circle(radius=20, density=1.0)
        ground.physics_body = PhysicsBody.rect(width=400, height=20, is_static=True)
    """

    def __init__(self):
        self._shape_type: str = ''
        self._shape_args: tuple = ()
        self._body_id = None
        self._shape_id = None
        self._node = None
        self._world: PhysicsWorld | None = None

        # Body properties (applied at creation; some sync live via properties below)
        self.is_static: bool = False
        self.is_kinematic: bool = False
        self._density: float = 1.0
        self._friction: float = 0.3
        self._restitution: float = 0.2
        self.mass: float | None = None
        self._linear_damping: float = 0.0
        self._angular_damping: float = 0.01
        self._gravity_scale: float = 1.0
        self.is_bullet: bool = False
        self.allow_sleep: bool = True
        self.is_sensor: bool = False

        # Collision filtering
        self.category: int = 1
        self.mask: int = 0xFFFFFFFFFFFFFFFF
        self.contact_events: bool = True

    # ── Live-sync properties (write syncs to Box2D if body exists) ──

    @property
    def linear_damping(self): return self._linear_damping
    @linear_damping.setter
    def linear_damping(self, v):
        self._linear_damping = v
        if self._body_id is not None:
            _physics.body_set_linear_damping(self._body_id, v)

    @property
    def angular_damping(self): return self._angular_damping
    @angular_damping.setter
    def angular_damping(self, v):
        self._angular_damping = v
        if self._body_id is not None:
            _physics.body_set_angular_damping(self._body_id, v)

    @property
    def gravity_scale(self): return self._gravity_scale
    @gravity_scale.setter
    def gravity_scale(self, v):
        self._gravity_scale = v
        if self._body_id is not None:
            _physics.body_set_gravity_scale(self._body_id, v)

    @property
    def density(self): return self._density
    @density.setter
    def density(self, v):
        self._density = v
        if self._shape_id is not None:
            _physics.shape_set_density(self._shape_id, v)
            if self._body_id is not None:
                self.mass = _physics.body_get_mass(self._body_id)

    @property
    def friction(self): return self._friction
    @friction.setter
    def friction(self, v):
        self._friction = v
        if self._shape_id is not None:
            _physics.shape_set_friction(self._shape_id, v)

    @property
    def restitution(self): return self._restitution
    @restitution.setter
    def restitution(self, v):
        self._restitution = v
        if self._shape_id is not None:
            _physics.shape_set_restitution(self._shape_id, v)

    # ── Factory methods ──

    @classmethod
    def circle(cls, radius: float, *, offset=(0, 0), **kw) -> PhysicsBody:
        body = cls._from_kw(kw)
        body._shape_type = 'circle'
        body._shape_args = (radius, offset[0], offset[1])
        return body

    @classmethod
    def rect(cls, width: float, height: float, *, center=(0, 0), rotation=0.0, **kw) -> PhysicsBody:
        body = cls._from_kw(kw)
        body._shape_type = 'box'
        body._shape_args = (width / 2, height / 2, center[0], center[1], rotation)
        return body

    @classmethod
    def polygon(cls, points, **kw) -> PhysicsBody:
        """Create from a convex polygon. *points* is [(x,y), ...] (3-8 vertices)."""
        body = cls._from_kw(kw)
        body._shape_type = 'polygon'
        flat = []
        for p in points:
            flat.extend(p)
        body._shape_args = (flat,)
        return body

    @classmethod
    def capsule(cls, start, end, radius: float, **kw) -> PhysicsBody:
        body = cls._from_kw(kw)
        body._shape_type = 'capsule'
        body._shape_args = (start[0], start[1], end[0], end[1], radius)
        return body

    @classmethod
    def segment(cls, start, end, **kw) -> PhysicsBody:
        """Create from a line segment."""
        body = cls._from_kw(kw)
        body._shape_type = 'segment'
        body._shape_args = (start[0], start[1], end[0], end[1])
        return body

    _READONLY = frozenset(('mass',))

    @classmethod
    def _from_kw(cls, kw):
        body = cls()
        for k, v in kw.items():
            if k in cls._READONLY:
                raise TypeError(f"PhysicsBody: '{k}' is read-only (computed from density and shape)")
            if hasattr(body, k):
                setattr(body, k, v)
            else:
                raise TypeError(f"PhysicsBody: unknown parameter '{k}'")
        return body

    # ── Internal: create in Box2D world ──

    def _create(self, world: PhysicsWorld, node):
        self._world = world
        self._node = node

        if self.is_static:
            btype = _physics.STATIC
        elif self.is_kinematic:
            btype = _physics.KINEMATIC
        else:
            btype = _physics.DYNAMIC

        self._body_id = _physics.create_body(
            world._world_id, btype,
            node.x, node.y, node.rotation,
            self.linear_damping, self.angular_damping,
            self.gravity_scale, int(self.is_bullet), int(self.allow_sleep),
        )

        cat, msk = self.category, self.mask
        sensor, contacts = int(self.is_sensor), int(self.contact_events)

        if self._shape_type == 'circle':
            r, ox, oy = self._shape_args
            self._shape_id = _physics.add_circle_shape(
                self._body_id, r, ox, oy,
                self.density, self.friction, self.restitution,
                cat, msk, sensor, contacts)
        elif self._shape_type == 'box':
            hw, hh, cx, cy, rot = self._shape_args
            self._shape_id = _physics.add_box_shape(
                self._body_id, hw, hh, cx, cy, rot,
                self.density, self.friction, self.restitution,
                cat, msk, sensor, contacts)
        elif self._shape_type == 'polygon':
            (flat,) = self._shape_args
            self._shape_id = _physics.add_polygon_shape(
                self._body_id, flat,
                self.density, self.friction, self.restitution,
                cat, msk, sensor, contacts)
        elif self._shape_type == 'capsule':
            x1, y1, x2, y2, r = self._shape_args
            self._shape_id = _physics.add_capsule_shape(
                self._body_id, x1, y1, x2, y2, r,
                self.density, self.friction, self.restitution,
                cat, msk, sensor, contacts)
        elif self._shape_type == 'segment':
            x1, y1, x2, y2 = self._shape_args
            self._shape_id = _physics.add_segment_shape(
                self._body_id, x1, y1, x2, y2,
                self.density, self.friction, self.restitution,
                cat, msk, sensor, contacts)

        if self._body_id is not None:
            self.mass = _physics.body_get_mass(self._body_id)

    def _destroy(self):
        if self._body_id is not None:
            _physics.destroy_body(self._body_id)
            self._body_id = None
            self._shape_id = None
            self._world = None
            self._node = None

    @property
    def alive(self) -> bool:
        return self._body_id is not None

    # ── Live property mutations ──

    @property
    def velocity(self):
        """(vx, vy) in points/second."""
        if self._body_id is None:
            return (0, 0)
        vx, vy, _ = _physics.body_get_velocity(self._body_id)
        return (vx, vy)

    @velocity.setter
    def velocity(self, v):
        if self._body_id is None:
            return
        _, _, omega = _physics.body_get_velocity(self._body_id)
        _physics.body_set_velocity(self._body_id, v[0], v[1], omega)

    @property
    def angular_velocity(self):
        if self._body_id is None:
            return 0.0
        _, _, omega = _physics.body_get_velocity(self._body_id)
        return omega

    @angular_velocity.setter
    def angular_velocity(self, v):
        if self._body_id is None:
            return
        vx, vy, _ = _physics.body_get_velocity(self._body_id)
        _physics.body_set_velocity(self._body_id, vx, vy, v)

    @property
    def enabled(self) -> bool:
        if self._body_id is None:
            return False
        return _physics.body_is_enabled(self._body_id)

    @enabled.setter
    def enabled(self, v):
        if self._body_id is None:
            return
        if v:
            _physics.body_enable(self._body_id)
        else:
            _physics.body_disable(self._body_id)

    @property
    def awake(self) -> bool:
        if self._body_id is None:
            return False
        return _physics.body_is_awake(self._body_id)

    @awake.setter
    def awake(self, v):
        if self._body_id is None:
            return
        _physics.body_set_awake(self._body_id, int(v))

    @property
    def sleep_enabled(self) -> bool:
        if self._body_id is None:
            return True
        return _physics.body_is_sleep_enabled(self._body_id)

    @sleep_enabled.setter
    def sleep_enabled(self, v):
        if self._body_id is None:
            return
        _physics.body_enable_sleep(self._body_id, int(v))

    @property
    def bullet(self) -> bool:
        if self._body_id is None:
            return self.is_bullet
        return _physics.body_is_bullet(self._body_id)

    @bullet.setter
    def bullet(self, v):
        if self._body_id is None:
            self.is_bullet = v
            return
        _physics.body_set_bullet(self._body_id, int(v))

    @property
    def motion_locks(self):
        """(lock_x, lock_y, lock_angular) — restrict motion axes."""
        if self._body_id is None:
            return (False, False, False)
        return _physics.body_get_motion_locks(self._body_id)

    @motion_locks.setter
    def motion_locks(self, v):
        if self._body_id is None:
            return
        _physics.body_set_motion_locks(self._body_id, int(v[0]), int(v[1]), int(v[2]))

    @property
    def inertia(self) -> float:
        if self._body_id is None:
            return 0.0
        return _physics.body_get_inertia(self._body_id)

    @property
    def center_of_mass(self):
        """World-space center of mass (x, y)."""
        if self._body_id is None:
            return (0, 0)
        return _physics.body_get_center_of_mass(self._body_id)

    # ── Coordinate conversion ──

    def local_point(self, world_point):
        """Convert world point to body-local point."""
        if self._body_id is None:
            return world_point
        return _physics.body_get_local_point(self._body_id, world_point[0], world_point[1])

    def world_point(self, local_point):
        """Convert body-local point to world point."""
        if self._body_id is None:
            return local_point
        return _physics.body_get_world_point(self._body_id, local_point[0], local_point[1])

    def local_vector(self, world_vector):
        if self._body_id is None:
            return world_vector
        return _physics.body_get_local_vector(self._body_id, world_vector[0], world_vector[1])

    def world_vector(self, local_vector):
        if self._body_id is None:
            return local_vector
        return _physics.body_get_world_vector(self._body_id, local_vector[0], local_vector[1])

    def set_target_transform(self, x, y, angle, dt):
        """Set target for kinematic bodies (smooth interpolation)."""
        if self._body_id is not None:
            _physics.body_set_target_transform(self._body_id, x, y, angle, dt)

    def set_mass_data(self, mass, center, inertia):
        """Override mass properties. center is (x, y) in local space."""
        if self._body_id is not None:
            _physics.body_set_mass_data(self._body_id, mass, center[0], center[1], inertia)

    def apply_mass_from_shapes(self):
        """Recompute mass from attached shapes."""
        if self._body_id is not None:
            _physics.body_apply_mass_from_shapes(self._body_id)

    # ── Forces / impulses ──

    def apply_force(self, force, point=None):
        if self._body_id is None:
            return
        if point is not None:
            _physics.body_apply_force(self._body_id, force[0], force[1], point[0], point[1])
        else:
            _physics.body_apply_force_center(self._body_id, force[0], force[1])

    def apply_impulse(self, impulse, point=None):
        if self._body_id is None:
            return
        if point is not None:
            _physics.body_apply_impulse(self._body_id, impulse[0], impulse[1], point[0], point[1])
        else:
            _physics.body_apply_impulse_center(self._body_id, impulse[0], impulse[1])

    def apply_torque(self, torque: float):
        if self._body_id is not None:
            _physics.body_apply_torque(self._body_id, torque)


# ────────────────────────────── PhysicsJoint ──────────────────────────────

class PhysicsJoint:
    """Base class for physics joints."""

    def __init__(self, joint_id, world, node_a, node_b):
        self._joint_id = joint_id
        self._world = world
        self.node_a = node_a
        self.node_b = node_b

    def destroy(self):
        if self._joint_id is not None:
            _physics.destroy_joint(self._joint_id)
            if self._world is not None:
                self._world._joints.discard(self)
            self._joint_id = None

    @property
    def is_valid(self) -> bool:
        return self._joint_id is not None and _physics.joint_is_valid(self._joint_id)

    @property
    def joint_type(self) -> int:
        if self._joint_id is None:
            return -1
        return _physics.joint_get_type(self._joint_id)

    @property
    def constraint_force(self):
        if self._joint_id is None:
            return (0, 0)
        return _physics.joint_get_constraint_force(self._joint_id)

    @property
    def constraint_torque(self) -> float:
        if self._joint_id is None:
            return 0.0
        return _physics.joint_get_constraint_torque(self._joint_id)

    @property
    def collide_connected(self) -> bool:
        if self._joint_id is None:
            return False
        return _physics.joint_get_collide_connected(self._joint_id)

    @collide_connected.setter
    def collide_connected(self, v):
        if self._joint_id is not None:
            _physics.joint_set_collide_connected(self._joint_id, int(v))

    def wake_bodies(self):
        if self._joint_id is not None:
            _physics.joint_wake_bodies(self._joint_id)

    # ── Factory methods (return typed subclasses) ──

    @classmethod
    def revolute(cls, world, node_a, node_b, anchor, *,
                 enable_limit=False, lower_angle=0, upper_angle=0,
                 enable_motor=False, motor_speed=0, max_motor_torque=0):
        ba, bb = node_a.physics_body, node_b.physics_body
        jid = _physics.create_revolute_joint(
            world._world_id, ba._body_id, bb._body_id,
            anchor[0], anchor[1],
            int(enable_limit), lower_angle, upper_angle,
            int(enable_motor), motor_speed, max_motor_torque)
        j = RevoluteJoint(jid, world, node_a, node_b)
        world._joints.add(j)
        return j

    @classmethod
    def distance(cls, world, node_a, node_b, anchor_a, anchor_b, *,
                 length=None, min_length=None, max_length=None,
                 hertz=0, damping_ratio=0):
        ba, bb = node_a.physics_body, node_b.physics_body
        if length is None:
            dx, dy = anchor_b[0] - anchor_a[0], anchor_b[1] - anchor_a[1]
            length = (dx * dx + dy * dy) ** 0.5
        jid = _physics.create_distance_joint(
            world._world_id, ba._body_id, bb._body_id,
            anchor_a[0], anchor_a[1], anchor_b[0], anchor_b[1],
            length,
            min_length if min_length is not None else -1.0,
            max_length if max_length is not None else -1.0,
            hertz, damping_ratio)
        j = DistanceJoint(jid, world, node_a, node_b)
        world._joints.add(j)
        return j

    @classmethod
    def weld(cls, world, node_a, node_b, anchor, *, hertz=0, damping_ratio=0):
        ba, bb = node_a.physics_body, node_b.physics_body
        jid = _physics.create_weld_joint(
            world._world_id, ba._body_id, bb._body_id,
            anchor[0], anchor[1], hertz, damping_ratio)
        j = WeldJoint(jid, world, node_a, node_b)
        world._joints.add(j)
        return j

    @classmethod
    def prismatic(cls, world, node_a, node_b, anchor, axis, *,
                  enable_limit=False, lower_translation=0, upper_translation=0,
                  enable_motor=False, motor_speed=0, max_motor_force=0):
        ba, bb = node_a.physics_body, node_b.physics_body
        jid = _physics.create_prismatic_joint(
            world._world_id, ba._body_id, bb._body_id,
            anchor[0], anchor[1], axis[0], axis[1],
            int(enable_limit), lower_translation, upper_translation,
            int(enable_motor), motor_speed, max_motor_force)
        j = PrismaticJoint(jid, world, node_a, node_b)
        world._joints.add(j)
        return j

    @classmethod
    def wheel(cls, world, node_a, node_b, anchor, axis, *,
              hertz=2, damping_ratio=0.7,
              enable_limit=False, lower_translation=0, upper_translation=0,
              enable_motor=False, motor_speed=0, max_motor_torque=0):
        ba, bb = node_a.physics_body, node_b.physics_body
        jid = _physics.create_wheel_joint(
            world._world_id, ba._body_id, bb._body_id,
            anchor[0], anchor[1], axis[0], axis[1],
            hertz, damping_ratio,
            int(enable_limit), lower_translation, upper_translation,
            int(enable_motor), motor_speed, max_motor_torque)
        j = WheelJoint(jid, world, node_a, node_b)
        world._joints.add(j)
        return j

    @classmethod
    def motor(cls, world, node_a, node_b, *,
              linear_velocity=(0, 0), angular_velocity=0,
              max_velocity_force=1, max_velocity_torque=1):
        ba, bb = node_a.physics_body, node_b.physics_body
        jid = _physics.create_motor_joint(
            world._world_id, ba._body_id, bb._body_id,
            linear_velocity[0], linear_velocity[1], angular_velocity,
            max_velocity_force, max_velocity_torque)
        j = MotorJoint(jid, world, node_a, node_b)
        world._joints.add(j)
        return j

    @classmethod
    def filter(cls, world, node_a, node_b):
        ba, bb = node_a.physics_body, node_b.physics_body
        jid = _physics.create_filter_joint(
            world._world_id, ba._body_id, bb._body_id)
        j = PhysicsJoint(jid, world, node_a, node_b)
        world._joints.add(j)
        return j


# ── Joint subclasses ──

def _jid(joint):
    return joint._joint_id

class RevoluteJoint(PhysicsJoint):
    @property
    def angle(self): return _physics.revolute_get_angle(_jid(self))
    @property
    def target_angle(self): return _physics.revolute_get_target_angle(_jid(self))
    @target_angle.setter
    def target_angle(self, v): _physics.revolute_set_target_angle(_jid(self), v)
    @property
    def spring_hertz(self): return _physics.revolute_get_spring_hertz(_jid(self))
    @spring_hertz.setter
    def spring_hertz(self, v): _physics.revolute_set_spring_hertz(_jid(self), v)
    @property
    def spring_damping(self): return _physics.revolute_get_spring_damping(_jid(self))
    @spring_damping.setter
    def spring_damping(self, v): _physics.revolute_set_spring_damping(_jid(self), v)
    def enable_spring(self, v=True): _physics.revolute_enable_spring(_jid(self), int(v))
    @property
    def limit_enabled(self): return _physics.revolute_is_limit_enabled(_jid(self))
    @limit_enabled.setter
    def limit_enabled(self, v): _physics.revolute_enable_limit(_jid(self), int(v))
    @property
    def lower_limit(self): return _physics.revolute_get_lower_limit(_jid(self))
    @property
    def upper_limit(self): return _physics.revolute_get_upper_limit(_jid(self))
    def set_limits(self, lower, upper): _physics.revolute_set_limits(_jid(self), lower, upper)
    @property
    def motor_enabled(self): return _physics.revolute_is_motor_enabled(_jid(self))
    @motor_enabled.setter
    def motor_enabled(self, v): _physics.revolute_enable_motor(_jid(self), int(v))
    @property
    def motor_speed(self): return _physics.revolute_get_motor_speed(_jid(self))
    @motor_speed.setter
    def motor_speed(self, v): _physics.revolute_set_motor_speed(_jid(self), v)
    @property
    def max_motor_torque(self): return _physics.revolute_get_max_motor_torque(_jid(self))
    @max_motor_torque.setter
    def max_motor_torque(self, v): _physics.revolute_set_max_motor_torque(_jid(self), v)
    @property
    def motor_torque(self): return _physics.revolute_get_motor_torque(_jid(self))


class DistanceJoint(PhysicsJoint):
    @property
    def length(self): return _physics.distance_get_length(_jid(self))
    @length.setter
    def length(self, v): _physics.distance_set_length(_jid(self), v)
    def enable_spring(self, v=True): _physics.distance_enable_spring(_jid(self), int(v))
    @property
    def spring_hertz(self): return _physics.distance_get_spring_hertz(_jid(self))
    @spring_hertz.setter
    def spring_hertz(self, v): _physics.distance_set_spring_hertz(_jid(self), v)
    @property
    def spring_damping(self): return _physics.distance_get_spring_damping(_jid(self))
    @spring_damping.setter
    def spring_damping(self, v): _physics.distance_set_spring_damping(_jid(self), v)
    @property
    def limit_enabled(self): return _physics.distance_is_limit_enabled(_jid(self))
    @limit_enabled.setter
    def limit_enabled(self, v): _physics.distance_enable_limit(_jid(self), int(v))
    @property
    def min_length(self): return _physics.distance_get_min_length(_jid(self))
    @property
    def max_length(self): return _physics.distance_get_max_length(_jid(self))
    def set_length_range(self, min_len, max_len):
        _physics.distance_set_length_range(_jid(self), min_len, max_len)
    @property
    def motor_enabled(self): return _physics.distance_is_motor_enabled(_jid(self))
    @motor_enabled.setter
    def motor_enabled(self, v): _physics.distance_enable_motor(_jid(self), int(v))
    @property
    def motor_speed(self): return _physics.distance_get_motor_speed(_jid(self))
    @motor_speed.setter
    def motor_speed(self, v): _physics.distance_set_motor_speed(_jid(self), v)
    @property
    def max_motor_force(self): return _physics.distance_get_max_motor_force(_jid(self))
    @max_motor_force.setter
    def max_motor_force(self, v): _physics.distance_set_max_motor_force(_jid(self), v)


class PrismaticJoint(PhysicsJoint):
    @property
    def translation(self): return _physics.prismatic_get_translation(_jid(self))
    @property
    def speed(self): return _physics.prismatic_get_speed(_jid(self))
    def enable_spring(self, v=True): _physics.prismatic_enable_spring(_jid(self), int(v))
    @property
    def spring_hertz(self): return _physics.prismatic_get_spring_hertz(_jid(self))
    @spring_hertz.setter
    def spring_hertz(self, v): _physics.prismatic_set_spring_hertz(_jid(self), v)
    @property
    def spring_damping(self): return _physics.prismatic_get_spring_damping(_jid(self))
    @spring_damping.setter
    def spring_damping(self, v): _physics.prismatic_set_spring_damping(_jid(self), v)
    @property
    def limit_enabled(self): return _physics.prismatic_is_limit_enabled(_jid(self))
    @limit_enabled.setter
    def limit_enabled(self, v): _physics.prismatic_enable_limit(_jid(self), int(v))
    @property
    def lower_limit(self): return _physics.prismatic_get_lower_limit(_jid(self))
    @property
    def upper_limit(self): return _physics.prismatic_get_upper_limit(_jid(self))
    def set_limits(self, lower, upper):
        _physics.prismatic_set_limits(_jid(self), lower, upper)
    @property
    def motor_enabled(self): return _physics.prismatic_is_motor_enabled(_jid(self))
    @motor_enabled.setter
    def motor_enabled(self, v): _physics.prismatic_enable_motor(_jid(self), int(v))
    @property
    def motor_speed(self): return _physics.prismatic_get_motor_speed(_jid(self))
    @motor_speed.setter
    def motor_speed(self, v): _physics.prismatic_set_motor_speed(_jid(self), v)
    @property
    def max_motor_force(self): return _physics.prismatic_get_max_motor_force(_jid(self))
    @max_motor_force.setter
    def max_motor_force(self, v): _physics.prismatic_set_max_motor_force(_jid(self), v)


class WeldJoint(PhysicsJoint):
    @property
    def linear_hertz(self): return _physics.weld_get_linear_hertz(_jid(self))
    @linear_hertz.setter
    def linear_hertz(self, v): _physics.weld_set_linear_hertz(_jid(self), v)
    @property
    def linear_damping(self): return _physics.weld_get_linear_damping(_jid(self))
    @linear_damping.setter
    def linear_damping(self, v): _physics.weld_set_linear_damping(_jid(self), v)
    @property
    def angular_hertz(self): return _physics.weld_get_angular_hertz(_jid(self))
    @angular_hertz.setter
    def angular_hertz(self, v): _physics.weld_set_angular_hertz(_jid(self), v)
    @property
    def angular_damping(self): return _physics.weld_get_angular_damping(_jid(self))
    @angular_damping.setter
    def angular_damping(self, v): _physics.weld_set_angular_damping(_jid(self), v)


class WheelJoint(PhysicsJoint):
    def enable_spring(self, v=True): _physics.wheel_enable_spring(_jid(self), int(v))
    @property
    def spring_hertz(self): return _physics.wheel_get_spring_hertz(_jid(self))
    @spring_hertz.setter
    def spring_hertz(self, v): _physics.wheel_set_spring_hertz(_jid(self), v)
    @property
    def spring_damping(self): return _physics.wheel_get_spring_damping(_jid(self))
    @spring_damping.setter
    def spring_damping(self, v): _physics.wheel_set_spring_damping(_jid(self), v)
    @property
    def limit_enabled(self): return _physics.wheel_is_limit_enabled(_jid(self))
    @limit_enabled.setter
    def limit_enabled(self, v): _physics.wheel_enable_limit(_jid(self), int(v))
    @property
    def lower_limit(self): return _physics.wheel_get_lower_limit(_jid(self))
    @property
    def upper_limit(self): return _physics.wheel_get_upper_limit(_jid(self))
    def set_limits(self, lower, upper):
        _physics.wheel_set_limits(_jid(self), lower, upper)
    @property
    def motor_enabled(self): return _physics.wheel_is_motor_enabled(_jid(self))
    @motor_enabled.setter
    def motor_enabled(self, v): _physics.wheel_enable_motor(_jid(self), int(v))
    @property
    def motor_speed(self): return _physics.wheel_get_motor_speed(_jid(self))
    @motor_speed.setter
    def motor_speed(self, v): _physics.wheel_set_motor_speed(_jid(self), v)
    @property
    def max_motor_torque(self): return _physics.wheel_get_max_motor_torque(_jid(self))
    @max_motor_torque.setter
    def max_motor_torque(self, v): _physics.wheel_set_max_motor_torque(_jid(self), v)


class MotorJoint(PhysicsJoint):
    @property
    def linear_velocity(self):
        return _physics.motor_joint_get_linear_velocity(_jid(self))
    @linear_velocity.setter
    def linear_velocity(self, v):
        _physics.motor_joint_set_linear_velocity(_jid(self), v[0], v[1])
    @property
    def angular_velocity(self):
        return _physics.motor_joint_get_angular_velocity(_jid(self))
    @angular_velocity.setter
    def angular_velocity(self, v):
        _physics.motor_joint_set_angular_velocity(_jid(self), v)
    @property
    def max_velocity_force(self):
        return _physics.motor_joint_get_max_force(_jid(self))
    @max_velocity_force.setter
    def max_velocity_force(self, v):
        _physics.motor_joint_set_max_force(_jid(self), v)
    @property
    def max_velocity_torque(self):
        return _physics.motor_joint_get_max_torque(_jid(self))
    @max_velocity_torque.setter
    def max_velocity_torque(self, v):
        _physics.motor_joint_set_max_torque(_jid(self), v)


# ────────────────────────────── ChainShape ────────────────────────────────

class ChainShape:
    """A chain of line segments for terrain/platform collision."""

    def __init__(self, body, points, *, is_loop=True,
                 friction=0.3, restitution=0.2,
                 category=1, mask=0xFFFFFFFFFFFFFFFF):
        pb = body.physics_body
        flat = []
        for p in points:
            flat.extend(p)
        self._chain_id = _physics.create_chain(
            pb._body_id, flat, int(is_loop),
            friction, restitution, category, mask)

    def destroy(self):
        if self._chain_id is not None:
            _physics.destroy_chain(self._chain_id)
            self._chain_id = None


# ────────────────────────────── PhysicsWorld ──────────────────────────────

class PhysicsWorld:
    """Manages a Box2D world. Owned by a Scene."""

    def __init__(self, gravity=(0, 980)):
        self._world_id = _physics.create_world(gravity[0], gravity[1])
        self._bodies: dict[int, PhysicsBody] = {}
        self._body_ids: list = []
        self._nodes: list = []
        self._joints: set[PhysicsJoint] = set()
        self._sub_steps = 4
        self._contact_callback = None
        self._sensor_callback = None
        self._joint_event_callback = None

    def destroy(self):
        if self._world_id is not None:
            wid = self._world_id
            self._world_id = None
            for pb in self._bodies.values():
                pb._body_id = None
                pb._shape_id = None
                pb._world = None
                pb._node = None
            for j in self._joints:
                j._joint_id = None
            self._bodies.clear()
            self._body_ids.clear()
            self._nodes.clear()
            self._joints.clear()
            _physics.destroy_world(wid)

    # ── Gravity ──

    @property
    def gravity(self):
        if self._world_id is None:
            return (0, 0)
        return _physics.world_get_gravity(self._world_id)

    @gravity.setter
    def gravity(self, v):
        if self._world_id is not None:
            _physics.world_set_gravity(self._world_id, v[0], v[1])

    @property
    def sub_steps(self) -> int:
        return self._sub_steps

    @sub_steps.setter
    def sub_steps(self, v: int):
        self._sub_steps = max(1, int(v))

    # ── World tuning ──

    @property
    def sleeping_enabled(self) -> bool:
        if self._world_id is None: return True
        return _physics.world_is_sleeping_enabled(self._world_id)

    @sleeping_enabled.setter
    def sleeping_enabled(self, v):
        if self._world_id is not None:
            _physics.world_enable_sleeping(self._world_id, int(v))

    @property
    def continuous_enabled(self) -> bool:
        if self._world_id is None: return True
        return _physics.world_is_continuous_enabled(self._world_id)

    @continuous_enabled.setter
    def continuous_enabled(self, v):
        if self._world_id is not None:
            _physics.world_enable_continuous(self._world_id, int(v))

    @property
    def restitution_threshold(self) -> float:
        if self._world_id is None: return 0
        return _physics.world_get_restitution_threshold(self._world_id)

    @restitution_threshold.setter
    def restitution_threshold(self, v):
        if self._world_id is not None:
            _physics.world_set_restitution_threshold(self._world_id, v)

    @property
    def hit_event_threshold(self) -> float:
        if self._world_id is None: return 0
        return _physics.world_get_hit_event_threshold(self._world_id)

    @hit_event_threshold.setter
    def hit_event_threshold(self, v):
        if self._world_id is not None:
            _physics.world_set_hit_event_threshold(self._world_id, v)

    @property
    def max_linear_speed(self) -> float:
        if self._world_id is None: return 0
        return _physics.world_get_max_linear_speed(self._world_id)

    @max_linear_speed.setter
    def max_linear_speed(self, v):
        if self._world_id is not None:
            _physics.world_set_max_linear_speed(self._world_id, v)

    # ── Event callbacks ──

    def on_contact(self, callback):
        """callback(node_a, node_b, event_type). event_type: 'begin'/'end'/'hit'."""
        self._contact_callback = callback

    def on_sensor(self, callback):
        """callback(sensor_node, visitor_node, event_type). event_type: 'begin'/'end'."""
        self._sensor_callback = callback

    def on_joint_event(self, callback):
        """callback(joint) — called when joint force/torque exceeds threshold."""
        self._joint_event_callback = callback

    # ── Body registration ──

    def _add_node(self, node):
        pb = node.physics_body
        if pb is None or pb.alive:
            return
        pb._create(self, node)
        nid = id(node)
        self._bodies[nid] = pb
        self._body_ids.append(pb._body_id)
        self._nodes.append(node)

    def _remove_node(self, node):
        nid = id(node)
        pb = self._bodies.pop(nid, None)
        if pb is None:
            return
        try:
            idx = self._nodes.index(node)
            self._body_ids.pop(idx)
            self._nodes.pop(idx)
        except ValueError:
            pass
        pb._destroy()

    # ── Simulation step ──

    def step(self, dt: float):
        if self._world_id is None or not self._body_ids:
            return
        dt = min(dt, 1.0 / 30.0)
        _physics.world_step(self._world_id, dt, self._sub_steps)

        # Sync transforms
        transforms = _physics.sync_bodies(self._body_ids)
        for i, (x, y, rot) in enumerate(transforms):
            node = self._nodes[i]
            node.x = x
            node.y = y
            node.rotation = rot

        # Process events
        if self._contact_callback is not None:
            self._process_contacts()
        if self._sensor_callback is not None:
            self._process_sensors()
        if self._joint_event_callback is not None:
            self._process_joint_events()

    def _build_body_lookup(self):
        return {self._body_ids[i]: self._nodes[i] for i in range(len(self._body_ids))}

    def _process_contacts(self):
        events = _physics.get_contact_events(self._world_id)
        if not events:
            return
        lookup = self._build_body_lookup()
        names = ('begin', 'end', 'hit')
        cb = self._contact_callback
        for ba_id, bb_id, etype in events:
            na, nb = lookup.get(ba_id), lookup.get(bb_id)
            if na is not None and nb is not None:
                cb(na, nb, names[etype])

    def _process_sensors(self):
        events = _physics.get_sensor_events(self._world_id)
        if not events:
            return
        lookup = self._build_body_lookup()
        names = ('begin', 'end')
        cb = self._sensor_callback
        for sa_id, sv_id, etype in events:
            na, nb = lookup.get(sa_id), lookup.get(sv_id)
            if na is not None and nb is not None:
                cb(na, nb, names[etype])

    def _process_joint_events(self):
        events = _physics.get_joint_events(self._world_id)
        if not events:
            return
        # Find joint by ID
        joint_lookup = {j._joint_id: j for j in self._joints if j._joint_id is not None}
        cb = self._joint_event_callback
        for jid in events:
            j = joint_lookup.get(jid)
            if j is not None:
                cb(j)

    # ── Queries ──

    def ray_cast(self, origin, direction, *, category=1, mask=0xFFFFFFFFFFFFFFFF):
        """Closest hit: (node, point, normal, fraction) or None."""
        if self._world_id is None:
            return None
        result = _physics.ray_cast_closest(
            self._world_id, origin[0], origin[1], direction[0], direction[1],
            category, mask)
        if result is None:
            return None
        bid, px, py, nx, ny, frac = result
        lookup = self._build_body_lookup()
        return (lookup.get(bid), (px, py), (nx, ny), frac)

    def ray_cast_all(self, origin, direction, *, category=1, mask=0xFFFFFFFFFFFFFFFF):
        """All hits: [(node, point, normal, fraction), ...]."""
        if self._world_id is None:
            return []
        results = _physics.cast_ray_all(
            self._world_id, origin[0], origin[1], direction[0], direction[1],
            category, mask)
        if not results:
            return []
        lookup = self._build_body_lookup()
        out = []
        for bid, px, py, nx, ny, frac in results:
            out.append((lookup.get(bid), (px, py), (nx, ny), frac))
        return out

    def overlap_aabb(self, min_point, max_point, *, category=1, mask=0xFFFFFFFFFFFFFFFF):
        """Nodes overlapping an AABB."""
        if self._world_id is None:
            return []
        bids = _physics.overlap_aabb(
            self._world_id, min_point[0], min_point[1],
            max_point[0], max_point[1], category, mask)
        lookup = self._build_body_lookup()
        seen = set()
        out = []
        for bid in bids:
            n = lookup.get(bid)
            if n is not None and id(n) not in seen:
                seen.add(id(n))
                out.append(n)
        return out

    def overlap_circle(self, center, radius, *, category=1, mask=0xFFFFFFFFFFFFFFFF):
        """Nodes overlapping a circle."""
        if self._world_id is None:
            return []
        bids = _physics.overlap_circle(
            self._world_id, center[0], center[1], radius, category, mask)
        lookup = self._build_body_lookup()
        seen = set()
        out = []
        for bid in bids:
            n = lookup.get(bid)
            if n is not None and id(n) not in seen:
                seen.add(id(n))
                out.append(n)
        return out

    # ── Mover (character controller) ──

    def cast_mover(self, capsule_start, capsule_end, radius, translation, *,
                   category=1, mask=0xFFFFFFFFFFFFFFFF):
        """Cast a capsule mover, return fraction (0-1)."""
        if self._world_id is None:
            return 1.0
        return _physics.cast_mover(
            self._world_id,
            capsule_start[0], capsule_start[1],
            capsule_end[0], capsule_end[1], radius,
            translation[0], translation[1], category, mask)

    def collide_mover(self, capsule_start, capsule_end, radius, *,
                      category=1, mask=0xFFFFFFFFFFFFFFFF):
        """Collide capsule mover, return [(nx, ny, offset, px, py, hit), ...]."""
        if self._world_id is None:
            return []
        return _physics.collide_mover(
            self._world_id,
            capsule_start[0], capsule_start[1],
            capsule_end[0], capsule_end[1], radius,
            category, mask)

    # ── Explosion ──

    def explode(self, center, radius, falloff, impulse_per_length):
        """Apply radial explosion force."""
        if self._world_id is not None:
            _physics.explode(
                self._world_id, center[0], center[1],
                radius, falloff, impulse_per_length)

    # ── Scan scene tree ──

    def _scan_tree(self, root):
        if hasattr(root, 'physics_body') and root.physics_body is not None:
            self._add_node(root)
        for child in root.children:
            self._scan_tree(child)
