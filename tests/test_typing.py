"""Check shipped type information and consumer inference independently of sensors."""

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import _cocoa
import clipboard

ROOT = Path(__file__).resolve().parents[1]


class TypingTests(unittest.TestCase):
    def test_generated_stubs_match_public_implementations(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools/generate_stubs.py"), "--check"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_installed_distribution_contains_type_information(self):
        directory = Path(clipboard.__file__).parent
        for module in ("device", "location", "motion", "clipboard", "share"):
            self.assertTrue((directory / (module + "-stubs") / "__init__.pyi").is_file())
        self.assertTrue((Path(_cocoa.__file__).parent / "py.typed").is_file())
        self.assertTrue((Path(_cocoa.__file__).parent / "_system.pyi").is_file())

    def check_consumer(self, checker):
        with tempfile.TemporaryDirectory(prefix="cocoa_py_typing_") as temporary:
            # Keep source paths and repository settings out of resolution. These
            # checks must exercise the distribution installed in this interpreter.
            config = Path(temporary) / "pyrightconfig.json"
            config.write_text(json.dumps({"pythonVersion": "3.14", "typeCheckingMode": "strict"}))
            for filename in ("system_api.py", "system_api_errors.py"):
                source = ROOT / "tests/typecheck" / filename
                target = Path(temporary) / filename
                target.write_text(source.read_text())
                expected = {index for index, line in enumerate(source.read_text().splitlines(), 1)
                            if "# expected-error" in line}
                if checker == "pyright":
                    command = [sys.executable, "-m", "pyright", "--project", str(config),
                               "--pythonpath", sys.executable, "--outputjson", str(target)]
                else:
                    command = [sys.executable, "-m", "mypy", "--strict", "--no-incremental",
                               "--python-version", "3.14", "--python-executable", sys.executable,
                               str(target)]
                result = subprocess.run(command, cwd=temporary, capture_output=True, text=True, timeout=60)
                output = result.stdout + result.stderr
                if checker == "pyright":
                    diagnostics = json.loads(result.stdout)["generalDiagnostics"]
                    actual = {item["range"]["start"]["line"] + 1 for item in diagnostics
                              if item["severity"] == "error"}
                else:
                    actual = {int(line) for line in re.findall(r":(\d+): error:", result.stdout)}
                self.assertEqual(actual, expected, output)
                self.assertEqual(result.returncode, int(bool(expected)), output)

    @unittest.skipUnless(importlib.util.find_spec("mypy"), "Install mypy to check consumer typing")
    def test_mypy_installed_consumer(self):
        self.check_consumer("mypy")

    @unittest.skipUnless(importlib.util.find_spec("pyright"), "Install pyright to check consumer typing")
    def test_pyright_installed_consumer(self):
        self.check_consumer("pyright")


if __name__ == "__main__":
    unittest.main()
