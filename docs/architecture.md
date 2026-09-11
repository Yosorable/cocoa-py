# Project direction

This document describes the migration goals. The implementation, tests, and
release notes define which features are currently available.

- The project and PyPI distribution are named `cocoa-py`. The first production
  PyPI upload successfully registered this name.
- Public modules use top-level names such as `audio`, `scene`, `coreml`, and
  `photos`, without a `cocoa.` prefix.
- A single source repository maintains and releases the modules. Pythona will
  consume the same source during its app build.
- macOS support targets ordinary desktop CPython, including terminal sessions,
  VS Code, and PyCharm.
- Other iOS Python IDEs can integrate the modules at build time. Host integration
  remains separate from the module implementations.
- Modules are imported independently. A shared distribution does not preload
  every module; resources are initialized when an operation needs them.
- Native Objective-C and Objective-C++ bridges provide system operations, while
  Python implements higher-level composition. Rubicon-ObjC is not a dependency.
- The previous ioskit API is not a compatibility baseline. Its system APIs can
  be redesigned, reusing existing Pythona implementations where appropriate.
- `scene` uses a custom Metal rendering engine. Its migration must include shader
  resources and physics components.
- Platform integration must cover thread dispatch, windows and event loops,
  permissions, cancellation, errors, file access, and resource lifetimes.
- Operations depend on actual platform capabilities. iOS-specific sensor APIs
  must not be presented as available on macOS without an implementation.

## Initial release

`0.1.0a1` is a Core ML preview that validates building, publishing, and using the
extension independently. It provides the top-level `coreml` module for macOS
and standard CPython 3.14; it does not yet contain the full module collection.

The source distribution includes native build sources and real model inference
tests. The wheel contains an Apple Silicon native extension. The first release
has been published to production PyPI, and its downloads and installation have
been verified.

The remaining migration will bring the other modules into this repository and
switch Pythona to the implementations maintained here.
