"""Schedule and manage this library's local notifications.

Requires a host app identity and explicit permission. Foreground presentation
and activation handling belong to the host; this module does not replace the
host's notification delegate. Identifiers are namespaced within the host app.
"""

import uuid
from _cocoa.requests import call as _call

__all__ = ["available", "permission", "request_permission", "schedule", "pending", "cancel", "cancel_all"]


def available() -> bool:
    """Whether the process has the app identity required by UserNotifications."""
    return _call("notification.available")


def permission() -> str:
    """Query permission without showing a prompt."""
    return _call("notification.permission")


def request_permission(*, timeout=120) -> str:
    """Ask to show local notifications and return the resulting status."""
    return _call("notification.request_permission", timeout=timeout)


def schedule(title: str, body="", *, delay=1, repeat=False, sound=True, identifier=None) -> str:
    """Schedule a local notice; the same identifier replaces its pending notice.

    delay uses seconds and must be at least 60 for repeating notices. Returns
    the caller's identifier or a generated UUID. Permission must be granted.
    """
    if identifier is None:
        identifier = str(uuid.uuid4())
    return _call("notification.schedule", dict(title=title, body=body, delay=delay,
                 repeat=bool(repeat), sound=bool(sound), identifier=identifier))


def pending() -> list[dict]:
    """List this library's pending notices, including their next Unix timestamp."""
    return _call("notification.pending")


def cancel(identifier: str):
    """Remove a library notice from pending and delivered notifications."""
    _call("notification.cancel", {"identifier": identifier})


def cancel_all():
    """Cancel this library's currently pending notices, preserving host notices."""
    for notice in pending():
        cancel(notice["identifier"])
