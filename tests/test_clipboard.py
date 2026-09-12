"""Real clipboard contracts on a private macOS pasteboard, never the user's."""

from collections import UserDict
from io import BytesIO
import importlib.util
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import threading
import unittest
from unittest.mock import patch

import clipboard


@unittest.skipUnless(sys.platform == "darwin", "Requires Apple's macOS SDK")
class ClipboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="cocoa_py_clipboard_test_")
        cls.addClassCleanup(cls.temporary.cleanup)
        root = Path(__file__).resolve().parents[1]
        binary = Path(cls.temporary.name) / ("_clipboard_fixture" + sysconfig.get_config_var("EXT_SUFFIX"))
        build = subprocess.run([
            "xcrun", "clang++", "-std=c++17", "-fobjc-arc", "-O2", "-Wall", "-Wextra",
            "-Wno-unused-function", "-Wno-unused-parameter", "-mmacosx-version-min=14.0",
            "-dynamiclib", "-undefined", "dynamic_lookup", "-I" + sysconfig.get_paths()["include"],
            "-framework", "Foundation", "-framework", "AppKit", "-framework", "CoreGraphics",
            str(root / "tests/native/clipboard_fixture.mm"), "-o", str(binary),
        ], capture_output=True, text=True, timeout=90)
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)
        spec = importlib.util.spec_from_file_location("_clipboard_fixture", binary)
        cls.fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixture)
        cls.addClassCleanup(cls.fixture.release)

    def setUp(self):
        patcher = patch.object(clipboard, "_native", self.fixture.clipboard)
        patcher.start()
        self.addCleanup(patcher.stop)
        clipboard.clear()

    def test_text_unicode_nul_empty_and_absent_are_distinct(self):
        self.assertIsNone(clipboard.read_text())
        for text in ("Hello", "中文 😀\0tail", ""):
            clipboard.write_text(text, local_only=True)
            self.assertEqual(clipboard.read_text(), text)
            self.assertIn("public.utf8-plain-text", clipboard.types())
        clipboard.clear()
        self.assertIsNone(clipboard.read_text())
        self.assertFalse(clipboard.has_text())
        self.assertEqual(clipboard.types(), [])

    def test_binary_round_trip_large_empty_and_mutable_buffers(self):
        kind = "org.cocoa-py.test.binary"
        for value in (b"", bytes(range(256)) * 8192, bytearray(b"\0\xffdata"), memoryview(b"view")):
            clipboard.write_bytes(value, type=kind, local_only=True)
            expected = bytes(value)
            if isinstance(value, bytearray):
                value[:] = b"x" * len(value)
            self.assertEqual(clipboard.read_bytes(kind), expected)
        self.assertIsNone(clipboard.read_bytes("org.cocoa-py.test.absent"))
        self.assertFalse(clipboard.has_text())

    def test_multiple_representations_belong_to_one_item(self):
        html, plain = b"<b>Hello</b>", b"Hello"
        clipboard.write_item(UserDict({"public.html": html, "public.utf8-plain-text": plain}), local_only=True)
        self.assertEqual(clipboard.read_bytes("public.html"), html)
        self.assertEqual(clipboard.read_text(), "Hello")
        self.assertTrue({"public.html", "public.utf8-plain-text"} <= set(clipboard.types()))
        clipboard.write_text("replacement")
        self.assertIsNone(clipboard.read_bytes("public.html"))

    def test_metadata_does_not_change_contents_and_counter_tracks_writes(self):
        clipboard.write_text("sentinel")
        before = clipboard.change_count()
        self.assertIsInstance(before, int)
        self.assertTrue(clipboard.has_text())
        self.assertFalse(clipboard.has_image())
        self.assertFalse(clipboard.has_urls())
        self.assertTrue(clipboard.types())
        self.assertEqual(clipboard.change_count(), before)
        clipboard.write_text("next")
        self.assertNotEqual(clipboard.change_count(), before)
        self.assertEqual(clipboard.read_text(), "next")

    def test_url_type_plain_text_fallback_and_file_urls(self):
        for url in ("https://example.org/a?q=1#b", "mailto:test@example.org",
                    "myapp://open/item", "file:///tmp/cocoa_py_clipboard_missing"):
            clipboard.write_url(url, local_only=True)
            self.assertEqual(clipboard.read_url(), url)
            self.assertEqual(clipboard.read_text(), url)
            self.assertTrue(clipboard.has_urls())
        clipboard.write_text("Ordinary text without a link.")
        self.assertEqual(clipboard.read_text(), "Ordinary text without a link.")
        self.assertIsNone(clipboard.read_url())
        self.assertFalse(clipboard.has_urls())
        self.assertFalse({"public.url", "public.file-url"} & set(clipboard.types()))

    def test_url_like_text_follows_system_advertised_representations(self):
        url = "https://example.org/"
        clipboard.write_text(url, local_only=True)
        self.assertEqual(clipboard.read_text(), url)
        offered = bool({"public.url", "public.file-url"} & set(clipboard.types()))
        self.assertEqual(clipboard.read_url(), url if offered else None)
        self.assertEqual(clipboard.has_urls(), offered)

        # Exercise the additional representation even when this OS does not
        # add it to a text write, as observed on an iOS device.
        clipboard.write_item({
            "public.utf8-plain-text": url.encode(),
            "public.url": url.encode(),
        }, local_only=True)
        self.assertEqual(clipboard.read_text(), url)
        self.assertEqual(clipboard.read_url(), url)
        self.assertTrue(clipboard.has_urls())

    def test_url_reads_require_advertised_types_even_with_native_coercion(self):
        self.fixture.url_type_contract()
        for kind, url in (("public.url", "https://example.org/"),
                          ("public.file-url", "file:///tmp/cocoa_py_clipboard_missing")):
            clipboard.write_bytes(url.encode(), type=kind, local_only=True)
            self.assertEqual(clipboard.read_url(), url)
            self.assertTrue(clipboard.has_urls())

    def test_invalid_inputs_preserve_previous_contents(self):
        clipboard.write_text("keep")
        invalid = [
            lambda: clipboard.write_text(None),
            lambda: clipboard.write_text("x", local_only=1),
            lambda: clipboard.write_text("x", expires_in=True),
            lambda: clipboard.write_text("x", expires_in=float("nan")),
            lambda: clipboard.write_text("x", expires_in=float("inf")),
            lambda: clipboard.write_text("x", expires_in=0),
            lambda: clipboard.write_text("x", expires_in=31536001),
            lambda: clipboard.write_bytes(b"x", type=""),
            lambda: clipboard.write_bytes(b"x", type="bad\0type"),
            lambda: clipboard.write_bytes(b"x", type=4),
            lambda: clipboard.write_bytes(memoryview(b"abcdef")[::2], type="public.data"),
            lambda: clipboard.write_item({}),
            lambda: clipboard.write_item([]),
            lambda: clipboard.write_item({"public.data": "not bytes"}),
            lambda: clipboard.write_item({"public.data": b"valid", "invalid": object()}),
            lambda: clipboard.write_image(b"invalid image"),
            lambda: clipboard.write_image("/tmp/file.png"),
            lambda: clipboard.write_url("relative/path"),
            lambda: clipboard.write_url("https://example.org/has space"),
            lambda: clipboard.read_bytes(None),
            lambda: clipboard.read_image(as_bytes=1),
            lambda: self.fixture.clipboard("invalid"),
            lambda: self.fixture.clipboard("read_text", b"unused"),
        ]
        before = clipboard.change_count()
        for action in invalid:
            with self.subTest(action=action), self.assertRaises((ValueError, TypeError, BufferError)):
                action()
            self.assertEqual(clipboard.change_count(), before)
            self.assertEqual(clipboard.read_text(), "keep")

    def test_macos_expiration_is_rejected_before_any_replacement(self):
        clipboard.write_text("keep")
        before = clipboard.change_count()
        for action in (
            lambda: clipboard.write_text("new", expires_in=1),
            lambda: clipboard.write_bytes(b"x", type="public.data", expires_in=1),
            lambda: clipboard.write_url("https://example.org/", expires_in=1),
            lambda: clipboard.write_image(b"x", expires_in=1),
            lambda: clipboard.write_item({"public.data": b"x"}, expires_in=1),
        ):
            with self.assertRaises(NotImplementedError):
                action()
        self.assertEqual(clipboard.change_count(), before)
        self.assertEqual(clipboard.read_text(), "keep")

    def test_reads_are_first_item_but_presence_checks_cover_any_item(self):
        self.fixture.seed_two_items()
        self.assertEqual(clipboard.read_text(), "first")
        self.assertIsNone(clipboard.read_url())
        self.assertIsNone(clipboard.read_bytes("public.url"))
        self.assertNotIn("public.url", clipboard.types())
        self.assertTrue(clipboard.has_urls())

    def test_worker_without_appkit_loop_fails_instead_of_deadlocking(self):
        errors = []
        def query():
            try:
                clipboard.types()
            except RuntimeError as error:
                errors.append(str(error))
        worker = threading.Thread(target=query, daemon=True)
        worker.start()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn("main thread", errors[0])

    def test_import_does_not_access_clipboard_or_load_image_libraries(self):
        result = subprocess.run([sys.executable, "-I", "-c", """
from _cocoa import _system
def unexpected(*args, **kwargs):
    raise AssertionError("Import must not inspect the clipboard")
_system.clipboard = unexpected
import clipboard, sys
assert "PIL" not in sys.modules
assert "numpy" not in sys.modules
assert "_cocoa.requests" not in sys.modules
"""], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_images_pillow_native_formats_and_optional_import(self):
        from PIL import Image
        with Image.new("RGBA", (9, 7), (20, 80, 150, 128)) as source:
            clipboard.write_image(source, local_only=True)
            self.assertTrue(clipboard.has_image())
            with clipboard.read_image() as result:
                self.assertEqual(result.size, source.size)
                self.assertEqual(result.convert("RGBA").getpixel((0, 0)), source.getpixel((0, 0)))
            self.assertEqual(source.getpixel((0, 0)), (20, 80, 150, 128))
            encoded = BytesIO()
            source.save(encoded, format="TIFF")
        import builtins
        original_import = builtins.__import__
        def no_pillow(name, *args, **kwargs):
            if name == "PIL" or name.startswith("PIL."):
                raise AssertionError("Encoded image operations must not import Pillow")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=no_pillow):
            clipboard.write_image(memoryview(encoded.getvalue()))
            png = clipboard.read_image(as_bytes=True)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        clipboard.write_bytes(encoded.getvalue(), type="public.tiff")
        with clipboard.read_image() as result:
            self.assertEqual(result.size, (9, 7))
        clipboard.clear()
        self.assertIsNone(clipboard.read_image())

    def test_encoded_and_pillow_images_apply_exif_orientation(self):
        from PIL import Image
        with Image.new("RGB", (12, 6), "red") as source:
            exif = Image.Exif()
            exif[274] = 6
            encoded = BytesIO()
            source.save(encoded, format="JPEG", exif=exif)
        for use_pillow in (False, True):
            with self.subTest(use_pillow=use_pillow):
                if use_pillow:
                    with Image.open(BytesIO(encoded.getvalue())) as image:
                        clipboard.write_image(image)
                else:
                    clipboard.write_image(encoded.getvalue())
                with clipboard.read_image() as result:
                    self.assertEqual(result.size, (6, 12))


if __name__ == "__main__":
    unittest.main()
