# Module API guide

All names below are top-level imports. Parameters and full docstrings are also
available through Python `help()`. Platform and lifetime details are in
[architecture](architecture.md), [macOS](macos.md), and [embedding](embedding.md).

## Audio

`audio` exposes `Sound`, `Channel`, `Recorder`, `InputStream`, `OutputStream` and
`Track`, plus `play`, `mix`, `export`, global controls and device/event queries.

- `Sound(path, stream=False)` decodes a file; `stream=True` reads long files in
  bounded chunks. `Sound.from_bytes(data)` accepts encoded audio and
  `Sound.from_pcm(...)` accepts PCM. Wait for `loaded` before accessing decoded
  PCM. `duration`, `sample_rate`, `channels` and `frames` describe the source.
- `sound.play(...)` returns a `Channel`. It supports pause/resume/stop, seeking,
  volume/pan, playback rate and independent semitone shifting, effects and fades.
  `state`, `error`, `finished` and `wait(timeout=...)` distinguish natural
  completion, explicit stop, pause and failure.
- `Recorder` supports in-memory recording and direct file recording. PCM stream
  input/output supports bounded capacity, read/write backpressure, and observable
  overflow/underrun counters. Their context managers release microphone and
  playback resources.
- `Track` describes a source region, placement, gain, pan, rate and pitch.
  `mix(tracks, ...)` creates an in-memory Sound. `export(tracks, path, ...)`
  renders incrementally to WAV, CAF or M4A; `tail` includes effect decay.
- `get_device_info()` returns routes, sample rate, latency, permission and engine
  state without starting the engine. `get_events()` consumes the bounded event
  queue. `close()` releases the module's active audio resources.

PCM arrays are interleaved by frame at the native boundary. NumPy helpers are
optional. Use the returned recording format rather than assuming the device
accepted a requested sample rate. Duration and seek positions refer to source
seconds, including after changing playback speed.

## Scene

```python
from scene import Circle, Label, Scene, run

class Demo(Scene):
    def setup(self):
        self.add(Circle(40, position=(120, 120), fill="#43b9ee"))
        self.add(Label("Hello", position=(240, 200), size=28))

run(Demo, title="cocoa-py")
```

Scenes provide lifecycle callbacks, a node hierarchy, shapes/sprites/text,
actions, hit testing, touch/mouse events and a physics world. `PhysicsBody`
creates Box2D bodies. `scene.gpu` exposes Metal windows, buffers, textures,
render/compute pipelines and blit operations for lower-level work.

macOS uses mouse input and resizable AppKit windows. iOS keeps UIKit touch input.
Window orientation options select an initial desktop aspect ratio. Close windows
and GPU resources explicitly, or use their supported context managers.

## Core ML

| Function | Purpose |
| --- | --- |
| `compile(model_path, output_dir=None)` | Compile a `.mlmodel` or `.mlpackage`. |
| `load(path, compute_units="all")` | Load a compiled model and return a Model. |
| `describe(model)` | Inspect names, shapes, types and optional inputs/outputs. |
| `predict(model, inputs)` | Infer from a dictionary of named inputs. |
| `batch_predict(model, inputs_list)` | Infer a batch of input dictionaries. |
| `metadata(model)` | Read author, license, version and description. |

`compute_units` accepts `all`, `cpuAndNeuralEngine`, `cpuAndGPU` and `cpuOnly`.
Tensor inputs support NumPy float16/32/64 and int32; image inputs use uint8
RGB/RGBA or the model's grayscale dtype. Color outputs are RGBA. Models are
caller-supplied. Stateful inference, training and sequence features are not
exposed by this preview.

## Photos

`pick_media`, `pick_image(s)` and `pick_video(s)` open the system picker and
return typed records containing copied temporary paths and media metadata.
Cancellation returns the wrapper's empty result rather than selecting a file.
Picker calls accept `timeout=300` and cancel pending selection when it expires.
Saving uses `save_image` and `save_video` and returns a typed success record.

