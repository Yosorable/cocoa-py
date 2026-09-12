"""Device state contracts independent of the test machine's charging state."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "darwin", "Requires Apple's macOS SDK")
class NativeDeviceTests(unittest.TestCase):
    def test_battery_charged_flag_and_external_power_states(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="cocoa_py_device_test_") as directory:
            binary = Path(directory) / "device"
            build = subprocess.run([
                "xcrun", "clang++", "-std=c++17", "-fobjc-arc", "-O2", "-Wall", "-Wextra",
                "-Wno-unused-function", "-Wno-unused-parameter", "-mmacosx-version-min=14.0",
                "-framework", "Foundation", "-framework", "AppKit", "-framework", "IOKit",
                str(root / "tests/native/device_contract.mm"), "-o", str(binary),
            ], capture_output=True, text=True, timeout=90)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("9 device battery scenarios passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
