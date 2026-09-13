"""scene._node — Node base class, Group, and Layer."""
from __future__ import annotations

import math

from ._common import (
    _IDENTITY, _apply, _avg_scale, _color, _identity, _invert, _matrix, _mul, _rot,
)
from ._engine import Cmd, KIND_TEX, Texture


class Node:
    _screen_space = False
    _stacking_context = False
    collision_category: int = 0xFFFFFFFF
    collision_mask: int = 0xFFFFFFFF

    def __init__(self, *, x=0, y=0, position=None, rotation=0, scale=1, opacity=1, z=0, speed=1,
                 clip=None):
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
        self._world_clips = ()
        self.clip = clip

    @property
    def clip(self):
        """A ClipRect, (x, y, width, height), or None, in local coordinates."""
        return self._clip

    @clip.setter
    def clip(self, value):
        from ._clip import ClipRect

        if value is not None and not isinstance(value, ClipRect):
            value = ClipRect(*value)
        previous = getattr(self, "_clip", None)
        self._clip = value
        if value != previous:
            ancestor = self
            while ancestor is not None:
                if isinstance(ancestor, Layer):
                    ancestor.invalidate()
                ancestor = getattr(ancestor, "parent", None)

    def _clip_state(self, world, parents):
        return parents + (self._clip._state(world),) if self._clip is not None else parents

    def _inside_clip(self, x, y):
        from ._clip import _contains

        return _contains(self._world_clips, x, y)

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
        if getattr(self, "_node_closing", False):
            raise RuntimeError("Cannot add children while a node is closing")
        # Validate the whole request before changing any parent links.
        for n in nodes:
            if not isinstance(n, Node):
                raise TypeError("children must be Node instances")
            validate_parent = getattr(n, "_validate_parent", None)
            if validate_parent is not None:
                validate_parent(self)
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
        """Detach children from the initial snapshot that remain attached here."""
        for child in list(self.children):
            if child.parent is not self:
                continue
            self._detach_child(child)
        return self

    def _tree_root(self):
        root = self
        while root.parent is not None:
            root = root.parent
        return root

    def _notify_detach(self, node, *, preserve_physics=False):
        root = self._tree_root()
        if hasattr(root, "_ensure_ui_nodes"):
            root._ui_structure_dirty = True
            root._hit_nodes_dirty = True
        if hasattr(root, '_touch_owners'):
            self._cleanup_subtree(root, node, preserve_physics=preserve_physics)

    def _notify_physics_attach(self, node):
        """Register node (and subtree) with the scene's physics world if applicable."""
        root = self._tree_root()
        if hasattr(root, "_ensure_ui_nodes"):
            root._ui_structure_dirty = True
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
        manager = getattr(scene, "_focus_manager", None)
        if manager is not None:
            manager.remove_subtree(node)
        router = getattr(scene, "_pointer_router", None)
        if router is not None:
            router.cancel_subtree(node)
        cancel_interaction = getattr(node, "_cancel_interaction", None)
        if cancel_interaction is not None:
            cancel_interaction()
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
        for c in list(node.children):
            Node._cleanup_subtree(scene, c, preserve_physics=preserve_physics)
        dispatch = getattr(node, "_dispatch_ui_events", None)
        if dispatch is not None:
            dispatch()

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

    def _current_geometry(self):
        """Derive current geometry for layout without advancing or collecting."""
        chain, node = [], self
        while node is not None:
            chain.append(node)
            node = node.parent
        root = chain[-1]
        world = root._camera_root_transform() if hasattr(root, "_camera_root_transform") else _IDENTITY
        opacity, visible, clips = 1., True, ()
        for node in reversed(chain):
            if node._screen_space:
                world, clips = _IDENTITY, ()
            world = _mul(world, _matrix(node.position, node.rotation, node.scale))
            opacity *= node.opacity
            visible = visible and node.visible and opacity > .001
            clips = node._clip_state(world, clips)
            if node is self:
                return world, opacity, visible, clips
            if isinstance(node, Layer) and node._lcenter is not None:
                anchor = node._cache_anchor if node._cache_anchor is not None else node._lcenter
                world = node._draw_transform(world, anchor)

    # ── hit testing ──

    def contains_point(self, wx, wy):
        if not self._inside_clip(wx, wy):
            return False
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
        parents = getattr(renderer, "_collect_clips", ())
        previous_layer = getattr(renderer, "_collect_layer", 0)
        previous_scope = getattr(renderer, "_collect_sort_scope", ())
        if self._stacking_context:
            order[0] += 1
            renderer._collect_sort_scope = previous_scope + ((float(self.z), order[0]),)
        if self._screen_space and self.parent is not None:
            transform = getattr(renderer, "_screen_transform", _IDENTITY)
            renderer._collect_layer = self.order
            parent_clips = ()
        else:
            parent_clips = parents
        world = _mul(transform, _matrix(self.position, self.rotation, self.scale))
        clips = self._clip_state(world, parent_clips)
        self._world_clips = clips
        renderer._collect_clips = clips
        mark = len(cmds)
        try:
            self._collect_unclipped(cmds, renderer, transform, opacity, order)
            # Children retain their own (possibly narrower) context.
            for cmd in cmds[mark:]:
                if cmd._clips is None:
                    cmd._clips = clips
                if cmd._render_layer is None:
                    cmd._render_layer = getattr(renderer, "_collect_layer", 0)
                if cmd._sort_scope is None:
                    cmd._sort_scope = getattr(renderer, "_collect_sort_scope", ())
        finally:
            renderer._collect_clips = parents
            renderer._collect_layer = previous_layer
            renderer._collect_sort_scope = previous_scope

    def _collect_unclipped(self, cmds, renderer, transform, opacity, order):
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
                    cmd._clips = self._world_clips
                    cmd._render_layer = getattr(renderer, "_collect_layer", 0)
                    cmd._sort_scope = getattr(renderer, "_collect_sort_scope", ())
                    cmds.append(cmd)
                self._record_interaction(renderer, world, op, order[0])
                for child in self.children:
                    child._collect(cmds, renderer, world, op, order)
                return
        world = _mul(transform, _matrix((self.x, self.y), self.rotation, self.scale))
        op = opacity * self.opacity
        self._world_transform = world
        self._world_opacity = op
        mark = len(cmds)
        self._emit(cmds, renderer, world, op, order)
        for cmd in cmds[mark:]:
            cmd._clips = self._world_clips
            cmd._render_layer = getattr(renderer, "_collect_layer", 0)
            cmd._sort_scope = getattr(renderer, "_collect_sort_scope", ())
        self._cache = (self._snap(), transform, opacity, world, op, cmds[mark:], renderer.screen_scale)
        self._record_interaction(renderer, world, op, order[0])
        for child in self.children:
            child._collect(cmds, renderer, world, op, order)

    def _record_interaction(self, renderer, world, opacity, order):
        records = getattr(renderer, "_collect_interactions", None)
        if records is not None:
            scope = getattr(renderer, "_collect_sort_scope", ())
            z = -math.inf if self._stacking_context else float(self.z)
            records.append((self, world, opacity, self._world_clips, scope + ((z, order),),
                            self._bounds() is not None))

    def _bounds(self):
        return None

    def _emit(self, cmds, renderer, world, opacity, order):
        pass

    def close(self):
        if getattr(self, "_node_closing", False):
            return
        self._node_closing = True
        failure = None
        def attempt(operation):
            nonlocal failure
            try:
                operation()
                return True
            except BaseException as error:
                if failure is None:
                    failure = error
                return False
        root = self._tree_root()
        if hasattr(root, "_ensure_ui_nodes"):
            root._ui_structure_dirty = True
            root._hit_nodes_dirty = True
        router = getattr(root, "_pointer_router", None)
        if router is not None:
            attempt(lambda: router.cancel_subtree(self))
        for c in list(self.children):
            if c.parent is not self:
                continue
            try:
                close = getattr(c, 'close', None)
                if close is not None:
                    if not attempt(close):
                        attempt(lambda child=c: Node.close(child))
            finally:
                if c.parent is self:
                    c.parent = None
        self.children.clear()
        self._actions.clear()
        self.gestures.clear()
        self.physics_body = None
        Node._drop_internal_caches(self)
        self._node_closing = False
        if failure is not None:
            raise failure


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
        self._cache_anchor = None
        self._clip_basis = None
        self._rscale = 1.0
        self._dirty = True
        self._hit_snapshot = []

    def add(self, *nodes):
        super().add(*nodes)
        self._dirty = True
        return self

    def _detach_child(self, node, *, preserve_physics=False):
        self._dirty = True
        super()._detach_child(node, preserve_physics=preserve_physics)

    def clear(self):
        self._dirty = True
        return super().clear()

    def invalidate(self):
        self._dirty = True

    @staticmethod
    def _draw_transform(world, anchor):
        # Match the existing cached quad, whose extent uses average scale.
        return _mul(_matrix(_apply(world, anchor), _rot(world), _avg_scale(world)),
                    _matrix((-anchor[0], -anchor[1]), 0, 1))

    def _current_clip_basis(self):
        if self.clip is None:
            return None
        world = self._world_transform
        scale = _avg_scale(world)
        if scale == 0:
            return (0., 0., 0., 0.)
        normalized = tuple(value / scale for value in world[:4])
        return _mul(_matrix((0, 0), -_rot(world), 1), (*normalized, 0, 0))[:4]

    def _prepare_cache(self, renderer, rscale):
        basis = self._current_clip_basis()
        changed = ((basis is None) != (self._clip_basis is None) or
                   (basis is not None and self._clip_basis is not None and
                    any(not math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
                        for a, b in zip(basis, self._clip_basis))))
        if self._tex is None or self._dirty or abs(rscale - self._rscale) > 1e-3 or changed:
            self._rebuild(renderer, rscale)

    def _texture_center(self, world=None):
        if self._capture_center is not None:
            return self._capture_center
        world = self._world_transform if world is None else world
        transform = self._draw_transform(world, self._cache_anchor) if self._cache_anchor is not None else world
        return _apply(transform, self._lcenter)

    def _collect_unclipped(self, cmds, renderer, transform, opacity, order):
        if not self.visible or opacity <= 0.001:
            return
        world = _mul(transform, _matrix((self.x, self.y), self.rotation, self.scale))
        op = opacity * self.opacity
        self._world_transform = world
        self._world_opacity = op
        self._record_interaction(renderer, world, op, order[0])
        if self.children:
            ds = _avg_scale(world)
            rs = max(ds, 1.0)
            self._prepare_cache(renderer, rs)
            if self._tex and self._lsize and self._lcenter:
                center = self._texture_center(world)
                order[0] += 1
                cmds.append(Cmd(
                    self.z, order[0], KIND_TEX,
                    center[0], center[1],
                    self._lsize[0] * ds * 0.5, self._lsize[1] * ds * 0.5,
                    _rot(world),
                    (KIND_TEX, 0, 0, 0), (0, 0, op, 0),
                    (0, 0, 0, 0), (1, 1, 1, 1), self._tex,
                ))
                records = getattr(renderer, "_collect_interactions", None)
                if records is not None:
                    prefix = getattr(renderer, "_collect_sort_scope", ()) + ((float(self.z), order[0]),)
                    for node, key, has_bounds in self._project_interactions(world, op, self._world_clips, all_nodes=True):
                        records.append((node, node._world_transform, node._world_opacity,
                                        node._world_clips, prefix + key, has_bounds))

    def _project_interactions(self, world, opacity, clips, *, all_nodes=False):
        """Project the cached drawing's interaction geometry with its quad."""
        if self._tex is None or self._lsize is None or opacity <= .001:
            return []
        scale = _avg_scale(world)
        if scale <= 1e-12:
            return []
        center = self._texture_center(world)
        placement = _mul(_matrix(center, _rot(world), scale),
                         _matrix((-self._lsize[0] / 2, -self._lsize[1] / 2), 0, 1))
        inverse = _invert(placement)
        result = []
        for node, local, local_opacity, local_clips, key, has_bounds in self._hit_snapshot:
            ancestor = node
            while ancestor is not None and ancestor is not self:
                ancestor = ancestor.parent
            if ancestor is None or getattr(node, "_closed", False):
                continue
            node._world_transform = _mul(placement, local)
            node._world_opacity = opacity * local_opacity
            node._world_clips = clips + tuple((*_mul(region[:6], inverse), *region[6:]) for region in local_clips)
            # Native colliders may have been collected before reparenting or
            # capture. Their transforms do not describe this cached drawing.
            node._c_cache = None
            if all_nodes:
                result.append((node, key, has_bounds))
            elif has_bounds and node.interactive and node.visible:
                result.append((node, key))
        return result

    def _rebuild(self, renderer, rscale):
        self._capture_center = None
        self._cache_anchor = None
        self._clip_basis = self._current_clip_basis()
        bounds = _measure_children(self.children)
        if self.clip is not None and (self.clip.width == 0 or self.clip.height == 0):
            bounds = None
        if bounds is None:
            if self._tex:
                self._tex.close(); self._tex = None
            self._lsize = self._lcenter = None
            self._hit_snapshot = []
            self._rscale = rscale; self._dirty = False
            return
        x0, y0, x1, y1 = bounds
        pad = 6.0
        x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
        capture_viewport = renderer._capture_viewport
        draw_transform = None
        if self.clip is not None:
            from ._capture import _inverse

            world = self._world_transform
            if _avg_scale(world) == 0:
                x1 = x0
            else:
                anchor = ((x0 + x1) * .5, (y0 + y1) * .5)
                draw_transform = self._draw_transform(world, anchor)
                # The clip follows the full affine transform. Project it back
                # into the cached quad's coordinates before cropping its raster.
                mapped = _mul(_inverse(draw_transform), world)
                clip = self.clip
                corners = [_apply(mapped, (x, y)) for x in (clip.x, clip.x + clip.width)
                           for y in (clip.y, clip.y + clip.height)]
                x0 = max(x0, min(p[0] for p in corners) - pad)
                y0 = max(y0, min(p[1] for p in corners) - pad)
                x1 = min(x1, max(p[0] for p in corners) + pad)
                y1 = min(y1, max(p[1] for p in corners) + pad)
                self._cache_anchor = anchor
            if x1 <= x0 or y1 <= y0:
                if self._tex:
                    self._tex.close(); self._tex = None
                self._lsize = self._lcenter = None
                self._hit_snapshot = []
                self._rscale, self._dirty = rscale, False
                return
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
                if draw_transform is None:
                    anchor = ((x0 + x1) * 0.5, (y0 + y1) * 0.5)
                    draw_transform = self._draw_transform(world, anchor)
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
        previous_clips = getattr(renderer, "_collect_clips", ())
        previous_scope = getattr(renderer, "_collect_sort_scope", ())
        previous_interactions = getattr(renderer, "_collect_interactions", None)
        interactions = []
        try:
            renderer._collect_clips = ()
            renderer._collect_sort_scope = ()
            renderer._collect_interactions = interactions
            if capture_viewport is not None:
                renderer._capture_viewport = (lw, lh)
            renderer.screen_scale *= rscale
            for child in self.children:
                child._collect(sub, renderer, local_root, 1.0, order)
            sub.sort(key=lambda c: (c._render_layer or 0, (c._sort_scope or ()) + ((c.z, c.order),)))
            renderer.render(sub, clear_color=(0, 0, 0, 0), target_texture=self._tex, viewport=(lw, lh))
        finally:
            renderer.screen_scale = previous_scale
            renderer._collect_clips = previous_clips
            renderer._collect_sort_scope = previous_scope
            renderer._collect_interactions = previous_interactions
            renderer._capture_viewport = capture_viewport
        self._lcenter = ((x0 + x1) * 0.5, (y0 + y1) * 0.5)
        if capture_viewport is not None and draw_transform is not None:
            self._capture_center = _apply(draw_transform, self._lcenter)
        self._lsize = (lw, lh)
        self._rscale = rscale
        self._dirty = False
        self._hit_snapshot = interactions

    def close(self):
        self._hit_snapshot = []
        if self._tex:
            self._tex.close(); self._tex = None
        self._lsize = None
        self._lcenter = None
        self._capture_center = None
        self._cache_anchor = self._clip_basis = None
        super().close()


def _measure_children(children):
    pts = []
    for c in children:
        if not c.visible:
            continue
        m = _matrix((c.x, c.y), c.rotation, c.scale)
        boxes = []
        bounds = c._bounds()
        if bounds is not None:
            boxes.append(bounds)
        if c.children:
            cb = _measure_children(c.children)
            if cb:
                boxes.append(cb)
        if not boxes:
            continue
        if c.clip is None:
            for b in boxes:
                for point in ((b[0], b[1]), (b[2], b[1]), (b[0], b[3]), (b[2], b[3])):
                    pts.append(_apply(m, point))
            continue
        x0 = min(min(b[0], b[2]) for b in boxes)
        y0 = min(min(b[1], b[3]) for b in boxes)
        x1 = max(max(b[0], b[2]) for b in boxes)
        y1 = max(max(b[1], b[3]) for b in boxes)
        if c.clip is not None:
            clip = c.clip
            x0, y0 = max(x0, clip.x), max(y0, clip.y)
            x1, y1 = min(x1, clip.x + clip.width), min(y1, clip.y + clip.height)
            if x1 <= x0 or y1 <= y0:
                continue
        for point in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            pts.append(_apply(m, point))
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))
