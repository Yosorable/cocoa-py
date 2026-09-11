#ifndef MetalModule_h
#define MetalModule_h

#ifdef __cplusplus
extern "C" {
#endif

/// Register the _metal built-in Python module.
/// Must be called BEFORE Py_Initialize().
void registerMetalModule(void);

#ifdef __cplusplus
}
#endif

#endif /* MetalModule_h */
