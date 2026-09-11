# Running on macOS

Use standard CPython 3.14 with the GIL on macOS 14 or later. The native extension
and Python interpreter must have matching architectures. Apple Silicon is tested;
universal2/x86_64 source builds require matching Python and NumPy installations
and have not been validated on Intel hardware.

## Ordinary Python and the permission launcher

Offline audio, Core ML and scene windows work from an ordinary terminal Python
process. Microphone, Photos, location and notifications depend on the identity
and Info.plist of the actual process. An interpreter supplied inside an app may
already provide these; a bare executable may not.

Use the installed `cocoa-py` command for a predictable app identity:

```sh
source .venv/bin/activate
cocoa-py script.py argument
cocoa-py -m package.module
python -m cocoa_run script.py
```

The command creates an ad-hoc-signed `CocoaPyRunner.app` inside
`~/Library/Application Support/cocoa-py/`. Its executable loads the current
interpreter's shared library and preserves its virtual environment, script
arguments and working directory. The app contains public usage descriptions;
Apple still asks the user to grant protected access. Importing a module does
not grant or request it.

All scripts launched this way share the `cocoa-py Python` app's permission
identity. The runner is not a sandbox or a separate security boundary. App
updates or changes to its signature can cause macOS to ask again. Permission
choices can be managed in System Settings. The command does not request
background location access.

In VS Code or PyCharm, use the same virtual environment and run
`python -m cocoa_run path/to/script.py` in the integrated terminal. The launcher
uses the same packages; it is not a debugger adapter. Debuggers that inject an
in-process helper need to start that helper explicitly under the launcher.

## Threads and windows

Call desktop system APIs and `scene.run()` on Python's main thread. Their native
waits service the macOS run loop without holding the GIL. A secondary Python
thread can use system APIs only while the main thread runs an AppKit event loop.
Do not synchronously join such a worker from the main thread while it is waiting
for a UI operation.

Photos uses Apple's system picker. Sharing opens a small window with a Share
button; clicking it opens the system service picker. Closing before selecting a
service cancels the operation. A service that has already begun may continue in
its own app after Python times out.

## Capability differences

- `motion.available()` reports false for phone sensors on native macOS.
- `device.battery()` returns an unavailable state on a desktop without a battery.
- Desktop clipboard expiration and `local_only` are not provided by NSPasteboard;
  requesting these options raises `NotImplementedError`.
- Location quality depends on available hardware and system settings; a Mac does
  not imply GPS-quality fixes. Inspect each fix's reported accuracy.
- Geocoder results depend on Apple's service, region and network availability.
- Notifications require an app identity. The library schedules local notices
  and does not replace a host application's notification delegate.

No module collects analytics or forwards user data to a cocoa-py server.
Geocoding uses Apple's service; sharing sends only the items and destination the
caller and user select.
