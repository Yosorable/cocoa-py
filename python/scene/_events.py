"""Window lifecycle and hardware key routing, consumed on the scene thread."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
import time


@dataclass(frozen=True, slots=True)
class WindowState:
    """Platform activity, foreground presentation, and key-window focus."""
    active: bool = True
    foreground: bool = True
    focused: bool = True


@dataclass(frozen=True, slots=True)
class KeyEvent:
    """A hardware key event. code is a Keyboard/Keypad USB HID usage.

    key is a lowercase logical key or a named key such as 'enter' or 'left'.
    Text composition belongs to TextField/TextView/TextInputSession. Cancelled
    releases clear held keys without completing a control activation.
    """
    key: str
    code: int
    phase: str
    modifiers: frozenset[str] = frozenset()
    repeat: bool = False
    cancelled: bool = False
    timestamp: float = 0.

    def __post_init__(self):
        object.__setattr__(self, "modifiers", frozenset(self.modifiers))


_MODIFIERS = ("shift", "control", "alt", "command", "caps_lock", "fn", "keypad")


@dataclass
class _HeldKey:
    event: KeyEvent
    owner: object = None
    next_repeat: float = math.inf
    native_repeat: bool = False


class PlatformInput:
    def __init__(self, scene):
        self.scene = scene
        initial = getattr(scene._window, "state", {})
        self.state = WindowState(*(bool(initial.get(name, True)) for name in ("active", "foreground", "focused")))
        self.gpu_allowed = initial.get("gpu_allowed", True)
        self.native_repeat = initial.get("native_repeat", True)
        self.epoch = initial.get("epoch", 0)
        self.start_sequence = initial.get("sequence", 0)
        self.pending_keys = []
        self.held = {}
        self.reset_dt = False
        self.polled = False
        enable = getattr(scene._window, "enable_key_events", None)
        if enable is not None:
            enable()

    @property
    def accepts_input(self):
        return self.state.active and self.state.foreground and self.state.focused

    @property
    def keys_down(self):
        return frozenset(held.event.key for held in self.held.values() if held.owner is None)

    @property
    def key_codes_down(self):
        return frozenset(code for code, held in self.held.items() if held.owner is None)

    def cancel_keys(self):
        held, self.held = self.held, {}
        self.pending_keys.clear()
        now = time.monotonic()
        for sample in held.values():
            if sample.owner is None:
                self.scene.key_up(replace(sample.event, phase="up", repeat=False, cancelled=True, timestamp=now))
            elif sample.owner is not True:
                sample.owner._cancel_interaction()

    def cancel_owner(self, owner):
        # Keep physical ownership until release, so it cannot turn into a game
        # key when the focused control disappears mid-press.
        for sample in self.held.values():
            if sample.owner is owner:
                sample.owner = True
        owner._cancel_interaction()

    def cancel_interactions(self, *, blur_text=True):
        scene = self.scene
        self.cancel_keys()
        scene._pointer_router.cancel_all()
        scene._focus_manager.clear()
        scene._ensure_ui_nodes()
        for node in list(scene._ui_event_nodes):
            cancel = getattr(node, "_cancel_interaction", None)
            if cancel is not None:
                cancel()
        if blur_text:
            scene.dismiss_keyboard()
        scene._dispatch_ui_events()

    def poll(self, payload=None, *, receive_keys=True, notify=True):
        if payload is None:
            consume = getattr(self.scene._window, "consume_platform_events", None)
            if consume is None:
                self.polled = True
                return
            payload = consume()
        self.polled = True
        latest = payload["state"]
        self.native_repeat = latest.get("native_repeat", True)
        for event in payload["events"]:
            if event.get("sequence", self.start_sequence + 1) <= self.start_sequence:
                continue
            kind = event["kind"]
            if kind == "key":
                if receive_keys and event.get("epoch", 0) == latest["epoch"]:
                    self.pending_keys.append(event)
            elif kind == "reset":
                if event["epoch"] <= self.epoch:
                    continue
                self.epoch = event["epoch"]
                self.cancel_interactions(blur_text=event["reason"] != "text_input")
            else:
                self.epoch = max(self.epoch, event.get("epoch", self.epoch))
                state = WindowState(*(event[name] for name in ("active", "foreground", "focused")))
                self._state_changed(state, notify=notify)
        # A final snapshot also recovers from bounded-queue overflow or a scene
        # starting after the corresponding native notification was delivered.
        self.epoch = latest["epoch"]
        self.gpu_allowed = latest["gpu_allowed"]
        state = WindowState(*(latest[name] for name in ("active", "foreground", "focused")))
        self._state_changed(state, notify=notify)

    def _state_changed(self, state, *, notify):
        previous = self.state
        if state == previous:
            return
        self.state = state
        self.reset_dt = True
        self.scene._render_fingerprint = 0
        self.scene._input_reveal_key = None
        if not state.foreground or (not self.native_repeat and not state.active):
            self.scene._invalidate_graphics()
        if not self.accepts_input:
            self.cancel_interactions()
        if notify:
            self.scene.window_state_changed(state)

    def frame_dt(self, dt):
        if not self.state.active and self.scene.pause_when_inactive:
            return None
        if self.reset_dt:
            self.reset_dt = False
            return 0.
        return dt

    def feed_key(self, raw):
        if not self.accepts_input or raw.get("epoch", self.epoch) != self.epoch:
            return
        modifiers = raw["modifiers"]
        if isinstance(modifiers, int):
            modifiers = frozenset(name for i, name in enumerate(_MODIFIERS) if modifiers & (1 << i))
        event = KeyEvent(raw["key"], raw["code"], raw["phase"], frozenset(modifiers),
                         raw.get("repeat", False), raw.get("cancelled", False), raw.get("timestamp", time.monotonic()))
        previous = self.held.get(event.code)
        if event.phase == "up":
            if previous is None:
                return
            self.held.pop(event.code)
            if previous.owner is None:
                self.scene.key_up(event)
            elif previous.owner is not True:
                if event.cancelled or previous.owner is not self.scene._focus_manager.current:
                    previous.owner._cancel_interaction()
                else:
                    previous.owner._handle_key(event)
            return
        if previous is not None:
            previous.native_repeat = True
            self._repeat(previous, replace(event, repeat=True))
            return
        if event.repeat:
            return  # A late OS repeat must not start a fresh hold after reset.
        if self.scene.focused_input is not None:
            return
        sample = _HeldKey(event)
        self.held[event.code] = sample
        owner = self.scene._focus_manager.current
        consumed = self.scene._focus_manager.handle_key(event)
        if self.held.get(event.code) is not sample:
            return
        if consumed:
            sample.owner = owner if owner is not None and event.key not in ("tab", "escape") else True
        else:
            handled = self.scene.key_down(event)
            if event.key == "escape" and not handled:
                self.scene._close_requested = True
        if not self.native_repeat and event.code < 224 and event.key not in ("caps_lock", "num_lock", "scroll_lock"):
            sample.next_repeat = event.timestamp + self._repeat_timing()[0]

    def _repeat_timing(self):
        delay, interval = float(self.scene.key_repeat_delay), float(self.scene.key_repeat_interval)
        if not math.isfinite(delay) or delay < 0 or not math.isfinite(interval) or interval <= 0:
            raise ValueError("key_repeat_delay must be nonnegative and key_repeat_interval positive and finite")
        return delay, interval

    def _repeat(self, sample, event):
        if sample.owner is None:
            self.scene.key_down(event)
        elif sample.owner is not True and sample.owner is self.scene._focus_manager.current:
            sample.owner._handle_key(event)

    def repeat_keys(self, now=None):
        if self.scene.focused_input is not None:
            self.cancel_keys()
            return
        if self.native_repeat or not self.accepts_input:
            return
        now = time.monotonic() if now is None else now
        for sample in list(self.held.values()):
            if not sample.native_repeat and now >= sample.next_repeat:
                sample.next_repeat = now + self._repeat_timing()[1]
                self._repeat(sample, replace(sample.event, repeat=True, timestamp=now))
