#ifndef CoreMLModule_h
#define CoreMLModule_h

#ifdef __cplusplus
extern "C" {
#endif

/// Register the coreml built-in Python module.
/// Must be called BEFORE Py_Initialize().
void registerCoreMLModule(void);

#ifdef __cplusplus
}
#endif

#endif /* CoreMLModule_h */
