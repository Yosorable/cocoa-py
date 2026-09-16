"""Bounded iOS motion sensor streams using Core Motion.

Native macOS reports unavailable sensors. Acceleration uses m/s², rotation uses
rad/s, magnetic fields use microteslas, and timestamps are seconds since boot.
"""

import math
from dataclasses import dataclass
from typing import Any, Literal, cast, overload

from _cocoa.requests import Stream, call as _call
from _cocoa.requests import StreamStats as StreamStats

__all__ = ["Sensor", "ReferenceFrame", "MagneticAccuracy", "MotionAvailability",
           "Vector3", "Attitude", "Quaternion", "AccelerometerSample", "GyroscopeSample",
           "MagnetometerSample", "DeviceMotionSample", "MotionSample", "StreamStats",
           "available", "reference_frames", "Watch", "watch"]

type Sensor = Literal["accelerometer", "gyroscope", "magnetometer", "device"]
type ReferenceFrame = Literal["arbitrary", "arbitrary_corrected", "magnetic_north", "true_north"]
type MagneticAccuracy = Literal["uncalibrated", "low", "medium", "high"]

_FRAMES = ("arbitrary", "arbitrary_corrected", "magnetic_north", "true_north")


@dataclass(frozen=True)
class MotionAvailability:
    """Hardware capability flags; querying them does not start sensors."""

    accelerometer: bool
    gyroscope: bool
    magnetometer: bool
    device: bool


@dataclass(frozen=True)
class Vector3:
    """A vector in device axes; units depend on the containing sample field."""

    x: float
    y: float
    z: float


@dataclass(frozen=True)
class Attitude:
    """Euler angles in radians, relative to the selected reference frame."""

    roll: float
    pitch: float
    yaw: float


@dataclass(frozen=True)
class Quaternion:
    """Orientation relative to the selected reference frame."""

    x: float
    y: float
    z: float
    w: float


@dataclass(frozen=True)
class AccelerometerSample:
    """Acceleration including gravity, in m/s²; timestamp is seconds since boot."""

    timestamp: float
    acceleration: Vector3


@dataclass(frozen=True)
class GyroscopeSample:
    """Angular velocity in rad/s; timestamp is seconds since boot."""

    timestamp: float
    rotation_rate: Vector3


@dataclass(frozen=True)
class MagnetometerSample:
    """Raw magnetic field in microteslas; timestamp is seconds since boot."""

    timestamp: float
    magnetic_field: Vector3


@dataclass(frozen=True)
class DeviceMotionSample:
    """Fused motion, with acceleration excluding gravity.

    Acceleration and gravity use m/s², rotation uses rad/s, and timestamp uses
    seconds since boot. magnetic_field is in microteslas, or None if calibrated
    data is unavailable; magnetic_accuracy is then "uncalibrated".
    """

    timestamp: float
    acceleration: Vector3
    gravity: Vector3
    rotation_rate: Vector3
    attitude: Attitude
    quaternion: Quaternion
    reference_frame: ReferenceFrame
    magnetic_field: Vector3 | None
    magnetic_accuracy: MagneticAccuracy


type MotionSample = AccelerometerSample | GyroscopeSample | MagnetometerSample | DeviceMotionSample


def _sample(sensor: Sensor, value: dict[str, Any]) -> MotionSample:
    timestamp = value["timestamp"]
    if sensor == "accelerometer":
        return AccelerometerSample(timestamp, Vector3(**value["acceleration"]))
    if sensor == "gyroscope":
        return GyroscopeSample(timestamp, Vector3(**value["rotation_rate"]))
    if sensor == "magnetometer":
        return MagnetometerSample(timestamp, Vector3(**value["magnetic_field"]))
    return DeviceMotionSample(
        timestamp=timestamp,
        acceleration=Vector3(**value["acceleration"]),
        gravity=Vector3(**value["gravity"]),
        rotation_rate=Vector3(**value["rotation_rate"]),
        attitude=Attitude(**value["attitude"]),
        quaternion=Quaternion(**value["quaternion"]),
        reference_frame=value["reference_frame"],
        magnetic_field=None if value["magnetic_field"] is None else Vector3(**value["magnetic_field"]),
        magnetic_accuracy=value["magnetic_accuracy"],
    )


