"""scene._node — Node base class, Group, and Layer."""
from __future__ import annotations

import math

from ._common import (
    _IDENTITY, _apply, _avg_scale, _color, _identity, _invert, _matrix, _mul, _rot,
)
from ._engine import Cmd, KIND_TEX, Texture


class Node:
    collision_category: int = 0xFFFFFFFF
    collision_mask: int = 0xFFFFFFFF

    def __init__(self, *, x=0, y=0, position=None, rotation=0, scale=1, opacity=1, z=0, speed=1):
        if position is not None:
            x, y = position
        self.x = float(x)
        self.y = float(y)
        self.rotation = float(rotation)
        self.scale = float(scale) if isinstance(scale, (int, float)) else (float(scale[0]), float(scale[1]))
        self.opacity = float(opacity)
        self.z = float(z)
        self.speed = speed
        self.visible = True
        self.interactive = True
        self.passthrough = False
        self.gestures: list = []
        self.parent: Node | None = None
        self.children: list[Node] = []
        self._actions: list = []
        self._cache = None
        self._world_transform = _IDENTITY
        self._world_opacity = 1.0
        self.physics_body = None

    @property
    def position(self):
        return (self.x, self.y)

    @position.setter
    def position(self, v):
        self.x, self.y = float(v[0]), float(v[1])

    @property
    def speed(self):
        return self._speed

    @speed.setter
    def speed(self, v):
        value = float(v)
        if value < 0:
            raise ValueError("speed must be >= 0")
        self._speed = value

    @property
    def time_scale(self):
        scale = 1.0
        node = self
        while node is not None:
            scale *= node.speed
            node = node.parent
        return scale

    # ── tree manipulation ──

    def add(self, *nodes):
        # Validate the whole request before changing any parent links.
        for n in nodes:
            if not isinstance(n, Node):
                raise TypeError("children must be Node instances")
            ancestor = self
            while ancestor is not None:
                if n is ancestor:
                    raise ValueError("a node cannot contain itself or an ancestor")
                ancestor = ancestor.parent
        for n in nodes:
            if n.parent is self:
                continue
            if n.parent is not None:
                old_parent = n.parent
                if old_parent._tree_root() is self._tree_root():
                    # Reparenting within a scene must not recreate live bodies.
                    old_parent._detach_child(n, preserve_physics=True)
                else:
                    old_parent.remove(n)
            n.parent = self
            self.children.append(n)
            self._notify_physics_attach(n)
        return self

    def remove(self, *nodes):
        for n in nodes:
            if n in self.children:
                self._detach_child(n)
        return self

    def _detach_child(self, node, *, preserve_physics=False):
        self.children.remove(node)
        node.parent = None
        self._notify_detach(node, preserve_physics=preserve_physics)

    def clear(self):
        for c in self.children:
            c.parent = None
            self._notify_detach(c)
        self.children.clear()
        return self

    def _tree_root(self):
        root = self
        while root.parent is not None:
            root = root.parent
        return root

    def _notify_detach(self, node, *, preserve_physics=False):
        root = self._tree_root()
        if hasattr(root, '_touch_owners'):
            self._cleanup_subtree(root, node, preserve_physics=preserve_physics)

    def _notify_physics_attach(self, node):
        """Register node (and subtree) with the scene's physics world if applicable."""
        root = self._tree_root()
        pw = getattr(root, '_physics_world', None)
        if pw is not None:
            self._register_physics_subtree(pw, node)

    @staticmethod
    def _register_physics_subtree(pw, node):
        if node.physics_body is not None:
            pw._add_node(node)
        for c in node.children:
            Node._register_physics_subtree(pw, c)

    @staticmethod
    def _cleanup_subtree(scene, node, *, preserve_physics=False):
        Node._drop_internal_caches(node)
        detach_input = getattr(node, "_detach_text_input", None)
        if detach_input is not None and not preserve_physics:
            detach_input()
        stale = [tid for tid, owner in scene._touch_owners.items() if owner is node]
        for tid in stale:
            del scene._touch_owners[tid]
        scene._gesture_nodes.discard(node)
        for gesture in node.gestures:
            gesture.reset()
        # Only destroy bodies when the subtree actually leaves this scene.
        pw = getattr(scene, '_physics_world', None)
        if not preserve_physics and pw is not None and node.physics_body is not None:
            pw._remove_node(node)
        for c in node.children:
            Node._cleanup_subtree(scene, c, preserve_physics=preserve_physics)

    @staticmethod
    def _drop_internal_caches(node):
        try:
            node._cache = None
        except Exception:
            pass
        try:
            node._c_cache = None
        except Exception:
            pass

    # ── image capture ──

    def capture(self, *, rect=None, size=None, background=None):
        """Render current content to an independent, in-memory ImageData.

        For a Scene, rect defaults to its viewport in screen coordinates and
        background defaults to the scene background. For another node, rect
        is required in that node's local coordinates; ancestors and camera
        are excluded, and the default background is transparent. Rectangles
        are (x, y, width, height), with x rightward and y downward.

        size specifies output pixels (width, height); by default, the rect is
        rendered at the window's display scale. Call from a running scene's
        setup, update or input callbacks, between GPU passes. Capture blocks
        for readback, but never ticks animation, physics or input processing.
        """
        from ._capture import capture

        return capture(self, rect=rect, size=size, background=background)

    # ── actions ──

    def run_action(self, act, key=None):
        if key is not None:
            self._actions = [a for a in self._actions if a.key != key]
        self._actions.append(act._bind(self, key))
        return self

    def remove_action(self, key):
        self._actions = [a for a in self._actions if a.key != key]
        return self

    # ── coordinate conversion ──

    @property
    def world_position(self):
        wt = self._world_transform
        return (wt[4], wt[5])

    def convert_to_world(self, x, y):
        return _apply(self._world_transform, (x, y))

    def convert_from_world(self, x, y):
        return _apply(_invert(self._world_transform), (x, y))

    # ── hit testing ──

    def contains_point(self, wx, wy):
        b = self._bounds()
        if b is None:
            return False
        lx, ly = self.convert_from_world(wx, wy)
        return b[0] <= lx <= b[2] and b[1] <= ly <= b[3]

    # ── collision ──

    def _collider(self):
        return None

    def collides_with(self, other):
        info = self.collision_info(other)
        return info is not None and info.hit

    def collision_info(self, other):
        if not ((self.collision_category & other.collision_mask) and
                (other.collision_category & self.collision_mask)):
            return None
        ca = self._collider()
        cb = other._collider()
        if ca is None or cb is None:
            return None
        from ._collision import _test_colliders
        return _test_colliders(ca, cb)

    # ── internal ──

    def _snap(self):
        return (self.x, self.y, self.rotation, self.scale, self.opacity, self.z)

    def _tick(self, dt):
        local_dt = dt * self.speed
        self._tick_self(local_dt)
        for c in list(self.children):
            c._tick(local_dt)

    def _tick_self(self, dt):
        if self._actions:
            self._actions = [a for a in self._actions if not a.tick(dt)]

    def _collect(self, cmds, renderer, transform, opacity, order):
        if not self.visible or opacity <= 0.001:
            return
        cache = self._cache
        if cache is not None:
            snap = self._snap()
            if (snap == cache[0]
                    and (transform is cache[1] or transform == cache[1])
                    and opacity == cache[2]
                    and renderer.screen_scale == cache[6]):
                world, op = cache[3], cache[4]
                self._world_transform = world
                self._world_opacity = op
                for cmd in cache[5]:
                    order[0] += 1
                    cmd.order = order[0]
                    cmds.append(cmd)
                for child in self.children:
                    child._collect(cmds, renderer, world, op, order)
                return
        world = _mul(transform, _matrix((self.x, self.y), self.rotation, self.scale))
        op = opacity * self.opacity
        self._world_transform = world
        self._world_opacity = op
        mark = len(cmds)
        self._emit(cmds, renderer, world, op, order)
        self._cache = (self._snap(), transform, opacity, world, op, cmds[mark:], renderer.screen_scale)
        for child in self.children:
            child._collect(cmds, renderer, world, op, order)

    def _bounds(self):
        return None

    def _emit(self, cmds, renderer, world, opacity, order):
        pass

    def close(self):
        for c in list(self.children):
            try:
                close = getattr(c, 'close', None)
                if close is not None:
                    close()
            finally:
                c.parent = None
        self.children.clear()
        self._actions.clear()
        self.gestures.clear()
        self.physics_body = None
        Node._drop_internal_caches(self)


