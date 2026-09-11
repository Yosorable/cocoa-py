// A small app executable gives the user's existing CPython environment a
// stable macOS identity and public Info.plist permission declarations.
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#import <Foundation/Foundation.h>
#include <dlfcn.h>
#include <cstdio>

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc < 4) {
            std::fprintf(stderr, "Use the cocoa-py command to launch this application.\n"); return 2;
        }
        void *library = dlopen(argv[1], RTLD_NOW | RTLD_GLOBAL);
        if (!library) { std::fprintf(stderr, "Cannot load CPython: %s\n", dlerror()); return 1; }
#define LOAD_API(name) auto api_##name = reinterpret_cast<decltype(&name)>(dlsym(library, #name)); \
        if (!api_##name) { std::fprintf(stderr, "Missing CPython API: %s\n", #name); return 1; }
        LOAD_API(PyPreConfig_InitPythonConfig)
        LOAD_API(Py_PreInitialize)
        LOAD_API(PyConfig_InitPythonConfig)
        LOAD_API(PyConfig_SetBytesString)
        LOAD_API(PyConfig_SetBytesArgv)
        LOAD_API(Py_InitializeFromConfig)
        LOAD_API(PyConfig_Clear)
        LOAD_API(Py_RunMain)
#undef LOAD_API
        auto failed = [](PyStatus status) {
            if (status._type == PyStatus::_PyStatus_TYPE_OK) return false;
            if (status.err_msg) std::fprintf(stderr, "CPython initialization failed: %s\n", status.err_msg);
            return true;
        };
        PyPreConfig pre;
        api_PyPreConfig_InitPythonConfig(&pre); pre.utf8_mode = 1;
        if (failed(api_Py_PreInitialize(&pre))) return 1;
        PyConfig config; api_PyConfig_InitPythonConfig(&config);
        // argv[2] is the selected virtual environment's Python executable.
        // Explicit executable/program_name preserve CPython's normal venv and
        // site-package discovery even though this process is an app binary.
        PyStatus status = api_PyConfig_SetBytesString(&config, &config.program_name, argv[2]);
        if (!failed(status)) status = api_PyConfig_SetBytesString(&config, &config.executable, argv[2]);
        if (!failed(status)) status = api_PyConfig_SetBytesArgv(&config, argc - 2, argv + 2);
        if (!failed(status)) status = api_Py_InitializeFromConfig(&config);
        api_PyConfig_Clear(&config);
        if (status._type == PyStatus::_PyStatus_TYPE_EXIT) return status.exitcode;
        if (failed(status)) return 1;
        return api_Py_RunMain();
    }
}
