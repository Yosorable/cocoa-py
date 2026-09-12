"""scene._scene — Scene, _SceneDirector, and run()."""
from __future__ import annotations

import time

from _cocoa import _scene_accel
from _cocoa import _metal
from . import gesture as gesture_module
from ._common import (
    _IDENTITY, CollisionInfo, Orientation, Touch, _PHASES, _apply, _color,
    _invert,
)
from ._collision import _collider_to_tuple, _SpatialHash
from ._camera import Camera
from ._engine import Renderer, Texture, Window, normalize_color
from ._node import Node


class Scene(Node):
    """Base class for scenes. Subclass and override setup/update/touch methods.

    Scene IS the root node of the scene graph.  All Node capabilities
    (position, rotation, scale, opacity, actions, children) are available
    directly on the scene instance.  Setting ``self.position`` or
    ``self.scale`` transforms the entire scene (simple camera).
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.camera = Camera(self)
        self._physics_world: PhysicsWorld | None = None

    def _ensure_bootstrap_state(self):
        """Ensure core scene state exists without resetting user scene content."""
        if not hasattr(self, 'children'):
            Node.__init__(self)
        if not hasattr(self, 'camera') or self.camera is None:
            self.camera = Camera(self)
        else:
            self.camera._scene = self
        if not hasattr(self, '_physics_world'):
            self._physics_world = None

    @property
    def physics_world(self):
        """Lazily-created physics world for this scene."""
        if self._physics_world is None:
            from ._physics import PhysicsWorld
            self._physics_world = PhysicsWorld()
        return self._physics_world

    def setup(self):
        pass

    def stop(self):
        pass

    def update(self, dt: float):
        pass

    def resize(self, width: float, height: float):
        pass

    def touch_began(self, touch: Touch):
        pass

    def touch_moved(self, touch: Touch):
        pass

    def touch_ended(self, touch: Touch):
        pass

    def keyboard_changed(self, frame):
        """The software keyboard's screen-space rectangle changed (or is None)."""
        pass

    @property
    def keyboard_frame(self):
        if not getattr(self, "_text_inputs", None):
            return None
        return _metal.text_input_keyboard(self._window.handle)

    @property
    def focused_input(self):
        return next((node for node in getattr(self, "_text_inputs", {}).values() if node.focused), None)

    def dismiss_keyboard(self):
        for node in list(getattr(self, "_text_inputs", {}).values()):
            node.blur()

    def focus_next_input(self, current=None, *, reverse=False):
        from ._text_input import _input_nodes

        nodes = [node for node, _, _, visible in _input_nodes(self)
                 if visible and node.enabled and not node.read_only and not node._closed]
        if not nodes:
            return None
        current = self.focused_input if current is None else current
        step = -1 if reverse else 1
        index = nodes.index(current) if current in nodes else (0 if reverse else -1)
        target = nodes[(index + step) % len(nodes)]
        target.focus()
        return target

    # ── built-in ──

    @property
    def width(self):
        return self._window.size[0]

    @property
    def height(self):
        return self._window.size[1]

    @property
    def size(self):
        return self._window.size

    @property
    def center(self):
        s = self._window.size
        return (s[0] / 2, s[1] / 2)

    @property
    def safe_area(self):
        """Safe area insets: (top, left, bottom, right) in points."""
        return self._window._metrics.get('safe_area', (0, 0, 0, 0))

    @property
    def fps(self):
        return self._fps

    @fps.setter
    def fps(self, value):
        target = max(1, int(value))
        if target == self._fps:
            return
        self._fps = int(_metal.set_target_fps(self._window._handle, target))

    # add / remove / clear / run_action / remove_action — inherited from Node

    # ── hit testing ──

    def hit_test(self, x, y):
        """Return the frontmost interactive node at (x, y), or None.
        Nodes with passthrough=True are skipped."""
        return _scene_accel.hit_test(self._interactive_nodes, x, y)

    def hit_test_all(self, x, y):
        """Return all interactive nodes at (x, y), front-to-back."""
        return _scene_accel.hit_test_all(self._interactive_nodes, x, y)

    # ── collision ──

    def collisions(self, group_a, group_b):
        """Test all pairs between two groups. Returns [(node_a, node_b, CollisionInfo)]."""
        return _scene_accel.collisions(list(group_a), list(group_b))

    # ── internal ──

    def _init(self, window, background, renderer=None):
        self._ensure_bootstrap_state()
        self._window = window
        self._renderer = renderer if renderer is not None else Renderer(window)
        self._owns_renderer = renderer is None
        self.background = normalize_color(background)
        self.dt = 0.0
        self.frame = 0
        self.elapsed = 0.0
        self._fps = 60
        self._prev_size = None
        self._interactive_nodes: list[Node] = []
        self._render_fingerprint = 0
        self._render_background = None
        self._render_metrics_key = None
        self._touch_owners: dict[int, Node] = {}
        self._gesture_nodes: set = set()
        self._text_inputs = {}
        self._keyboard_frame = None
        self._text_input_offscreen = False
        self._director = None

    def _frame(self, dt):
        self._renderer._begin_onscreen_slot()
        try:
            self._tick_frame(dt)
            self._render()
            self._dispatch_frame_touches(dt)
        except Exception:
            self._renderer._abort_onscreen_slot()
            raise

    def _tick_frame(self, dt):
        """Advance time, actions, camera, user update — no rendering."""
        scene_dt = dt * self.speed
        self.dt = scene_dt
        self.elapsed += scene_dt
        self.frame += 1
        self._window.sync()

        from ._text_input import process_inputs
        process_inputs(self)
        keyboard = self.keyboard_frame
        if keyboard != self._keyboard_frame:
            self._keyboard_frame = keyboard
            self.keyboard_changed(keyboard)

        sz = self._window.size
        if sz != self._prev_size:
            self._prev_size = sz
            self.resize(sz[0], sz[1])

        self._tick_self(scene_dt)
        for c in list(self.children):
            c._tick(scene_dt)
        self.camera._update(scene_dt)

        # Physics step (before user update so positions are fresh)
        if self._physics_world is not None:
            self._physics_world.step(scene_dt)

        self.update(scene_dt)

    def _dispatch_frame_touches(self, dt):
        """Process touch events and gestures for this frame."""
        self._process_touches()
        for n in list(self._gesture_nodes):
            gesture_dt = dt * n.time_scale
            for g in n.gestures:
                g.tick(gesture_dt)
            self._prune_gesture_node(n)

    def _camera_root_transform(self):
        """Compute the root transform combining camera with Scene's own Node transform."""
        cam = self.camera._transform(self.width, self.height)
        return cam

    def _render(self, target_texture=None):
        from ._text_input import sync_inputs

        self._renderer.screen_scale = self._window.scale
        self._renderer._text_input_offscreen = target_texture is not None
        if self._text_input_offscreen != (target_texture is not None):
            self._text_input_offscreen = target_texture is not None
            for control in self._text_inputs.values():
                control._changed()
        if target_texture is None:
            self._renderer._begin_onscreen_slot()
        root_tf = self._camera_root_transform()
        metrics_key = (getattr(self._window, '_rev', 0), self._window.size, self._window.scale)
        allow_static_skip = (
            target_texture is None
            and self.background == self._render_background
            and metrics_key == self._render_metrics_key
        )
        previous_fingerprint = self._render_fingerprint if allow_static_skip else 0
        try:
            result = _scene_accel.collect(
                self, root_tf, 1.0, self._renderer.screen_scale, self._renderer,
                previous_fingerprint)
        except Exception:
            if target_texture is None:
                self._renderer._abort_onscreen_slot()
            raise
        if len(result) == 2 and result[0] is None:
            self._render_fingerprint = int(result[1])
            if target_texture is None:
                self._renderer._abort_onscreen_slot()
                sync_inputs(self)
            return

        try:
            fingerprint = 0
            if len(result) in (6, 9):
                fingerprint = int(result[-1])
                result = result[:-1]

            if len(result) >= 8:
                vb, qb, count, batches, interactive, mesh_vb, mesh_ib, mesh_batches = result
                self._renderer.render_packed(vb, qb, count, batches, clear_color=self.background,
                                             mesh_vb=mesh_vb, mesh_ib=mesh_ib, mesh_batches=mesh_batches,
                                             target_texture=target_texture)
                self._interactive_nodes = interactive
            elif len(result) >= 5:
                vb, qb, count, batches, interactive = result
                self._renderer.render_packed(vb, qb, count, batches, clear_color=self.background,
                                             target_texture=target_texture)
                self._interactive_nodes = interactive
            else:
                self._renderer.render_packed(*result, clear_color=self.background,
                                             target_texture=target_texture)
            if target_texture is None:
                self._render_fingerprint = fingerprint
                self._render_background = self.background
                self._render_metrics_key = metrics_key
        except Exception:
            if target_texture is None:
                self._renderer._abort_onscreen_slot()
            raise
        if target_texture is None:
            sync_inputs(self)

    def _process_touches(self):
        for e in self._window.consume_touches():
            phase = _PHASES[e["phase"]] if e["phase"] < 4 else _PHASES[3]
            # Touches stay in screen space — _world_transform includes camera,
            # so hit_test and contains_point work correctly in screen space.
            # Use camera.screen_to_world() for world coordinates in game logic.
            t = Touch(e["id"], (e["x"], e["y"]), (e["prev_x"], e["prev_y"]), phase)

            if phase == "began":
                node = self.hit_test(t.position[0], t.position[1])
                from ._text_input import _TextInput
                input_node = node
                while input_node is not None and not isinstance(input_node, _TextInput):
                    input_node = input_node.parent
                if self._text_inputs and (input_node is None or not input_node.enabled):
                    for control in list(self._text_inputs.values()):
                        if isinstance(control, _TextInput):
                            control.blur()
                owner = self._dispatch_touch(node, 'on_touch_began', t)
                if owner is not None:
                    self._touch_owners[t.id] = owner
                else:
                    self.touch_began(t)
            elif phase == "moved":
                owner = self._touch_owners.get(t.id)
                if owner is not None:
                    self._call_handler(owner, 'on_touch_moved', t)
                    self._prune_gesture_node(owner)
                else:
                    self.touch_moved(t)
            elif phase == "cancelled":
                owner = self._touch_owners.pop(t.id, None)
                if owner is not None:
                    self._call_handler(owner, 'on_touch_cancelled', t)
                    self._prune_gesture_node(owner)
                    # fallback to on_touch_ended if no cancelled handler
                else:
                    self.touch_ended(t)
            else:  # ended
                owner = self._touch_owners.pop(t.id, None)
                if owner is not None:
                    self._call_handler(owner, 'on_touch_ended', t)
                    self._prune_gesture_node(owner)
                else:
                    self.touch_ended(t)

    def _dispatch_touch(self, node, method, touch):
        """Walk up from node, find first handler. Returns handling node or None."""
        cur = node
        while cur is not None and cur is not self:
            # Check gestures first
            if cur.gestures:
                gesture_method = {'on_touch_began': 'touch_began',
                                  'on_touch_moved': 'touch_moved',
                                  'on_touch_ended': 'touch_ended'}
                gm = gesture_method.get(method)
                if gm:
                    for g in cur.gestures:
                        g._node = cur
                        if getattr(g, gm)(touch):
                            self._gesture_nodes.add(cur)
                            return cur
            # Then check node-level handlers
            handler = getattr(cur, method, None)
            if handler is not None:
                handler(touch)
                return cur
            cur = cur.parent
        return None

    def _call_handler(self, node, method, touch):
        # For captured touches, also route through gestures
        if node.gestures:
            gesture_method = {'on_touch_moved': 'touch_moved',
                              'on_touch_ended': 'touch_ended',
                              'on_touch_cancelled': 'touch_cancelled'}
            gm = gesture_method.get(method)
            if gm:
                for g in node.gestures:
                    g._node = node
                    if getattr(g, gm)(touch):
                        return
        handler = getattr(node, method, None)
        if handler is not None:
            handler(touch)
        elif method == 'on_touch_cancelled':
            handler = getattr(node, 'on_touch_ended', None)
            if handler is not None:
                handler(touch)

    def _node_has_active_gestures(self, node):
        return any(getattr(g, 'is_active', False) for g in node.gestures)

    def _prune_gesture_node(self, node):
        if node in self._gesture_nodes and not self._node_has_active_gestures(node):
            self._gesture_nodes.discard(node)

    def present(self, scene_or_class, transition=None):
        """Request a new scene, applied at the start of the next frame.

        scene_or_class: Scene subclass or instance.
        transition: a transition object from scene.transition (e.g. transition.fade(0.5)),
                    or None for a switch without animation.
        """
        if not hasattr(self, '_director') or self._director is None:
            raise RuntimeError("present() can only be called on a running scene")
        self._director._present(scene_or_class, transition)

    def _close(self):
        for handle, node in list(self._text_inputs.items()):
            try:
                node.blur()
            except KeyError:
                pass
        try:
            self.stop()
        except Exception:
            import traceback
            traceback.print_exc()
        # Destroy physics world before clearing children
        if self._physics_world is not None:
            self._physics_world.destroy()
            self._physics_world = None
        Node.close(self)
        # Input sessions are intentionally outside the drawable node tree.
        for session in list(self._text_inputs.values()):
            session.close()
        self._interactive_nodes = []
        self._touch_owners.clear()
        self._gesture_nodes.clear()
        self._render_fingerprint = 0
        self._render_background = None
        self._render_metrics_key = None
        if getattr(self, '_renderer', None) is not None and getattr(self, '_owns_renderer', True):
            self._renderer.close()
        self._renderer = None


