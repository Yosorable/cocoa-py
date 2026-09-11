"""scene._enums — pure enum types with zero internal dependencies.

Kept dependency-free so any other scene submodule (including low-level ones
like ``gpu``) can import these without triggering circular imports.

All enums here subclass ``StrEnum`` so they are interchangeable with their
string values: ``Orientation.LANDSCAPE == "landscape"`` is ``True``, JSON
serialisation produces a plain string, and existing user code that passes
raw strings continues to work.
"""
from __future__ import annotations

from enum import StrEnum


class Orientation(StrEnum):
    """Scene window interface orientation lock.

    - AUTO      : follow device / app default (no lock)
    - PORTRAIT  : lock to portrait
    - LANDSCAPE : lock to landscape (system picks left or right based on device)
    """
    AUTO = "auto"
    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"


class FillRule(StrEnum):
    """Polygon fill rule for paths and contours.

    - EVEN_ODD : alternate fill on each crossing (SVG default)
    - NON_ZERO : fill where winding number is non-zero
    """
    EVEN_ODD = "even_odd"
    NON_ZERO = "non_zero"

    @classmethod
    def _missing_(cls, value):
        # Accept case/separator variants: "EVEN_ODD", "evenodd", "even-odd",
        # "NonZero", "non-zero", etc.
        if isinstance(value, str):
            v = value.lower().replace("-", "_").replace(" ", "_")
            if v in ("evenodd", "even_odd"):
                return cls.EVEN_ODD
            if v in ("nonzero", "non_zero"):
                return cls.NON_ZERO
        return None


class LineJoin(StrEnum):
    """Stroke line join style."""
    MITER = "miter"
    BEVEL = "bevel"
    ROUND = "round"


class LineCap(StrEnum):
    """Stroke line cap style."""
    BUTT = "butt"
    SQUARE = "square"
    ROUND = "round"


class TouchPhase(StrEnum):
    """Touch event lifecycle phase.

    The native layer reports these as small ints; the Python side maps them
    to this enum so user code can write ``if touch.phase is TouchPhase.BEGAN``
    or the equivalent ``if touch.phase == "began"``.
    """
    BEGAN = "began"
    MOVED = "moved"
    ENDED = "ended"
    CANCELLED = "cancelled"


class Alignment(StrEnum):
    """Cross-axis alignment for HStack/VStack.

    Note: ZStack uses freeform substring matching (e.g. ``"topleft"``) and
    does NOT use this enum.
    """
    START = "start"
    CENTER = "center"
    END = "end"
