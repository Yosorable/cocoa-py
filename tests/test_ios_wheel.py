"""Inspect an actual iPhoneOS wheel without running an iOS simulator."""

import base64
import csv
from email.parser import Parser
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
import zipfile


@unittest.skipUnless(os.environ.get("COCOA_PY_IOS_WHEEL"), "Set COCOA_PY_IOS_WHEEL to validate an iPhoneOS artifact.")
class IOSWheelTests(unittest.TestCase):
    def test_tag_resources_metadata_and_record_hashes(self):
        with zipfile.ZipFile(os.environ["COCOA_PY_IOS_WHEEL"]) as wheel:
            names = set(wheel.namelist())
            metadata = next(name.rsplit("/", 1)[0] for name in names if name.endswith(".dist-info/WHEEL"))
            info = Parser().parsestr(wheel.read(metadata + "/WHEEL").decode())
            self.assertEqual(info.get_all("Tag"), ["cp314-cp314-ios_17_0_arm64_iphoneos"])
            self.assertEqual(info["Root-Is-Purelib"], "false")
            self.assertNotIn(metadata + "/entry_points.txt", names)
            self.assertNotIn("cocoa_run.py", names)
            self.assertNotIn("_cocoa/_runner", names)
            self.assertIn("_cocoa/__init__.py", names)
            self.assertIn("_cocoa/requests.py", names)
            self.assertFalse(any(name.startswith(("_cocoa_support/", "_cocoakit.")) for name in names))
            self.assertTrue(wheel.read("scene/_resources/SceneShaders.metallib").startswith(b"MTLB"))
            privacy = plistlib.loads(wheel.read("_cocoa/_system.xcprivacy"))
            self.assertTrue(privacy["NSPrivacyAccessedAPITypes"])
            self.assertEqual(json.loads(wheel.read("_cocoa/build.json"))["platform"], "ios_17_0_arm64_iphoneos")
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
                    self.assertTrue(name.endswith(".cpython-314-iphoneos.so"))
                    binary = Path(temporary) / name
                    binary.parent.mkdir(parents=True, exist_ok=True)
                    binary.write_bytes(wheel.read(name))
                    def inspect(*command):
                        return subprocess.check_output([*command, str(binary)], text=True)
                    self.assertEqual(inspect("lipo", "-archs").strip(), "arm64")
                    build = inspect("xcrun", "vtool", "-show-build")
                    self.assertIn("platform IOS\n", build)
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


if __name__ == "__main__":
    unittest.main()
