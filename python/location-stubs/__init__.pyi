"""Generated from location.py by tools/generate_stubs.py; do not edit."""

from dataclasses import dataclass
from typing import Literal
from _cocoa.requests import Stream, call as _call
from _cocoa.requests import StreamStats as StreamStats
__all__ = ['Coordinates', 'Place', 'Watch', 'status', 'permission', 'request_permission', 'current', 'watch', 'geocode', 'reverse_geocode', 'Heading', 'HeadingWatch', 'heading_available', 'heading', 'watch_heading', 'LocationStatus', 'Permission', 'Orientation', 'StreamStats']
type Permission = Literal['not_determined', 'authorized', 'denied', 'restricted']
type Orientation = Literal['portrait', 'portrait_upside_down', 'landscape_left', 'landscape_right']

@dataclass(frozen=True)
class LocationStatus:
    permission: Permission
    enabled: bool
    precise: bool

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

def status() -> LocationStatus:
    ...

def permission() -> Permission:
    ...

def request_permission(*, timeout: float | None=120) -> Permission:
    ...

def current(*, timeout: float | None=30, accuracy: float=10, max_age: float=15) -> Coordinates:
    ...

class Watch(Stream[Coordinates]):

    def __init__(self, *, accuracy: float=10, max_age: float=15, distance_filter: float=0, capacity: int=128) -> None:
        ...

def watch(*, accuracy: float=10, max_age: float=15, distance_filter: float=0, capacity: int=128) -> Watch:
    ...

def heading_available() -> bool:
    ...

def heading(*, timeout: float | None=10, max_age: float=5, true_north: bool=False, orientation: Orientation='portrait') -> Heading:
    ...

class HeadingWatch(Stream[Heading]):

    def __init__(self, *, angle_filter: float=1, max_age: float=5, true_north: bool=False, orientation: Orientation='portrait', capacity: int=128) -> None:
        ...

def watch_heading(*, angle_filter: float=1, max_age: float=5, true_north: bool=False, orientation: Orientation='portrait', capacity: int=128) -> HeadingWatch:
    ...

def geocode(address: str, *, timeout: float | None=30) -> list[Place]:
    ...

def reverse_geocode(latitude: float, longitude: float, *, timeout: float | None=30) -> list[Place]:
    ...
