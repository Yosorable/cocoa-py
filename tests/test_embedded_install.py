"""Verify embedded copying and upgrades without relying on a native device."""

from contextlib import redirect_stdout
import csv
import importlib.metadata
import io
from pathlib import Path
import runpy
import tempfile
import unittest

install = runpy.run_path(str(Path(__file__).parents[1] / "tools/install_embedded.py"))["install"]


class EmbeddedInstallerTests(unittest.TestCase):
    def test_installed_files_and_metadata_match_the_native_host_layout(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            target = Path(temporary)
            install(target)
            distribution = next(importlib.metadata.distributions(path=[str(target)]))
            self.assertEqual(distribution.metadata["Name"], "cocoa-py")
            self.assertTrue((target / "scene/_resources/SceneShaders.metal").is_file())
            self.assertTrue((target / "_cocoa/__init__.py").is_file())
            self.assertTrue((target / "_cocoa/requests.py").is_file())
            self.assertTrue((target / "_cocoa/py.typed").is_file())
            self.assertTrue((target / "_cocoa/_system.pyi").is_file())
            for name in ("device", "location", "motion", "share", "clipboard"):
                self.assertTrue((target / (name + "-stubs") / "__init__.pyi").is_file())
            self.assertFalse((target / "cocoa_run.py").exists())
            self.assertFalse((target / "_cocoa/_runner").exists())
            self.assertFalse(list(target.rglob("*.egg-info")))
            self.assertFalse(list(target.rglob("*.so")))
            for file in distribution.files:
                self.assertTrue(distribution.locate_file(file).is_file(), str(file))

    def test_upgrade_removes_stale_owned_modules_and_preserves_unrelated_files(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            target = Path(temporary) / "site-packages"
            install(target)
            metadata = next(target.glob("cocoa_py-*.dist-info"))
            obsolete = target / "scene/obsolete.py"
            obsolete.write_text("old = True\n")
            obsolete_stub = target / "motion-stubs/obsolete.pyi"
            obsolete_stub.write_text("old: bool\n")
            unrelated = target / "unrelated.py"
            unrelated.write_text("preserve = True\n")
            outside = Path(temporary) / "outside.py"
            outside.write_text("preserve = True\n")
            with (metadata / "RECORD").open("a", newline="") as output:
                writer = csv.writer(output)
                writer.writerows([("scene/obsolete.py", "", ""), ("motion-stubs/obsolete.pyi", "", ""),
                                  ("unrelated.py", "", ""), ("../outside.py", "", "")])
            install(target)
            self.assertFalse(obsolete.exists())
            self.assertFalse(obsolete_stub.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(outside.exists())

    def test_upgrade_removes_the_previous_request_helper_package(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            target = Path(temporary)
            install(target)
            metadata = next(target.glob("cocoa_py-*.dist-info"))
            old = target / "_cocoa_support"
            old.mkdir()
            (old / "__init__.py").write_text("old = True\n")
            with (metadata / "RECORD").open("a", newline="") as output:
                csv.writer(output).writerow(("_cocoa_support/__init__.py", "", ""))
            install(target)
            self.assertFalse(old.exists())
            self.assertTrue((target / "_cocoa/requests.py").is_file())

    def test_upgrade_rejects_a_symlink_to_an_outside_directory(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            target = Path(temporary) / "site-packages"
            outside = Path(temporary) / "outside"
            target.mkdir()
            outside.mkdir()
            marker = outside / "__init__.py"
            marker.write_text("preserve = True\n")
            (target / "scene").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                install(target)
            self.assertEqual(marker.read_text(), "preserve = True\n")


if __name__ == "__main__":
    unittest.main()