# ───────────────────────────────────────────────────────────────────────────
# _SceneDirector — manages scene lifecycle and transitions
# ───────────────────────────────────────────────────────────────────────────

class _SceneDirector:
    """Internal: orchestrates scene switching and transition animations."""

    def __init__(self, window, background, fps):
        self._window = window
        self._background = background
        self._fps = fps
        self._renderer = Renderer(window)
        self._current = None
        self._incoming = None
        self._transition = None
        self._transition_elapsed = 0.0
        self._pending_present = None
        self._tex_old = None
        self._tex_new = None
        self._tex_size = None

    def _init_scene(self, scene_or_class):
        """Create and initialize a scene instance."""
        if isinstance(scene_or_class, Scene):
            instance = scene_or_class
        else:
            instance = scene_or_class()
        instance._init(self._window, self._background, self._renderer)
        instance._director = self
        instance.fps = self._fps
        return instance

    def _setup_first(self, scene_or_class):
        """Initialize the first scene. setup() is deferred to the first frame."""
        self._current = self._init_scene(scene_or_class)
        self._window.sync()
        self._needs_setup = True

    def _present(self, scene_or_class, transition=None):
        """Queue replacement so callbacks can safely finish the current frame."""
        if self._incoming is not None or self._pending_present is not None:
            return
        if scene_or_class is self._current:
            raise ValueError("cannot present the current scene again")
        self._pending_present = (scene_or_class, transition)

    def _apply_present(self, scene_or_class, transition):
        if scene_or_class is self._current:
            raise ValueError("cannot present the current scene again")
        incoming = self._init_scene(scene_or_class)
        try:
            self._window.sync()
            incoming.setup()
        except BaseException:
            incoming._director = None
            incoming._close()
            raise
        if transition is None:
            # Immediate switch
            old = self._current
            self._current = incoming
            old._director = None
            old._close()
        else:
            from ._text_input import suspend_inputs
            suspend_inputs(self._current)
            self._incoming = incoming
            self._transition = transition
            self._transition_elapsed = 0.0
            self._ensure_textures()

    def _ensure_textures(self):
        """Create or resize off-screen textures for transition."""
        w, h = self._window.size
        scale = self._window.scale
        pw, ph = int(w * scale), int(h * scale)
        if self._tex_size == (pw, ph):
            return
        self._release_textures()
        self._tex_old = Texture.render_target(pw, ph)
        self._tex_new = Texture.render_target(pw, ph)
        self._tex_size = (pw, ph)

    def _release_textures(self):
        if self._tex_old is not None:
            self._tex_old.close()
            self._tex_old = None
        if self._tex_new is not None:
            self._tex_new.close()
            self._tex_new = None
        self._tex_size = None

    def _frame(self, dt):
        """One frame: tick scenes, render, process input."""
        if self._pending_present is not None and self._incoming is None:
            request = self._pending_present
            self._pending_present = None
            self._apply_present(*request)
        if self._needs_setup:
            self._needs_setup = False
            self._current.setup()
        if self._incoming is not None:
            self._frame_transition(dt)
        else:
            self._current._frame(dt)

    def _frame_transition(self, dt):
        """Frame during active transition."""
        self._transition_elapsed += dt
        progress = min(1.0, self._transition_elapsed / self._transition.duration)
        self._renderer._begin_onscreen_slot()
        try:
            # Ensure textures match current window size
            self._ensure_textures()

            # Tick both scenes (actions, update, camera)
            self._current._tick_frame(dt)
            self._incoming._tick_frame(dt)

            # Render each scene to its off-screen texture
            self._current._render(target_texture=self._tex_old)
            self._incoming._render(target_texture=self._tex_new)

            # Compose to screen
            renderer = self._current._renderer
            self._transition.compose(progress, self._tex_old, self._tex_new,
                                     renderer, self._window)
        except Exception:
            self._renderer._abort_onscreen_slot()
            raise

        # Touches go to the incoming scene only
        self._incoming._dispatch_frame_touches(dt)

        # Transition complete?
        if progress >= 1.0:
            self._finish_transition()

    def _finish_transition(self):
        """Complete transition: swap scenes, clean up."""
        old = self._current
        self._current = self._incoming
        self._incoming = None
        self._transition = None
        self._transition_elapsed = 0.0
        self._release_textures()
        old._director = None
        old._close()

    def _close(self):
        """Clean up all scenes and resources."""
        self._pending_present = None
        if self._incoming is not None:
            self._incoming._director = None
            self._incoming._close()
            self._incoming = None
        self._release_textures()
        if self._current is not None:
            self._current._director = None
            self._current._close()
            self._current = None
        if self._renderer is not None:
            self._renderer.close()
            self._renderer = None


