#include <Python.h>
#include "CocoaPy.h"

extern "C" {
PyObject *PyInit__audio(void);
PyObject *PyInit_coreml(void);
PyObject *PyInit__metal(void);
PyObject *PyInit__photos(void);
PyObject *PyInit__scene_accel(void);
PyObject *PyInit__physics(void);
PyObject *PyInit__cocoakit(void);
}

int registerCocoaPyModules(void) {
    const struct { const char *name; PyObject *(*init)(void); } modules[] = {
        {"_audio", PyInit__audio}, {"coreml", PyInit_coreml},
        {"_metal", PyInit__metal}, {"_photos", PyInit__photos},
        {"_scene_accel", PyInit__scene_accel}, {"_physics", PyInit__physics},
        {"_cocoakit", PyInit__cocoakit},
    };
    for (const auto &module : modules)
        if (PyImport_AppendInittab(module.name, module.init) < 0) return -1;
    return 0;
}
