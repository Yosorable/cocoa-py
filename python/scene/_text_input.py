"""Scene text controls backed by the platform's text editing system."""
from __future__ import annotations

import math
import operator

from _cocoa import _metal
from ._common import _IDENTITY, _apply, _avg_scale, _color, _matrix, _mul, _rot
from ._engine import Cmd, KIND_TEX, Texture
from ._node import Layer, Node
from ._path_node import _max_scale, _usable_scale


def _string(value):
    if not isinstance(value, str):
        raise TypeError("Text must be a string")
    value.encode("utf-8")
    return value


def _positive(value):
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 10_000_000:
        raise ValueError("Value must be finite and in (0, 10000000]")
    return value


def _nonnegative(value):
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 10_000_000:
        raise ValueError("Value must be finite and in [0, 10000000]")
    return value


def _choice(*values):
    def validate(value):
        if value not in values:
            raise ValueError(f"Expected one of {values!r}")
        return value
    return validate


def _limit(value):
    if value is None:
        return None
    value = operator.index(value)
    if value < 0 or value > 10_000_000:
        raise ValueError("max_length must be between 0 and 10000000, or None")
    return value


def _padding(value):
    if isinstance(value, (int, float)):
        return (_nonnegative(value),) * 4
    values = tuple(_nonnegative(v) for v in value)
    if len(values) != 4:
        raise ValueError("padding must be a number or (top, left, bottom, right)")
    return values


def _input_color(value):
    if not isinstance(value, str):
        value = tuple(float(v) for v in value)
        if not all(math.isfinite(v) for v in value):
            raise ValueError("Color components must be finite")
    color = _color(value)
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in color):
        raise ValueError("Color components must be finite and in 0..1")
    return color


class _Option:
    def __init__(self, validate):
        self.validate = validate

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner):
        return self if instance is None else instance._options[self.name]

    def __set__(self, instance, value):
        value = self.validate(value)
        if self.name == "submit_behavior" and value == "newline" and not instance.multiline:
            raise ValueError("The newline submit behavior requires multiline input")
        if instance._options.get(self.name, object()) != value:
            instance._options[self.name] = value
            instance._config_revision += 1
            instance._changed()
            if self.name == "max_length":
                instance.text = instance._text


