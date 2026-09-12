#define PY_SSIZE_T_CLEAN
#include <Python.h>

// Keep Objective-C classes distinct from the installed extension and replace
// only presentation. Preparation, the Python bridge, delegates and cleanup are
// the actual production code; no sharing service performs an operation.
#define CocoaPyRequest CocoaPyShareTestBaseRequest
#define CocoaPyShareRequest CocoaPyShareTestRequest
#define CocoaPyLocationRequest CocoaPyShareTestLocationRequest
#define PyInit__system PyInit__share_test_system
#define registerCocoaSystemModule registerShareTestSystemModule
#define CocoaPyShare CocoaPyOriginalShare
#include "../../native/system/Share.h"
#undef CocoaPyShare

static NSUInteger presentations;
static BOOL rejectPresentation;
static BOOL declinePresentation;
static NSURL *rejectedDirectory;
static BOOL rejectCapsule;
static dispatch_block_t pendingDismissal;
static NSUInteger dismissals;
static BOOL testPresent(CocoaPyShareRequest *request) {
    presentations++;
    rejectedDirectory = request.temporaryDirectory;
    if (rejectPresentation) {
        rejectedDirectory = request.temporaryDirectory;
        @throw [NSException exceptionWithName:NSInternalInconsistencyException
                                      reason:@"Test presentation failure" userInfo:nil];
    }
    return !declinePresentation;
}
static CocoaPyRequest *CocoaPyShare(NSString *name, NSDictionary *args) {
    return CocoaPyOriginalShare(name, args, testPresent);
}
static PyObject *testCapsule(void *pointer, const char *name, PyCapsule_Destructor destructor) {
    if (rejectCapsule) return PyErr_NoMemory();
    return PyCapsule_New(pointer, name, destructor);
}
#define PyCapsule_New testCapsule
#include "../../native/system/SystemModule.mm"
#undef PyCapsule_New

