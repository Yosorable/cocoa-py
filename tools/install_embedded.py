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
    resolved_destination = destination.resolve()

    def contained(path):
        if not path.resolve().is_relative_to(resolved_destination):
            raise ValueError(f"An embedded destination path escapes site-packages: {path}")
        return path

    owned_roots = {path.name for pattern in ("*.py", "*.pyi") for path in source.glob(pattern)}
    owned_roots.update(path.name for path in source.iterdir() if path.is_dir()
                       and ((path / "__init__.py").is_file() or (path / "__init__.pyi").is_file()))
    owned_roots.add("_cocoa_support")
    emptied = set()
    # Reconcile only files owned by an earlier embedded installation. Never
    # follow a RECORD entry outside site-packages or delete another package.
    for previous in destination.glob("cocoa_py-*.dist-info"):
        record = contained(previous / "RECORD")
        if record.is_file():
            with record.open(newline="") as stored:
                for row in csv.reader(stored):
                    if not row:
                        continue
                    relative = Path(row[0])
                    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] not in owned_roots:
                        continue
                    target = contained(destination / relative)
                    if target.is_file() and not target.is_symlink():
                        target.unlink()
                        emptied.update(parent for parent in target.parents if parent != destination and destination in parent.parents)
        shutil.rmtree(previous)
    for directory in sorted(emptied, key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    files = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if not path.is_file() or (path.suffix not in (".py", ".pyi", ".metal", ".metallib")
                                  and path.name != "py.typed"):
            continue
        if any(part == "__pycache__" or part.startswith(".") or part.endswith((".egg-info", ".dist-info")) for part in relative.parts):
            continue
        if relative == Path("cocoa_run.py") or relative.name == "_runner":
            continue
        target = contained(destination / relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files.append(target)
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
    (metadata / "top_level.txt").write_text("_cocoa\naudio\nclipboard\ncoreml\ndevice\nlocation\nmotion\nphotos\nscene\nshare\n")
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
