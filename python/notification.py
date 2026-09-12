"""Schedule local notifications on iOS and macOS.

Requires an app identity and explicit permission. Importing this module never
installs a notification delegate. See ``docs/notifications.md`` for host setup,
calendar semantics, and the distinction between pending and delivered notices.
"""

from dataclasses import dataclass, asdict as _asdict
from datetime import datetime
import math
from numbers import Real
import time
import uuid

from _cocoa.requests import call as _call

__all__ = [
    "CalendarTrigger", "available", "permission", "request_permission", "settings",
    "schedule", "pending", "delivered", "cancel_pending", "remove_delivered",
    "cancel", "cancel_all",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarTrigger:
    """Repeat at a local clock time, daily or on one weekday.

    ``weekday`` uses Monday=0 through Sunday=6; None means every day.
    ``timezone`` is an IANA name such as "Asia/Shanghai", or None to follow the
    device's local timezone. Apple resolves daylight-saving clock changes.
    """

    hour: int
    minute: int = 0
    second: int = 0
    weekday: int | None = None
    timezone: str | None = None

    def __post_init__(self):
        for name, maximum in (("hour", 23), ("minute", 59), ("second", 59), ("weekday", 6)):
            value = getattr(self, name)
            if name == "weekday" and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be an integer from 0 to {maximum}")
        if self.timezone is not None and (not isinstance(self.timezone, str) or not self.timezone):
            raise ValueError("timezone must be an IANA timezone name or None")


def available() -> bool:
    """Whether the process has the app identity required by UserNotifications."""
    return _call("notification.available")


def permission() -> str:
    """Query authorization without prompting: authorized, denied, not_determined,
    provisional, or (on iOS) ephemeral.
    """
    return _call("notification.permission")


def request_permission(*, timeout=120) -> str:
    """Ask for alerts, sounds and badges; return the resulting authorization."""
    return _call("notification.request_permission", timeout=timeout)


def settings() -> dict:
    """Query authorization, alert style and individual notification settings.

    This does not request permission or predict suppression by Focus modes.
    """
    return _call("notification.settings")


def _identifier(value):
    if not isinstance(value, str) or not value or len(value.encode("utf-16-le")) // 2 > 128:
        raise ValueError("identifier must contain 1 to 128 UTF-16 code units")
    return value


def schedule(title: str, body="", *, subtitle="", delay=None, repeat=False,
             at=None, calendar=None, sound=True, foreground=True, identifier=None) -> str:
    """Schedule a notice and return its ID; reusing an ID replaces a pending one.

    Choose one trigger: delay in seconds (default 1; 0 means immediate), an
    aware datetime ``at``, or a repeating ``CalendarTrigger``. ``repeat=True``
    applies only to delays of at least 60 seconds. Delays are at most one year.
    ``sound=False`` is silent. ``foreground=False`` suppresses presentation
    while the host is active, if its delegate supports cocoa-py's policy.

    Scheduling requires permission. Acceptance is not proof of delivery. If
    an exception or interruption occurs after submission, the OS may still
    accept the notice; use a caller-supplied ID to inspect or cancel it.
    """
    identifier = str(uuid.uuid4()) if identifier is None else _identifier(identifier)
    for name, text in (("title", title), ("body", body), ("subtitle", subtitle)):
        if not isinstance(text, str):
            raise TypeError(f"{name} must be a string")
    if sum(value is not None for value in (delay, at, calendar)) > 1:
        raise ValueError("choose only one of delay, at, or calendar")
    if repeat and (at is not None or calendar is not None):
        raise ValueError("repeat applies only to delay; CalendarTrigger already repeats")
    if at is not None:
        if not isinstance(at, datetime) or at.utcoffset() is None:
            raise ValueError("at must be a timezone-aware datetime")
        timestamp = at.timestamp()
        if not math.isfinite(timestamp) or timestamp <= time.time():
            raise ValueError("at must be in the future")
        trigger = {"kind": "date", "timestamp": math.ceil(timestamp)}
    elif calendar is not None:
        if not isinstance(calendar, CalendarTrigger):
            raise TypeError("calendar must be a CalendarTrigger")
        trigger = {"kind": "calendar", **{key: value for key, value in _asdict(calendar).items() if value is not None}}
    else:
        delay = 1 if delay is None else delay
        if isinstance(delay, bool) or not isinstance(delay, Real) or not math.isfinite(delay):
            raise ValueError("delay must be a finite number of seconds")
        if not 0 <= delay <= 31536000 or (repeat and delay < 60):
            raise ValueError("delay must be between 0 and 31536000 seconds, or at least 60 when repeating")
        trigger = ({"kind": "immediate"} if delay == 0 else
                   {"kind": "interval", "seconds": float(delay), "repeat": bool(repeat)})
    return _call("notification.schedule", dict(
        title=title, body=body, subtitle=subtitle, trigger=trigger,
        sound=bool(sound), foreground=bool(foreground), identifier=identifier,
    ))


def pending() -> list[dict]:
    """List library notices waiting to fire, with trigger details and next_date.

    next_date is a Unix timestamp or None. The list is a snapshot, not a
    guarantee that a request is still pending when a later operation runs.
    """
    return _call("notification.pending")


def delivered() -> list[dict]:
    """List library notices currently in Notification Center, with delivered_at.

    delivered_at is a Unix timestamp. This is not a delivery or read history:
    dismissed notices disappear and suppressed notices may never appear.
    """
    return _call("notification.delivered")


def cancel_pending(identifier: str | None = None):
    """Remove one pending library notice, or all pending library notices.

    Delivered notices are preserved. OS removal is asynchronous.
    """
    _call("notification.cancel_pending", {} if identifier is None else {"identifier": _identifier(identifier)})


def remove_delivered(identifier: str | None = None):
    """Remove one delivered library notice, or all delivered library notices.

    Pending schedules are preserved. OS removal is asynchronous.
    """
    _call("notification.remove_delivered", {} if identifier is None else {"identifier": _identifier(identifier)})


def cancel(identifier: str):
    """Remove a library ID from both pending and delivered notifications."""
    _call("notification.cancel", {"identifier": _identifier(identifier)})


def cancel_all():
    """Cancel currently pending library IDs, also removing their delivered copies.

    Kept for compatibility. To clear delivered-only notices as well, call
    cancel_pending() and remove_delivered(). Other host notices are preserved.
    """
    for notice in pending():
        cancel(notice["identifier"])
