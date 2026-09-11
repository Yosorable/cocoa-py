"""Copy canonical Python modules and metadata into an embedded host's bundle.

Run with Python 3.11+ at build time. Native modules must be linked separately;
this installer does not compile code or alter the host interpreter.
"""

import argparse
import base64
import csv
import hashlib
from pathlib import Path
import shutil
import tomllib


def install(destination: Path):
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    source = root / "python"
    destination.mkdir(parents=True, exist_ok=True)
    files = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if not path.is_file() or "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if relative == Path("cocoa_run.py") or relative.name == "_runner":
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files.append(target)
    # Remove metadata left by a previous build of this same distribution.
    for previous in destination.glob("cocoa_py-*.dist-info"):
        shutil.rmtree(previous)
    metadata = destination / f"cocoa_py-{project['version']}.dist-info"
    licenses = metadata / "licenses"
    licenses.mkdir(parents=True)
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.4\n"
        f"Name: {project['name']}\nVersion: {project['version']}\n"
        f"Summary: {project['description']}\nRequires-Python: {project['requires-python']}\n"
        "License-Expression: MIT\nLicense-File: LICENSE\nLicense-File: Box2D-LICENSE\n"
        "Project-URL: Source, https://github.com/Yosorable/cocoa-py\n"
        "Provides-Extra: coreml\nRequires-Dist: numpy>=2.3.3,<3; extra == 'coreml'\n\n"
        "Native extensions are linked into the host and registered as built-in modules.\n"
    )
    (metadata / "INSTALLER").write_text("cocoa-py embedded installer\n")
    (metadata / "top_level.txt").write_text("audio\nclipboard\ncoreml\ndevice\nlocation\nmotion\nnotification\nphotos\nscene\nshare\n")
    shutil.copy2(root / "LICENSE", licenses / "LICENSE")
    shutil.copy2(root / "native/physics/box2d/LICENSE", licenses / "Box2D-LICENSE")
    files.extend(path for path in metadata.rglob("*") if path.is_file())
    with (metadata / "RECORD").open("w", newline="") as output:
        writer = csv.writer(output)
        for path in sorted(files):
            data = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            writer.writerow((path.relative_to(destination).as_posix(), "sha256=" + digest, len(data)))
        writer.writerow(((metadata / "RECORD").relative_to(destination).as_posix(), "", ""))
    print(f"Installed cocoa-py {project['version']} into {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="The built app's Python site-packages directory")
    install(parser.parse_args().destination)
