#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include "SystemModule.h"
#include "SystemRequest.h"
#include "Device.h"
#include "ClipboardBridge.h"
#include "Location.h"
#include "Motion.h"
#include "Share.h"
#include "ShareBridge.h"

static CocoaPyRequest *CocoaPyStart(NSString *name, NSDictionary *args) {
    if ([name hasPrefix:@"location."]) return CocoaPyLocation(name, args);
    if ([name hasPrefix:@"motion."]) return CocoaPyMotion(name, args);
    if ([name hasPrefix:@"share."]) return CocoaPyShare(name, args);
    if ([name hasPrefix:@"device."]) return CocoaPyDevice(name, args);
    return CocoaPyFailure(@"value", @"Unknown system operation.");
}

static CocoaPyRequest *CocoaPyGetRequest(PyObject *capsule) {
    return (__bridge CocoaPyRequest *)PyCapsule_GetPointer(capsule, "cocoa-py.request");
}
static void CocoaPyReleaseRequest(PyObject *capsule) {
    void *pointer = PyCapsule_GetPointer(capsule, "cocoa-py.request");
    if (!pointer) { PyErr_Clear(); return; }
    CocoaPyRequest *request = CFBridgingRelease(pointer);
    // Destruction may happen while Python owns the main thread. Never dispatch
    // synchronously from a finalizer while holding the GIL.
    if (NSThread.isMainThread) [request close];
    else dispatch_async(dispatch_get_main_queue(), ^{ [request close]; });
}
static PyObject *system_start(PyObject *, PyObject *args) {
    const char *operation, *json; Py_ssize_t length;
    PyObject *buffers = Py_None;
    if (!PyArg_ParseTuple(args, "ss#|O", &operation, &json, &length, &buffers)) return nullptr;
    @autoreleasepool {
        NSString *name = [NSString stringWithUTF8String:operation];
        NSError *error;
        id payload = [NSJSONSerialization JSONObjectWithData:[NSData dataWithBytes:json length:length]
                                                    options:0 error:&error];
        if (![payload isKindOfClass:NSDictionary.class]) {
            PyErr_SetString(PyExc_ValueError, "The operation payload must be a JSON object."); return nullptr;
        }
        if ([name isEqual:@"share.present"]) {
            payload = [payload mutableCopy];
            if (!CocoaPyShareBuffers(buffers, payload)) return nullptr;
        } else if (buffers != Py_None) {
            PyErr_SetString(PyExc_TypeError, "Only sharing accepts binary buffers."); return nullptr;
        }
#if !COCOA_PY_UIKIT
        if (!NSThread.isMainThread && !NSApp.isRunning) {
            PyErr_SetString(PyExc_RuntimeError, "Call system APIs on the Python main thread, or run an AppKit event loop.");
            return nullptr;
        }
#endif
        __block CocoaPyRequest *request;
        Py_BEGIN_ALLOW_THREADS
        CocoaPyRunOnMain(^{
            @try { request = CocoaPyStart(name, payload); }
            @catch (NSException *exception) {
                request = CocoaPyFailure(@"runtime", exception.reason);
            }
        });
        Py_END_ALLOW_THREADS
        void *pointer = (__bridge_retained void *)request;
        PyObject *capsule = PyCapsule_New(pointer, "cocoa-py.request", CocoaPyReleaseRequest);
        if (!capsule) {
            CFBridgingRelease(pointer);
            // A sharing request may already be retained by its presentation.
            // Reclaim it even when Python cannot allocate the owning handle.
            if (NSThread.isMainThread) [request close];
            else dispatch_async(dispatch_get_main_queue(), ^{ [request close]; });
        }
        return capsule;
    }
}
static PyObject *system_poll(PyObject *, PyObject *args) {
    PyObject *capsule; double seconds; int consume = 1;
    if (!PyArg_ParseTuple(args, "Od|p", &capsule, &seconds, &consume)) return nullptr;
    CocoaPyRequest *request = CocoaPyGetRequest(capsule);
    if (!request) return nullptr;
    if (!std::isfinite(seconds) || seconds < 0 || seconds > 0.25) {
        PyErr_SetString(PyExc_ValueError, "The native polling interval must be between 0 and 0.25 seconds."); return nullptr;
    }
    @autoreleasepool {
        BOOL ready;
        @synchronized(request) {
            ready = request.done || request.closed || (consume && request.samples.count);
        }
        if (!ready && seconds > 0) {
            Py_BEGIN_ALLOW_THREADS
            CocoaPyWaitSemaphore(request.signal, seconds);
            Py_END_ALLOW_THREADS
        }
        if (PyErr_CheckSignals() < 0) return nullptr;
        NSData *json = [NSJSONSerialization dataWithJSONObject:[request snapshot:consume] options:0 error:nil];
        if (!json) { PyErr_SetString(PyExc_RuntimeError, "The system returned non-serializable data."); return nullptr; }
        return PyUnicode_DecodeUTF8((const char *)json.bytes, json.length, "strict");
    }
}
static PyObject *system_close(PyObject *, PyObject *capsule) {
    CocoaPyRequest *request = CocoaPyGetRequest(capsule);
    if (!request) return nullptr;
    Py_BEGIN_ALLOW_THREADS
    CocoaPyRunOnMain(^{ [request close]; });
    Py_END_ALLOW_THREADS
    Py_RETURN_NONE;
}
static CocoaPyPasteboard *CocoaPyGeneralPasteboard() {
#if COCOA_PY_UIKIT
    return UIPasteboard.generalPasteboard;
#else
    return NSPasteboard.generalPasteboard;
#endif
}
static PyObject *system_clipboard(PyObject *, PyObject *args, PyObject *kwargs) {
    return CocoaPyClipboardCall(args, kwargs, CocoaPyGeneralPasteboard);
}
static PyMethodDef methods[] = {
    {"clipboard", (PyCFunction)system_clipboard, METH_VARARGS | METH_KEYWORDS,
     "Access clipboard data directly, preserving binary buffers."},
    {"start", system_start, METH_VARARGS, "Start a native system request."},
    {"poll", system_poll, METH_VARARGS, "Read a bounded request snapshot."},
    {"close", system_close, METH_O, "Cancel and release a request's native resources."},
    {nullptr, nullptr, 0, nullptr}
};
static PyModuleDef module = {PyModuleDef_HEAD_INIT, "_cocoa._system", "Native Apple system services.", -1, methods};
PyMODINIT_FUNC PyInit__system(void) { return PyModule_Create(&module); }
int registerCocoaSystemModule(void) { return PyImport_AppendInittab("_cocoa._system", PyInit__system); }
