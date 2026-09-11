#pragma once

#ifdef __cplusplus
extern "C" {
#endif

// Call once after Py_PreInitialize and before Py_InitializeFromConfig. Returns
// zero on success, or -1 if CPython cannot extend its built-in module table.
int registerCocoaPyModules(void);

// Optional file-access hooks. Hosts may define both functions to retain their
// own security-scoped URL/bookmark tokens through asynchronous native I/O.
// A null return means no token is needed, not an access failure.
void *CocoaPyBeginFileAccess(const char *path);
void CocoaPyEndFileAccess(void *token);

#ifdef __cplusplus
}
#endif
