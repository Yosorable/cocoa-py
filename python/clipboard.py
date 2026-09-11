"""Explicit text and typed byte access to the Apple system clipboard.

iOS may ask for paste permission when reading another application's content.
Importing this module never reads the clipboard.
"""

import base64
from _cocoa.requests import call as _call

__all__ = ["read_text", "write_text", "types", "read_bytes", "write_bytes", "clear"]


def read_text() -> str | None:
    """Return clipboard text, or None if it contains no text."""
    return _call("clipboard.read_text")


def write_text(text: str, *, local_only=False, expires_in=None):
    """Replace the clipboard with text. Expiration/local_only require iOS."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    _call("clipboard.write_text", {"text": text, "local_only": bool(local_only), "expires_in": expires_in})


def types() -> list[str]:
    """Return available Uniform Type Identifier strings."""
    return _call("clipboard.types")


def read_bytes(type: str) -> bytes | None:
    """Read a representation such as public.png; return None if unavailable."""
    data = _call("clipboard.read_bytes", {"type": type})
    return base64.b64decode(data) if data is not None else None


def write_bytes(data, *, type: str):
    """Replace the clipboard with one typed byte representation."""
    encoded = base64.b64encode(memoryview(data)).decode("ascii")
    _call("clipboard.write_bytes", {"type": type, "data": encoded})


def clear():
    """Remove all current clipboard items."""
    _call("clipboard.clear")
