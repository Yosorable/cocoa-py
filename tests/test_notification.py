"""Notification contracts without real permission prompts or scheduled alerts."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import notification


class NotificationTests(unittest.TestCase):
    def test_ambiguous_or_invalid_triggers_never_reach_native(self):
        future = datetime.now(timezone.utc) + timedelta(days=1)
        daily = notification.CalendarTrigger(hour=9)
        invalid = [
            {"delay": 1, "at": future}, {"delay": 0, "calendar": daily},
            {"at": future, "calendar": daily}, {"at": future, "repeat": True},
            {"calendar": daily, "repeat": True}, {"calendar": {}},
            {"at": datetime(2099, 1, 1)}, {"at": datetime(2000, 1, 1, tzinfo=timezone.utc)},
            {"delay": True}, {"delay": float("nan")}, {"delay": float("inf")},
            {"delay": -1}, {"delay": 31536001}, {"delay": 59, "repeat": True},
            {"identifier": ""}, {"identifier": "a" * 129}, {"identifier": "😀" * 65},
            {"subtitle": None},
        ]
        with patch.object(notification, "_call") as call:
            for args in invalid:
                with self.subTest(args=args), self.assertRaises((ValueError, TypeError)):
                    notification.schedule("Test", **args)
            call.assert_not_called()

    def test_absolute_date_preserves_instant_and_rounds_up(self):
        at = datetime(2099, 4, 5, 10, 30, 0, 250000, tzinfo=timezone(timedelta(hours=8)))
        with patch.object(notification, "_call", return_value="alarm") as call:
            self.assertEqual(notification.schedule("Test", at=at, identifier="alarm"), "alarm")
            self.assertEqual(call.call_args.args[1]["trigger"],
                             {"kind": "date", "timestamp": int(at.timestamp()) + 1})

    def test_calendar_is_immutable_and_validates_clock_fields(self):
        from dataclasses import FrozenInstanceError
        daily = notification.CalendarTrigger(hour=8, minute=30)
        with self.assertRaises(FrozenInstanceError):
            daily.hour = 7
        for args in ({"hour": 24}, {"hour": True}, {"hour": 8, "minute": 1.5},
                     {"hour": 8, "weekday": -1}, {"hour": 8, "weekday": 7},
                     {"hour": 8, "second": 60}, {"hour": 8, "timezone": ""}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                notification.CalendarTrigger(**args)
        with patch.object(notification, "_call") as call:
            notification.schedule("Local", calendar=daily)
            local = call.call_args.args[1]["trigger"]
            self.assertNotIn("timezone", local)
            self.assertNotIn("weekday", local)

    def test_legacy_defaults_and_immediate_delivery_remain_distinct(self):
        with patch.object(notification, "_call") as call:
            notification.schedule("Default")
            options = call.call_args.args[1]
            self.assertEqual(options["trigger"], {"kind": "interval", "seconds": 1, "repeat": False})
            self.assertTrue(options["sound"])
            self.assertTrue(options["foreground"])
            notification.schedule("Immediate", delay=0, sound=False, foreground=False)
            options = call.call_args.args[1]
            self.assertEqual(options["trigger"], {"kind": "immediate"})
            self.assertFalse(options["sound"])
            self.assertFalse(options["foreground"])

    def test_legacy_cancel_all_keeps_its_pending_id_scope(self):
        with patch.object(notification, "pending", return_value=[{"identifier": "a"}, {"identifier": "b"}]), \
             patch.object(notification, "cancel") as cancel:
            notification.cancel_all()
            self.assertEqual([args.args[0] for args in cancel.call_args_list], ["a", "b"])


@unittest.skipUnless(sys.platform == "darwin", "Requires Apple's macOS SDK")
class NativeNotificationTests(unittest.TestCase):
    def test_native_triggers_permissions_namespaces_and_lifetimes(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="cocoa_py_notification_test_") as directory:
            binary = Path(directory) / "notifications"
            build = subprocess.run([
                "xcrun", "clang++", "-std=c++17", "-fobjc-arc", "-O2", "-Wall", "-Wextra",
                "-Wno-unused-function", "-Wno-unused-parameter", "-mmacosx-version-min=14.0",
                "-framework", "Foundation", "-framework", "AppKit", "-framework", "UserNotifications",
                str(root / "tests/native/notification_contract.mm"), "-o", str(binary),
            ], capture_output=True, text=True, timeout=90)
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("notification native contracts passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