class _TextInputState:
    """Common plain-text model, native editor ownership and scene rendering."""

    width = _Option(_positive)
    height = _Option(_positive)
    placeholder = _Option(_string)
    font_size = _Option(_positive)
    font = _Option(lambda v: None if v is None else _string(v))
    text_color = _Option(_input_color)
    placeholder_color = _Option(_input_color)
    background = _Option(_input_color)
    border_color = _Option(_input_color)
    border_width = _Option(_nonnegative)
    corner_radius = _Option(_nonnegative)
    padding = _Option(_padding)
    alignment = _Option(_choice("natural", "left", "center", "right"))
    enabled = _Option(bool)
    read_only = _Option(bool)
    max_length = _Option(_limit)
    keyboard_type = _Option(_choice("default", "ascii", "number", "decimal", "phone", "email", "url"))
    return_key = _Option(_choice("default", "done", "go", "search", "send", "next"))
    autocapitalization = _Option(_choice("none", "sentences", "words", "all"))
    autocorrection = _Option(bool)
    spell_check = _Option(bool)
    content_type = _Option(_choice(None, "name", "username", "password", "new_password", "one_time_code", "email", "telephone", "url"))
    keyboard_appearance = _Option(_choice("default", "light", "dark"))
    avoid_keyboard = _Option(bool)
    select_all_on_focus = _Option(bool)
    submit_behavior = _Option(_choice("blur", "stay", "next", "newline"))

    def __init__(self, width, height, text="", *, multiline, secure=False,
                 placeholder="", font_size=18, font=None,
                 text_color="#17202a", placeholder_color="#78818b",
                 background="#ffffff", border_color="#aab2bd", border_width=1,
                 corner_radius=8, padding=10, alignment="natural", enabled=True,
                 read_only=False, max_length=None, keyboard_type="default",
                 return_key="done", autocapitalization="sentences", autocorrection=True,
                 spell_check=True, content_type=None, keyboard_appearance="default",
                 avoid_keyboard=True, select_all_on_focus=False, submit_behavior="blur",
                 on_change=None, on_submit=None, on_focus=None, on_blur=None,
                 on_selection_change=None, **kw):
        self._options = {}
        self._revision = 0
        self._config_revision = 0
        self._native = None
        self._input_scene = None
        self._native_revision = -1
        self._write_revision = -1
        self._native_frame = None
        self._sent_revision = -1
        self._focused = False
        self._marked_range = None
        self._text = ""
        self._selection = (0, 0)
        self._pending_focus = None
        self._snapshot_texture = None
        self._snapshot_key = None
        self._snapshot_scale = 0.0
        self._closed = False
        self._multiline = bool(multiline)
        self._secure = bool(secure)
        if self._secure and self._multiline:
            raise ValueError("Secure entry requires single-line input")
        super().__init__(**kw)
        values = locals()
        for name, option in vars(_TextInputState).items():
            if isinstance(option, _Option):
                setattr(self, name, values[name])
        for name in ("on_change", "on_submit", "on_focus", "on_blur", "on_selection_change"):
            callback = values[name]
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} must be callable or None")
            setattr(self, name, callback)
        self.text = text

    @property
    def multiline(self):
        return self._multiline

    @property
    def secure(self):
        return self._secure

    @property
    def text(self):
        return self._text

    @text.setter
    def text(self, value):
        value = _metal.text_input_normalize(_string(value), self.multiline, self._options.get("max_length"))
        self._text = value
        self._selection = tuple(min(i, len(value)) for i in self._selection)
        self._marked_range = None
        self._changed()
        if self._native is not None:
            self._push({"text": value, "selection": self._selection})

    @property
    def selection(self):
        """Half-open selection range in Python Unicode code-point indices."""
        return self._selection

    @selection.setter
    def selection(self, value):
        start, end = (operator.index(v) for v in value)
        if not 0 <= start <= end <= len(self._text):
            raise ValueError("selection must satisfy 0 <= start <= end <= len(text)")
        self._selection = (start, end)
        if self._native is not None:
            self._push({"selection": self._selection})

    @property
    def marked_range(self):
        return self._marked_range

    @property
    def is_composing(self):
        return self._marked_range is not None

    @property
    def focused(self):
        return self._focused

    def focus(self, *, select_all=None):
        """Request editing after the control is attached to a running scene."""
        if self._closed:
            raise RuntimeError("Text input is closed")
        self._pending_focus = self.select_all_on_focus if select_all is None else bool(select_all)
        root = self._tree_root()
        manager = getattr(root, "_focus_manager", None)
        if manager is not None and self.enabled and not self.read_only:
            if not isinstance(self, Node) or manager.eligible(self):
                manager.clear()
        return self

    def blur(self):
        self._pending_focus = None
        if self._native is not None:
            self._command("blur")
        return self

    def select_all(self):
        self.selection = (0, len(self.text))
        return self

    def replace_selection(self, text):
        """Replace the selection; native editing preserves the undo history."""
        text = _string(text)
        if self.read_only or not self.enabled:
            raise RuntimeError("Text input is not editable")
        if self._native is not None:
            self._command("insert", text)
        else:
            text = _metal.text_input_normalize(text, self.multiline, None)
            start, end = self.selection
            self.text = self.text[:start] + text + self.text[end:]
            caret = min(start + len(text), len(self.text))
            self.selection = (caret, caret)
        return self

    def undo(self):
        return self._command("undo")

    def redo(self):
        return self._command("redo")

    def _command(self, name, value=None):
        if self._native is None:
            if name == "blur":
                return self
            raise RuntimeError("Text input must be attached to a running scene")
        if self._sent_revision != self._config_revision:
            self._push(self._options)
        self._accept_state(_metal.text_input_command(self._native, name, value))
        return self

    def _changed(self):
        self._revision += 1
        parent = getattr(self, "parent", None)
        while parent is not None:
            if isinstance(parent, Layer):
                parent.invalidate()
            parent = parent.parent

    def _configuration(self):
        return dict(self._options, text=self._text, selection=self._selection,
                    multiline=self.multiline, secure=self.secure)

    def _ensure_native(self):
        root = self._tree_root()
        window = getattr(root, "_window", None)
        if window is None or window.handle is None or getattr(root, "_renderer", None) is None:
            raise RuntimeError("Text input requires a running scene")
        if self._input_scene is not root:
            self._detach_text_input()
        if self._native is None:
            self._native = _metal.text_input_create(window.handle, self._configuration(),
                                                   isinstance(self, TextInputSession))
            self._input_scene = root
            root._text_inputs[self._native] = self
            self._sent_revision = self._config_revision
        elif self._sent_revision != self._config_revision:
            self._push(self._options)
        return self._native

    def _push(self, changes):
        writes_text = "text" in changes or "selection" in changes
        if self._sent_revision != self._config_revision:
            changes = dict(self._options, **changes)
        state = _metal.text_input_update(self._native, changes)
        if writes_text:
            self._write_revision = state["revision"]
        self._accept_state(state)
        self._sent_revision = self._config_revision

    def _accept_state(self, state):
        revision = state["revision"]
        if revision < self._native_revision:
            return
        before = (self._text, self._selection, self._marked_range, self._focused)
        self._native_revision = revision
        self._text = state["text"]
        self._selection = tuple(state["selection"])
        self._marked_range = None if state["marked_range"] is None else tuple(state["marked_range"])
        self._focused = bool(state["focused"])
        if self._focused and self._input_scene is not None:
            self._input_scene._focus_manager.clear()
        if before != (self._text, self._selection, self._marked_range, self._focused):
            self._changed()

    def _event(self, event):
        if self._closed:
            return
        stale = event["revision"] < self._write_revision
        self._accept_state(event)
        kind = event["kind"]
        if kind in ("change", "selection") and stale:
            return
        if kind == "focus" and self._input_scene is not None:
            self._input_scene._focus_manager.dispatch()
        callback = getattr(self, {"change": "on_change", "selection": "on_selection_change",
                                 "focus": "on_focus", "blur": "on_blur", "submit": "on_submit"}.get(kind, ""), None)
        if callback is not None:
            callback(self)
        if kind in ("next", "previous") or (kind == "submit" and self.submit_behavior == "next"):
            self._navigate_input(reverse=kind == "previous")

    def _navigate_input(self, *, reverse=False):
        if self._input_scene is not None:
            self._input_scene.focus_next(self, reverse=reverse)

    def on_touch_began(self, touch):
        if self.enabled and not self._closed:
            self.focus()

    def _bounds(self):
        if self._closed:
            return None
        return (-self.width / 2, -self.height / 2, self.width / 2, self.height / 2)

    def _snap(self):
        return (self.x, self.y, self.rotation, self.scale, self.opacity, self.z,
                self._revision, self._focused, self._snapshot_texture._handle if self._snapshot_texture else None)

    def _emit(self, cmds, renderer, world, opacity, order):
        if self._closed:
            return
        self._ensure_native()
        if self.focused and not getattr(renderer, "_capturing", False) and not getattr(renderer, "_text_input_offscreen", False):
            return
        scale = renderer.screen_scale * _max_scale(world)
        if not math.isfinite(scale):
            raise ValueError("Text input scale must be finite")
        if scale <= 0:
            return
        key = (self._revision, self._native_revision)
        if self._snapshot_texture is None or self._snapshot_key != key or not _usable_scale(self._snapshot_scale, scale):
            result = _metal.text_input_snapshot(self._native, scale)
            texture = Texture(result["handle"], result["size"])
            if self._snapshot_texture is not None:
                self._snapshot_texture.close()
            self._snapshot_texture = texture
            self._snapshot_key = key
            self._snapshot_scale = scale
        center = _apply(world, (0, 0))
        sx, sy = math.hypot(world[0], world[1]), math.hypot(world[2], world[3])
        order[0] += 1
        cmds.append(Cmd(self.z, order[0], KIND_TEX, center[0], center[1],
                        self.width * sx / 2, self.height * sy / 2, _rot(world),
                        (KIND_TEX, 0, 0, 0), (0, 0, opacity, 0), (0, 0, 0, 0),
                        (1, 1, 1, 1), self._snapshot_texture))

    def _detach_text_input(self):
        if self._native is not None:
            try:
                self._accept_state(_metal.text_input_command(self._native, "blur"))
            except KeyError:
                pass  # The window may already have released its editors.
            if self._input_scene is not None:
                self._input_scene._text_inputs.pop(self._native, None)
            _metal.text_input_close(self._native)
        self._native = None
        self._input_scene = None
        self._native_revision = -1
        self._write_revision = -1
        self._native_frame = None
        self._sent_revision = -1
        self._focused = False
        self._marked_range = None
        if self._snapshot_texture is not None:
            self._snapshot_texture.close()
            self._snapshot_texture = None
        self._snapshot_key = None

    def close(self):
        self._detach_text_input()
        self._closed = True
        self.interactive = False
        self._changed()
        self._pending_focus = None
        super().close()


