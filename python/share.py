"""Present Apple's system sharing UI for text, URLs, and files."""

import os
from dataclasses import dataclass
from _cocoa_support import Request, seconds

__all__ = ["ShareResult", "ShareRequest", "open", "present"]


@dataclass(frozen=True)
class ShareResult:
    completed: bool
    activity: str | None


class ShareRequest(Request):
    """A sharing window with wait, done, and close methods.

    On macOS, click Share in the window to open the system service picker.
    Closing before selection cancels the picker. Once a service starts, it can
    finish independently; source file access remains valid until it completes.
    """

    def __init__(self, *, text=None, files=(), urls=()):
        if isinstance(files, (str, bytes, os.PathLike)) or isinstance(urls, str):
            raise TypeError("files and urls must be sequences of individual items")
        paths = [os.path.abspath(os.path.expanduser(os.fsdecode(path))) for path in files]
        super().__init__("share.present", dict(text=text, files=paths, urls=list(urls)),
                         lambda value: ShareResult(**value))


def open(*, text=None, files=(), urls=()) -> ShareRequest:
    """Open a share window without waiting. Retain and explicitly close it."""
    return ShareRequest(text=text, files=files, urls=urls)


def present(*, text=None, files=(), urls=(), timeout=300) -> ShareResult:
    """Wait for sharing or cancellation; raise TimeoutError when time runs out."""
    seconds(timeout, allow_none=True)
    with open(text=text, files=files, urls=urls) as request:
        return request.wait(timeout)
