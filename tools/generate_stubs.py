"""Generate the five system modules' stubs from their inline Python annotations.

Run with CPython 3.14. Use --check to verify the checked-in stubs without writing.
Single-file modules ship companion stub packages so installed type checkers can
use their annotations. The Python implementations remain the source of truth.
"""

import argparse
import ast
from pathlib import Path

MODULES = ("clipboard", "device", "location", "motion", "share")


def declarations(nodes, exports=None):
    overloaded = {node.name for node in nodes if isinstance(node, ast.FunctionDef)
                  and any(isinstance(item, ast.Name) and item.id == "overload"
                          for item in node.decorator_list)}
    result = []
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            result.append(node)
        elif isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            result.extend(declarations(node.body))
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__all__"
                                                for target in node.targets):
            result.append(node)
        elif isinstance(node, ast.AnnAssign):
            result.append(node)
        elif isinstance(node, ast.TypeAlias):
            if exports is None or node.name.id in exports:
                result.append(node)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            if exports is not None and node.name not in exports:
                continue
            if exports is None and node.name.startswith("_") and node.name not in ("__init__", "__enter__", "__exit__", "__iter__"):
                continue
            if isinstance(node, ast.ClassDef):
                node.body = declarations(node.body) or [ast.Expr(ast.Constant(Ellipsis))]
            else:
                is_overload = any(isinstance(item, ast.Name) and item.id == "overload"
                                  for item in node.decorator_list)
                if node.name in overloaded and not is_overload:
                    continue
                node.body = [ast.Expr(ast.Constant(Ellipsis))]
            result.append(node)
    return result


def stub(source):
    tree = ast.parse(source.read_text())
    exports = next(ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__all__"
                                                         for target in node.targets))
    tree.body = declarations(tree.body, exports)
    return (f'"""Generated from {source.name} by tools/generate_stubs.py; do not edit."""\n\n'
            + ast.unparse(ast.fix_missing_locations(tree)) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parents[1] / "python"
    outdated = []
    for module in MODULES:
        source = directory / (module + ".py")
        target = directory / (module + "-stubs") / "__init__.pyi"
        content = stub(source)
        if args.check:
            if not target.is_file() or target.read_text() != content:
                outdated.append(str(target.relative_to(directory)))
        else:
            target.parent.mkdir(exist_ok=True)
            target.write_text(content)
    if outdated:
        parser.exit(1, "Run tools/generate_stubs.py to update: " + ", ".join(outdated) + "\n")


if __name__ == "__main__":
    main()