static PyObject *test_files(PyObject *, PyObject *capsule) {
    CocoaPyShareRequest *request = (CocoaPyShareRequest *)CocoaPyGetRequest(capsule);
    if (!request) return nullptr;
    @autoreleasepool {
        NSMutableArray *paths = [NSMutableArray array];
        for (id item in request.items)
            if ([item isKindOfClass:NSURL.class] && [item isFileURL]) [paths addObject:[item path]];
        return CocoaPyClipboardResult(paths);
    }
}
static PyObject *test_images(PyObject *, PyObject *capsule) {
    CocoaPyShareRequest *request = (CocoaPyShareRequest *)CocoaPyGetRequest(capsule);
    if (!request) return nullptr;
    @autoreleasepool {
        NSMutableArray *images = [NSMutableArray array];
        for (id item in request.items) {
            if (![item isKindOfClass:NSImage.class]) continue;
            NSData *png = CocoaPyClipboardPNG(item);
            if (!png) { PyErr_SetString(PyExc_AssertionError, "Cannot render the shared image."); return nullptr; }
            [images addObject:png];
        }
        return CocoaPyClipboardResult(images);
    }
}
static PyObject *test_strings(PyObject *, PyObject *capsule) {
    CocoaPyShareRequest *request = (CocoaPyShareRequest *)CocoaPyGetRequest(capsule);
    if (!request) return nullptr;
    @autoreleasepool {
        NSMutableArray *values = [NSMutableArray array];
        for (id item in request.items) {
            if ([item isKindOfClass:NSString.class]) [values addObject:item];
            else if ([item isKindOfClass:NSURL.class] && ![item isFileURL]) [values addObject:[item absoluteString]];
        }
        return CocoaPyClipboardResult(values);
    }
}
static NSSharingService *testService() {
    return [[NSSharingService alloc] initWithTitle:@"Test service" image:[NSImage new] alternateImage:nil handler:^{}];
}
static PyObject *test_select(PyObject *, PyObject *capsule) {
    CocoaPyShareRequest *request = (CocoaPyShareRequest *)CocoaPyGetRequest(capsule);
    if (!request) return nullptr;
    @autoreleasepool { [request sharingServicePicker:nil didChooseSharingService:testService()]; }
    Py_RETURN_NONE;
}
static void testComplete(CocoaPyShareRequest *request, const char *outcome) {
    NSSharingService *service = request.service ?: testService();
    if (!strcmp(outcome, "success")) [request sharingService:service didShareItems:request.items];
    else if (!strcmp(outcome, "cancel_picker")) [request sharingServicePicker:nil didChooseSharingService:nil];
    else {
        NSString *domain = !strcmp(outcome, "cancel") ? NSCocoaErrorDomain : NSPOSIXErrorDomain;
        NSInteger code = !strcmp(outcome, "error") ? EACCES : NSUserCancelledError;
        NSError *error = [NSError errorWithDomain:domain code:code userInfo:@{NSLocalizedDescriptionKey: @"Test service error"}];
        [request sharingService:service didFailToShareItems:request.items error:error];
    }
}
static PyObject *test_finish(PyObject *, PyObject *args) {
    PyObject *capsule; const char *outcome;
    if (!PyArg_ParseTuple(args, "Os", &capsule, &outcome)) return nullptr;
    CocoaPyShareRequest *request = (CocoaPyShareRequest *)CocoaPyGetRequest(capsule);
    if (!request) return nullptr;
    @autoreleasepool { testComplete(request, outcome); }
    Py_RETURN_NONE;
}
static PyObject *test_finish_active(PyObject *, PyObject *) {
    @autoreleasepool {
        dispatch_block_t completion = pendingDismissal;
        pendingDismissal = nil;
        if (completion) completion();
        for (CocoaPyShareRequest *request in CocoaPyActiveShares.allObjects) testComplete(request, "success");
    }
    Py_RETURN_NONE;
}
static PyObject *test_dismiss(PyObject *, PyObject *capsule) {
    CocoaPyShareRequest *request = (CocoaPyShareRequest *)CocoaPyGetRequest(capsule);
    if (!request) return nullptr;
    [request closeWithDismissal:^(dispatch_block_t completion) {
        dismissals++;
        pendingDismissal = completion;
    }];
    return PyLong_FromUnsignedLong(dismissals);
}
static PyObject *test_finish_dismissal(PyObject *, PyObject *) {
    dispatch_block_t completion = pendingDismissal;
    pendingDismissal = nil;
    if (completion) completion();
    Py_RETURN_NONE;
}
static PyObject *test_counts(PyObject *, PyObject *) {
    return Py_BuildValue("nn", (Py_ssize_t)presentations, (Py_ssize_t)CocoaPyActiveShares.count);
}
static PyObject *test_reject(PyObject *, PyObject *value) {
    int enabled = PyObject_IsTrue(value);
    if (enabled < 0) return nullptr;
    rejectPresentation = enabled;
    Py_RETURN_NONE;
}
static PyObject *test_rejected_directory(PyObject *, PyObject *) {
    return CocoaPyClipboardResult(rejectedDirectory.path);
}
static PyObject *test_reject_capsule(PyObject *, PyObject *value) {
    int enabled = PyObject_IsTrue(value);
    if (enabled < 0) return nullptr;
    rejectCapsule = enabled;
    Py_RETURN_NONE;
}
static PyObject *test_decline(PyObject *, PyObject *value) {
    int enabled = PyObject_IsTrue(value);
    if (enabled < 0) return nullptr;
    declinePresentation = enabled;
    Py_RETURN_NONE;
}
static PyMethodDef fixtureMethods[] = {
    {"start", system_start, METH_VARARGS, nullptr},
    {"poll", system_poll, METH_VARARGS, nullptr},
    {"close", system_close, METH_O, nullptr},
    {"files", test_files, METH_O, nullptr},
    {"images", test_images, METH_O, nullptr},
    {"strings", test_strings, METH_O, nullptr},
    {"select", test_select, METH_O, nullptr},
    {"finish", test_finish, METH_VARARGS, nullptr},
    {"finish_active", test_finish_active, METH_NOARGS, nullptr},
    {"dismiss", test_dismiss, METH_O, nullptr},
    {"finish_dismissal", test_finish_dismissal, METH_NOARGS, nullptr},
    {"counts", test_counts, METH_NOARGS, nullptr},
    {"reject_presentation", test_reject, METH_O, nullptr},
    {"decline_presentation", test_decline, METH_O, nullptr},
    {"rejected_directory", test_rejected_directory, METH_NOARGS, nullptr},
    {"reject_capsule", test_reject_capsule, METH_O, nullptr},
    {nullptr, nullptr, 0, nullptr},
};
static PyModuleDef fixtureModule = {PyModuleDef_HEAD_INIT, "_share_fixture", nullptr, -1, fixtureMethods};
PyMODINIT_FUNC PyInit__share_fixture(void) { return PyModule_Create(&fixtureModule); }
