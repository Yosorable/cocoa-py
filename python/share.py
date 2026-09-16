"""Present Apple's system sharing UI for text, URLs, files, and images.

Images accept Pillow objects or encoded buffers. Named attachments accept
contiguous buffers without JSON or Base64 conversion. Pillow is optional and
is imported only when converting a Pillow object.
"""

import os
from collections.abc import Buffer, Iterable, Mapping
from dataclasses import dataclass
from typing import cast

from _cocoa._types import ImageInput as ImageInput, PathInput as PathInput
from _cocoa.requests import Request, seconds

__all__ = ["ImageInput", "PathInput", "ShareResult", "ShareRequest", "open", "present"]


@dataclass(frozen=True)
class ShareResult:
    """The system service's result, not a receipt from the eventual recipient.

    completed is False for user cancellation. activity is an iOS activity type
    or a localized macOS service title, or None when no service was selected.
    It is diagnostic information, not a portable service identifier.
    """

    completed: bool
    activity: str | None


class ShareRequest(Request[ShareResult]):
    """A sharing window with wait, done, and close methods.

    On macOS, click Share in the window to open the system service picker.
    Closing before selection cancels the picker. Once a service starts, it can
    finish independently; source file access remains valid until it completes.
    On iOS, closing cancels visible sharing UI. Its dismissal is asynchronous,
    and temporary attachments remain available until that dismissal finishes.
    """

    def __init__(self, *, text: str | None = None, files: Iterable[PathInput] = (),
                 urls: Iterable[str] = (), images: Iterable[ImageInput] = (),
                 attachments: Mapping[str, Buffer] | None = None) -> None:
        if isinstance(files, (str, bytes, os.PathLike)) or isinstance(urls, str):
            raise TypeError("files and urls must be sequences of individual items")
        if isinstance(images, (str, bytes, bytearray, memoryview, os.PathLike)):
            raise TypeError("images must be a sequence of individual images")
        if attachments is not None and not isinstance(attachments, Mapping):
            raise TypeError("attachments must map filenames to contiguous buffers")
        paths = [os.path.abspath(os.path.expanduser(os.fsdecode(path))) for path in files]
        buffers = dict(images=[_image_buffer(image) for image in images],
                       attachments={} if attachments is None else dict(attachments))
        super().__init__("share.present", dict(text=text, files=paths, urls=list(urls)),
                         lambda value: ShareResult(**value), _buffers=buffers)


def _image_buffer(image: ImageInput) -> Buffer:
    try:
        view = memoryview(cast(Buffer, image))
    except TypeError:
        if not any(cls.__module__.startswith("PIL.") for cls in type(image).__mro__):
            raise TypeError("Each image must be a Pillow Image or an encoded image buffer") from None
        from io import BytesIO
        from PIL import Image, ImageOps
        if not isinstance(image, Image.Image):
            raise TypeError("Each image must be a Pillow Image or an encoded image buffer")
        with BytesIO() as output:
            oriented = ImageOps.exif_transpose(image)
            try:
                with oriented.convert("RGBA") as pixels:
                    pixels.save(output, format="PNG")
            finally:
                oriented.close()
            return output.getvalue()
    else:
        with view:
            if not view.c_contiguous:
                raise BufferError("Image buffers must be C-contiguous")
        return cast(Buffer, image)


def open(*, text: str | None = None, files: Iterable[PathInput] = (),
         urls: Iterable[str] = (), images: Iterable[ImageInput] = (),
         attachments: Mapping[str, Buffer] | None = None) -> ShareRequest:
    """Open a share window without waiting. Retain and explicitly close it.

    files contains local paths; urls contains absolute non-file URLs. images
    contains Pillow images or encoded PNG/JPEG (or other OS-supported image)
    buffers. Images are decoded by the OS; their original encoding and metadata
    are not guaranteed to survive sharing. Pillow inputs use the current frame
    with EXIF orientation applied.

    attachments maps plain filenames to contiguous buffers, for example
    {"report.pdf": pdf_bytes}. Empty buffers are allowed. Names cannot be empty,
    '.', '..', or contain a slash, backslash, or NUL. Each attachment gets its
    own temporary file, removed after cancellation, failure, or completion.
    Original local files are never removed. A wait timeout leaves the request
    open. On iOS, close() cancels visible UI and releases files after dismissal;
    it does not wait for the animation. After an iOS handoff has removed the
    local sheet, or a macOS service has started, files remain available until
    the service reports completion.

    Buffers are copied before returning, so later mutations do not change the
    shared contents. Use attachments to preserve exact encoded image bytes.
    """
    return ShareRequest(text=text, files=files, urls=urls, images=images, attachments=attachments)


def present(*, text: str | None = None, files: Iterable[PathInput] = (),
            urls: Iterable[str] = (), images: Iterable[ImageInput] = (),
            attachments: Mapping[str, Buffer] | None = None,
            timeout: float | None = 300) -> ShareResult:
    """Wait for sharing or cancellation; raise TimeoutError when time runs out.

    Content arguments are the same as open(). User cancellation returns
    ShareResult(completed=False, ...); an actual service failure raises OSError.
    """
    seconds(timeout, allow_none=True)
    with open(text=text, files=files, urls=urls, images=images, attachments=attachments) as request:
        return request.wait(timeout)
