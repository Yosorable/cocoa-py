"""Build the standalone native extension with Apple's toolchain."""

import os
import re
import sys
import sysconfig

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
            extension.include_dirs.append(numpy.get_include())
        super().build_extensions()


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

setup(
    ext_modules=[Extension(
        "coreml",
        sources=["native/coreml/CoreMLModule.mm"],
        depends=["native/coreml/CoreMLModule.h"],
        language="c++",
        extra_compile_args=["-std=c++17", "-fobjc-arc", "-O2", "-g0", "-mmacosx-version-min=14.0"],
        extra_link_args=[
            "-mmacosx-version-min=14.0",
            "-framework", "CoreML",
            "-framework", "CoreVideo",
            "-framework", "Foundation",
        ],
    )],
    cmdclass={"build_ext": AppleBuildExt},
    options={"bdist_wheel": {"plat_name": f"macosx-14.0-{wheel_arch}"}},
)