def available() -> MotionAvailability:
    """Report accelerometer, gyroscope, magnetometer, and device motion support."""
    return _call("motion.available", convert=lambda value: MotionAvailability(**value))


def reference_frames() -> list[ReferenceFrame]:
    """Return supported device-motion reference frames without starting sensors.

    Native macOS returns an empty list. Availability does not guarantee a
    calibrated reading or that true north can currently be determined.
    """
    return _call("motion.reference_frames")


class Watch[Sample: MotionSample = MotionSample](Stream[Sample]):
    """Read immutable motion samples; close or use a with statement.

    interval is a requested period from 0.01 through 1 second. Watches share
    one native manager, with separate queues and per-watch delivery intervals.
    Actual timing depends on hardware; inspect sample timestamps.

    reference_frame applies only to sensor="device". None selects "arbitrary";
    other names are "arbitrary_corrected", "magnetic_north", and "true_north".
    Simultaneous device watches must use the same frame. Device samples report
    reference_frame, magnetic_field (a vector or None), and magnetic_accuracy
    (uncalibrated, low, medium, or high), alongside acceleration and attitude.
    """

    @overload
    def __init__(self: Watch[DeviceMotionSample], sensor: Literal["device"] = "device", *,
                 interval: float = 1 / 60, capacity: int = 128,
                 reference_frame: ReferenceFrame | None = None) -> None: ...

    @overload
    def __init__(self: Watch[AccelerometerSample], sensor: Literal["accelerometer"], *,
                 interval: float = 1 / 60, capacity: int = 128,
                 reference_frame: ReferenceFrame | None = None) -> None: ...

    @overload
    def __init__(self: Watch[GyroscopeSample], sensor: Literal["gyroscope"], *,
                 interval: float = 1 / 60, capacity: int = 128,
                 reference_frame: ReferenceFrame | None = None) -> None: ...

    @overload
    def __init__(self: Watch[MagnetometerSample], sensor: Literal["magnetometer"], *,
                 interval: float = 1 / 60, capacity: int = 128,
                 reference_frame: ReferenceFrame | None = None) -> None: ...

    @overload
    def __init__(self: Watch[MotionSample], sensor: Sensor, *,
                 interval: float = 1 / 60, capacity: int = 128,
                 reference_frame: ReferenceFrame | None = None) -> None: ...

    def __init__(self, sensor: Sensor = "device", *, interval: float = 1 / 60,
                 capacity: int = 128, reference_frame: ReferenceFrame | None = None) -> None:
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
                                             reference_frame=reference_frame),
                         lambda value: cast(Sample, _sample(sensor, value)))


@overload
def watch(sensor: Literal["device"] = "device", *, interval: float = 1 / 60,
          capacity: int = 128, reference_frame: ReferenceFrame | None = None) -> Watch[DeviceMotionSample]: ...


@overload
def watch(sensor: Literal["accelerometer"], *, interval: float = 1 / 60,
          capacity: int = 128, reference_frame: ReferenceFrame | None = None) -> Watch[AccelerometerSample]: ...


@overload
def watch(sensor: Literal["gyroscope"], *, interval: float = 1 / 60,
          capacity: int = 128, reference_frame: ReferenceFrame | None = None) -> Watch[GyroscopeSample]: ...


@overload
def watch(sensor: Literal["magnetometer"], *, interval: float = 1 / 60,
          capacity: int = 128, reference_frame: ReferenceFrame | None = None) -> Watch[MagnetometerSample]: ...


@overload
def watch(sensor: Sensor, *, interval: float = 1 / 60,
          capacity: int = 128, reference_frame: ReferenceFrame | None = None) -> Watch[MotionSample]: ...


def watch(sensor: Sensor = "device", *, interval: float = 1 / 60,
          capacity: int = 128, reference_frame: ReferenceFrame | None = None) -> Watch[MotionSample]:
    """Start a sensor stream. Use a with statement to stop it reliably."""
    return Watch(sensor, interval=interval, capacity=capacity, reference_frame=reference_frame)
