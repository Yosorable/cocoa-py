"""Inspect an actual arm64 iOS wheel for its declared device or simulator target."""

import base64
import csv
from email.parser import Parser
import hashlib
import io
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from build_ios_wheel import validate_python_framework


@unittest.skipUnless(sys.platform == "darwin", "Requires Apple's SDKs")
class IOSBuildInputTests(unittest.TestCase):
    def test_device_and_simulator_frameworks_are_not_interchangeable(self):
        with tempfile.TemporaryDirectory() as temporary:
            for target, suffix in (("iphoneos", ""), ("iphonesimulator", "-simulator")):
                framework = Path(temporary) / target / "Python.framework"
                headers = framework / "Headers"
                headers.mkdir(parents=True)
                (headers / "Python.h").touch()
                subprocess.run([
                    "xcrun", "--sdk", target, "clang", "-target", "arm64-apple-ios17.0" + suffix,
                    "-dynamiclib", "-x", "c", "-", "-o", str(framework / "Python"),
                ], input="int fixture(void) { return 1; }\n", text=True, check=True, capture_output=True)
                self.assertEqual(validate_python_framework(framework, target), framework.resolve())
                other = "iphoneos" if target == "iphonesimulator" else "iphonesimulator"
                with self.assertRaisesRegex(ValueError, "must target"):
                    validate_python_framework(framework, other)


@unittest.skipUnless(os.environ.get("COCOA_PY_IOS_WHEEL"), "Set COCOA_PY_IOS_WHEEL to validate an iOS artifact.")
class IOSWheelTests(unittest.TestCase):
    def setUp(self):
        filename = Path(os.environ["COCOA_PY_IOS_WHEEL"]).name
        self.target = "iphonesimulator" if filename.endswith("_iphonesimulator.whl") else "iphoneos"
        self.platform = "IOSSIMULATOR" if self.target == "iphonesimulator" else "IOS"

    def test_tag_resources_metadata_and_record_hashes(self):
        with zipfile.ZipFile(os.environ["COCOA_PY_IOS_WHEEL"]) as wheel:
            names = set(wheel.namelist())
            metadata = next(name.rsplit("/", 1)[0] for name in names if name.endswith(".dist-info/WHEEL"))
            info = Parser().parsestr(wheel.read(metadata + "/WHEEL").decode())
            self.assertEqual(info.get_all("Tag"), [f"cp314-cp314-ios_17_0_arm64_{self.target}"])
            self.assertEqual(info["Root-Is-Purelib"], "false")
            self.assertNotIn(metadata + "/entry_points.txt", names)
            self.assertNotIn("cocoa_run.py", names)
            self.assertNotIn("_cocoa/_runner", names)
            self.assertIn("_cocoa/__init__.py", names)
            self.assertIn("_cocoa/requests.py", names)
            self.assertIn("_cocoa/py.typed", names)
            self.assertIn("_cocoa/_system.pyi", names)
            for module in ("device", "location", "motion", "share", "clipboard"):
                self.assertIn(module + ".py", names)
                self.assertNotIn(module + ".pyi", names)
                self.assertFalse(any(name.startswith(module + "-stubs/") for name in names))
            self.assertFalse(any(name.startswith(("_cocoa_support/", "_cocoakit.")) for name in names))
            self.assertTrue(wheel.read("scene/_resources/SceneShaders.metallib").startswith(b"MTLB"))
            privacy = plistlib.loads(wheel.read("_cocoa/_system.xcprivacy"))
            self.assertTrue(privacy["NSPrivacyAccessedAPITypes"])
            self.assertNotIn("_cocoa/build.json", names)
            for name, digest, size in csv.reader(io.StringIO(wheel.read(metadata + "/RECORD").decode())):
                self.assertIn(name, names)
                if not digest:
                    self.assertEqual(name, metadata + "/RECORD")
                    continue
                payload = wheel.read(name)
                expected = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
                self.assertEqual(digest, expected, name)
                self.assertEqual(int(size), len(payload), name)

    def test_native_binaries_are_ios_and_do_not_link_to_a_host(self):
        modules = {"coreml", "_cocoa._audio", "_cocoa._system", "_cocoa._metal",
                   "_cocoa._physics", "_cocoa._scene_accel", "_cocoa._photos"}
        with zipfile.ZipFile(os.environ["COCOA_PY_IOS_WHEEL"]) as wheel, tempfile.TemporaryDirectory() as temporary:
            binaries = [name for name in wheel.namelist() if name.endswith(".so")]
            self.assertEqual({name.split(".")[0].replace("/", ".") for name in binaries}, modules)
            self.assertEqual(len(binaries), 7)
            for name in binaries:
                with self.subTest(module=name):
                    self.assertTrue(name.endswith(f".cpython-314-{self.target}.so"))
                    binary = Path(temporary) / name
                    binary.parent.mkdir(parents=True, exist_ok=True)
                    binary.write_bytes(wheel.read(name))
                    def inspect(*command):
                        return subprocess.check_output([*command, str(binary)], text=True)
                    self.assertEqual(inspect("lipo", "-archs").strip(), "arm64")
                    build = inspect("xcrun", "vtool", "-show-build")
                    self.assertIn(f"platform {self.platform}\n", build)
                    self.assertRegex(build, r"minos 17\.0\b")
                    dependencies = inspect("otool", "-L")
                    self.assertIn("@rpath/Python.framework/Python", dependencies)
                    module = name.split(".")[0].replace("/", ".")
                    self.assertIn(f"@rpath/{module}.framework/{module}", dependencies)
                    for line in dependencies.splitlines()[1:]:
                        self.assertTrue(line.strip().startswith(("@rpath/", "/System/Library/", "/usr/lib/")), line)
                    imports = inspect("nm", "-u")
                    self.assertNotIn("CocoaPyBeginFileAccess", imports)
                    self.assertNotIn("CocoaPyEndFileAccess", imports)
                    exports = inspect("xcrun", "dyld_info", "-exports")
                    self.assertIn("_PyInit_" + module.rsplit(".", 1)[-1], exports)

    def test_shader_library_matches_the_wheel_platform(self):
        with zipfile.ZipFile(os.environ["COCOA_PY_IOS_WHEEL"]) as wheel, tempfile.TemporaryDirectory() as temporary:
            shader = Path(wheel.extract("scene/_resources/SceneShaders.metallib", temporary))
            strings = subprocess.check_output(["strings", str(shader)], text=True)
            targets = set(re.findall(r"air64(?:_v\d+)?-apple-ios[\d.]+(?:-simulator)?", strings))
            self.assertTrue(targets, "No iOS AIR target in the shader library")
            suffix = "-simulator" if self.target == "iphonesimulator" else ""
            self.assertTrue(all(re.fullmatch(r"air64(?:_v\d+)?-apple-ios17\.0(?:\.0)?" + suffix, target)
                                for target in targets), targets)


if __name__ == "__main__":
    unittest.main()
