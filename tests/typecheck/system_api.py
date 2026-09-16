"""Static assertions against installed modules; these functions are never run."""

from collections import UserDict
from pathlib import Path
from typing import assert_type

from PIL.Image import Image

import clipboard
import device
import location
import motion
import share


def results(sensor: motion.Sensor, as_bytes: bool) -> None:
    assert_type(device.info(), device.DeviceInfo)
    assert_type(device.info().platform, device.Platform)
    assert_type(device.battery().level, float | None)
    assert_type(device.battery().state, device.BatteryState)
    assert_type(device.storage(Path(".")), device.StorageInfo)
    assert_type(device.storage(b".").free, int)
    assert_type(location.status(), location.LocationStatus)
    assert_type(location.permission(), location.Permission)
    assert_type(location.request_permission(timeout=None), location.Permission)
    assert_type(location.geocode("London"), list[location.Place])
    assert_type(location.current(), location.Coordinates)
    with location.watch(distance_filter=10, capacity=4) as positions:
        assert_type(positions, location.Watch)
        assert_type(positions.read(), location.Coordinates | None)
        assert_type(next(iter(positions)), location.Coordinates)
        assert_type(positions.stats, location.StreamStats)
    with location.watch_heading(orientation="landscape_left") as headings:
        assert_type(headings.read(), location.Heading | None)
        assert_type(next(iter(headings)), location.Heading)

    assert_type(motion.available(), motion.MotionAvailability)
    assert_type(motion.reference_frames(), list[motion.ReferenceFrame])
    with motion.watch() as updates:
        assert_type(updates, motion.Watch[motion.DeviceMotionSample])
        assert_type(updates.read(), motion.DeviceMotionSample | None)
        sample = next(iter(updates))
        assert_type(sample.acceleration, motion.Vector3)
        assert_type(sample.magnetic_field, motion.Vector3 | None)
        assert_type(sample.attitude.roll, float)
        assert_type(updates.stats, motion.StreamStats)
    assert_type(motion.watch("accelerometer").read(), motion.AccelerometerSample | None)
    assert_type(motion.watch("gyroscope").read(), motion.GyroscopeSample | None)
    assert_type(motion.watch("magnetometer").read(), motion.MagnetometerSample | None)
    assert_type(motion.watch(sensor).read(), motion.MotionSample | None)
    assert_type(motion.Watch().read(), motion.DeviceMotionSample | None)
    assert_type(motion.Watch("accelerometer").read(), motion.AccelerometerSample | None)
    assert_type(motion.Watch("gyroscope").read(), motion.GyroscopeSample | None)
    assert_type(motion.Watch("magnetometer").read(), motion.MagnetometerSample | None)
    assert_type(motion.Watch(sensor).read(), motion.MotionSample | None)

    assert_type(clipboard.read_image(), Image | None)
    assert_type(clipboard.read_image(as_bytes=True), bytes | None)
    assert_type(clipboard.read_image(as_bytes=as_bytes), Image | bytes | None)
    clipboard.write_image(memoryview(b"encoded image"))
    clipboard.write_item(UserDict({"public.data": bytearray(b"content")}))
    assert_type(share.present(files=[Path("report.txt")], images=[b"encoded image"]), share.ShareResult)
    with share.open(attachments=UserDict({"report.txt": memoryview(b"content")})) as request:
        assert_type(request, share.ShareRequest)
        assert_type(request.wait(None), share.ShareResult)
        assert_type(request.done, bool)
