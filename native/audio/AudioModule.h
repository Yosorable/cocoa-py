#ifndef AudioModule_h
#define AudioModule_h

#ifdef __cplusplus
extern "C" {
#endif

/// Register the _cocoa._audio built-in Python module.
/// Must be called BEFORE Py_Initialize().
void registerAudioModule(void);

#ifdef __cplusplus
}
#endif

#endif /* AudioModule_h */
