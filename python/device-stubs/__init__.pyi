"""Generated from device.py by tools/generate_stubs.py; do not edit."""

import os
from dataclasses import dataclass
from typing import Literal
from _cocoa._types import PathInput as PathInput
from _cocoa.requests import call as _call
__all__ = ['Platform', 'ThermalState', 'BatteryState', 'PathInput', 'DeviceInfo', 'BatteryInfo', 'StorageInfo', 'info', 'battery', 'storage']
type Platform = Literal['ios', 'macos']
type ThermalState = Literal['nominal', 'fair', 'serious', 'critical']
type BatteryState = Literal['charging', 'full', 'unplugged', 'unknown', 'unavailable', 'not_charging']

@dataclass(frozen=True)
class DeviceInfo:
    platform: Platform
    system_version: str
    machine: str
    cpu_count: int
    active_cpu_count: int
    physical_memory: int
    uptime: float
    low_power: bool
    thermal_state: ThermalState

@dataclass(frozen=True)
class BatteryInfo:
    level: float | None
    state: BatteryState

@dataclass(frozen=True)
class StorageInfo:
    total: int
    free: int

def info() -> DeviceInfo:
    ...

def battery() -> BatteryInfo:
    ...

def storage(path: PathInput='.') -> StorageInfo:
    ...
