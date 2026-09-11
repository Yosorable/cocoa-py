#ifndef PhysicsModule_h
#define PhysicsModule_h

#ifdef __cplusplus
extern "C" {
#endif

/// Register the _cocoa._physics built-in Python module.
/// Must be called BEFORE Py_Initialize().
void registerPhysicsModule(void);

#ifdef __cplusplus
}
#endif

#endif
