"""Owned CPU images returned by scene capture and texture readback."""
from __future__ import annotations

from dataclasses import dataclass
import operator
from pathlib import Path

from _cocoa import _metal


@dataclass(frozen=True, slots=True)
class ImageData:
    """An immutable, tightly packed, top-to-bottom RGBA8 image.

    RGB channels use straight (unassociated) alpha. The bytes are owned by
    Python and remain valid after the originating texture or scene closes.
    NumPy and Pillow are imported only by their explicit conversion methods.
    """

    width: int
    height: int
    rgba: bytes

    def __post_init__(self):
        width, height = operator.index(self.width), operator.index(self.height)
        if width <= 0 or height <= 0:
            raise ValueError("Image dimensions must be positive integers.")
        with memoryview(self.rgba) as view:
            if view.nbytes != width * height * 4:
                raise ValueError("RGBA data must contain width * height * 4 bytes.")
            data = self.rgba if isinstance(self.rgba, bytes) else view.tobytes()
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "height", height)
        object.__setattr__(self, "rgba", data)

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def to_png(self) -> bytes:
        """Encode PNG bytes in memory using ImageIO; no optional dependencies."""
        return _metal.encode_png(self.width, self.height, self.rgba)

    def save(self, path) -> None:
        """Save a PNG to a writable path. No file is created until this call."""
        destination = Path(path)
        if destination.suffix.lower() not in ("", ".png"):
            raise ValueError("ImageData.save() supports PNG; use a .png filename.")
        destination.write_bytes(self.to_png())

    def to_numpy(self, *, copy=True):
        """Return a uint8 array shaped (height, width, 4).

        The default is an independent writable copy. With copy=False, the
        array is a read-only view that keeps these immutable bytes alive.
        """
        import numpy as np

        array = np.frombuffer(self.rgba, dtype=np.uint8).reshape(self.height, self.width, 4)
        return array.copy() if copy else array

    def to_pil(self):
        """Return an independent Pillow Image in RGBA mode."""
        from PIL import Image

        return Image.frombytes("RGBA", self.size, self.rgba)
