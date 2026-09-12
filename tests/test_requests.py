"""Real native request waits without sensors, permission prompts, or windows."""

import ctypes
import importlib.util
import json
from pathlib import Path
import queue
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from _cocoa import requests


@unittest.skipUnless(sys.platform == "darwin", "Requires Apple's macOS SDK")
class NativeRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="cocoa_py_requests_test_")
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(__file__).resolve().parents[1]
        binary = Path(cls.temporary.name) / ("_requests_fixture" + sysconfig.get_config_var("EXT_SUFFIX"))
        build = subprocess.run([
            "xcrun", "clang++", "-std=c++17", "-fobjc-arc", "-O2", "-Wall", "-Wextra",
            "-Wno-unused-function", "-Wno-unused-parameter", "-mmacosx-version-min=14.0",
            "-dynamiclib", "-undefined", "dynamic_lookup", "-I" + sysconfig.get_paths()["include"],
            "-framework", "Foundation", "-framework", "AppKit", "-framework", "CoreGraphics",
            "-framework", "CoreLocation", "-framework", "UserNotifications", "-framework", "IOKit",
            str(root / "tests/native/requests_fixture.mm"), "-o", str(binary),
        ], capture_output=True, text=True, timeout=90)
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)
        spec = importlib.util.spec_from_file_location("_requests_fixture", binary)
        cls.fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixture)

    def setUp(self):
        patcher = patch.object(requests, "_system", self.fixture)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, kind=requests.Request):
        request = kind("fixture.pending", {})
        self.addCleanup(request.close)
        return request

    def worker(self, request, function):
        values, errors = [], []
        done = threading.Event()

        def run():
            try:
                values.append(function())
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def cleanup():
            request.close()
            thread.join(2)
            self.assertFalse(thread.is_alive(), "A request waiter did not exit after close")

        self.addCleanup(cleanup)
        return thread, done, values, errors

    def await_blocking_wait(self, previous):
        deadline = time.monotonic() + 1
        while self.fixture.blocking_waits() == previous and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertGreater(self.fixture.blocking_waits(), previous)

    def test_consumed_samples_leave_no_old_wake_signals(self):
        stream = self.request(requests.Stream)
        for value in range(1000):
            self.fixture.push(stream._handle, value)
            self.assertEqual(stream.read(0), value)
        self.assertEqual(stream.stats["buffered"], 0)
        self.assertEqual(self.fixture.drain_signals(stream._handle), 0)
        self.fixture.push(stream._handle, 1)
        self.fixture.push(stream._handle, 2)
        self.assertEqual(stream.read(0), 1)
        self.assertEqual(stream.stats["buffered"], 1)
        self.assertEqual(stream.read(0), 2)
        self.assertEqual(self.fixture.drain_signals(stream._handle), 0)

    def test_idle_background_poll_waits_once_for_the_requested_interval(self):
        request = self.request()
        previous = self.fixture.blocking_waits()

        def poll():
            started = time.monotonic()
            state = json.loads(self.fixture.poll(request._handle, 0.06, False))
            return state, time.monotonic() - started

        thread, done, values, errors = self.worker(request, poll)
        self.assertTrue(done.wait(1))
        thread.join(1)
        self.assertEqual(errors, [])
        state, elapsed = values[0]
        self.assertFalse(state["done"])
        self.assertGreaterEqual(elapsed, 0.05)
        self.assertEqual(self.fixture.blocking_waits() - previous, 1)

    def test_completion_wait_does_not_spin_on_unread_stream_samples(self):
        stream = self.request(requests.Stream)
        self.fixture.push(stream._handle, 7)
        with patch.object(stream, "_snapshot", wraps=stream._snapshot) as snapshot:
            with self.assertRaises(TimeoutError):
                stream.wait(0.08)
            self.assertLessEqual(snapshot.call_count, 5)
        self.assertFalse(stream.closed)
        self.assertEqual(stream.read(0), 7)
        self.fixture.finish(stream._handle)
        self.assertEqual(stream.wait(0), 42)
        self.assertEqual(stream.wait(0), 42)

    def test_terminal_transitions_wake_a_blocked_native_poll(self):
        for action in ("finish", "fail", "close"):
            with self.subTest(action=action):
                request = self.request()
                previous = self.fixture.blocking_waits()
                thread, done, values, errors = self.worker(
                    request, lambda: json.loads(self.fixture.poll(request._handle, 0.25, False)))
                self.await_blocking_wait(previous)
                getattr(self.fixture, action)(request._handle)
                self.assertTrue(done.wait(0.2), "The waiter did not wake on the terminal event")
                thread.join(1)
                self.assertEqual(errors, [])
                state = values[0]
                if action == "finish":
                    self.assertTrue(state["done"])
                    self.assertEqual(state["result"], 42)
                elif action == "fail":
                    self.assertEqual(state["error"]["kind"], "runtime")
                    with self.assertRaisesRegex(RuntimeError, "Native test failure"):
                        request.wait(0)
                else:
                    self.assertTrue(state["closed"])

    def test_main_thread_wait_processes_queued_completion(self):
        request = self.request()
        self.fixture.finish_on_main(request._handle)
        self.assertEqual(request.wait(1), 42)

    def test_wait_and_read_remain_interruptible_without_a_native_result(self):
        inject = ctypes.PYFUNCTYPE(ctypes.c_int, ctypes.c_ulong, ctypes.py_object)(
            ("PyThreadState_SetAsyncExc", ctypes.pythonapi))
        clear = ctypes.PYFUNCTYPE(ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p)(
            ("PyThreadState_SetAsyncExc", ctypes.pythonapi))
        for kind, method in ((requests.Request, "wait"), (requests.Stream, "read")):
            with self.subTest(method=method):
                request = self.request(kind)
                previous = self.fixture.blocking_waits()
                thread, done, values, errors = self.worker(
                    request, lambda: getattr(request, method)(None))
                self.await_blocking_wait(previous)
                affected = inject(thread.ident, KeyboardInterrupt)
                if affected > 1:
                    clear(thread.ident, None)
                self.assertEqual(affected, 1)
                self.assertTrue(done.wait(0.5), "The injected interrupt did not reach Python")
                thread.join(1)
                self.assertEqual(values, [])
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], KeyboardInterrupt)
                self.assertFalse(request.done)

    def test_concurrent_empty_queue_transitions_preserve_delivery(self):
        stream = self.request(requests.Stream)
        received = queue.Queue()

        def consume():
            for _ in range(200):
                received.put(stream.read(1))

        thread, done, values, errors = self.worker(stream, consume)
        for value in range(200):
            self.fixture.push(stream._handle, value)
            self.assertEqual(received.get(timeout=2), value)
        self.assertTrue(done.wait(1))
        thread.join(1)
        self.assertEqual(errors, [])
        self.assertEqual(stream.stats["buffered"], 0)
        self.assertEqual(self.fixture.drain_signals(stream._handle), 0)


if __name__ == "__main__":
    unittest.main()
