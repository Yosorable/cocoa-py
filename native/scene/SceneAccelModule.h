#ifndef SceneAccelModule_h
#define SceneAccelModule_h

#ifdef __cplusplus
extern "C" {
#endif

/// Register the _scene_accel built-in Python module.
/// Must be called BEFORE Py_Initialize().
void registerSceneAccelModule(void);

#ifdef __cplusplus
}
#endif

#endif
