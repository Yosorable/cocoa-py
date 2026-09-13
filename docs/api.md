# Module API guide

All names below are top-level imports. Parameters and full docstrings are also
available through Python `help()`. Platform and lifetime details are in
[architecture](architecture.md), [macOS](macos.md), and [embedding](embedding.md).

## Audio

`audio` exposes `Sound`, `Stream`, `Channel`, `Recorder`, `InputStream`, `OutputStream` and
`Track`, plus `play`, `mix`, `export`, global controls and device/event queries.

- `Sound(path)` decodes a file; `Stream(path)` reads long files in
  bounded chunks. `Sound(encoded_bytes)` accepts in-memory audio and
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

`Path` and `Polygon` tessellate curves and round strokes at the rendering
resolution, accounting for display scale, node/parent and camera transforms,
and capture size. Existing path commands need no changes. `curve_tolerance`
remains an upper bound in local points; rendering refines it toward a subpixel
error target. `curve_depth` bounds recursive subdivision (default 10), and
round arcs are limited to 4096 segments, so extreme magnification can still
reach a quality limit. Small scale changes reuse sufficiently detailed meshes.
Actual corners between curve commands retain their shape.
Curve bounds are measured from the original commands before Layer allocation,
so increasing tessellation quality does not change the measured extent. Stroke
bounds conservatively cover square caps and miter joins. Thumbnail textures use
the requested pixel scale, including scales below one pixel per local point.

`Scene.capture()` and `Node.capture(rect=...)` return an owned `ImageData` with
RGBA pixels, in-memory PNG encoding, PNG saving and optional NumPy/Pillow
conversion. `gpu.Texture.to_image(window)` provides low-level image readback.
See [scene capture and image export](scene-capture.md) for coordinates, alpha,
resolution and lifetime rules, and a drawing/export example.

`TextField` and `TextView` add single-line and multiline native text editing,
including input methods, selection, secure entry, undo/redo and iOS keyboard
configuration. See [scene text input](scene-text-input.md) for options,
callbacks, keyboard avoidance and native overlay behavior.

`TextInputSession` exposes the same editing model without a drawable input
component. Call `session.begin(scene)` / `session.end()` and draw the text,
selection and caret yourself. Its `caret_rect` positions input-method UI, while
`Scene.keyboard_changed()` lets a custom interface respond to the keyboard.

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
| `heading_available()` | Whether this device provides compass headings; does not start sensors. |
| `heading(timeout=10, max_age=5, true_north=False, orientation="portrait")` | One valid `Heading`, then stops its sensors. |
| `watch_heading(angle_filter=1, max_age=5, true_north=False, orientation="portrait", capacity=128)` | Context-managed `HeadingWatch`. |
| `geocode(address, timeout=30)` | A list of `Place` records from Apple. |
| `reverse_geocode(latitude, longitude, timeout=30)` | Address records for supplied coordinates. |

`Coordinates` contains latitude/longitude in degrees, altitude and accuracy in
meters, speed in m/s, course in degrees, and a Unix timestamp. Unavailable
altitude/speed/course values are None. Requested accuracy is a preference;
inspect `horizontal_accuracy` on each fix.

`current()` chooses the newest valid fix within each delivered batch. A positive
`max_age` limits sample age in seconds; zero rejects measurements made before
this request starts updating, while allowing normal delivery latency.

`Watch.read(timeout=1)` returns one sample or None. `stats` contains `capacity`,
`buffered` and `dropped`. Old samples are dropped when the queue is full.
Close the watch to stop updates. Permission refusal raises `PermissionError`;
no fix before the deadline raises `TimeoutError` for `current`.

Geocoding uses Apple's regional network service and does not read the device's
location. Results can be ambiguous or absent. `Place` contains optional address
components, coordinates and a time-zone name.

Compass headings describe device orientation, while `Coordinates.course`
describes movement. Magnetic mode does not request location access. Setting
`true_north=True` also starts foreground location updates and waits for a valid
true heading. `Heading` contains `magnetic_heading`, optional `true_heading`,
`accuracy` in degrees, and a Unix `timestamp`. Unsupported devices, including
native macOS, report `heading_available() == False`; starting a compass request
then raises `NotImplementedError`. See [location and compass usage](location.md)
for freshness, orientation, permissions and cleanup.

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

`read_text()` and `read_url()` return the first item's text or typed URL, or
`None`. `write_text(text)` and `write_url(url)` replace all items; URL writes
include a plain-text fallback. `read_image()` returns a Pillow image, while
`read_image(as_bytes=True)` returns PNG bytes without Pillow. `write_image(image)`
accepts a Pillow image or encoded image buffer and writes a still PNG.

The OS may add a URL representation after `write_text()`; `read_url()` and
`has_urls()` honor the types it advertises. Use `write_url()` to explicitly
supply a URL and its text fallback on both platforms.

`types()` lists the first item's UTI strings. `read_bytes(type)` and
`write_bytes(data, type="public.png")` transfer binary buffers directly.
`write_item({type: data, ...})` writes several representations of one item, such
as HTML and plain text together. Every write replaces all previous items;
`clear()` removes them. Empty bytes are distinct from a missing representation.

All writes accept `local_only=True` on iOS and macOS to prevent Universal
Clipboard transfer. `expires_in=60` adds an iOS expiration; macOS rejects
expiration before changing the clipboard.

`has_text()`, `has_image()`, and `has_urls()` inspect advertised types across
all items without fetching their contents. `change_count()` reports the OS
change counter. Content reads may invoke system paste permission. Importing the
module does not inspect the clipboard or import Pillow.

See the [clipboard guide](clipboard.md) for examples, format handling, validation,
optional Pillow installation and platform details.

## Sharing

`present(text=None, files=(), urls=(), images=(), attachments=None, timeout=300)`
waits for the system share UI and returns `ShareResult(completed, activity)`.
Put local paths in `files` and absolute non-file URLs in `urls`. `images` accepts
Pillow images and encoded image buffers; `attachments` maps filenames to exact
binary buffers, such as `{"report.pdf": pdf_bytes}`. No destination is chosen
automatically. Binary data is copied directly into native-owned storage.

`open(...)` returns a `ShareRequest` immediately for callers that need explicit
`done`, `wait(timeout)`, and `close()` control. Retain the request. On iOS,
closing cancels visible sharing UI and removes temporary attachments after the
asynchronous dismissal finishes. If UIKit already removed the local sheet for
a handoff, or a macOS service was selected, native completion releases the files.
Closing an unselected macOS picker cancels it immediately. Original files from
`files` are never removed.

User cancellation, including cancellation within a macOS service, returns
`completed=False`; an actual service error raises `OSError`. `activity` contains
an iOS activity type, a localized macOS service title, or `None`. It is not a
portable service identifier or proof that a recipient received the content.
See the [sharing guide](sharing.md) for data formats, ownership and examples.

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

`schedule()` accepts an immediate/delayed trigger, a timezone-aware `at` datetime,
or a repeating `CalendarTrigger` for daily/weekly wall-clock times. Content
includes title, subtitle, body, default sound or silence, and a foreground
presentation preference. Reusing an identifier replaces its pending notice.

`settings()` queries individual authorization settings. `pending()` and
`delivered()` list library notices; `cancel_pending()` and `remove_delivered()`
manage those sets independently. The existing `cancel(id)` and `cancel_all()`
retain their behavior. Other host notices are preserved.

See [Local notifications](notifications.md) for signatures, result schemas,
calendar examples and foreground delegate integration. The optional macOS
launcher implements that integration; other hosts own their delegate policy.
