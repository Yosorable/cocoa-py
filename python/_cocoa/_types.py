"""Shared input types; optional image libraries are not imported at runtime."""

from collections.abc import Buffer
from os import PathLike
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image

type PathInput = str | bytes | PathLike[str] | PathLike[bytes]
type ImageInput = Buffer | Image
