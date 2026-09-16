# System module types

`device`, `location`, `motion`, `clipboard` and `share` provide explicit parameter
types and installed companion stub packages. These describe the current source API;
the previously published 0.1.0a5 wheel predates these result types.

## Records and mappings

Fixed results use immutable dataclasses:

| API | Result |
| --- | --- |
| `device.info()` | `DeviceInfo` |
| `device.battery()` | `BatteryInfo` |
| `device.storage(path)` | `StorageInfo` |
| `location.status()` | `LocationStatus` |
| `location.current()` / `heading()` | `Coordinates` / `Heading` |
| `location.geocode()` / `reverse_geocode()` | `list[Place]` |
| `motion.available()` | `MotionAvailability` |
| `motion.watch(sensor).read()` | The sensor's sample type, or `None` on timeout |
| A location or motion watch's `stats` | `StreamStats` |
| `share.present()` / `ShareRequest.wait()` | `ShareResult` |

Use attributes to access fields and `dataclasses.asdict()` to obtain a dictionary:

```python
from dataclasses import asdict
import device

battery = device.battery()
if battery.level is not None:
    print(f"{battery.level:.0%}")
print(asdict(battery))
```

Optional measurements explicitly allow `None`. An unavailable battery reports
`BatteryInfo(level=None, state="unavailable")`; macOS also supports the
`"not_charging"` state. Native macOS motion capability flags are all false and
starting a sensor raises `NotImplementedError`. The native adapters define the
record fields across platforms; these are not unfiltered operating-system
metadata dictionaries. See the [API guide](api.md), [location guide](location.md)
and [motion guide](motion.md) for units, availability and field semantics.

Share attachments and clipboard representations remain mappings because their
keys are supplied by the caller. Buffer arguments use `collections.abc.Buffer`
and require contiguous data where documented. `ImageInput` accepts a buffer or
a Pillow image. `PathInput` accepts strings, bytes and `os.PathLike` objects.
Neither the annotations nor importing these modules imports Pillow or NumPy.

## Inference and validation

Watch factories expose their keyword arguments directly. Both motion factories
and constructors infer their sample type from a literal sensor name:

```python
import motion

with motion.watch("gyroscope", interval=0.05) as updates:
    sample = updates.read(timeout=1)
    if sample is not None:
        print(sample.rotation_rate.z)
```

`read()` preserves the possibility of a timeout; iteration yields only samples.
Context managers preserve the concrete request or watch type. String choices
such as `Sensor`, `ReferenceFrame`, `Orientation` and `BatteryState` are `Literal`
type aliases; callers continue to pass ordinary strings.

`clipboard.read_image(as_bytes=True)` returns `bytes | None`. The default returns
`PIL.Image.Image | None` and requires Pillow. Existing runtime validation still
checks inputs; type annotations alone do not enforce numeric ranges or validate
manually constructed dataclasses.

## Maintaining declarations

Inline Python annotations are the source of truth. Regenerate the companion
`*-stubs/__init__.pyi` files after changing a public signature or result type:

```sh
python3.14 tools/generate_stubs.py
python3.14 tools/generate_stubs.py --check
```

Wheels and source-based embedded installations include the same declarations.
To check consumer inference and rejection of invalid calls using an installed
build, install `mypy` and `pyright` in the development environment, then run:

```sh
python3.14 -m unittest discover -s tests -p test_typing.py -v
```