# ───────────────────────────────────────────────────────────────────────────
# run()
# ───────────────────────────────────────────────────────────────────────────

def run(scene_class, *, title="Scene", background="#000000", fps=60,
        orientation: Orientation | str = Orientation.AUTO):
    """Launch a scene. Pass a Scene subclass or an already-created Scene instance.

    orientation controls the scene window's interface orientation. Accepts an
    :class:`Orientation` member or its string value:
      - ``Orientation.AUTO``      / ``"auto"``      : follow device / app default
      - ``Orientation.PORTRAIT``  / ``"portrait"``  : lock to portrait
      - ``Orientation.LANDSCAPE`` / ``"landscape"`` : lock to landscape

    The lock is released automatically when the window closes.
    """
    orientation = Orientation(orientation)
    window = Window(title, background, orientation=orientation)
    director = _SceneDirector(window, background, fps)

    try:
        director._setup_first(scene_class)

        # Matches Unity's Time.maximumDeltaTime default (1/3s). Prevents a
        # giant dt from blowing up particles / physics after the app returns
        # from the background, where CADisplayLink has been paused.
        MAX_DT = 1.0 / 3.0

        prev = time.perf_counter()
        while True:
            now = time.perf_counter()
            dt = min(now - prev, MAX_DT)
            prev = now

            actions = window.consume_actions()
            if actions.get("close", 0) > 0:
                break

            director._frame(dt)
            _metal.vsync(window._handle)
    finally:
        director._close()
        window.close()
