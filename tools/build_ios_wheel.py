"""Build an arm64 iPhoneOS wheel using a host's CPython framework.

Run with CPython 3.14 on macOS and a full Xcode installation. The build frontend
creates an isolated environment for setuptools and NumPy. It neither builds nor
launches a simulator, signs an application, nor uploads the resulting wheel.
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-framework", required=True, type=Path,
                        help="Path to an iPhoneOS CPython 3.14 Python.framework")
    parser.add_argument("--outdir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    framework = args.python_framework.resolve()
    if framework.name != "Python.framework" or not (framework / "Python").is_file():
        parser.error("--python-framework must point to an existing iPhoneOS Python.framework")
    if not (framework / "Headers" / "Python.h").is_file():
        parser.error("The framework must include its CPython headers")
    environment = os.environ.copy()
    environment["COCOA_PY_IOS_FRAMEWORK"] = str(framework)
    subprocess.run([
        sys.executable, "-m", "build", "--wheel", "--outdir", str(args.outdir.resolve()),
    ], cwd=Path(__file__).resolve().parents[1], env=environment, check=True)


if __name__ == "__main__":
    main()