class _TextInput(_TextInputState, Node):
    """A visible scene node using the shared native editing state."""


def _caret_rect(value):
    x, y, width, height = (float(v) for v in value)
    if not all(math.isfinite(v) and abs(v) <= 10_000_000 for v in (x, y, width, height)):
        raise ValueError("caret_rect must have finite coordinates within 10000000 points")
    if width <= 0 or height <= 0:
        raise ValueError("caret_rect width and height must be positive")
    return x, y, width, height


class TextInputSession(_TextInputState):
    """Native text input for an application-drawn interface, without a scene node.

    Call begin(scene) to request input and end() to finish editing. The scene
    owns native resources until close() or scene shutdown. caret_rect is the
    caret's rectangle in window points, used to position input-method UI.
    Text, selection, composition, callbacks and keyboard options follow the
    same rules as TextField/TextView. Drawing and pointer selection belong to
    the application. Call these APIs from the scene-loop thread.
    """

    def __init__(self, text="", *, multiline=False, secure=False,
                 caret_rect=(0, 0, 1, 20), on_next=None, on_previous=None, **options):
        allowed = {"enabled", "read_only", "max_length", "keyboard_type", "return_key",
                   "autocapitalization", "autocorrection", "spell_check", "content_type",
                   "keyboard_appearance", "select_all_on_focus", "submit_behavior",
                   "on_change", "on_submit", "on_focus", "on_blur", "on_selection_change"}
        unknown = options.keys() - allowed
        if unknown:
            raise TypeError(f"Unknown TextInputSession options: {', '.join(sorted(unknown))}")
        for callback in (on_next, on_previous):
            if callback is not None and not callable(callback):
                raise TypeError("Navigation callbacks must be callable or None")
        self._bound_scene = None
        self._caret_rect = _caret_rect(caret_rect)
        self._sent_caret_rect = None
        self.on_next, self.on_previous = on_next, on_previous
        options.setdefault("submit_behavior", "newline" if multiline else "blur")
        options.setdefault("return_key", "default" if multiline else "done")
        super().__init__(256, 96, text, multiline=multiline, secure=secure,
                         padding=0, border_width=0, background=(0, 0, 0, 0),
                         avoid_keyboard=False, **options)

    @property
    def active(self):
        """Whether this session currently owns native text input focus."""
        return self.focused

    @property
    def caret_rect(self):
        return self._caret_rect

    @caret_rect.setter
    def caret_rect(self, value):
        self._caret_rect = _caret_rect(value)

    def begin(self, scene, *, select_all=None):
        """Request input on the next onscreen frame; return this session."""
        from ._scene import Scene
        if self._closed:
            raise RuntimeError("Text input session is closed")
        if not isinstance(scene, Scene):
            raise TypeError("begin() requires a Scene")
        window = getattr(scene, "_window", None)
        if window is None or window.handle is None or getattr(scene, "_renderer", None) is None:
            raise RuntimeError("Text input session requires a running scene")
        self._bound_scene = scene
        self._ensure_native()
        self.focus(select_all=select_all)
        return self

    def end(self):
        """Commit composition and end editing, preserving text and native history."""
        return self.blur()

    def commit_composition(self):
        """Confirm provisional text while keeping input active; return this session."""
        return self._command("commit") if self._native is not None else self

    def _tree_root(self):
        return self._bound_scene

    def _navigate_input(self, *, reverse=False):
        callback = self.on_previous if reverse else self.on_next
        if callback is not None:
            callback(self)

    def _sync_session(self, visible):
        self._ensure_native()
        rect = self.caret_rect
        if rect != self._sent_caret_rect:
            _metal.text_input_anchor(self._native, rect)
            self._sent_caret_rect = rect
        x, y, _, _ = rect
        world = (1.0, 0.0, 0.0, 1.0, x, y)
        frame = (world, bool(visible and self.enabled), self._sent_revision)
        if frame != self._native_frame:
            _metal.text_input_frame(self._native, world, 1.0, bool(visible and self.enabled))
            self._native_frame = frame
        if self._pending_focus is not None and visible and self.enabled:
            select_all = self._pending_focus
            self._pending_focus = None
            self._command("focus", select_all)
            _refresh_focus(self._input_scene, self)

    def _detach_text_input(self):
        super()._detach_text_input()
        self._sent_caret_rect = None

    def close(self):
        self._detach_text_input()
        self._bound_scene = None
        self._closed = True
        self._pending_focus = None


