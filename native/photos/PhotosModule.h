#ifndef PhotosModule_h
#define PhotosModule_h

#ifdef __cplusplus
extern "C" {
#endif

/// Register the _cocoa._photos built-in Python module.
/// Must be called BEFORE Py_Initialize().
void registerPhotosModule(void);

#ifdef __cplusplus
}
#endif

#endif /* PhotosModule_h */
