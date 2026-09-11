# Embedding in an iOS Python application

Install a matching binary wheel **while building the host application**. An
already-distributed iOS app cannot acquire these native modules just by running
pip on the device. The host supplies CPython, UIKit's main loop and system
permission descriptions. Pythona uses this same binary integration boundary.

## Obtain or build the wheel

The current development artifact is
`cocoa_py-0.1.0a2-cp314-cp314-ios_17_0_arm64_iphoneos.whl`.
It targets iOS 17+, arm64 physical devices and ordinary GIL-enabled CPython 3.14.
It is not a macOS or simulator binary. The published 0.1.0a1 release contains
Core ML only; the complete 0.1.0a2 distribution has not yet been uploaded to PyPI.

To build from a checkout or unpacked source distribution, use CPython 3.14 on a
Mac with full Xcode, the Metal toolchain and an iPhoneOS Python.framework that
includes its matching headers:

```sh
python3.14 -m pip install build
python3.14 tools/build_ios_wheel.py \
  --python-framework /path/to/Python.xcframework/ios-arm64/Python.framework
```

This creates a normal wheel in `dist/` containing seven extensions, all Python
wrappers, scene's precompiled Metal library, distribution metadata, licenses and
the SDK privacy manifest. Box2D is compiled into the physics extension. The build
does not include the macOS launcher or invoke a simulator. It does not require a
Pythona checkout; any matching iPhoneOS Python.framework can be supplied.

## Install and package

Install the wheel into the directory the host copies into its bundled
`site-packages`. Use target platform options so the Mac build interpreter does
not select a macOS wheel:

```sh
python3.14 -m pip install --no-deps --no-compile --only-binary=:all: \
  --platform ios_17_0_arm64_iphoneos --python-version 3.14 \
  --implementation cp --abi cp314 --target app_packages \
  dist/cocoa_py-0.1.0a2-cp314-cp314-ios_17_0_arm64_iphoneos.whl
```

Use a fresh staging directory when upgrading, then replace the previous
distribution's files. Hosts using Core ML must also bundle compatible NumPy 2.x
extensions. Other modules do not require NumPy unless an array helper is used.

Process this directory with CPython's normal iOS build script, as for other
binary Python packages. It converts each `.so` into a signed framework under
the app's `Frameworks/` directory, leaving a `.fwork` import marker in
`site-packages`. Private extensions live in `_cocoa/`, so their frameworks have
names such as `_cocoa._audio.framework`; the public `coreml` extension remains
top-level. It also moves `_cocoa/_system.xcprivacy` into
`_cocoa._system.framework/PrivacyInfo.xcprivacy`. See the
[CPython iOS guide](https://docs.python.org/3.14/using/ios.html#binary-extension-modules).

Keep `scene/_resources/SceneShaders.metallib` with the Python package.
`scene.gpu.Library('__default__')` loads that resource; the host need not
compile a shader or provide its own `default.metallib`.

Do not compile the library sources into the app or call
`registerCocoaPyModules()` when using a wheel. Native modules load normally
when imported. Public calls remain `import audio`, `import coreml` and
`from scene import ...`; no import hook is required.

## Optional file-access hooks

A host that manages security-scoped bookmarks can export this pair of C
functions. No cocoa-py header or linked library is needed:

```cpp
extern "C" void *CocoaPyBeginFileAccess(const char *path);
extern "C" void CocoaPyEndFileAccess(void *token);
```

Begin returns an owned token or null when no token is needed. End receives each
non-null token exactly once. Callbacks can execute on worker threads, must not
raise language exceptions across the C boundary, and must remain loaded for the
process lifetime. Retain a token independently of any view, sheet or script run.

The wheel resolves both functions with `dlsym(RTLD_DEFAULT, ...)` when file
access is first needed. Export the functions before using the library. For an
Xcode app, retain and export the symbols in **both Debug and Release**, for example
with these additional linker flags:

```text
$(inherited) -Wl,-u,_CocoaPyBeginFileAccess -Wl,-u,_CocoaPyEndFileAccess -Wl,-export_dynamic
```

Swift hosts can implement public, nonisolated functions with the corresponding
`@_cdecl` names. Hosts loading the callbacks from a separate dynamic library
must load that library globally before using cocoa-py and keep it loaded.

File tokens cover preflight checks and asynchronous audio, model, image, Photos
and sharing operations. Source and destination each receive access where needed.
A selected sharing service may continue after Python stops waiting, and retains
its tokens until the service completes. If either callback is missing, neither
is used; the library falls back to Foundation security-scoped URL access.

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

The wheel's `_cocoa/_system.xcprivacy` declares
the library's file metadata access, local timing calculations, and disk capacity
display/write checks. The host must separately declare its own API usage.
Use device uptime for local timing and storage information for visible capacity
display or write-space decisions; do not transmit these raw device signals for
profiling. Review Apple's current [required-reason API documentation](https://developer.apple.com/documentation/bundleresources/describing-use-of-required-reason-api)
against the host's actual behavior before distributing an application.

## Validate the integration

Compile an iPhoneOS device target, verify all dynamic imports and metadata,
and run the host's real-device checks. Cover permission acceptance/refusal,
closing pending UI, bounded sensor streams, microphone shutdown and file access
outside the app container. Audio routing, interruptions and true sensor data
require real hardware. macOS bridge tests do not replace those device checks.

## Optional source integration

Hosts that intentionally compile the native sources can still use
`native/common/CocoaPy.h` and `registerCocoaPyModules()` once after
`Py_PreInitialize` and before `Py_InitializeFromConfig`. Compile the seven
module implementation files plus `native/common/CocoaPy.mm` and Box2D's C
sources, using the corresponding frameworks and flags in `setup.py`. Exclude
the macOS runner. `tools/install_embedded.py` copies the canonical Python
wrappers, including the `_cocoa` package, and metadata; also include the SDK
privacy bundle in the host resources. Private built-ins are registered by their
full names such as `_cocoa._audio`, with no top-level `_audio` alias.
In this mode scene can compile its packaged Metal source on first use. Do not
combine source registration and wheel extensions in the same interpreter.
