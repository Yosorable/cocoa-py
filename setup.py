"""Build the standalone native extension with Apple's toolchain."""

import os
import re
import sys
import sysconfig
import subprocess
from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


class AppleBuildExt(build_ext):
    def build_extensions(self):
        if sys.platform != "darwin":
            raise RuntimeError("This preview requires macOS 14 or later.")
        if sysconfig.get_config_var("Py_GIL_DISABLED"):
            raise RuntimeError("This preview requires a standard CPython build with the GIL.")
        import numpy

        if ".mm" not in self.compiler.src_extensions:
            self.compiler.src_extensions.append(".mm")
        self.compiler.language_map[".mm"] = "c++"
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
        runner = Path(self.build_lib) / "_cocoa_support" / "_runner"
        runner.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "xcrun", "clang++", "-std=c++17", "-fobjc-arc", "-O2", "-g0",
            "-mmacosx-version-min=14.0", "-I" + sysconfig.get_paths()["include"],
            *[arg for arch in sorted(architectures) for arg in ("-arch", arch)],
            "-framework", "Foundation", "native/runner/CocoaPyRunner.mm", "-o", str(runner),
        ], check=True)


os.environ.setdefault("MACOSX_DEPLOYMENT_TARGET", "14.0")

architectures = set(re.findall(r"-arch\s+(\S+)", os.environ.get("ARCHFLAGS", "")))
if architectures == {"arm64", "x86_64"}:
    wheel_arch = "universal2"
elif len(architectures) == 1:
    wheel_arch = next(iter(architectures))
elif not architectures:
    wheel_arch = sysconfig.get_platform().rsplit("-", 1)[-1]
else:
    raise RuntimeError("Unsupported macOS wheel architectures.")

def native_extension(name, source, frameworks=(), *, extra_sources=(), include_dirs=()):
    return Extension(
        name,
        sources=[source, *extra_sources],
        depends=[str(path) for path in Path("native").rglob("*.h")],
        include_dirs=list(include_dirs),
        language="c++",
        extra_compile_args=["-std=c++17", "-fobjc-arc", "-O2", "-g0", "-mmacosx-version-min=14.0"],
        extra_link_args=[
            "-mmacosx-version-min=14.0",
            "-framework", "Foundation",
            *[arg for framework in frameworks for arg in ("-framework", framework)],
        ],
    )


setup(
    ext_modules=[
        native_extension("_cocoakit", "native/system/SystemModule.mm",
                         ("AppKit", "CoreLocation", "UserNotifications", "IOKit")),
        native_extension("coreml", "native/coreml/CoreMLModule.mm", ("CoreML", "CoreVideo")),
        native_extension("_audio", "native/audio/AudioModule.mm",
                         ("AVFoundation", "AudioToolbox", "CoreAudio", "QuartzCore", "AppKit")),
        native_extension("_metal", "native/metal/MetalModule.mm",
                         ("Metal", "QuartzCore", "ImageIO", "AppKit")),
        native_extension("_photos", "native/photos/PhotosModule.mm",
                         ("AVFoundation", "Photos", "PhotosUI", "UniformTypeIdentifiers", "ImageIO", "AppKit")),
        native_extension("_scene_accel", "native/scene/SceneAccelModule.mm"),
        native_extension("_physics", "native/physics/PhysicsModule.mm",
                         extra_sources=sorted(str(path) for path in Path("native/physics/box2d/src").glob("*.c")),
                         include_dirs=("native/physics/box2d/include", "native/physics/box2d/src")),
    ],
    cmdclass={"build_ext": AppleBuildExt},
    options={"bdist_wheel": {"plat_name": f"macosx-14.0-{wheel_arch}"}},
)
