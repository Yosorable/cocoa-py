# Embedding in an iOS Python application

The library supports build-time integration with an existing CPython host. It
does not install native code into an already-distributed iOS application.
Pythona uses this same integration boundary.

## Source and compiler setup

Pin a source revision, for example with a Git submodule. Compile these sources
into the application or a linked native target:

- `native/common/CocoaPy.mm`
- `native/audio/AudioModule.mm`
- `native/coreml/CoreMLModule.mm`
- `native/metal/MetalModule.mm`
- `native/photos/PhotosModule.mm`
- `native/physics/PhysicsModule.mm`
- `native/scene/SceneAccelModule.mm`
- `native/system/SystemModule.mm`
- All `.c` files in `native/physics/box2d/src/`

Use Objective-C++17 or newer with ARC for `.mm` files, C17 for Box2D, and an iOS
17 deployment target. Do **not** compile `native/runner/CocoaPyRunner.mm` into
the iOS host; it is a separate macOS app executable.

Header search paths must include the host's CPython 3.14 headers, its NumPy
`_core/include` directory, and Box2D's `include` and `src` directories. NumPy's
runtime package must be bundled to use Core ML. The interpreter must be a
standard GIL-enabled build.

Link Foundation, CoreFoundation, UIKit, AVFoundation, AudioToolbox, QuartzCore,
Metal, ImageIO, Photos, PhotosUI, UniformTypeIdentifiers, CoreML, CoreVideo,
CoreLocation, CoreMotion and UserNotifications. Most Xcode projects already
link several of these through their SDK modules.

Compile `python/scene/_resources/SceneShaders.metal` in the app's Metal sources
phase so it is present in the app's `default.metallib`. It is the canonical
shader source; do not maintain a second copy.

## Registration and Python files

After `Py_PreInitialize` and before `Py_InitializeFromConfig`, call:

```cpp
#include "native/common/CocoaPy.h"

if (registerCocoaPyModules() != 0) {
    // Abort this interpreter initialization transaction.
}
```

The hook adds `_audio`, `coreml`, `_metal`, `_photos`, `_scene_accel`, `_physics`
and `_cocoakit` to CPython's built-in module table. Do not also compile older
copies of these modules or register them a second time.

Copy Python wrappers into the app's built `site-packages` directory:

```sh
python3.14 tools/install_embedded.py /path/to/built/app/python/lib/python3.14/site-packages
```

The installer also includes `cocoa-py` distribution metadata and licenses, so
`importlib.metadata.version('cocoa-py')` works in the host. Run the installer after
the host copies its standard library. It excludes the macOS launcher. No wheel
needs to be installed on the iPhone, and import names stay unchanged.

## File-access hooks

If the host manages security-scoped bookmarks, define both C functions:

```cpp
extern "C" void *CocoaPyBeginFileAccess(const char *path);
extern "C" void CocoaPyEndFileAccess(void *token);
```

Begin must return an owned token or null when no token is needed. End receives
that same token exactly once. The hooks can run on background threads. A token
must remain valid independently of any file-browser view, sheet or script run.

The bridges acquire access before path preflight and retain it throughout
asynchronous file decoding, recording/export, model loading, image loading,
Photos saving and sharing. For operations with different source and destination
paths, each required path gets its own token. A share service that already
started retains its tokens until its completion callback, even if Python stops
waiting. Without host hooks, Foundation security-scoped URL access is used.

## Permissions and host lifecycle

Add accurate user-facing usage descriptions to the host's Info.plist:

| Key | Feature |
| --- | --- |
| `NSMicrophoneUsageDescription` | Audio recording and PCM input. |
| `NSPhotoLibraryAddUsageDescription` | Saving images and videos to Photos. |
| `NSLocationWhenInUseUsageDescription` | Foreground location fixes and streams. |
| `NSMotionUsageDescription` | Motion sensor sampling. |

The system photo picker grants access to selected items and does not require
full-library read authorization. The library does not request always-on location
or background location modes. Local notifications use Apple's explicit
permission request; the library does not replace the host's notification delegate.
The host determines foreground notification presentation and notification-tap
handling.

Keep UIKit's main loop running while Python executes on its worker thread. All
UI presentation and Core Location setup are dispatched to that main thread with
the GIL released. Do not make the main thread synchronously wait for Python when
Python may be waiting for a system UI callback. Call `close()` or use context
managers for continuous sensors, requests, audio streams and windows.

Importing these modules does not bypass the host's authorization, entitlements
or package policy. The host still owns its privacy policy, App Store privacy
answers, required-reason API declarations, and application lifecycle.

Copy `native/common/CocoaPyPrivacy.bundle` into the app's resources. It declares
the library's file metadata access, local timing calculations, and disk capacity
display/write checks. The host must separately declare its own API usage.
Use device uptime for local timing and storage information for visible capacity
display or write-space decisions; do not transmit these raw device signals for
profiling. Review Apple's current [required-reason API documentation](https://developer.apple.com/documentation/bundleresources/describing-use-of-required-reason-api)
against the host's actual behavior before distributing an application.

## Validate the integration

Compile an iPhoneOS device target, verify all registered imports and metadata,
and run the host's real-device checks. Cover permission acceptance/refusal,
closing pending UI, bounded sensor streams, microphone shutdown and file access
outside the app container. Audio routing, interruptions and true sensor data
require real hardware. macOS bridge tests do not replace those device checks.
