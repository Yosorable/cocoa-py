"""Sharing data and native lifecycle contracts without sending any content."""

from collections import UserDict
import gc
from io import BytesIO
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from unittest.mock import patch

import share
from _cocoa import requests
from PIL import Image


@unittest.skipUnless(sys.platform == "darwin", "Requires Apple's macOS SDK")
class ShareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="cocoa_py_share_test_")
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(__file__).resolve().parents[1]
        binary = Path(cls.temporary.name) / ("_share_fixture" + sysconfig.get_config_var("EXT_SUFFIX"))
        build = subprocess.run([
            "xcrun", "clang++", "-std=c++17", "-fobjc-arc", "-O2", "-Wall", "-Wextra",
            "-Wno-unused-function", "-Wno-unused-parameter", "-mmacosx-version-min=14.0",
            "-dynamiclib", "-undefined", "dynamic_lookup", "-I" + sysconfig.get_paths()["include"],
            "-framework", "Foundation", "-framework", "AppKit", "-framework", "CoreGraphics",
            "-framework", "CoreLocation", "-framework", "IOKit",
            str(root / "tests/native/share_fixture.mm"), "-o", str(binary),
        ], capture_output=True, text=True, timeout=90)
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)
        spec = importlib.util.spec_from_file_location("_share_fixture", binary)
        cls.fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixture)

    def setUp(self):
        patcher = patch.object(requests, "_system", self.fixture)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.fixture.finish_active)
        self.addCleanup(self.fixture.reject_presentation, False)
        self.addCleanup(self.fixture.decline_presentation, False)
        self.addCleanup(self.fixture.reject_capsule, False)
        self.assertEqual(self.fixture.counts()[1], 0)

    def open(self, **kwargs):
        request = share.open(**kwargs)
        self.addCleanup(request.close)
        return request

    def files(self, request):
        return [Path(path) for path in self.fixture.files(request._handle)]

    def test_text_urls_and_local_files_remain_supported(self):
        with tempfile.TemporaryDirectory(prefix="cocoa_py_share_original_") as directory:
            file = Path(directory) / "original.txt"
            file.write_bytes(b"keep the original")
            request = self.open(text="Hello 😀\0tail", urls=["https://example.org/", "demo://item"], files=[file])
            self.assertEqual(self.fixture.strings(request._handle), ["Hello 😀\0tail", "https://example.org/", "demo://item"])
            self.assertEqual(self.files(request)[0].resolve(), file.resolve())
            request.close()
            self.assertEqual(file.read_bytes(), b"keep the original")

    def test_named_buffers_are_owned_preserved_and_removed_on_close(self):
        mutable = bytearray(bytes(range(256)) * 8192)
        expected = bytes(mutable)
        attachments = UserDict({"报告.bin": mutable, "empty.txt": b"", "A.txt": b"A", "a.txt": memoryview(b"a")})
        request = self.open(attachments=attachments)
        mutable[:] = b"x" * len(mutable)
        attachments.clear()
        paths = self.files(request)
        self.assertEqual([path.name for path in paths], ["报告.bin", "empty.txt", "A.txt", "a.txt"])
        self.assertEqual([path.read_bytes() for path in paths], [expected, b"", b"A", b"a"])
        directories = {path.parent.parent for path in paths}
        self.assertEqual(len(directories), 1)
        self.assertTrue(next(iter(directories)).name.startswith("cocoa_py_share_"))
        self.assertEqual(next(iter(directories)).stat().st_mode & 0o777, 0o700)
        request.close()
        self.assertTrue(all(not path.exists() for path in directories))
        self.assertEqual(self.fixture.counts()[1], 0)

    def test_images_preserve_pixels_transparency_and_buffer_ownership(self):
        with Image.new("RGBA", (12, 8), (90, 180, 45, 128)) as original:
            encoded = BytesIO()
            original.save(encoded, format="PNG")
            buffer = bytearray(encoded.getvalue())
            request = self.open(images=[original, buffer, memoryview(encoded.getvalue())])
            original.paste((0, 0, 0, 255), (0, 0, 12, 8))
            buffer[:] = b"x" * len(buffer)
        self.assertEqual(self.files(request), [])
        images = self.fixture.images(request._handle)
        self.assertEqual(len(images), 3)
        for data in images:
            with Image.open(BytesIO(data)) as image:
                self.assertEqual(image.size, (12, 8))
                actual = image.convert("RGBA").getpixel((5, 3))
                self.assertTrue(all(abs(a - b) <= 2 for a, b in zip(actual, (90, 180, 45, 128))))

    def test_jpeg_orientation_and_exact_encoded_attachment(self):
        with Image.new("RGB", (10, 6), "red") as image:
            exif = Image.Exif()
            exif[274] = 6
            output = BytesIO()
            image.save(output, "JPEG", exif=exif)
        data = output.getvalue()
        with Image.open(BytesIO(data)) as image:
            request = self.open(images=[image, data], attachments={"original.jpg": data})
        for png in self.fixture.images(request._handle):
            with Image.open(BytesIO(png)) as decoded:
                self.assertEqual(decoded.size, (6, 10))
        self.assertEqual(self.files(request)[0].read_bytes(), data)

    def test_completion_cancellation_and_errors_have_distinct_results(self):
        for outcome in ("success", "cancel_picker", "cancel", "error", "same_code_other_domain"):
            with self.subTest(outcome=outcome):
                request = self.open(attachments={"result.txt": b"result"})
                paths = self.files(request)
                if outcome != "cancel_picker":
                    self.fixture.select(request._handle)
                self.fixture.finish(request._handle, outcome)
                self.assertTrue(request.done)
                if outcome in ("error", "same_code_other_domain"):
                    with self.assertRaisesRegex(OSError, "Test service error"):
                        request.wait(0)
                else:
                    result = request.wait(0)
                    self.assertEqual(result, share.ShareResult(outcome == "success", None if outcome == "cancel_picker" else "Test service"))
                    self.assertEqual(request.wait(0), result)
                self.assertTrue(all(not path.exists() for path in paths))
                self.assertEqual(self.fixture.counts()[1], 0)
                request.close()

    def test_selected_service_keeps_attachments_after_close_and_handle_destruction(self):
        request = share.open(attachments={"pending.txt": b"still needed"})
        paths = self.files(request)
        self.fixture.select(request._handle)
        with self.assertRaises(TimeoutError):
            request.wait(0.01)
        request.close()
        del request
        gc.collect()
        self.assertEqual(self.fixture.counts()[1], 1)
        self.assertEqual(paths[0].read_bytes(), b"still needed")
        self.fixture.finish_active()
        self.assertFalse(paths[0].parent.parent.exists())
        self.assertEqual(self.fixture.counts()[1], 0)

    def test_unselected_handle_destruction_reclaims_temporary_files(self):
        request = share.open(attachments={"unused.txt": b"unused"})
        directory = self.files(request)[0].parent.parent
        del request
        gc.collect()
        self.assertFalse(directory.exists())
        self.assertEqual(self.fixture.counts()[1], 0)

    def test_dismissal_keeps_attachments_until_its_completion(self):
        request = share.open(attachments={"pending.txt": b"needed during dismissal"})
        paths = self.files(request)
        self.fixture.select(request._handle)
        count = self.fixture.dismiss(request._handle)
        self.assertEqual(self.fixture.dismiss(request._handle), count)
        with self.assertRaisesRegex(ValueError, "closed"):
            request.wait(0)
        request.close()
        del request
        gc.collect()
        self.assertEqual(paths[0].read_bytes(), b"needed during dismissal")
        self.assertEqual(self.fixture.counts()[1], 1)
        self.fixture.finish_dismissal()
        self.assertFalse(paths[0].parent.parent.exists())
        self.assertEqual(self.fixture.counts()[1], 0)

    def test_activity_completion_can_arrive_during_dismissal(self):
        request = self.open(attachments={"finished.txt": b"finished"})
        directory = self.files(request)[0].parent.parent
        self.fixture.dismiss(request._handle)
        self.fixture.finish(request._handle, "success")
        self.assertFalse(directory.exists())
        self.fixture.finish_dismissal()
        self.assertEqual(self.fixture.counts()[1], 0)

    def test_present_timeout_closes_unselected_request(self):
        before = set(Path(tempfile.gettempdir()).glob("cocoa_py_share_*"))
        with self.assertRaises(TimeoutError):
            share.present(attachments={"timeout.txt": b"timeout"}, timeout=0.01)
        self.assertEqual(self.fixture.counts()[1], 0)
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("cocoa_py_share_*")), before)

    def test_presentation_failure_cleans_registered_request_and_files(self):
        self.fixture.reject_presentation(True)
        with self.assertRaisesRegex(RuntimeError, "Test presentation failure"):
            share.open(attachments={"prepared.txt": b"prepared"})
        self.assertFalse(Path(self.fixture.rejected_directory()).exists())
        self.assertEqual(self.fixture.counts()[1], 0)

    def test_handle_allocation_failure_closes_prepared_share(self):
        self.fixture.reject_capsule(True)
        with self.assertRaises(MemoryError):
            share.open(attachments={"prepared.txt": b"prepared"})
        self.assertFalse(Path(self.fixture.rejected_directory()).exists())
        self.assertEqual(self.fixture.counts()[1], 0)

    def test_declined_presentation_fails_and_removes_prepared_files(self):
        self.fixture.decline_presentation(True)
        with self.assertRaisesRegex(RuntimeError, "could not present"):
            share.open(attachments={"prepared.txt": b"prepared"})
        self.assertFalse(Path(self.fixture.rejected_directory()).exists())
        self.assertEqual(self.fixture.counts()[1], 0)

    def test_invalid_inputs_do_not_present_or_leave_temporary_files(self):
        before = set(Path(tempfile.gettempdir()).glob("cocoa_py_share_*"))
        count = self.fixture.counts()[0]
        cases = [
            {}, {"text": 123}, {"images": b"not a sequence"}, {"images": [b"not an image"]},
            {"images": [memoryview(b"abcdef")[::2]]}, {"attachments": []},
            {"attachments": {1: b"x"}}, {"attachments": {"x.txt": "text"}},
            {"attachments": {"x.txt": memoryview(b"abcdef")[::2]}},
            {"files": "not-a-sequence"}, {"files": ["/tmp/not\0a-path"]},
            {"urls": ["relative"]}, {"urls": ["file:///tmp/x"]},
        ]
        cases += [{"attachments": {name: b"x"}} for name in ("", ".", "..", "../x", "a/b", "a\\b", "nul\0.txt")]
        for options in cases:
            with self.subTest(options=repr(options)):
                with self.assertRaises((ValueError, TypeError, BufferError)):
                    share.open(**options)
                self.assertEqual(self.fixture.counts(), (count, 0))
                self.assertEqual(set(Path(tempfile.gettempdir()).glob("cocoa_py_share_*")), before)
        # A failure after writing the first attachment must remove it as well.
        with self.assertRaises(OSError):
            share.open(attachments={"first.txt": b"first", "x" * 300: b"too long"})
        self.assertEqual(self.fixture.counts(), (count, 0))
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("cocoa_py_share_*")), before)
        for timeout in (-1, float("nan"), 3601):
            with self.assertRaises(ValueError):
                share.present(text="invalid timeout", timeout=timeout)
            self.assertEqual(self.fixture.counts(), (count, 0))

    def test_buffer_exporter_cannot_invalidate_native_container_iteration(self):
        attachments = {"second.txt": b"second"}
        class Exporter:
            def __buffer__(self, flags):
                attachments.clear()
                return memoryview(b"first")
        attachments["first.txt"] = Exporter()
        native = {"images": [], "attachments": attachments}
        handle = self.fixture.start("share.present", json.dumps(dict(text=None, files=[], urls=[])), native)
        try:
            paths = [Path(path) for path in self.fixture.files(handle)]
            self.assertEqual([path.read_bytes() for path in paths], [b"second", b"first"])
        finally:
            self.fixture.close(handle)

    def test_native_buffer_validation_and_legacy_start(self):
        payload = json.dumps(dict(text="legacy", files=[], urls=[]))
        handle = self.fixture.start("share.present", payload)
        self.fixture.close(handle)
        for buffers in ([], {}, {"images": [], "attachments": [], "extra": 1}):
            with self.assertRaises(TypeError):
                self.fixture.start("share.present", payload, buffers)
        with self.assertRaises(TypeError):
            self.fixture.start("device.info", "{}", {"images": [], "attachments": {}})

    def test_input_containers_survive_reentrant_sequence_copy(self):
        # Isolate the native lifetime regression so a future failure cannot
        # terminate the rest of the test suite. The fixture never presents UI.
        script = '''
import importlib.util, json, sys
from pathlib import Path

spec = importlib.util.spec_from_file_location('_share_fixture', sys.argv[1])
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
payload = json.dumps(dict(text=None, files=[], urls=[]))
for base in (list, tuple):
    for reject in (False, True):
        events = []
        class Attachments(dict):
            def __del__(self):
                events.append('released')
        class Images(base):
            def __iter__(self):
                buffers.clear()
                events.append('iterated')
                if reject:
                    raise LookupError('Test sequence failure')
                return super().__iter__()
        buffers = {'images': Images(), 'attachments': Attachments({'report.txt': b'retained'})}
        before = fixture.counts()
        try:
            handle = fixture.start('share.present', payload, buffers)
        except LookupError as error:
            assert reject and str(error) == 'Test sequence failure', error
            assert fixture.counts() == before
        else:
            try:
                assert not reject
                paths = [Path(path) for path in fixture.files(handle)]
                assert [path.read_bytes() for path in paths] == [b'retained']
                directory = paths[0].parent.parent
            finally:
                fixture.close(handle)
            assert not directory.exists()
            assert fixture.counts() == (before[0] + 1, 0)
        assert buffers == {}
        assert events == ['iterated', 'released'], events
'''
        result = subprocess.run([sys.executable, "-c", script, self.fixture.__file__],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_encoded_images_do_not_import_pillow(self):
        with Image.new("RGB", (2, 2), "blue") as image:
            output = BytesIO()
            image.save(output, "PNG")
        # Reject presentation to exercise the public wrapper and real decoder
        # in a fresh interpreter without showing a window or importing Pillow.
        script = '''
import importlib.util, sys
import share
from _cocoa import requests
spec = importlib.util.spec_from_file_location('_share_fixture', sys.argv[1])
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
requests._system = fixture
fixture.reject_presentation(True)
try:
    share.open(images=[bytes.fromhex(sys.argv[2])])
except RuntimeError as error:
    assert 'Test presentation failure' in str(error), error
else:
    raise AssertionError('The presentation failure was not reported')
assert not any(name == 'PIL' or name.startswith('PIL.') for name in sys.modules)
assert fixture.counts()[1] == 0
'''
        result = subprocess.run([sys.executable, "-c", script, self.fixture.__file__, output.getvalue().hex()],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("COCOA_PY_UI_TESTS") == "1",
                     "Set COCOA_PY_UI_TESTS=1 for the desktop sharing window")
class ShareWindowTests(unittest.TestCase):
    def test_image_and_attachment_window_can_be_closed_without_selecting_a_service(self):
        with Image.new("RGB", (16, 12), "blue") as image:
            with share.open(images=[image], attachments={"test.txt": b"cocoa-py window test"}) as request:
                self.assertFalse(request.done)
                with self.assertRaises(TimeoutError):
                    request.wait(0.05)
            self.assertTrue(request.closed)


if __name__ == "__main__":
    unittest.main()
