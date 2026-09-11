# cocoa-py

Native Apple platform modules for Python, maintained independently of their
original host, [Pythona](https://pythona.app).

One distribution provides independent top-level imports:

| Module | Capabilities |
| --- | --- |
| `audio` | File and PCM playback, streaming recording, effects, rate/pitch control, offline mixing and WAV/CAF/M4A export. |
| `scene` | Metal-rendered 2D scenes, shapes, sprites, text, animation, input and Box2D physics. |
| `coreml` | Core ML model compilation, inspection and NumPy-based inference. |
| `photos` | System media picker and saving images/videos to Photos. |
| `location` | Foreground location fixes, bounded update streams and address geocoding. |
| `motion` | Bounded iOS accelerometer, gyroscope, magnetometer and device-motion streams. |
| `clipboard` | Text and typed byte representations on the system clipboard. |
| `share` | System sharing UI for text, URLs and files. |
| `device` | OS, hardware, power, battery and storage information. |
| `notification` | Permission, scheduling and management of local notifications. |

There is no `cocoa.` import prefix. Importing one module does not import the
others or request permissions. `coreml` initializes its NumPy interface when
imported; the other modules do not require NumPy unless using an array helper.
Rubicon-ObjC is not a dependency.

## Status and installation

The development source is **0.1.0a2**. The published **0.1.0a1** preview on PyPI
contains **only Core ML**; installing that version does not install the module
collection described above.

The current source targets **macOS 14+**, **iOS 17+**, and standard **CPython 3.14
with the GIL**. macOS wheels are built with Apple's SDK; iOS hosts integrate the
source when building their application. Native macOS motion sensors are not
available and are reported as unsupported.

To build the current development version on a Mac with Xcode command-line tools:

```sh
git clone https://github.com/Yosorable/cocoa-py.git
cd cocoa-py
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install '.[coreml]'
```

The `coreml` extra installs NumPy for inference. Build isolation uses NumPy's
headers regardless of whether that runtime extra is selected.

## Start using the modules

```python
import device
import audio

print(device.info())
with audio.Sound("sound.wav") as sound:
    channel = sound.play()
    channel.wait()
```

For permission-sensitive desktop scripts, use the supplied launcher:

```sh
cocoa-py my_script.py
cocoa-py -m my_package
```

The launcher keeps the current virtual environment and runs CPython in a small
macOS app with the required usage descriptions. It does not install a second
Python distribution. Ordinary `python` remains suitable for offline audio,
Core ML inference and scene windows. See [macOS execution](docs/macos.md) for
permissions, IDE use and event-loop requirements.

```python
import location

position = location.current(timeout=30)
print(position.latitude, position.longitude, position.horizontal_accuracy)

with location.watch(distance_filter=10) as updates:
    sample = updates.read(timeout=10)
    print(sample)
```

The host application's system permissions apply to scripts running inside it.
Applications embedding these modules must supply their own usage descriptions
and lifecycle integration; see [iOS embedding](docs/embedding.md).

## Documentation

- [Module API guide](docs/api.md)
- [macOS execution and permissions](docs/macos.md)
- [Embedding in an iOS application](docs/embedding.md)
- [Architecture and resource ownership](docs/architecture.md)
- [Third-party code](docs/third-party.md)
- [Initial PyPI release](docs/releases/0.1.0a1.md)

The Python wrappers contain full signatures and docstrings, available through
`help(audio.Sound)`, `help(location.Watch)`, and equivalent Python introspection.

## Build and validate

```sh
python -m pip install build
MACOSX_DEPLOYMENT_TARGET=14.0 python -m build
python -m unittest discover -s tests -v
COCOA_PY_UI_TESTS=1 python -m unittest discover -s tests -v
```

Tests exercise actual native audio rendering, Metal readback, Box2D and Core ML.
The UI option briefly opens desktop scene and sharing windows without selecting
or sharing personal data. `COCOA_PY_NETWORK_TESTS=1` additionally exercises Apple's
geocoder with a public address. Permission prompts, recording, phone sensors,
Photos selection and hardware routing are separate device checks.

## License

MIT. Copyright (c) 2026 Yosorable. Box2D retains its upstream MIT license.
