"""Check installed package imports in fresh interpreters without device access."""

import subprocess
import sys
import textwrap
import unittest


NATIVE_NAMES = {"_cocoa." + name for name in (
    "_audio", "_photos", "_metal", "_physics", "_scene_accel", "_system",
)}


class ImportTests(unittest.TestCase):
    def run_python(self, source):
        result = subprocess.run([sys.executable, "-I", "-c", textwrap.dedent(source)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_private_package_does_not_load_features_or_modify_search_paths(self):
        self.run_python("""
            import sys
            before = sys.path[:]
            import _cocoa
            assert sys.path == before
            assert not any(name.startswith('_cocoa.') for name in sys.modules)
            assert not any(name in sys.modules for name in ('numpy', 'coreml', 'audio', 'scene', 'photos'))
        """)

    def test_public_imports_preserve_independent_native_loading(self):
        cases = {
            "import audio": {"_cocoa._audio"},
            "import photos": {"_cocoa._photos"},
            "import scene": {"_cocoa._metal", "_cocoa._scene_accel"},
            "from scene import PhysicsBody": {"_cocoa._metal", "_cocoa._scene_accel", "_cocoa._physics"},
            "import coreml": set(),
        }
        cases.update({"import " + name: {"_cocoa._system"} for name in (
            "location", "motion", "clipboard", "device", "share",
        )})
        for statement, expected in cases.items():
            with self.subTest(statement=statement):
                self.run_python(f"""
                    import sys
                    before = sys.path[:]
                    {statement}
                    actual = set(sys.modules) & {NATIVE_NAMES!r}
                    assert actual == {expected!r}, actual
                    assert sys.path == before
                    if {statement!r} != 'import coreml':
                        assert 'numpy' not in sys.modules and 'coreml' not in sys.modules
                """)

    def test_extensions_have_package_names_and_no_legacy_top_level_aliases(self):
        self.run_python(f"""
            import importlib
            import importlib.util
            from pathlib import Path
            import sys
            for name in {sorted(NATIVE_NAMES)!r}:
                module = importlib.import_module(name)
                assert module.__name__ == name, module.__name__
                assert module.__spec__.name == name
                assert Path(module.__file__).parent.name == '_cocoa', module.__file__
            from _cocoa import _audio, _metal, _physics, _photos, _scene_accel, _system
            for module, function in ((_audio, _audio.close), (_metal, _metal.resource_counts),
                                     (_physics, _physics.create_world), (_photos, _photos.pick_image),
                                     (_scene_accel, _scene_accel.mul), (_system, _system.start)):
                assert function.__module__ == module.__name__, function.__module__
            for name in ('_audio', '_metal', '_physics', '_photos', '_scene_accel', '_cocoakit', '_cocoa_support'):
                assert name not in sys.modules
                assert importlib.util.find_spec(name) is None, name
        """)

    def test_system_type_annotations_do_not_require_optional_image_libraries(self):
        self.run_python("""
            import importlib.abc
            import inspect
            import pydoc
            import sys

            class BlockOptional(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.split('.')[0] in {'PIL', 'numpy', 'matplotlib'}:
                        raise AssertionError('Unexpected optional import: ' + fullname)

            sys.meta_path.insert(0, BlockOptional())
            import clipboard, device, location, motion, share
            for module in (clipboard, device, location, motion, share):
                pydoc.render_doc(module)
                for name in module.__all__:
                    value = getattr(module, name)
                    if inspect.isfunction(value) or inspect.isclass(value):
                        inspect.signature(value)
            for factory in (location.watch, location.watch_heading, motion.watch):
                assert all(parameter.kind != inspect.Parameter.VAR_KEYWORD
                           for parameter in inspect.signature(factory).parameters.values())
            assert 'PIL' not in sys.modules and 'numpy' not in sys.modules
        """)
