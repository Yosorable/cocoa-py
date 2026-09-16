"""Text, URLs, images and typed data on the Apple system clipboard.

Importing this module does not inspect the clipboard. Content reads may invoke
the operating system's paste-permission UI. All writes replace the clipboard
with one item; use write_item() to include several representations of that item.
Pillow is optional and imported only for Pillow image conversion.
"""

from __future__ import annotations

from collections.abc import Buffer, Mapping
from typing import TYPE_CHECKING, Literal, cast, overload

from _cocoa._types import ImageInput as ImageInput
from _cocoa._system import clipboard as _native

if TYPE_CHECKING:
    from PIL.Image import Image as PillowImage

__all__ = [
    "read_text", "write_text", "read_url", "write_url", "read_image", "write_image",
    "types", "read_bytes", "write_bytes", "write_item", "clear",
    "has_text", "has_image", "has_urls", "change_count", "ImageInput",
]


def read_text() -> str | None:
    """Return the first item's plain text, or None if unavailable."""
    return _native("read_text")


def write_text(text: str, *, local_only: bool = False,
               expires_in: float | None = None) -> None:
    """Replace all items with text, including an empty string.

    The OS may add other representations, such as a URL for URL-like text.
    Use write_url() to explicitly supply a URL and its plain-text fallback.

    local_only prevents Universal Clipboard transfer to other devices on iOS
    and macOS. expires_in is an iOS lifetime in seconds (0.001..31536000);
    macOS raises NotImplementedError before changing the clipboard.
    """
    _native("write_text", text, local_only=local_only, expires_in=expires_in)


def read_url() -> str | None:
    """Return the first item's URL as a string, or None; never open the URL.

    Read an advertised public.url or public.file-url representation. The OS
    may provide one for URL-like text, even after write_text(). This module
    does not independently detect URLs in text.
    """
    return _native("read_url")


def write_url(url: str, *, local_only: bool = False,
              expires_in: float | None = None) -> None:
    """Write an absolute URL together with a plain-text fallback.

    Custom schemes and file URLs are accepted. No file is read and no URL is
    opened. Write options are the same as write_text().
    """
    _native("write_url", url, local_only=local_only, expires_in=expires_in)


@overload
def read_image(*, as_bytes: Literal[False] = False) -> PillowImage | None: ...


@overload
def read_image(*, as_bytes: Literal[True]) -> bytes | None: ...


@overload
def read_image(*, as_bytes: bool) -> PillowImage | bytes | None: ...


def read_image(*, as_bytes: bool = False) -> PillowImage | bytes | None:
    """Return the first item's image, or None if unavailable.

    The default returns a detached Pillow Image and requires Pillow. With
    as_bytes=True, return PNG bytes without importing Pillow. The native image
    decoder accepts formats supported by the OS; only a still image is returned.
    Metadata, animation and the original encoding are not preserved. Use
    read_bytes(type) for an unchanged encoded representation.
    """
    if not isinstance(as_bytes, bool):
        raise TypeError("as_bytes must be bool")
    data = _native("read_image")
    if data is None or as_bytes:
        return data
    from io import BytesIO
    from PIL import Image
    with Image.open(BytesIO(data)) as image:
        return image.copy()


def write_image(image: ImageInput, *, local_only: bool = False,
                expires_in: float | None = None) -> None:
    """Write a Pillow Image or encoded image buffer as a still PNG image.

    Encoded bytes, bytearray and contiguous memoryview inputs do not require
    Pillow. Pillow inputs use their current frame with EXIF orientation applied.
    Invalid image data raises ValueError before the clipboard is replaced.
    Options are the same as write_text(). File paths are not accepted.
    """
    try:
        view = memoryview(cast(Buffer, image))
    except TypeError:
        if not any(cls.__module__.startswith("PIL.") for cls in type(image).__mro__):
            raise TypeError("image must be a Pillow Image or an encoded image buffer") from None
        from io import BytesIO
        from PIL import Image, ImageOps
        if not isinstance(image, Image.Image):
            raise TypeError("image must be a Pillow Image or an encoded image buffer")
        with BytesIO() as output:
            oriented = ImageOps.exif_transpose(image)
            try:
                with oriented.convert("RGBA") as pixels:
                    pixels.save(output, format="PNG")
            finally:
                oriented.close()
            data = output.getvalue()
        _native("write_image", data, local_only=local_only, expires_in=expires_in)
    else:
        with view:
            _native("write_image", view, local_only=local_only, expires_in=expires_in)


def types() -> list[str]:
    """Return the first item's Uniform Type Identifier strings, without data.

    These include any representations the OS adds after writing.
    """
    return _native("types")


def read_bytes(type: str) -> bytes | None:
    """Return the first item's exact typed data, or None if unavailable.

    Empty data is b'', distinct from None. No Base64 or JSON conversion occurs.
    """
    return _native("read_bytes", type)


def write_bytes(data: Buffer, *, type: str, local_only: bool = False,
                expires_in: float | None = None) -> None:
    """Write one typed buffer; see write_item() and write_text() for options."""
    write_item({type: data}, local_only=local_only, expires_in=expires_in)


def write_item(representations: Mapping[str, Buffer], *, local_only: bool = False,
               expires_in: float | None = None) -> None:
    """Replace all items with one item containing multiple representations.

    Pass a nonempty mapping of UTI strings to contiguous buffers, for example
    {"public.html": b"<b>Hello</b>", "public.utf8-plain-text": b"Hello"}.
    Encode text explicitly. All values are validated and copied before writing;
    modifying an input buffer after this call does not change the clipboard.
    Write options are the same as write_text().
    """
    if not isinstance(representations, Mapping):
        raise TypeError("representations must be a mapping of type strings to buffers")
    _native("write_item", dict(representations), local_only=local_only, expires_in=expires_in)


def has_text() -> bool:
    """Return whether any item advertises text, without reading its contents."""
    return _native("has_text")


def has_image() -> bool:
    """Return whether any item advertises an image, without decoding it."""
    return _native("has_image")


def has_urls() -> bool:
    """Return whether any item advertises a URL, without reading its contents."""
    return _native("has_urls")


def change_count() -> int:
    """Return the OS change counter, without reading clipboard data.

    Compare for inequality to detect changes. This is not an item count, stable
    identifier, or transaction lock; another application may change it at any time.
    """
    return _native("change_count")


def clear() -> None:
    """Remove all clipboard items."""
    _native("clear")
