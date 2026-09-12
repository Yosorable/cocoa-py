# cocoa-py contributor guide

## Project scope

`cocoa-py` is one distribution with independently imported top-level modules.
Keep module implementations independent of Pythona and other host applications;
host integration belongs in a separate layer.

Read `README.md` for the current release scope and supported environments, and
`docs/architecture.md` for the migration direction. Planned modules and platforms
must not be documented as already available.

## Source layout

See `docs/architecture.md` for the canonical source map. Python wrappers and
shader resources live under `python/`, native implementations under `native/`.
`tools/build_ios_wheel.py` builds a device wheel with a supplied Python.framework.
`tools/install_embedded.py` supports optional source-based host integration.
The macOS runner is a separate executable and must not enter an iOS source target.

The macOS launcher is optional; document `python script.py` as the default.
Preserve it for hosts that lack an app identity or required permission usage
descriptions. A `Python.app` identity alone does not guarantee those declarations.

## Native boundaries

- Validate Python inputs before passing them to Apple frameworks, and translate
  native failures into Python exceptions.
- Hold the GIL whenever accessing Python objects or interpreter state. Keep
  Python object access outside any section that releases the GIL.
- Preserve object ownership across Python, NumPy, and Objective-C boundaries,
  including failure paths and temporary buffer lifetimes.
- Preserve both the standalone `PyInit_coreml` entry point and the
  `registerCoreMLModule` hook used for embedded registration.
- Keep model compilation and loading behind explicit API calls. Importing the
  module must not start inference or request user interaction.
- When changing array conversion, account for strides, dtype, byte order, shape,
  and image channel layout. Extend the relevant public API regression coverage.
- iOS wheels must not depend on host-specific symbols. Optional file-access
  hooks are resolved at runtime; preserve their paired asynchronous lifetime.
- Package scene shaders with the distribution. A wheel must not require the
  host to compile shader source into its application target.

## Validation

For native code or packaging changes, use a virtual environment with standard
CPython 3.14 on macOS and Apple's command-line tools and SDK:

```sh
python3.14 -m pip install build
MACOSX_DEPLOYMENT_TARGET=14.0 python3.14 -m build
python3.14 -m pip install --force-reinstall '.[coreml,images]'
python3.14 -m unittest discover -s tests -v
```

Tests exercise real Core ML inference, offline audio, Box2D and native system
requests. Set `COCOA_PY_UI_TESTS=1` for desktop Metal and sharing windows; set
`COCOA_PY_NETWORK_TESTS=1` for a public-address geocoder request. For packaging changes, also inspect the source archive and wheel
contents, and verify that the wheel platform tag matches the native deployment
target. Record which systems were actually tested separately from the declared
minimum deployment target. Documentation-only edits need relevant content and
link checks, without rebuilding the native extension.

For iOS packaging, build with `tools/build_ios_wheel.py` and validate the result
using `tests/test_ios_wheel.py` with `COCOA_PY_IOS_WHEEL` set to its path. Check
the native platform, architecture, Python linkage, resources and RECORD hashes.
