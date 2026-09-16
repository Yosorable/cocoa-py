"""Generated from motion.py by tools/generate_stubs.py; do not edit."""

import math
from dataclasses import dataclass
from typing import Any, Literal, cast, overload
from _cocoa.requests import Stream, call as _call
from _cocoa.requests import StreamStats as StreamStats
__all__ = ['Sensor', 'ReferenceFrame', 'MagneticAccuracy', 'MotionAvailability', 'Vector3', 'Attitude', 'Quaternion', 'AccelerometerSample', 'GyroscopeSample', 'MagnetometerSample', 'DeviceMotionSample', 'MotionSample', 'StreamStats', 'available', 'reference_frames', 'Watch', 'watch']
type Sensor = Literal['accelerometer', 'gyroscope', 'magnetometer', 'device']
type ReferenceFrame = Literal['arbitrary', 'arbitrary_corrected', 'magnetic_north', 'true_north']
type MagneticAccuracy = Literal['uncalibrated', 'low', 'medium', 'high']

@dataclass(frozen=True)
class MotionAvailability:
    accelerometer: bool
    gyroscope: bool
    magnetometer: bool
    device: bool

@dataclass(frozen=True)
class Vector3:
    x: float
    y: float
    z: float

@dataclass(frozen=True)
class Attitude:
    roll: float
    pitch: float
    yaw: float

@dataclass(frozen=True)
class Quaternion:
    x: float
    y: float
    z: float
    w: float

@dataclass(frozen=True)
class AccelerometerSample:
    timestamp: float
    acceleration: Vector3

@dataclass(frozen=True)
class GyroscopeSample:
    timestamp: float
    rotation_rate: Vector3

@dataclass(frozen=True)
class MagnetometerSample:
    timestamp: float
    magnetic_field: Vector3

@dataclass(frozen=True)
class DeviceMotionSample:
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

def available() -> MotionAvailability:
    ...

def reference_frames() -> list[ReferenceFrame]:
    ...

class Watch[Sample: MotionSample = MotionSample](Stream[Sample]):

    @overload
    def __init__(self: Watch[DeviceMotionSample], sensor: Literal['device']='device', *, interval: float=1 / 60, capacity: int=128, reference_frame: ReferenceFrame | None=None) -> None:
        ...

    @overload
    def __init__(self: Watch[AccelerometerSample], sensor: Literal['accelerometer'], *, interval: float=1 / 60, capacity: int=128, reference_frame: ReferenceFrame | None=None) -> None:
        ...

    @overload
    def __init__(self: Watch[GyroscopeSample], sensor: Literal['gyroscope'], *, interval: float=1 / 60, capacity: int=128, reference_frame: ReferenceFrame | None=None) -> None:
        ...

    @overload
    def __init__(self: Watch[MagnetometerSample], sensor: Literal['magnetometer'], *, interval: float=1 / 60, capacity: int=128, reference_frame: ReferenceFrame | None=None) -> None:
        ...

    @overload
    def __init__(self: Watch[MotionSample], sensor: Sensor, *, interval: float=1 / 60, capacity: int=128, reference_frame: ReferenceFrame | None=None) -> None:
        ...

@overload
def watch(sensor: Literal['device']='device', *, interval: float=1 / 60, capacity: int=128, reference_frame: ReferenceFrame | None=None) -> Watch[DeviceMotionSample]:
    ...

@overload
def watch(sensor: Literal['accelerometer'], *, interval: float=1 / 60, capacity: int=128, reference_frame: ReferenceFrame | None=None) -> Watch[AccelerometerSample]:
    ...

@overload
def watch(sensor: Literal['gyroscope'], *, interval: float=1 / 60, capacity: int=128, reference_frame: ReferenceFrame | None=None) -> Watch[GyroscopeSample]:
    ...

@overload
def watch(sensor: Literal['magnetometer'], *, interval: float=1 / 60, capacity: int=128, reference_frame: ReferenceFrame | None=None) -> Watch[MagnetometerSample]:
    ...

@overload
def watch(sensor: Sensor, *, interval: float=1 / 60, capacity: int=128, reference_frame: ReferenceFrame | None=None) -> Watch[MotionSample]:
    ...