class Group(Node):
    """Container node. Children are re-rendered every frame."""

    def close(self):
        super().close()


class Layer(Node):
    """Cached container. Children are rendered to a texture once, reused until invalidated."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self._tex: Texture | None = None
        self._lsize = None
        self._lcenter = None
        self._capture_center = None
        self._rscale = 1.0
        self._dirty = True

    def add(self, *nodes):
        super().add(*nodes)
        self._dirty = True
        return self

    def _detach_child(self, node, *, preserve_physics=False):
        super()._detach_child(node, preserve_physics=preserve_physics)
        self._dirty = True

    def clear(self):
        super().clear()
        self._dirty = True
        return self

    def invalidate(self):
        self._dirty = True

    def _collect(self, cmds, renderer, transform, opacity, order):
        if not self.visible or opacity <= 0.001:
            return
        world = _mul(transform, _matrix((self.x, self.y), self.rotation, self.scale))
        op = opacity * self.opacity
        self._world_transform = world
        self._world_opacity = op
        if self.children:
            ds = _avg_scale(world)
            rs = max(ds, 1.0)
            if self._tex is None or self._dirty or abs(rs - self._rscale) > 1e-3:
                self._rebuild(renderer, rs)
            if self._tex and self._lsize and self._lcenter:
                center = (self._capture_center if self._capture_center is not None
                          else _apply(world, self._lcenter))
                order[0] += 1
                cmds.append(Cmd(
                    self.z, order[0], KIND_TEX,
                    center[0], center[1],
                    self._lsize[0] * ds * 0.5, self._lsize[1] * ds * 0.5,
                    _rot(world),
                    (KIND_TEX, 0, 0, 0), (0, 0, op, 0),
                    (0, 0, 0, 0), (1, 1, 1, 1), self._tex,
                ))

    def _rebuild(self, renderer, rscale):
        self._capture_center = None
        bounds = _measure_children(self.children)
        if bounds is None:
            if self._tex:
                self._tex.close(); self._tex = None
            self._lsize = self._lcenter = None
            self._rscale = rscale; self._dirty = False
            return
        x0, y0, x1, y1 = bounds
        pad = 6.0
        x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
        capture_viewport = renderer._capture_viewport
        draw_transform = None
        if capture_viewport is not None:
            from ._capture import _inverse

            # Rasterize only the visible part of a cached layer. A tiny crop
            # at high magnification must not allocate its whole enlarged image.
            cw, ch = capture_viewport
            world = self._world_transform
            draw_scale = _avg_scale(world)
            if draw_scale == 0:
                x1 = x0
            else:
                # Match the existing Layer quad: its center uses the full
                # affine transform, while its extent uses average scale.
                anchor = ((x0 + x1) * 0.5, (y0 + y1) * 0.5)
                draw_transform = _mul(
                    _matrix(_apply(world, anchor), _rot(world), draw_scale),
                    _matrix((-anchor[0], -anchor[1]), 0, 1))
                inverse = _inverse(draw_transform)
                corners = [_apply(inverse, point) for point in ((0, 0), (cw, 0), (0, ch), (cw, ch))]
                margin = 2 / (renderer.screen_scale * rscale)
                x0 = max(x0, min(point[0] for point in corners) - margin)
                y0 = max(y0, min(point[1] for point in corners) - margin)
                x1 = min(x1, max(point[0] for point in corners) + margin)
                y1 = min(y1, max(point[1] for point in corners) + margin)
            if x1 <= x0 or y1 <= y0:
                if self._tex:
                    self._tex.close()
                    self._tex = None
                self._lsize = self._lcenter = None
                self._rscale, self._dirty = rscale, False
                return
        lw = x1 - x0 if capture_viewport is not None else max(1.0, x1 - x0)
        lh = y1 - y0 if capture_viewport is not None else max(1.0, y1 - y0)
        pw = max(1, int(round(lw * renderer.screen_scale * rscale)))
        ph = max(1, int(round(lh * renderer.screen_scale * rscale)))
        if self._tex is None or self._lsize != (lw, lh) or abs(self._rscale - rscale) > 1e-3:
            if self._tex: self._tex.close()
            self._tex = Texture.render_target(pw, ph)
        local_root = _matrix((-x0, -y0), 0, 1)
        sub = []
        order = [0]
        previous_scale = renderer.screen_scale
        try:
            if capture_viewport is not None:
                renderer._capture_viewport = (lw, lh)
            renderer.screen_scale *= rscale
            for child in self.children:
                child._collect(sub, renderer, local_root, 1.0, order)
            sub.sort(key=lambda c: (c.z, c.order))
            renderer.render(sub, clear_color=(0, 0, 0, 0), target_texture=self._tex, viewport=(lw, lh))
        finally:
            renderer.screen_scale = previous_scale
            renderer._capture_viewport = capture_viewport
        self._lcenter = ((x0 + x1) * 0.5, (y0 + y1) * 0.5)
        if draw_transform is not None:
            self._capture_center = _apply(draw_transform, self._lcenter)
        self._lsize = (lw, lh)
        self._rscale = rscale
        self._dirty = False

    def close(self):
        if self._tex:
            self._tex.close(); self._tex = None
        self._lsize = None
        self._lcenter = None
        self._capture_center = None
        super().close()


def _measure_children(children):
    pts = []
    for c in children:
        if not c.visible:
            continue
        m = _matrix((c.x, c.y), c.rotation, c.scale)
        b = c._bounds()
        if b is not None:
            for bx, by in ((b[0], b[1]), (b[2], b[1]), (b[0], b[3]), (b[2], b[3])):
                pts.append(_apply(m, (bx, by)))
        if c.children:
            cb = _measure_children(c.children)
            if cb:
                for bx, by in ((cb[0], cb[1]), (cb[2], cb[1]), (cb[0], cb[3]), (cb[2], cb[3])):
                    pts.append(_apply(m, (bx, by)))
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))
