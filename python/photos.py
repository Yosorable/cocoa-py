"""Photo library selection and saving on Apple platforms.

Backed by the native module ``_photos``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from pathlib import Path
from typing import Any, Sequence

from _cocoa import _photos

__all__ = [
    "MediaType",
    "PickedMedia",
    "PickedImage",
    "PickedVideo",
    "SaveImageResult",
    "SaveVideoResult",
    "pick_media",
    "pick_image",
    "pick_images",
    "pick_video",
    "pick_videos",
    "save_image",
    "save_video",
]


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


@dataclass(frozen=True)
class PickedMedia:
    type: MediaType
    path: str
    filename: str
    width: int | None = None
    height: int | None = None
    duration: float | None = None


@dataclass(frozen=True)
class PickedImage:
    path: str
    filename: str
    width: int
    height: int


@dataclass(frozen=True)
class PickedVideo:
    path: str
    filename: str
    duration: float


@dataclass(frozen=True)
class SaveImageResult:
    ok: bool
    local_id: str


@dataclass(frozen=True)
class SaveVideoResult:
    ok: bool
    local_id: str


def pick_media(
    types: Sequence[MediaType | str] = (MediaType.IMAGE,),
    limit: int = 1,
    *,
    timeout: float = 300.0,
) -> list[PickedMedia]:
    """Choose media, returning an empty list on cancellation.

    ``limit=0`` allows any number of selections. ``timeout`` bounds the wait
    for selection and file loading; timing out closes the picker.
    """
    raw_types = _normalize_media_types(types)
    raw_items = _photos.pick_media(types=raw_types, limit=limit, timeout=timeout)
    result: list[PickedMedia] = []
    for raw in raw_items:
        raw_type = str(raw.get("type", MediaType.IMAGE.value))
        result.append(
            PickedMedia(
                type=MediaType(raw_type),
                path=str(raw["path"]),
                filename=str(raw["filename"]),
                width=int(raw["width"]) if "width" in raw and raw["width"] is not None else None,
                height=int(raw["height"]) if "height" in raw and raw["height"] is not None else None,
                duration=float(raw["duration"]) if "duration" in raw and raw["duration"] is not None else None,
            )
        )
    return result


def pick_image(*, timeout: float = 300.0) -> PickedImage | None:
    """Open the system photo picker and return an image or None when cancelled.

    Return shape:
    - {"path": str, "filename": str, "width": int, "height": int}
    - None (cancelled)
    """

    items = pick_media(types=(MediaType.IMAGE,), limit=1, timeout=timeout)
    if not items:
        return None
    raw = items[0]
    return PickedImage(
        path=raw.path,
        filename=raw.filename,
        width=int(raw.width or 0),
        height=int(raw.height or 0),
    )


def pick_images(limit: int = 1, *, timeout: float = 300.0) -> list[PickedImage]:
    items = pick_media(types=(MediaType.IMAGE,), limit=limit, timeout=timeout)
    result: list[PickedImage] = []
    for raw in items:
        result.append(
            PickedImage(
                path=raw.path,
                filename=raw.filename,
                width=int(raw.width or 0),
                height=int(raw.height or 0),
            )
        )
    return result


def pick_video(*, timeout: float = 300.0) -> PickedVideo | None:
    items = pick_media(types=(MediaType.VIDEO,), limit=1, timeout=timeout)
    if not items:
        return None
    raw = items[0]
    return PickedVideo(
        path=raw.path,
        filename=raw.filename,
        duration=float(raw.duration or 0.0),
    )


def pick_videos(limit: int = 1, *, timeout: float = 300.0) -> list[PickedVideo]:
    items = pick_media(types=(MediaType.VIDEO,), limit=limit, timeout=timeout)
    result: list[PickedVideo] = []
    for raw in items:
        result.append(
            PickedVideo(
                path=raw.path,
                filename=raw.filename,
                duration=float(raw.duration or 0.0),
            )
        )
    return result


def save_image(
    image: Any | None = None,
    *,
    path: str | Path | None = None,
    data: bytes | bytearray | memoryview | None = None,
    format: str | None = None,
) -> SaveImageResult:
    """Save an image to Photos.

    Exactly one image source is used with this priority:
    1) explicit ``path`` or ``data``
    2) ``image`` inferred type:
       - ``str``/``Path`` -> path
       - bytes-like -> data
       - PIL Image
       - matplotlib Figure
       - numpy ndarray (via cv2 if available, else PIL)
    """

    if path is not None and data is not None:
        raise ValueError("save_image() accepts only one of: path or data")

    if path is None and data is None and image is None:
        raise ValueError("save_image() needs one image source")

    if path is not None:
        raw = _photos.save_image(path=str(path))
        return SaveImageResult(ok=bool(raw.get("ok", False)), local_id=str(raw.get("local_id", "")))

    if data is not None:
        raw = _photos.save_image(data=bytes(data), format=_normalize_format(format))
        return SaveImageResult(ok=bool(raw.get("ok", False)), local_id=str(raw.get("local_id", "")))

    # Infer from `image`
    if isinstance(image, (str, Path)):
        raw = _photos.save_image(path=str(image))
        return SaveImageResult(ok=bool(raw.get("ok", False)), local_id=str(raw.get("local_id", "")))

    if isinstance(image, (bytes, bytearray, memoryview)):
        raw = _photos.save_image(data=bytes(image), format=_normalize_format(format))
        return SaveImageResult(ok=bool(raw.get("ok", False)), local_id=str(raw.get("local_id", "")))

    # PIL.Image.Image
    if _looks_like_pil_image(image):
        payload = _encode_pil_image(image, format)
        raw = _photos.save_image(data=payload, format=_normalize_format(format))
        return SaveImageResult(ok=bool(raw.get("ok", False)), local_id=str(raw.get("local_id", "")))

    # matplotlib.figure.Figure
    if _looks_like_matplotlib_figure(image):
        payload = _encode_matplotlib_figure(image, format)
        raw = _photos.save_image(data=payload, format=_normalize_format(format))
        return SaveImageResult(ok=bool(raw.get("ok", False)), local_id=str(raw.get("local_id", "")))

    # numpy / opencv arrays
    if _looks_like_numpy_array(image):
        payload, fmt = _encode_ndarray(image, format)
        raw = _photos.save_image(data=payload, format=fmt)
        return SaveImageResult(ok=bool(raw.get("ok", False)), local_id=str(raw.get("local_id", "")))

    raise TypeError(f"Unsupported image type: {type(image)!r}")


def save_video(path: str | Path) -> SaveVideoResult:
    raw = _photos.save_video(path=str(path))
    return SaveVideoResult(ok=bool(raw.get("ok", False)), local_id=str(raw.get("local_id", "")))


def _normalize_format(fmt: str | None) -> str | None:
    if fmt is None:
        return None
    out = fmt.strip().lower()
    if out == "jpg":
        out = "jpeg"
    return out


def _normalize_media_types(types: Sequence[MediaType | str]) -> list[str]:
    normalized: list[str] = []
    for item in types:
        raw = item.value if isinstance(item, MediaType) else str(item).strip().lower()
        if raw == MediaType.IMAGE.value:
            normalized.append(MediaType.IMAGE.value)
            continue
        if raw == MediaType.VIDEO.value:
            normalized.append(MediaType.VIDEO.value)
            continue
        raise ValueError(f"Unsupported media type: {item!r}")
    if not normalized:
        raise ValueError("types must not be empty")
    return normalized


def _looks_like_pil_image(obj: Any) -> bool:
    return hasattr(obj, "save") and hasattr(obj, "mode") and hasattr(obj, "size")


def _looks_like_matplotlib_figure(obj: Any) -> bool:
    return hasattr(obj, "savefig") and hasattr(obj, "canvas")


def _looks_like_numpy_array(obj: Any) -> bool:
    return hasattr(obj, "shape") and hasattr(obj, "dtype")


def _encode_pil_image(image: Any, fmt: str | None) -> bytes:
    chosen = (_normalize_format(fmt) or "png").upper()
    buffer = BytesIO()
    image.save(buffer, format=chosen)
    return buffer.getvalue()


def _encode_matplotlib_figure(fig: Any, fmt: str | None) -> bytes:
    chosen = _normalize_format(fmt) or "png"
    buffer = BytesIO()
    fig.savefig(buffer, format=chosen)
    return buffer.getvalue()


def _encode_ndarray(array: Any, fmt: str | None) -> tuple[bytes, str]:
    chosen = _normalize_format(fmt) or "png"
    ext = ".jpg" if chosen == "jpeg" else f".{chosen}"

    try:
        import cv2  # type: ignore

        ok, encoded = cv2.imencode(ext, array)
        if not ok:
            raise RuntimeError("cv2.imencode failed")
        return encoded.tobytes(), ("jpeg" if ext == ".jpg" else chosen)
    except Exception:
        # Fallback: PIL Image.fromarray
        from PIL import Image  # type: ignore

        image = Image.fromarray(array)
        return _encode_pil_image(image, chosen), chosen
