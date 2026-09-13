"""Foreground location, compass headings, and Apple's address geocoder.

Coordinates use degrees, distances use meters, speed uses meters/second, and
timestamps use Unix seconds. Geocoding may contact Apple and requires network
access. Location permissions are requested only by explicit calls.
"""

from dataclasses import dataclass
from _cocoa.requests import Stream, call as _call

__all__ = ["Coordinates", "Place", "Watch", "status", "permission", "request_permission",
           "current", "watch", "geocode", "reverse_geocode", "Heading", "HeadingWatch",
           "heading_available", "heading", "watch_heading"]


@dataclass(frozen=True)
class Coordinates:
    latitude: float
    longitude: float
    altitude: float | None
    horizontal_accuracy: float
    vertical_accuracy: float | None
    speed: float | None
    course: float | None
    timestamp: float


@dataclass(frozen=True)
class Heading:
    """Compass angles clockwise from north, with accuracy in degrees."""

    magnetic_heading: float
    true_heading: float | None
    accuracy: float
    timestamp: float


@dataclass(frozen=True)
class Place:
    latitude: float | None
    longitude: float | None
    name: str | None
    street: str | None
    street_number: str | None
    city: str | None
    region: str | None
    country: str | None
    country_code: str | None
    postal_code: str | None
    time_zone: str | None


def status() -> dict:
    """Return permission, whether Location Services are enabled, and precision."""
    return _call("location.status")


def permission() -> str:
    """Return not_determined, authorized, denied, or restricted without prompting."""
    return status()["permission"]


def request_permission(*, timeout=120) -> str:
    """Request foreground location access and return the resulting status."""
    return _call("location.request_permission", timeout=timeout)


def _options(accuracy, max_age, distance_filter=0, capacity=128):
    if not isinstance(capacity, int) or isinstance(capacity, bool):
        raise TypeError("capacity must be an integer")
    return dict(accuracy=accuracy, max_age=max_age, distance_filter=distance_filter, capacity=capacity)


def current(*, timeout=30, accuracy=10, max_age=15) -> Coordinates:
    """Get one valid fix; request permission if needed and stop the sensor after.

    accuracy is a desired accuracy in meters, not a guarantee. max_age rejects
    older cached fixes; zero accepts only samples measured after updates start.
    The newest valid sample in a delivered batch is returned. Inspect
    horizontal_accuracy on the returned sample.
    """
    return _call("location.current", _options(accuracy, max_age), timeout=timeout,
                 convert=lambda value: Coordinates(**value))


class Watch(Stream):
    """Continuous foreground fixes in delivery order. Use with or close.

    max_age=0 rejects samples measured before this watch starts updating.
    """

    def __init__(self, *, accuracy=10, max_age=15, distance_filter=0, capacity=128):
        super().__init__("location.watch", _options(accuracy, max_age, distance_filter, capacity),
                         lambda value: Coordinates(**value))


def watch(**options) -> Watch:
    """Start a Watch; see Watch for sampling options."""
    return Watch(**options)


def heading_available() -> bool:
    """Check compass hardware support without starting sensors or prompting."""
    return _call("location.heading_available")


def _heading_options(max_age, true_north, orientation, angle_filter=1, capacity=128):
    if not isinstance(true_north, bool):
        raise TypeError("true_north must be a bool")
    if not isinstance(capacity, int) or isinstance(capacity, bool):
        raise TypeError("capacity must be an integer")
    return dict(max_age=max_age, true_north=true_north, orientation=orientation,
                angle_filter=angle_filter, capacity=capacity)


def heading(*, timeout=10, max_age=5, true_north=False, orientation="portrait") -> Heading:
    """Get one reliable compass sample, then stop its sensors.

    Magnetic headings do not request location permission. true_north=True
    also starts foreground location updates and waits for a valid true heading.
    orientation sets the reference edge: portrait, portrait_upside_down,
    landscape_left, or landscape_right. max_age=0 rejects pre-start samples.
    Unsupported hardware raises NotImplementedError; no valid sample before
    the deadline raises TimeoutError.
    """
    return _call("location.heading", _heading_options(max_age, true_north, orientation),
                 timeout=timeout, convert=lambda value: Heading(**value))


class HeadingWatch(Stream):
    """Bounded compass updates. Use with or explicitly close.

    angle_filter is the minimum change in degrees, from 0 through 180; zero
    requests all updates. true_north=True also starts location updates and
    withholds samples until their true heading is valid. Other options match
    heading(). Invalid or stale readings are skipped, not queued.
    """

    def __init__(self, *, angle_filter=1, max_age=5, true_north=False,
                 orientation="portrait", capacity=128):
        super().__init__("location.watch_heading",
                         _heading_options(max_age, true_north, orientation, angle_filter, capacity),
                         lambda value: Heading(**value))


def watch_heading(**options) -> HeadingWatch:
    """Start a HeadingWatch; see HeadingWatch for sampling options."""
    return HeadingWatch(**options)


def geocode(address: str, *, timeout=30) -> list[Place]:
    """Resolve an address through Apple; does not require location permission."""
    return _call("location.geocode", {"address": address}, timeout=timeout,
                 convert=lambda values: [Place(**value) for value in values])


def reverse_geocode(latitude, longitude, *, timeout=30) -> list[Place]:
    """Resolve coordinates through Apple; does not read the device location."""
    return _call("location.reverse_geocode", {"latitude": latitude, "longitude": longitude}, timeout=timeout,
                 convert=lambda values: [Place(**value) for value in values])
