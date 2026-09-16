"""Read device, battery, and storage information without requesting permissions."""

import os
from dataclasses import dataclass
from typing import Literal

from _cocoa._types import PathInput as PathInput
from _cocoa.requests import call as _call

__all__ = ["Platform", "ThermalState", "BatteryState", "PathInput", "DeviceInfo",
           "BatteryInfo", "StorageInfo", "info", "battery", "storage"]

type Platform = Literal["ios", "macos"]
type ThermalState = Literal["nominal", "fair", "serious", "critical"]
type BatteryState = Literal["charging", "full", "unplugged", "unknown", "unavailable", "not_charging"]


@dataclass(frozen=True)
class DeviceInfo:
    """Device snapshot. Memory uses bytes and uptime uses seconds since boot."""

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
    """Battery snapshot; level is 0..1, or None when no reading is available.

    Desktops without a battery report state="unavailable". macOS can report
    "not_charging" on external power. "full" follows the OS charged state and
    does not require a level of exactly 1.0.
    """

    level: float | None
    state: BatteryState


@dataclass(frozen=True)
class StorageInfo:
    """Total and free bytes on one filesystem."""

    total: int
    free: int


def info() -> DeviceInfo:
    """Return OS, machine, CPU, RAM, uptime, power mode, and thermal state.

    Memory uses bytes; uptime uses seconds. No persistent device identifier is
    included. This does not activate audio, location, or motion services.
    """
    return _call("device.info", convert=lambda value: DeviceInfo(**value))


def battery() -> BatteryInfo:
    """Return level (0..1 or None) and state.

    States are charging, full, unplugged, unknown, or unavailable. macOS also
    reports not_charging when connected to external power without charging or
    being reported charged by the OS. full follows the OS charged state, which
    need not mean exactly 100 percent. Desktops without a battery are unavailable.
    """
    return _call("device.battery", convert=lambda value: BatteryInfo(**value))


def storage(path: PathInput = ".") -> StorageInfo:
    """Return total and free bytes for the filesystem containing path."""
    return _call("device.storage", {"path": os.path.abspath(os.path.expanduser(os.fsdecode(path)))},
                 convert=lambda value: StorageInfo(**value))