Save operations request add permission when needed. Picking selected files does
not require broad Photos access. Temporary selected files are owned by the
caller after return; copy them to permanent storage when needed. A timed-out
Photos save may still complete in the background because the framework does not
provide cancellation of an already-submitted change transaction.

## Location

| API | Result |
| --- | --- |
| `status()` / `permission()` | Current authorization without prompting. |
| `request_permission(timeout=120)` | Foreground permission status. |
| `current(timeout=30, accuracy=10, max_age=15)` | One `Coordinates` fix, then stops updates. |
| `watch(accuracy=10, max_age=15, distance_filter=0, capacity=128)` | Context-managed `Watch`. |
| `geocode(address, timeout=30)` | A list of `Place` records from Apple. |
| `reverse_geocode(latitude, longitude, timeout=30)` | Address records for supplied coordinates. |

`Coordinates` contains latitude/longitude in degrees, altitude and accuracy in
meters, speed in m/s, course in degrees, and a Unix timestamp. Unavailable
altitude/speed/course values are None. Requested accuracy is a preference;
inspect `horizontal_accuracy` on each fix.

`Watch.read(timeout=1)` returns one sample or None. `stats` contains `capacity`,
`buffered` and `dropped`. Old samples are dropped when the queue is full.
Close the watch to stop updates. Permission refusal raises `PermissionError`;
no fix before the deadline raises `TimeoutError` for `current`.

Geocoding uses Apple's regional network service and does not read the device's
location. Results can be ambiguous or absent. `Place` contains optional address
components, coordinates and a time-zone name.

## Motion

`available()` reports support for `accelerometer`, `gyroscope`, `magnetometer`
and fused `device` motion. `watch(sensor="device", interval=1/60, capacity=128)`
returns a context-managed stream with the same read/stats/close contract as
location. The minimum interval is 0.01 seconds; device delivery is best effort.

Samples are dictionaries. Acceleration and gravity use m/s², rotation uses rad/s,
magnetic fields use microteslas, attitude angles use radians, and timestamps use
seconds since system boot. Device motion also provides an orientation quaternion.
Native macOS raises `NotImplementedError` when starting phone sensors.

## Clipboard

`read_text()` returns str or None; `write_text(text)` replaces all items.
`types()` lists UTI strings. `read_bytes(type)` and
`write_bytes(data, type="public.png")` handle one typed representation; `clear()`
removes all items. iOS can prompt when pasting another app's content.

`write_text(..., local_only=True, expires_in=60)` supports iOS's device-local
clipboard and expiration options. These options raise `NotImplementedError` on
macOS. Importing the module does not inspect the clipboard.

## Sharing

`present(text=None, files=(), urls=(), timeout=300)` waits for the system share
UI and returns `ShareResult(completed, activity)`. Put local paths in `files`
and absolute non-file URLs in `urls`. No destination is chosen automatically.

`open(...)` returns a `ShareRequest` immediately for callers that need explicit
`done`, `wait(timeout)`, and `close()` control. Retain the request. Closing before
selection dismisses the UI. A selected sharing service can finish independently;
its source file access remains valid until completion.

## Device

`info()` returns platform, OS version, architecture, CPU counts, physical memory
(bytes), uptime (seconds), low-power mode and thermal state. `battery()` returns
level (0..1 or None) and charging state. `storage(path=".")` returns total/free
filesystem bytes. No persistent device identifier is returned.

## Local notifications

`available()` checks host identity; `permission()` reads status and
`request_permission(timeout=120)` prompts. Then:

```python
import notification

if notification.request_permission() in {"authorized", "provisional"}:
    identifier = notification.schedule("Timer", "Your timer finished", delay=60)
    print(notification.pending())
    notification.cancel(identifier)
```

`schedule(title, body="", delay=1, repeat=False, sound=True, identifier=None)`
returns an identifier. Repetition requires at least 60 seconds. Reusing an ID
replaces that pending notice. `pending()` lists this library's notices;
`cancel(id)` removes that pending/delivered notice. `cancel_all()` cancels the
currently pending library notices. Other host notices are preserved.

Foreground presentation and notification activation belong to the host.
Scheduling an alert does not imply that Focus settings allow immediate display.
