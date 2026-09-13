"""Bounded iOS motion sensor streams using Core Motion.

Native macOS reports unavailable sensors. Acceleration uses m/s², rotation uses
rad/s, magnetic fields use microteslas, and timestamps are seconds since boot.
"""

import math
from _cocoa.requests import Stream, call as _call

__all__ = ["available", "reference_frames", "Watch", "watch"]

_FRAMES = ("arbitrary", "arbitrary_corrected", "magnetic_north", "true_north")


def available() -> dict[str, bool]:
    """Report accelerometer, gyroscope, magnetometer, and device motion support."""
    return _call("motion.available")


def reference_frames() -> list[str]:
    """Return supported device-motion reference frames without starting sensors.

    Native macOS returns an empty list. Availability does not guarantee a
    calibrated reading or that true north can currently be determined.
    """
    return _call("motion.reference_frames")


class Watch(Stream):
    """Read bounded motion dictionaries; close or use a with statement.

    interval is a requested period from 0.01 through 1 second. Watches share
    one native manager, with separate queues and per-watch delivery intervals.
    Actual timing depends on hardware; inspect sample timestamps.

    reference_frame applies only to sensor="device". None selects "arbitrary";
    other names are "arbitrary_corrected", "magnetic_north", and "true_north".
    Simultaneous device watches must use the same frame. Device samples report
    reference_frame, magnetic_field (a vector or None), and magnetic_accuracy
    (uncalibrated, low, medium, or high), alongside acceleration and attitude.
    """

    def __init__(self, sensor="device", *, interval=1 / 60, capacity=128, reference_frame=None):
        if sensor not in ("accelerometer", "gyroscope", "magnetometer", "device"):
            raise ValueError("Unknown motion sensor")
        if not isinstance(capacity, int) or isinstance(capacity, bool):
            raise TypeError("capacity must be an integer")
        if not 1 <= capacity <= 4096:
            raise ValueError("capacity must be between 1 and 4096")
        if not isinstance(interval, (int, float)) or isinstance(interval, bool):
            raise TypeError("interval must be a number")
        if not 0.01 <= interval <= 1 or not math.isfinite(interval):
            raise ValueError("interval must be between 0.01 and 1 second")
        if reference_frame is not None and (sensor != "device" or reference_frame not in _FRAMES):
            raise ValueError("reference_frame must name a device-motion reference frame")
        super().__init__("motion.watch", dict(sensor=sensor, interval=interval, capacity=capacity,
                                             reference_frame=reference_frame))


def watch(sensor="device", **options) -> Watch:
    """Start a sensor stream. Use a with statement to stop it reliably."""
    return Watch(sensor, **options)
