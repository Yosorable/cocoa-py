"""Generated from clipboard.py by tools/generate_stubs.py; do not edit."""

from __future__ import annotations
from collections.abc import Buffer, Mapping
from typing import TYPE_CHECKING, Literal, cast, overload
from _cocoa._types import ImageInput as ImageInput
from _cocoa._system import clipboard as _native
from PIL.Image import Image as PillowImage
__all__ = ['read_text', 'write_text', 'read_url', 'write_url', 'read_image', 'write_image', 'types', 'read_bytes', 'write_bytes', 'write_item', 'clear', 'has_text', 'has_image', 'has_urls', 'change_count', 'ImageInput']

def read_text() -> str | None:
    ...

def write_text(text: str, *, local_only: bool=False, expires_in: float | None=None) -> None:
    ...

def read_url() -> str | None:
    ...

def write_url(url: str, *, local_only: bool=False, expires_in: float | None=None) -> None:
    ...

@overload
def read_image(*, as_bytes: Literal[False]=False) -> PillowImage | None:
    ...

@overload
def read_image(*, as_bytes: Literal[True]) -> bytes | None:
    ...

@overload
def read_image(*, as_bytes: bool) -> PillowImage | bytes | None:
    ...

def write_image(image: ImageInput, *, local_only: bool=False, expires_in: float | None=None) -> None:
    ...

def types() -> list[str]:
    ...

def read_bytes(type: str) -> bytes | None:
    ...

def write_bytes(data: Buffer, *, type: str, local_only: bool=False, expires_in: float | None=None) -> None:
    ...

def write_item(representations: Mapping[str, Buffer], *, local_only: bool=False, expires_in: float | None=None) -> None:
    ...

def has_text() -> bool:
    ...

def has_image() -> bool:
    ...

def has_urls() -> bool:
    ...

def change_count() -> int:
    ...

def clear() -> None:
    ...