class TextField(_TextInput):
    """Single-line text entry with native input methods and optional secure entry."""
    def __init__(self, width=240, height=44, text="", **kwargs):
        super().__init__(width, height, text, multiline=False, **kwargs)


class TextView(_TextInput):
    """Scrollable multiline plain-text entry using the system text editor."""
    def __init__(self, width=320, height=160, text="", **kwargs):
        kwargs.setdefault("submit_behavior", "newline")
        kwargs.setdefault("return_key", "default")
        super().__init__(width, height, text, multiline=True, **kwargs)


def _input_nodes(root):
    result = []
    root._ensure_ui_nodes()
    for node in root._input_control_nodes:
        if node._tree_root() is not root:
            continue
        world, opacity, visible, clips = node._current_geometry()
        node._input_clips = clips
        result.append((node, world, opacity, visible))
    return result


def sync_inputs(root, *, visible=True):
    if not root._text_inputs:
        return
    for node, world, opacity, shown in _input_nodes(root):
        if node._closed:
            continue
        node._ensure_native()
        # Match the renderer's textured quad decomposition, including nested
        # non-uniform transforms. Native editing and snapshots occupy one rect.
        world = _matrix(_apply(world, (0, 0)), _rot(world),
                        (math.hypot(world[0], world[1]), math.hypot(world[2], world[3])))
        clips = node._input_clips
        parent = node.parent
        managed = False
        while parent is not None:
            if getattr(parent, "_is_scroll_view", False) and parent.direction != "horizontal":
                managed = node.avoid_keyboard
                break
            parent = parent.parent
        frame = (world, opacity, bool(visible and shown and node.enabled), clips, node._sent_revision, managed)
        if frame != getattr(node, "_native_frame", None):
            _metal.text_input_frame(node._native, world, opacity, bool(visible and shown and node.enabled), clips, managed)
            node._native_frame = frame
        if node._pending_focus is not None and visible and shown and node.enabled:
            select_all = node._pending_focus
            node._pending_focus = None
            node._command("focus", select_all)
            _refresh_focus(root, node)
    for session in list(root._text_inputs.values()):
        if isinstance(session, TextInputSession) and not session._closed:
            session._sync_session(visible)


def _refresh_focus(root, current):
    for previous in root._text_inputs.values():
        if previous is not current and previous.focused:
            previous._accept_state(_metal.text_input_state(previous._native))


def suspend_inputs(root):
    for node in list(root._text_inputs.values()):
        node.blur()
        node._native_frame = None


def process_inputs(root):
    # Events are consumed per control, so transition scenes sharing one window
    # cannot steal each other's queued edits or submit events.
    for handle, node in list(root._text_inputs.items()):
        if node._native != handle:
            continue
        for event in _metal.text_input_events(handle):
            if node._native != handle:
                break
            node._event(event)
