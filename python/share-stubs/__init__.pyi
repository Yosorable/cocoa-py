"""Generated from share.py by tools/generate_stubs.py; do not edit."""

import os
from collections.abc import Buffer, Iterable, Mapping
from dataclasses import dataclass
from typing import cast
from _cocoa._types import ImageInput as ImageInput, PathInput as PathInput
from _cocoa.requests import Request, seconds
__all__ = ['ImageInput', 'PathInput', 'ShareResult', 'ShareRequest', 'open', 'present']

@dataclass(frozen=True)
class ShareResult:
    completed: bool
    activity: str | None

class ShareRequest(Request[ShareResult]):

    def __init__(self, *, text: str | None=None, files: Iterable[PathInput]=(), urls: Iterable[str]=(), images: Iterable[ImageInput]=(), attachments: Mapping[str, Buffer] | None=None) -> None:
        ...

def open(*, text: str | None=None, files: Iterable[PathInput]=(), urls: Iterable[str]=(), images: Iterable[ImageInput]=(), attachments: Mapping[str, Buffer] | None=None) -> ShareRequest:
    ...

def present(*, text: str | None=None, files: Iterable[PathInput]=(), urls: Iterable[str]=(), images: Iterable[ImageInput]=(), attachments: Mapping[str, Buffer] | None=None, timeout: float | None=300) -> ShareResult:
    ...
