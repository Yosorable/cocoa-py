#include <Python.h>
#include "CocoaPy.h"

extern "C" {
PyObject *PyInit__audio(void);
PyObject *PyInit_coreml(void);
PyObject *PyInit__metal(void);
PyObject *PyInit__photos(void);
PyObject *PyInit__scene_accel(void);
PyObject *PyInit__physics(void);
PyObject *PyInit__system(void);
}

int registerCocoaPyModules(void) {
    const struct { const char *name; PyObject *(*init)(void); } modules[] = {
        {"_cocoa._audio", PyInit__audio}, {"coreml", PyInit_coreml},
        {"_cocoa._metal", PyInit__metal}, {"_cocoa._photos", PyInit__photos},
        {"_cocoa._scene_accel", PyInit__scene_accel}, {"_cocoa._physics", PyInit__physics},
        {"_cocoa._system", PyInit__system},
    };
    for (const auto &module : modules)
        if (PyImport_AppendInittab(module.name, module.init) < 0) return -1;
    return 0;
}
