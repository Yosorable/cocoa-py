# Architecture

`cocoa-py` owns the implementations; host applications consume a versioned wheel.
Source integration is optional. Public APIs use top-level Python imports and have no Pythona runtime,
Pro entitlement, file-browser, or Rubicon dependency.

## Source layout

- `python/`: public Python wrappers, the `scene` package and private request helpers.
- `native/audio/`: AVAudioEngine playback, PCM I/O and offline rendering.
- `native/coreml/`: Core ML and NumPy conversion.
- `native/metal/`: shared Metal rendering with UIKit and AppKit window adapters.
- `native/scene/`: scene acceleration; shaders live in `python/scene/_resources/`.
- `native/physics/`: Box2D bridge and vendored Box2D C sources.
- `native/photos/`: system photo picker and Photos saving.
- `native/system/`: location, motion, clipboard, device and sharing.
- `native/common/`: shared thread/file-access helpers and embedded registration.
- `native/runner/`: optional macOS app executable for permission-aware Python.
- `tools/install_embedded.py`: copy Python wrappers and distribution metadata into a host bundle.
- `tools/build_ios_wheel.py`: compile and package an arm64 iOS device or simulator wheel.

## Installed layout

Public imports remain top-level: `audio`, `photos`, `scene`, `coreml`, and the
system modules. The private `_cocoa` package contains shared Python request
helpers, launcher resources, and six independently built native extensions:
`_audio`, `_photos`, `_metal`, `_physics`, `_scene_accel`, and `_system`.
Core ML remains a top-level native `coreml` module.

`_cocoa/__init__.py` does not import its extensions or request helpers. Each
public wrapper imports the backend it needs using ordinary package imports;
no search-path changes or top-level extension aliases are installed. Physics
continues to load when its public scene classes are first requested. Grouping
these binaries in a directory does not combine their engine state or dependencies.

## Native boundaries

Python handles composition, typed results, timeout policy and blocking PCM
backpressure. Native code validates input and owns Apple framework resources.
Delegate and render callbacks do not call Python. Asynchronous system services
return serializable snapshots to a Python wait loop that releases the GIL
while waiting and checks interrupts between bounded waits. Clipboard calls use
a synchronous typed bridge in the same extension: buffers become owned native
data before releasing the GIL, and results become Python bytes after reacquiring
it. They do not encode binary content as JSON or Base64. All clipboard framework
access stays on the main thread.

Request waits return to Python at intervals of up to 50 milliseconds so injected
interrupts can be delivered. Background threads block on a semaphore for each
interval and wake early on notification; the main thread services its event
loop while waiting. A sample's pending wake is retired when its queue becomes
empty, under the same lock as the producer's notification. Waiting for completion
does not treat unread stream samples as a completed operation.

Each system request owns its resources or a subscription to a shared resource.
Motion streams share one native manager and the sampling source for each sensor,
while retaining independent bounded queues. Sensor streams discard their oldest
samples on overflow, with an observable counter. A context manager or `close()`
ends the request's subscription; a shared motion source stops after its final
subscriber closes. Capsule destruction also arranges native cleanup without
blocking a Python finalizer on the UI thread.

Playback and recording resources keep their existing explicit lifecycle APIs.
Active one-shot audio playback can outlive an unreferenced Python Channel until
native playback completes. Offline audio uses its own rendering engine and does
not require a live audio device.

## Hosts and file access

An iOS host installs the wheel into its bundled packages at build time. CPython's
standard iOS packager moves extensions into signed frameworks and leaves `.fwork`
import markers. There is no built-in registration or library source dependency.
The wheel includes a precompiled scene shader library and the SDK privacy manifest.

A host can export two optional C file-access hooks, resolved with `dlsym`,
that retain and release its own security-scoped bookmarks. Tokens cover preflight
checks and asynchronous I/O, including source and destination where required.
Without a complete pair of hooks, the implementation uses Foundation scoped-URL access.
Host callbacks must remain loaded for the process lifetime. Each token stores its
matching release function; worker-thread operations do not call Python.

A sharing service may continue copying files after Python stops waiting. Its
request retains file-access tokens through native completion or cancellation.
Sharing adds a typed buffer argument to the private request bridge: Python
containers are snapshotted and buffer contents are copied with the GIL held,
before any UI or asynchronous service starts. Images become native image
objects. Named buffers become files in a private `cocoa_py_share_*` temporary
directory owned by the request; preparation failures, cancellation and service
completion clean up that directory. Closing visible iOS UI dismisses it and
retains files until the dismissal completes. A close during presentation is
applied after presentation finishes; item-fetch callbacks are not selection
notifications. An iOS handoff that already removed the sheet, or a selected
macOS service, retains files until the service's completion callback.
Other location, motion and picker requests stop or dismiss when closed.

## Platforms and import behavior

Apple frameworks are linked in native extensions, but importing a wrapper does
not open a window, activate a microphone, read the clipboard, or request location.
System modules share `_cocoa._system`; `audio`, `coreml`, `photos`, and scene components
use separate extensions. NumPy is initialized only by Core ML or an explicit
array conversion helper.

macOS scene windows use AppKit, mouse events, CADisplayLink and Metal. On iOS,
UIKit touch input and app windows are retained. Desktop scripts service the
main run loop during native waits and rendering; scripts called from a secondary
thread need an independently running AppKit loop.

An optional macOS launcher gives the selected Python environment an application
identity and permission descriptions. It loads that environment's CPython
library, preserves virtual-environment discovery and does not run an additional
interpreter or alter the user's package installation.

## Boundaries of this preview

Native macOS does not provide the iPhone motion sensors. Location remains a
foreground service. The project does not implement a general UI toolkit,
contacts/reminders, Bluetooth, speech, or Pythonista API compatibility.

The original `0.1.0a1` release contained Core ML only; its historical release
record remains in `docs/releases/0.1.0a1.md`.
