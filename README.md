# cocoa-py

Native Apple platform modules for Python, maintained independently of their
original host, [Pythona](https://pythona.app).

One distribution provides independent top-level imports:

| Module | Capabilities |
| --- | --- |
| `audio` | File and PCM playback, streaming recording, effects, rate/pitch control, offline mixing and WAV/CAF/M4A export. |
| `scene` | Metal-rendered 2D scenes, shapes, sprites, text, animation, controls, text editing, image capture and Box2D physics. |
| `coreml` | Core ML model compilation, inspection and NumPy-based inference. |
| `photos` | System media picker and saving images/videos to Photos. |
| `location` | Foreground location fixes, compass readings, bounded update streams and address geocoding. |
| `motion` | Shared iOS motion sensors with independent bounded streams and attitude reference frames. |
| `clipboard` | Text, URLs, images, multiple representations and clipboard state queries. |
| `share` | System sharing UI for text, URLs, files, images and in-memory attachments. |
| `device` | OS, hardware, power, battery and storage information. |

There is no `cocoa.` import prefix. Importing one module does not import the
others or request permissions. `coreml` initializes its NumPy interface when
imported; the other modules do not require NumPy unless using an array helper.
Rubicon-ObjC is not a dependency.

## Status and installation

**0.1.0a5** is the latest published alpha release.
It provides Apple Silicon macOS, arm64 iPhoneOS, and arm64 iOS Simulator wheels on
[PyPI](https://pypi.org/project/cocoa-py/0.1.0a5/), plus a source distribution.
The earlier **0.1.0a1** preview contained only Core ML.

The current source targets **macOS 14+**, **iOS 17+**, and standard **CPython 3.14
with the GIL**. macOS wheels are built with Apple's SDK; iOS hosts install a
matching device or simulator wheel before packaging their application. Native macOS motion sensors are not
available and are reported as unsupported.

On an Apple Silicon Mac, install the wheel in a CPython 3.14 environment:

```sh
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install 'cocoa-py[coreml]==0.1.0a5'
```

The `coreml` extra installs NumPy for inference. Use `cocoa-py==0.1.0a5` without
the extra if you do not need NumPy. Installing a matching wheel does not require
Xcode. Source builds require Apple's development tools and use NumPy headers in
an isolated build environment.

## Start using the modules

```python
import device
import audio

print(device.info())
with audio.Sound("sound.wav") as sound:
    channel = sound.play()
    channel.wait()
```

Use `python my_script.py` for normal execution. The optional macOS launcher is
available when your Python host lacks an app identity or the usage descriptions
needed for microphone, location or photo library access:

```sh
cocoa-py my_script.py
cocoa-py -m my_package
```

The launcher keeps the current virtual environment and runs CPython in a small
macOS app with the required usage descriptions. It does not install a second
Python distribution. Ordinary `python` remains suitable for offline audio,
Core ML inference and scene windows. See [macOS execution](https://github.com/Yosorable/cocoa-py/blob/main/docs/macos.md) for
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
and lifecycle integration; see [iOS embedding](https://github.com/Yosorable/cocoa-py/blob/main/docs/embedding.md).

## Documentation

- [Module API guide](https://github.com/Yosorable/cocoa-py/blob/main/docs/api.md)
- [System module types](https://github.com/Yosorable/cocoa-py/blob/main/docs/typing.md)
- [macOS execution and permissions](https://github.com/Yosorable/cocoa-py/blob/main/docs/macos.md)
- [Embedding in an iOS application](https://github.com/Yosorable/cocoa-py/blob/main/docs/embedding.md)
- [Architecture and resource ownership](https://github.com/Yosorable/cocoa-py/blob/main/docs/architecture.md)
- [Third-party code](https://github.com/Yosorable/cocoa-py/blob/main/docs/third-party.md)
- [0.1.0a5 arm64 iOS Simulator wheels](https://github.com/Yosorable/cocoa-py/blob/main/docs/releases/0.1.0a5.md)
- [0.1.0a4 system modules and scene UI](https://github.com/Yosorable/cocoa-py/blob/main/docs/releases/0.1.0a4.md)
- [0.1.0a3 scene cache fix](https://github.com/Yosorable/cocoa-py/blob/main/docs/releases/0.1.0a3.md)
- [0.1.0a2 module collection](https://github.com/Yosorable/cocoa-py/blob/main/docs/releases/0.1.0a2.md)
- [Initial Core ML release](https://github.com/Yosorable/cocoa-py/blob/main/docs/releases/0.1.0a1.md)

The Python wrappers contain full signatures and docstrings, available through
`help(audio.Sound)`, `help(location.Watch)`, and equivalent Python introspection.

## Build and validate

```sh
git clone https://github.com/Yosorable/cocoa-py.git
cd cocoa-py
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

To produce an iOS wheel with a CPython 3.14 framework for arm64 devices:

```sh
python3.14 tools/build_ios_wheel.py --python-framework /path/to/ios-arm64/Python.framework
COCOA_PY_IOS_WHEEL=dist/cocoa_py-0.1.0a5-cp314-cp314-ios_17_0_arm64_iphoneos.whl \
  python3.14 -m unittest discover -s tests -p test_ios_wheel.py -v
```

For Apple Silicon simulators, supply the simulator framework and target:

```sh
python3.14 tools/build_ios_wheel.py --target iphonesimulator \
  --python-framework /path/to/ios-arm64_x86_64-simulator/Python.framework
COCOA_PY_IOS_WHEEL=dist/cocoa_py-0.1.0a5-cp314-cp314-ios_17_0_arm64_iphonesimulator.whl \
  python3.14 -m unittest discover -s tests -p test_ios_wheel.py -v
```

Both targets compile all seven extensions and the matching scene shader library.
The build command does not launch Simulator. The iOS wheel excludes the macOS launcher. Published
versions correspond to Git tags such as `v0.1.0a5`; hosts should pin the release
version and record the downloaded wheel's SHA-256.

## License

MIT. Copyright (c) 2026 Yosorable. Box2D retains its upstream MIT license.
