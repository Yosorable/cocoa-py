"""Read device, battery, and storage information without requesting permissions."""

import os
from _cocoa.requests import call as _call

__all__ = ["info", "battery", "storage"]


def info() -> dict:
    """Return OS, machine, CPU, RAM, uptime, power mode, and thermal state.

    Memory uses bytes; uptime uses seconds. No persistent device identifier is
    included. This does not activate audio, location, or motion services.
    """
    return _call("device.info")


def battery() -> dict:
    """Return level (0..1 or None) and state.

    States are charging, full, unplugged, unknown, or unavailable. macOS also
    reports not_charging when connected to external power without charging or
    being reported charged by the OS. full follows the OS charged state, which
    need not mean exactly 100 percent. Desktops without a battery are unavailable.
    """
    return _call("device.battery")


def storage(path=".") -> dict:
    """Return total and free bytes for the filesystem containing path."""
    return _call("device.storage", {"path": os.path.abspath(os.path.expanduser(os.fsdecode(path)))})
