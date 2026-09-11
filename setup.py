"""Build macOS extensions or an iPhoneOS wheel with Apple's toolchain."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


ios_framework = os.environ.get("COCOA_PY_IOS_FRAMEWORK")
is_ios = bool(ios_framework)
minimum = "17.0" if is_ios else "14.0"
os.environ.setdefault("MACOSX_DEPLOYMENT_TARGET", "14.0")
architectures = set(re.findall(r"-arch\s+(\S+)", os.environ.get("ARCHFLAGS", "")))
if is_ios:
    if architectures and architectures != {"arm64"}:
        raise RuntimeError("iPhoneOS wheels require arm64. Remove incompatible ARCHFLAGS.")
    wheel_platform = "ios_17_0_arm64_iphoneos"
elif architectures == {"arm64", "x86_64"}:
    wheel_platform = "macosx_14_0_universal2"
elif len(architectures) <= 1:
    wheel_arch = next(iter(architectures), sysconfig.get_platform().rsplit("-", 1)[-1])
    wheel_platform = f"macosx_14_0_{wheel_arch}"
else:
    raise RuntimeError("Unsupported macOS wheel architectures.")


class AppleBuildExt(build_ext):
    def get_ext_filename(self, name):
        if is_ios:
            return name.replace(".", os.sep) + ".cpython-314-iphoneos.so"
        return super().get_ext_filename(name)

    def build_extensions(self):
        if sys.platform != "darwin" or sys.version_info[:2] != (3, 14):
            raise RuntimeError("Build with CPython 3.14 on macOS and Apple's SDK.")
        if sysconfig.get_config_var("Py_GIL_DISABLED"):
            raise RuntimeError("A standard CPython build with the GIL is required.")
        import numpy

        if ".mm" not in self.compiler.src_extensions:
            self.compiler.src_extensions.append(".mm")
        self.compiler.language_map[".mm"] = "c++"
        if is_ios:
            framework = Path(ios_framework).resolve()
            headers = framework / "Headers"
            version = (headers / "patchlevel.h").read_text()
            config = (headers / "pyconfig.h").read_text()
            if not (re.search(r"#define\s+PY_MAJOR_VERSION\s+3\b", version)
                    and re.search(r"#define\s+PY_MINOR_VERSION\s+14\b", version)):
                raise RuntimeError("The iOS Python.framework must provide CPython 3.14 headers.")
            if re.search(r"^#define\s+Py_GIL_DISABLED\s+1\b", config, re.MULTILINE):
                raise RuntimeError("Free-threaded iOS Python frameworks are not supported.")
            sdk = subprocess.check_output(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"], text=True).strip()
            target_flags = ["-target", "arm64-apple-ios17.0", "-isysroot", sdk]
            # Replace host Python includes and macOS compiler/linker flags.
            clang = [subprocess.check_output(["xcrun", "--sdk", "iphoneos", "--find", "clang"], text=True).strip()]
            clangxx = [subprocess.check_output(["xcrun", "--sdk", "iphoneos", "--find", "clang++"], text=True).strip()]
            self.compiler.set_executables(
                compiler=clang, compiler_so=clang + ["-fPIC"],
                compiler_cxx=clangxx, compiler_so_cxx=clangxx + ["-fPIC"],
                linker_so=clangxx + ["-dynamiclib"],
                linker_so_cxx=clangxx + ["-dynamiclib"],
            )
            self.compiler.include_dirs = [str(headers)]
            for extension in self.extensions:
                extension.extra_compile_args += target_flags
                extension.extra_link_args += [
                    *target_flags, "-F" + str(framework.parent), "-framework", "Python",
                    "-Wl,-install_name,@rpath/" + extension.name + ".framework/" + extension.name,
                    "-Wl,-rpath,@executable_path/Frameworks",
                ]
        for extension in self.extensions:
            if extension.name == "coreml":
                extension.include_dirs.append(numpy.get_include())

        # Box2D is C17; the surrounding bridges are Objective-C++ with ARC.
        compile_source = self.compiler._compile

        def compile_with_language(obj, src, ext, cc_args, extra_args, pp_opts):
            if ext == ".c":
                extra_args = [arg for arg in extra_args if arg not in ("-std=c++17", "-fobjc-arc")]
                extra_args = [*extra_args, "-std=c17"]
            return compile_source(obj, src, ext, cc_args, extra_args, pp_opts)

        self.compiler._compile = compile_with_language
        try:
            super().build_extensions()
        finally:
            self.compiler._compile = compile_source

        output = Path(self.build_lib)
        if is_ios:
            resources = output / "scene" / "_resources"
            resources.mkdir(parents=True, exist_ok=True)
            intermediate = Path(self.build_temp) / "SceneShaders.air"
            subprocess.run([
                "xcrun", "--sdk", "iphoneos", "metal", "-c", "-target", "air64-apple-ios17.0",
                "python/scene/_resources/SceneShaders.metal", "-o", str(intermediate),
            ], check=True)
            subprocess.run([
                "xcrun", "--sdk", "iphoneos", "metallib", str(intermediate),
                "-o", str(resources / "SceneShaders.metallib"),
            ], check=True)
            # The standard CPython packager moves this SDK-wide manifest into
            # _cocoa._system.framework. It covers the complete distribution.
            shutil.copy2("native/common/CocoaPyPrivacy.bundle/PrivacyInfo.xcprivacy",
                         output / "_cocoa" / "_system.xcprivacy")
        else:
            runner = output / "_cocoa" / "_runner"
            runner.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([
                "xcrun", "clang++", "-std=c++17", "-fobjc-arc", "-O2", "-g0",
                "-mmacosx-version-min=14.0", "-I" + sysconfig.get_paths()["include"],
                *[arg for arch in sorted(architectures) for arg in ("-arch", arch)],
                "-framework", "Foundation", "native/runner/CocoaPyRunner.mm", "-o", str(runner),
            ], check=True)
        source_hash = hashlib.sha256()
        sources = [Path("setup.py"), Path("pyproject.toml")]
        sources += [path for directory in ("native", "python") for path in Path(directory).rglob("*")
                    if path.is_file() and path.suffix in (".py", ".h", ".mm", ".c", ".metal", ".plist", ".xcprivacy")
                    and not any(part.startswith(".") or part.endswith((".egg-info", ".dist-info"))
                                or part == "__pycache__" for part in path.parts)]
        for path in sorted(sources):
            source_hash.update(path.as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
        support = output / "_cocoa"
        support.mkdir(parents=True, exist_ok=True)
        (support / "build.json").write_text(json.dumps({
            "platform": wheel_platform, "python_abi": "cp314", "minimum_os": minimum,
            "source_sha256": source_hash.hexdigest(),
        }, indent=2) + "\n")


def native_extension(name, source, macos=(), ios=(), *, extra_sources=(), include_dirs=()):
    flags = [] if is_ios else ["-mmacosx-version-min=14.0"]
    return Extension(
        name, sources=[source, *extra_sources],
        depends=["setup.py", "pyproject.toml", *[str(path) for path in Path("native").rglob("*.h")]],
        include_dirs=list(include_dirs), language="c++",
        extra_compile_args=["-std=c++17", "-fobjc-arc", "-O2", "-g0", "-DNDEBUG", *flags],
        extra_link_args=[*flags, "-framework", "Foundation", "-framework", "CoreFoundation", "-framework", "CoreGraphics",
                         *[arg for framework in (ios if is_ios else macos) for arg in ("-framework", framework)]],
    )


setup(
    py_modules=["audio", "photos", "location", "motion", "device", "clipboard", "share", "notification"]
               + ([] if is_ios else ["cocoa_run"]),
    entry_points={} if is_ios else {"console_scripts": ["cocoa-py = cocoa_run:main"]},
    ext_modules=[
        native_extension("_cocoa._system", "native/system/SystemModule.mm",
                         ("AppKit", "CoreLocation", "UserNotifications", "IOKit"),
                         ("UIKit", "CoreLocation", "CoreMotion", "UserNotifications")),
        native_extension("coreml", "native/coreml/CoreMLModule.mm",
                         ("CoreML", "CoreVideo"), ("CoreML", "CoreVideo")),
        native_extension("_cocoa._audio", "native/audio/AudioModule.mm",
                         ("AVFoundation", "AudioToolbox", "CoreAudio", "QuartzCore", "AppKit"),
                         ("AVFoundation", "AudioToolbox", "QuartzCore")),
        native_extension("_cocoa._metal", "native/metal/MetalModule.mm",
                         ("Metal", "QuartzCore", "ImageIO", "AppKit"),
                         ("Metal", "QuartzCore", "ImageIO", "UIKit")),
        native_extension("_cocoa._photos", "native/photos/PhotosModule.mm",
                         ("AVFoundation", "CoreMedia", "Photos", "PhotosUI", "UniformTypeIdentifiers", "ImageIO", "AppKit"),
                         ("AVFoundation", "CoreMedia", "Photos", "PhotosUI", "UniformTypeIdentifiers", "ImageIO", "UIKit")),
        native_extension("_cocoa._scene_accel", "native/scene/SceneAccelModule.mm"),
        native_extension("_cocoa._physics", "native/physics/PhysicsModule.mm",
                         extra_sources=sorted(str(path) for path in Path("native/physics/box2d/src").glob("*.c")),
                         include_dirs=("native/physics/box2d/include", "native/physics/box2d/src")),
    ],
    cmdclass={"build_ext": AppleBuildExt},
    options={"bdist_wheel": {"plat_name": wheel_platform},
             "build": {"build_base": "build/iphoneos-arm64" if is_ios else "build/macos"}},
)
