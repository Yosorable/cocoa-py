"""Native system service checks that do not ask for access to personal data."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import device
import location
import motion
import notification
import share
from _cocoa.requests import Request


class SystemTests(unittest.TestCase):
    def test_imports_leave_unrelated_modules_unloaded(self):
        result = subprocess.run([sys.executable, "-c", "import sys, location, motion, device, clipboard, share, notification; "
                                 "assert not any(name in sys.modules for name in ('numpy', 'coreml', 'scene', 'audio', 'rubicon'))"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_device_and_storage_values(self):
        info = device.info()
        self.assertEqual(info["platform"], "macos")
        self.assertGreater(info["physical_memory"], 0)
        self.assertGreaterEqual(info["cpu_count"], info["active_cpu_count"])
        self.assertGreater(info["uptime"], 0)
        storage = device.storage(Path.cwd())
        self.assertGreater(storage["total"], storage["free"])
        self.assertGreaterEqual(storage["free"], 0)
        battery = device.battery()
        if battery["level"] is not None:
            self.assertTrue(0 <= battery["level"] <= 1)

    def test_native_request_lifecycle(self):
        with Request("device.info", {}) as request:
            self.assertTrue(request.done)
            self.assertFalse(request.closed)
            first = request.wait(0)
            self.assertEqual(request.wait(0), first)
        self.assertTrue(request.closed)
        request.close()
        with self.assertRaises(ValueError):
            request.wait(0)

    def test_unavailable_desktop_motion_reports_capability_and_error(self):
        self.assertFalse(any(motion.available().values()))
        with self.assertRaises(NotImplementedError):
            motion.watch()

    def test_invalid_geocoder_inputs_fail_before_network_access(self):
        for coordinates in ((91, 0), (0, -181)):
            with self.assertRaises(ValueError):
                location.reverse_geocode(*coordinates)
        with self.assertRaises(ValueError):
            location.geocode("")

    def test_permissions_can_be_queried_without_prompting(self):
        self.assertIn(location.permission(), ("authorized", "not_determined", "denied", "restricted"))
        if notification.available():
            self.assertIn(notification.permission(), ("authorized", "not_determined", "denied", "provisional"))

    def test_missing_shared_file_fails_before_presenting_ui(self):
        with self.assertRaises(FileNotFoundError):
            share.open(files=[Path("/nonexistent-cocoa-py-test/file.txt")])

    @unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1", "Desktop window test")
    def test_unselected_share_window_can_be_cancelled(self):
        with share.open(text="cocoa-py cancellation test") as request:
            self.assertFalse(request.done)
            with self.assertRaises(TimeoutError):
                request.wait(0.05)
        self.assertTrue(request.closed)

    @unittest.skipUnless(os.environ.get("COCOA_PY_NETWORK_TESTS") == "1", "Uses Apple's geocoding service")
    def test_geocoder_resolves_public_address_without_reading_device_location(self):
        places = location.geocode("北京市天安门", timeout=20)
        self.assertTrue(places)
        self.assertTrue(39 < places[0].latitude < 40)
        self.assertTrue(116 < places[0].longitude < 117)


class LauncherTests(unittest.TestCase):
    def test_launcher_preserves_venv_and_module_imports(self):
        result = subprocess.run([
            sys.executable, "-m", "cocoa_run", "-c",
            "import json,sys,device,notification; "
            "print(json.dumps({'prefix':sys.prefix,'executable':sys.executable,'platform':device.info()['platform'],'notifications':notification.available()}))",
        ], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        self.assertEqual(Path(values["prefix"]).resolve(), Path(sys.prefix).resolve())
        self.assertEqual(Path(values["executable"]).resolve(), Path(sys.executable).resolve())
        self.assertEqual(values["platform"], "macos")
        self.assertTrue(values["notifications"])


if __name__ == "__main__":
    unittest.main()
