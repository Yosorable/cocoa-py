"""Bounded iOS motion sensor streams using Core Motion.

Native macOS reports unavailable sensors. Acceleration uses m/s², rotation uses
rad/s, magnetic fields use microteslas, and timestamps are seconds since boot.
"""

from _cocoa.requests import Stream, call as _call

__all__ = ["available", "Watch", "watch"]


def available() -> dict[str, bool]:
    """Report accelerometer, gyroscope, magnetometer, and device motion support."""
    return _call("motion.available")


class Watch(Stream):
    """Read motion dictionaries without running Python in a sensor callback."""

    def __init__(self, sensor="device", *, interval=1 / 60, capacity=128):
        if sensor not in ("accelerometer", "gyroscope", "magnetometer", "device"):
            raise ValueError("Unknown motion sensor")
        if not isinstance(capacity, int) or isinstance(capacity, bool):
            raise TypeError("capacity must be an integer")
        super().__init__("motion.watch", dict(sensor=sensor, interval=interval, capacity=capacity))


def watch(sensor="device", **options) -> Watch:
    """Start a sensor stream. Use a with statement to stop it reliably."""
    return Watch(sensor, **options)
