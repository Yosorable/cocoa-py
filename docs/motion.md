# Motion streams

`motion` reads accelerometer, gyroscope, magnetometer, and fused device-motion
data on supported iOS devices. Importing it does not start sensors or prompt.
`available()` returns a `MotionAvailability` record with four capability flags.
Native macOS returns `False` for
all four and an empty list from `reference_frames()`; starting a phone sensor
there raises `NotImplementedError`.

```python
import motion

if motion.available().device:
    with motion.watch(interval=0.05, capacity=1) as updates:
        sample = updates.read(timeout=3)
        if sample is not None:
            print(sample.gravity.z, sample.attitude.roll)
```

## Samples and units

Samples are immutable dataclasses, and three-dimensional `Vector3` values have
`x`, `y`, and `z` attributes. Every sample contains `timestamp`, in seconds since system boot. It is
not a Unix timestamp and must not be compared with `time.time()`.

| Sensor | Sample type | Fields in addition to `timestamp` |
| --- | --- | --- |
| `accelerometer` | `AccelerometerSample` | `acceleration`: acceleration including gravity, in m/s². |
| `gyroscope` | `GyroscopeSample` | `rotation_rate`: angular velocity in rad/s. |
| `magnetometer` | `MagnetometerSample` | `magnetic_field`: raw magnetic field in microteslas, including device bias. |
| `device` | `DeviceMotionSample` | `acceleration`, `gravity`, `rotation_rate`, `attitude`, `quaternion`, `reference_frame`, `magnetic_field`, and `magnetic_accuracy`. |

Both `watch("gyroscope")` and `Watch("gyroscope")` preserve the sample type in
`read()` and iteration. `read()` may also return `None` on timeout. When the sensor
is selected dynamically, `MotionSample` describes the union of all four records;
use `isinstance(sample, motion.GyroscopeSample)` to narrow it when necessary.
Use `dataclasses.asdict(sample)` when a dictionary is needed for serialization.

For device motion:

- `acceleration` is user acceleration with gravity removed, in m/s². It differs
  from the raw accelerometer's field with the same name. `gravity` is the gravity
  vector, also in m/s². Both come from Core Motion's sensor fusion.
- `rotation_rate` uses rad/s. `attitude` is an `Attitude` record with `roll`, `pitch`, and `yaw` in
  radians, as supplied by `CMAttitude`. `quaternion` is a `Quaternion` with `x`, `y`, `z`, and `w`
  components. Euler angles can wrap; use the quaternion for continuous rotations.
- `reference_frame` identifies the reference selected for the device-motion
  service. It describes attitude, not a rotation applied to every returned vector.
- `magnetic_field` is a calibrated field vector, or `None` when calibration is
  unavailable or the vector is invalid. `magnetic_accuracy` is `"uncalibrated"`,
  `"low"`, `"medium"`, or `"high"`. The field includes surrounding magnetic
  interference; calibration removes device bias, not every external influence.

Vectors follow Core Motion's device axes. On an upright iPhone facing you, +x
points right, +y points toward the top, and +z points out of the screen. These
axes follow the physical device, not the orientation of a host app's window.

Required measurements with non-finite values, invalid timestamps, or a missing
attitude are discarded. Invalid optional magnetic data does not discard an
otherwise valid attitude sample. Repeated or older timestamps within one
native sensor session are also discarded. These omissions do not increment
`stats.dropped`, which counts buffer overflow only.

## Timing and multiple watches

`watch(sensor="device", interval=1/60, capacity=128, reference_frame=None)`
starts a stream. `interval` must be a finite number between 0.01 and 1 second;
booleans are rejected. `capacity` must be an integer from 1 through 4096.

The library owns one `CMMotionManager`, with a separate native service and
callback queue for each sensor type. Watches of the same sensor share that
service. Its requested interval is the smallest interval of its active watches;
each watch has its own delivery schedule and bounded buffer. Adding or removing
a watch updates that interval without restarting the remaining watches.

Actual timing is best effort. The hardware may deliver fewer samples, and a
shared stream can deliver a sample shortly after its requested deadline.
Inspect measurement timestamps when timing matters. No artificial samples are
generated to fill a gap. Values from different raw sensor types are not promised
to be synchronized; use `device` motion for a fused snapshot.

`read(timeout=1)` returns the oldest buffered sample, or `None` when the wait
expires. A read timeout leaves the stream running. A full buffer discards its
oldest sample and increments `stats.dropped`. `stats` is a `StreamStats` record
with `capacity`, `buffered` and `dropped` attributes. Use `capacity=1` for controls
that need the most recent buffered reading instead of a history.

Closing a watch clears its buffer and unsubscribes it. Only closing the last
watch of a sensor stops that native service. Other sensor types keep running.
The idle manager remains available for future watches; it does not keep sensors
running. Use `with`, or call `close()` explicitly, including when a loop exits
with an exception.

## Attitude reference frames

`reference_frames()` returns the supported frame names without starting a
sensor. The `reference_frame` argument applies only to `sensor="device"`;
passing it to a raw sensor raises `ValueError`. `None` selects `"arbitrary"`.

| Name | Reference |
| --- | --- |
| `arbitrary` | Vertical z-axis and an arbitrary horizontal x-axis. Yaw may drift. |
| `arbitrary_corrected` | The same arrangement, using a calibrated magnetometer when available to correct yaw drift. |
| `magnetic_north` | Vertical z-axis and x-axis pointing toward magnetic north. |
| `true_north` | Vertical z-axis and x-axis pointing toward geographic north. |

```python
if "magnetic_north" in motion.reference_frames():
    with motion.watch(reference_frame="magnetic_north") as updates:
        sample = updates.read(timeout=5)
        if sample is not None:
            print(sample.reference_frame, sample.magnetic_accuracy)
```

All simultaneous device-motion watches must request the same frame. A conflicting
watch raises `ValueError` and leaves the existing stream unchanged. Close those
watches before selecting another frame. The module does not convert between
frames or silently change a running watch's reference.

Unsupported frames raise `NotImplementedError`. A supported frame may still be
unusable because calibration or location information is unavailable. Core Motion
errors are returned as Python exceptions; the library does not open calibration
UI or start a separate location request. True north should not be assumed to work
just because the frame appears in the capability list.

## Errors and host integration

The host must include `NSMotionUsageDescription`. Hardware availability is not
a generic authorization status: this module does not substitute activity or
pedometer authorization for raw-sensor access. System authorization failures
raise `PermissionError`; other Core Motion errors raise `OSError`.

A native service error terminates and unsubscribes its watches, preserving the
error for their next read. It does not stop other sensor types. Close the Python
watch objects normally, and create a new watch to retry. Closing also prevents
late callbacks from delivering data or errors to a later sensor session.

This API provides live foreground streams, not background recording, step counts,
activity classification, or headphone tracking. Host execution in the background
does not guarantee sensor delivery. No samples are saved or transmitted by the
module itself.

See Apple's [motion manager guidance](https://developer.apple.com/documentation/coremotion/cmmotionmanager),
[attitude reference frames](https://developer.apple.com/documentation/coremotion/cmattitudereferenceframe),
and [calibrated magnetic fields](https://developer.apple.com/documentation/coremotion/cmdevicemotion/magneticfield).
