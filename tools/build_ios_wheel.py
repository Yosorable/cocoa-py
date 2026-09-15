"""Build an arm64 iOS device or simulator wheel with a matching CPython framework.

Run with CPython 3.14 on macOS and a full Xcode installation. The build frontend
creates an isolated environment for setuptools and NumPy. It does not launch
Simulator, sign an application, or upload the resulting wheel.
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys


def validate_python_framework(framework, target):
    framework = Path(framework).resolve()
    if target not in {"iphoneos", "iphonesimulator"}:
        raise ValueError("The target must be iphoneos or iphonesimulator.")
    binary = framework / "Python"
    if framework.name != "Python.framework" or not binary.is_file():
        raise ValueError("--python-framework must point to an existing Python.framework")
    if not (framework / "Headers" / "Python.h").is_file():
        raise ValueError("The framework must include its CPython headers")
    architectures = subprocess.check_output(["lipo", "-archs", str(binary)], text=True).split()
    if "arm64" not in architectures:
        raise ValueError("The Python.framework must contain an arm64 slice")
    build = subprocess.check_output(["xcrun", "vtool", "-arch", "arm64", "-show-build", str(binary)], text=True)
    platform = "IOSSIMULATOR" if target == "iphonesimulator" else "IOS"
    if f"platform {platform}\n" not in build:
        raise ValueError(f"The Python.framework arm64 slice must target {platform}")
    return framework


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-framework", required=True, type=Path,
                        help="Path to the matching CPython 3.14 Python.framework")
    parser.add_argument("--target", choices=("iphoneos", "iphonesimulator"), default="iphoneos")
    parser.add_argument("--outdir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    try:
        framework = validate_python_framework(args.python_framework, args.target)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    environment = os.environ.copy()
    environment["COCOA_PY_IOS_FRAMEWORK"] = str(framework)
    environment["COCOA_PY_IOS_TARGET"] = args.target
    subprocess.run([
        sys.executable, "-m", "build", "--wheel", "--outdir", str(args.outdir.resolve()),
    ], cwd=Path(__file__).resolve().parents[1], env=environment, check=True)


if __name__ == "__main__":
    main()
