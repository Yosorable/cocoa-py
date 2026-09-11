"""Run the current Python environment with a macOS app permission identity.

Usage: cocoa-py script.py [args], cocoa-py -m module, or cocoa-py -c code.
This is a launcher only; public library imports remain top-level modules.
"""

import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import sysconfig
import tempfile


def _python_library():
    candidates = [Path(sysconfig.get_config_var("LIBDIR") or "") /
                  (sysconfig.get_config_var("LDLIBRARY") or "")]
    framework = sysconfig.get_config_var("PYTHONFRAMEWORK")
    if framework:
        candidates.insert(0, Path(sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX")) /
                          f"{framework}.framework" / "Versions" / "3.14" / framework)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise RuntimeError("This Python installation does not provide a loadable CPython library")


def _bundle():
    import _cocoa

    source = Path(_cocoa.__file__).with_name("_runner")
    if not source.is_file():
        raise RuntimeError("The native macOS launcher is missing; install a built cocoa-py wheel")
    root = Path.home() / "Library" / "Application Support" / "cocoa-py"
    bundle = root / "CocoaPyRunner.app"
    executable = bundle / "Contents" / "MacOS" / "CocoaPyRunner"
    info = {
        "CFBundleIdentifier": "org.cocoa-py.runner",
        "CFBundleName": "cocoa-py Python",
        "CFBundleDisplayName": "cocoa-py Python",
        "CFBundleExecutable": "CocoaPyRunner",
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1",
        "CFBundleShortVersionString": "0.1",
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        "NSLocationUsageDescription": "Python scripts you run can request your location.",
        "NSLocationWhenInUseUsageDescription": "Python scripts you run can request your location while this app is in use.",
        "NSMicrophoneUsageDescription": "Python scripts you run can record microphone audio.",
        "NSPhotoLibraryUsageDescription": "Python scripts you run can access photos when you request it.",
        "NSPhotoLibraryAddUsageDescription": "Python scripts you run can save images and videos to your photo library.",
    }
    encoded = plistlib.dumps(info, sort_keys=True)
    plist = bundle / "Contents" / "Info.plist"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    marker = bundle / "Contents" / "Resources" / "RunnerSource.sha256"
    # Keep the executable and ad-hoc signature unchanged between launches so
    # permission decisions are not needlessly invalidated by reinstalling it.
    def installed():
        return (executable.is_file() and plist.is_file() and plist.read_bytes() == encoded and
                marker.is_file() and marker.read_text() == digest)

    if installed():
        return executable
    root.mkdir(parents=True, exist_ok=True)
    # Serialize installation across simultaneous terminals. The lock file is
    # outside the app so replacing its content cannot change the lock inode.
    import fcntl
    with (root / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if installed():
            return executable
        with tempfile.TemporaryDirectory(prefix="runner-", dir=root) as temporary:
            stage = Path(temporary) / "CocoaPyRunner.app"
            target = stage / "Contents" / "MacOS" / "CocoaPyRunner"
            target.parent.mkdir(parents=True)
            shutil.copy2(source, target)
            target.chmod(0o755)
            (stage / "Contents" / "Info.plist").write_bytes(encoded)
            resources = stage / "Contents" / "Resources"
            resources.mkdir()
            (resources / "RunnerSource.sha256").write_text(digest)
            # The compiler already signs Apple Silicon executables. Signing the
            # entire app additionally seals its public permission declarations.
            subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(stage)],
                           check=True, capture_output=True)
            if bundle.exists():
                shutil.rmtree(bundle)
            shutil.move(str(stage), str(bundle))
    return executable


def main():
    if sys.platform != "darwin":
        raise SystemExit("The cocoa-py launcher is for native macOS Python")
    if len(sys.argv) == 1:
        raise SystemExit("Usage: cocoa-py script.py [args] | cocoa-py -m module | cocoa-py -c code")
    library = _python_library()
    executable = _bundle()
    os.execv(str(executable), [str(executable), library, sys.executable, *sys.argv[1:]])


if __name__ == "__main__":
    main()
