"""Motion API validation and native coordination without physical sensors."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import motion
from _cocoa import _system


@unittest.skipUnless(sys.platform == "darwin", "Requires Apple's macOS SDK")
class MotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="cocoa_py_motion_test_")
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(__file__).resolve().parents[1]
        binary = Path(cls.temporary.name) / "motion_fixture"
        build = subprocess.run([
            "xcrun", "clang++", "-std=c++17", "-fobjc-arc", "-O2", "-Wall", "-Wextra",
            "-Wno-unused-function", "-Wno-unused-parameter", "-mmacosx-version-min=14.0",
            "-framework", "Foundation", "-framework", "AppKit", "-framework", "CoreMotion",
            str(root / "tests/native/motion_fixture.mm"), "-o", str(binary),
        ], capture_output=True, text=True, timeout=60)
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15, check=True)
        cls.events = json.loads(result.stdout)

    def test_native_options_fail_before_creating_manager(self):
        self.assertEqual(self.events["validation"]["managers_created"], 0)
        self.assertEqual(self.events["validation"]["errors"], ["value"] * 8)

    def test_capability_queries_share_idle_manager(self):
        self.assertEqual(self.events["queries"], dict(managers=1, starts=0,
                         frames=["arbitrary", "arbitrary_corrected", "magnetic_north", "true_north"]))

    def test_same_sensor_uses_one_producer_with_independent_delivery(self):
        result = self.events["fanout"]
        self.assertTrue(result["same_source"])
        self.assertEqual(result["native_starts"], 1)
        self.assertEqual(result["interval"], 0.01)
        self.assertEqual(result["fast_samples"], 31)
        self.assertEqual(result["slow_samples"], 7)
        self.assertAlmostEqual(result["acceleration"]["y"], 2 * 9.80665)

    def test_closing_fast_watch_keeps_slow_watch_and_changes_native_rate(self):
        self.assertEqual(self.events["close_fast"],
                         dict(interval=0.05, slow_continues=True, fast_detached=True))

    def test_last_close_stops_only_once_and_retires_source(self):
        self.assertEqual(self.events["last_close"], dict(sources=0, handlers=0, stops=1))

    def test_sensor_queues_and_shutdown_are_independent(self):
        result = self.events["independent_sensors"]
        for field in ("shared_manager", "separate_queues", "gyro_running"):
            self.assertTrue(result[field])
        self.assertEqual(result["gyro_samples"], 1)
        self.assertEqual(result["rotation"], dict(x=0.1, y=0.2, z=0.3))

    def test_bounded_queue_drops_oldest_measurements(self):
        result = self.events["bounded"]
        self.assertEqual(result["state"]["buffered"], 2)
        self.assertEqual(result["state"]["dropped"], 3)
        self.assertAlmostEqual(result["first"], 300.06)
        self.assertAlmostEqual(result["second"], 300.08)

    def test_raw_magnetic_field_preserves_microteslas(self):
        result = self.events["raw_magnetic"]
        self.assertEqual(result["magnetic_field"], dict(x=10, y=-20, z=30))
        self.assertEqual(result["timestamp"], 250)
        self.assertTrue(self.events["invalid_magnetic_skipped"])

    def test_fused_sample_has_units_frame_and_calibrated_magnetic_field(self):
        result = self.events["device"]
        self.assertAlmostEqual(result["gravity"]["z"], -9.80665)
        self.assertAlmostEqual(result["acceleration"]["x"], 0.1 * 9.80665)
        self.assertEqual(result["attitude"], dict(roll=0.5, pitch=-0.2, yaw=0.4))
        self.assertEqual(result["quaternion"], dict(x=0, y=0, z=0, w=1))
        self.assertEqual(result["magnetic_accuracy"], "high")
        self.assertEqual(result["magnetic_field"], dict(x=10, y=20, z=30))
        self.assertEqual(result["reference_frame"], "magnetic_north")
        self.assertEqual(self.events["native_frame"], 4)

    def test_reference_frame_conflict_does_not_change_active_stream(self):
        self.assertEqual(self.events["conflict"], dict(error="value", still_running=True))
        self.assertTrue(self.events["same_frame"])
        self.assertEqual(self.events["frame_after_close"], 8)
        self.assertEqual(self.events["unsupported_frame"], "not_implemented")

    def test_unusable_optional_magnetic_field_does_not_discard_attitude(self):
        for sample in self.events["optional_magnetic"]:
            self.assertIsNone(sample["magnetic_field"])
            self.assertEqual(sample["magnetic_accuracy"], "uncalibrated")
            self.assertEqual(sample["quaternion"]["w"], 1)

    def test_invalid_required_fields_and_out_of_order_samples_are_discarded(self):
        for name in ("invalid_device_skipped", "invalid_acceleration_skipped", "out_of_order_skipped"):
            with self.subTest(name=name):
                self.assertTrue(self.events[name])

    def test_late_error_cannot_close_a_reopened_sensor(self):
        self.assertEqual(self.events["late_callbacks"], dict(old_stopped=True, new_sample=1, new_error=None))

    def test_sensor_error_closes_its_watches_and_preserves_other_sensors(self):
        result = self.events["error_cleanup"]
        for key in ("first", "second"):
            self.assertTrue(result[key]["done"])
            self.assertTrue(result[key]["closed"])
            self.assertEqual(result[key]["buffered"], 0)
            self.assertEqual(result[key]["error"]["kind"], "permission")
        self.assertTrue(result["detached"])
        self.assertTrue(result["other_sensor_continues"])
        self.assertEqual(self.events["join_failing_source"], "permission")
        self.assertTrue(self.events["restart_after_error"])

    def test_missing_true_north_is_an_error_with_cleanup(self):
        result = self.events["unavailable_north"]
        self.assertEqual(result["state"]["error"]["kind"], "os")
        self.assertTrue(result["state"]["closed"])
        self.assertTrue(result["detached"])

    def test_native_start_failure_leaves_no_subscription(self):
        self.assertEqual(self.events["start_exception"], dict(error="runtime", sources=0))
        self.assertEqual(self.events["missing_usage"], "runtime")
        self.assertEqual(self.events["missing_hardware"], "not_implemented")

    def test_concurrent_close_ignores_callbacks_and_releases_all_sources(self):
        self.assertTrue(self.events["concurrent_close"])
        self.assertEqual(self.events["final"], dict(managers=1, sources=0, handlers=0))

    def test_invalid_public_options_are_rejected_on_every_platform(self):
        for options in (dict(sensor="unknown"), dict(sensor=[]), dict(interval=True),
                        dict(interval="0.1"), dict(interval=None), dict(interval=0),
                        dict(interval=1.1), dict(interval=float("nan")), dict(interval=float("inf")),
                        dict(interval=10**1000), dict(capacity=True), dict(capacity=1.5),
                        dict(capacity=0), dict(capacity=4097), dict(reference_frame=True),
                        dict(reference_frame=[]), dict(reference_frame="unknown"),
                        dict(sensor="gyroscope", reference_frame="arbitrary")):
            with self.subTest(options=options):
                with self.assertRaises((ValueError, TypeError)):
                    motion.watch(**options)

    def test_native_bridge_rejects_invalid_types_even_on_desktop(self):
        for extra in (dict(capacity=1.5), dict(interval=True), dict(reference_frame=True)):
            options = dict(sensor="device", interval=0.1, capacity=2)
            options.update(extra)
            handle = _system.start("motion.watch", json.dumps(options))
            try:
                self.assertEqual(json.loads(_system.poll(handle, 0, False))["error"]["kind"], "value")
            finally:
                _system.close(handle)

    def test_desktop_reports_no_phone_sensors_or_reference_frames(self):
        self.assertFalse(any(motion.available().values()))
        self.assertEqual(motion.reference_frames(), [])
        with self.assertRaises(NotImplementedError):
            motion.watch()


if __name__ == "__main__":
    unittest.main()
