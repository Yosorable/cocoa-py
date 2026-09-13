"""Native location/heading event handling without acquiring a real position."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import location
from _cocoa import _system


@unittest.skipUnless(sys.platform == "darwin", "Requires Apple's macOS SDK")
class LocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="cocoa_py_location_test_")
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(__file__).resolve().parents[1]
        binary = Path(cls.temporary.name) / "location_fixture"
        build = subprocess.run([
            "xcrun", "clang++", "-std=c++17", "-fobjc-arc", "-O2", "-Wall", "-Wextra",
            "-Wno-unused-function", "-Wno-unused-parameter", "-mmacosx-version-min=14.0",
            "-framework", "Foundation", "-framework", "AppKit", "-framework", "CoreLocation",
            str(root / "tests/native/location_fixture.mm"), "-o", str(binary),
        ], capture_output=True, text=True, timeout=60)
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10, check=True)
        cls.events = json.loads(result.stdout)

    def test_current_uses_newest_valid_sample_and_stops(self):
        self.assertEqual(self.events["latest"]["sample"]["latitude"], 2)
        self.assertEqual(self.events["latest"]["stops"], 1)
        self.assertEqual(self.events["invalid_latest"]["latitude"], 1)

    def test_zero_age_rejects_cache_but_accepts_delayed_fresh_sample(self):
        result = self.events["zero_age"]
        self.assertTrue(result["rejected_cache"])
        self.assertTrue(result["state"]["done"])
        self.assertEqual(result["state"]["result"]["latitude"], 2)

    def test_watch_preserves_chronology_and_bounded_capacity(self):
        result = self.events["stream"]
        self.assertEqual(result["latitudes"], [2, 3])
        self.assertEqual(result["state"]["buffered"], 2)
        self.assertEqual(result["state"]["dropped"], 1)

    def test_invalid_coordinates_accuracy_and_old_samples_are_ignored(self):
        self.assertTrue(self.events["invalid_locations_skipped"])

    def test_magnetic_heading_preserves_missing_true_heading(self):
        sample = location.Heading(**self.events["magnetic"])
        self.assertEqual(sample.magnetic_heading, 123)
        self.assertIsNone(sample.true_heading)
        self.assertEqual(sample.accuracy, 5)

    def test_true_north_waits_for_a_fresh_heading_and_stops_location(self):
        result = self.events["true_north"]
        for key in ("waits_for_true", "ignores_fix", "rejects_old"):
            self.assertTrue(result[key])
        self.assertEqual(result["sample"]["true_heading"], 125)
        self.assertEqual(result["location_stops"], 1)

    def test_heading_stream_filters_unreliable_samples_and_counts_overflow(self):
        result = self.events["heading_stream"]
        self.assertTrue(result["rejects_invalid"])
        self.assertEqual(result["angles"], [2, 3])
        self.assertEqual(result["state"]["buffered"], 2)
        self.assertEqual(result["state"]["dropped"], 1)

    def test_closed_streams_release_manager_and_ignore_late_events(self):
        for key in ("closed_stream", "closed_heading"):
            with self.subTest(key=key):
                result = self.events[key]
                self.assertTrue(result["released_manager"])
                self.assertTrue(result["state"]["closed"])
                self.assertEqual(result["state"]["buffered"], 0)

    def test_magnetic_mode_does_not_need_location_authorization(self):
        result = self.events["magnetic_without_location"]
        self.assertTrue(result["started"])
        self.assertEqual(result["location_starts"], 0)
        self.assertIsNone(result["state"]["error"])

    def test_authorization_starts_once_and_revocation_stops_updates(self):
        result = self.events["authorization"]
        self.assertTrue(result["waited"])
        self.assertEqual(result["starts"], 1)
        self.assertEqual(result["stops"], 1)
        self.assertLess(abs(time.time() - result["started_at"]), 10)
        self.assertEqual(result["state"]["error"]["kind"], "permission")

    def test_fatal_error_stops_location_while_transient_error_can_retry(self):
        result = self.events["failure"]
        self.assertTrue(result["ignored_transient"])
        self.assertEqual(result["stops"], 1)
        self.assertEqual(result["state"]["error"]["kind"], "os")

    def test_desktop_compass_reports_unsupported_without_a_prompt(self):
        self.assertIs(location.heading_available(), False)
        with self.assertRaises(NotImplementedError):
            location.heading(timeout=0)
        with self.assertRaises(NotImplementedError):
            location.watch_heading()

    def test_refused_authorization_never_starts_sampling(self):
        for result in self.events["refused_permissions"]:
            self.assertEqual(result["starts"], 0)
            self.assertEqual(result["state"]["error"]["kind"], "permission")

    def test_permission_cancel_ignores_late_authorization(self):
        result = self.events["cancelled_permission"]
        self.assertTrue(result["was_pending"])
        self.assertTrue(result["released_manager"])
        self.assertTrue(result["state"]["closed"])
        self.assertEqual(result["starts"], 0)

    def test_nonfinite_optional_measurements_are_unavailable(self):
        result = self.events["nonfinite_optional"]
        self.assertTrue(result["serializable"])
        for field in ("altitude", "vertical_accuracy", "speed", "course"):
            self.assertIsNone(result["sample"][field])

    def test_invalid_geocoding_inputs_do_not_start_network_work(self):
        for result in self.events["invalid_geocoding"]:
            self.assertFalse(result["started"])
            self.assertEqual(result["state"]["error"]["kind"], "value")

    def test_geocoding_cancel_releases_producer_and_ignores_late_completion(self):
        result = self.events["cancelled_geocoding"]
        self.assertEqual(result["cancellations"], 1)
        self.assertTrue(result["released_geocoder"])
        self.assertTrue(result["state"]["closed"])
        self.assertFalse(result["state"]["done"])
        self.assertIsNone(result["state"]["result"])

    def test_geocoding_network_error_finishes_request(self):
        state = self.events["geocoding_failure"]
        self.assertTrue(state["done"])
        self.assertEqual(state["error"]["kind"], "os")

    def test_invalid_sampling_options_fail_before_access(self):
        cases = [
            (location.current, {"max_age": -1}), (location.current, {"accuracy": True}),
            (location.current, {"accuracy": 0}), (location.watch, {"distance_filter": -1}),
            (location.watch, {"capacity": 0}), (location.watch, {"capacity": 1.5}),
            (location.heading, {"orientation": "face_up"}), (location.heading, {"orientation": []}),
            (location.heading, {"true_north": 1}), (location.heading, {"max_age": True}),
            (location.heading, {"max_age": float("nan")}), (location.heading, {"max_age": 86401}),
            (location.watch_heading, {"angle_filter": 181}), (location.watch_heading, {"angle_filter": -1}),
            (location.watch_heading, {"capacity": True}), (location.watch_heading, {"capacity": 4097}),
        ]
        for function, arguments in cases:
            with self.subTest(function=function.__name__, arguments=arguments):
                with self.assertRaises((TypeError, ValueError)):
                    function(**arguments)

    def test_native_bridge_rejects_fractional_capacity_and_nonboolean_mode(self):
        for values in (dict(max_age=5, capacity=1.5),
                       dict(max_age=5, capacity=2, angle_filter=1, orientation="portrait", true_north=1)):
            handle = _system.start("location.watch_heading", json.dumps(values))
            try:
                state = json.loads(_system.poll(handle, 0, False))
                self.assertEqual(state["error"]["kind"], "value")
            finally:
                _system.close(handle)


if __name__ == "__main__":
    unittest.main()
