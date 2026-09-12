#define PY_SSIZE_T_CLEAN
#include <Python.h>
#import <Foundation/Foundation.h>
#include <atomic>

static std::atomic<unsigned long> blockingWaits{0};
static intptr_t CountedSemaphoreWait(dispatch_semaphore_t semaphore, dispatch_time_t timeout) {
    if (timeout != DISPATCH_TIME_NOW) blockingWaits.fetch_add(1, std::memory_order_relaxed);
    return dispatch_semaphore_wait(semaphore, timeout);
}

// Exercise the production poll/close entry points alongside the installed
// extension, with separate Objective-C class names and no OS service requests.
#define CocoaPyRequest CocoaPyWaitTestRequest
#define CocoaPyLocationRequest CocoaPyWaitTestLocationRequest
#define CocoaPyShareRequest CocoaPyWaitTestShareRequest
#define PyInit__system PyInit__wait_test_system
#define registerCocoaSystemModule registerWaitTestSystemModule
#define dispatch_semaphore_wait CountedSemaphoreWait
#include "../../native/system/SystemModule.mm"
#undef dispatch_semaphore_wait

static PyObject *test_start(PyObject *, PyObject *args) {
    const char *operation, *options;
    if (!PyArg_ParseTuple(args, "ss", &operation, &options)) return nullptr;
    @autoreleasepool {
        CocoaPyRequest *request = [CocoaPyRequest new];
        void *pointer = (__bridge_retained void *)request;
        PyObject *capsule = PyCapsule_New(pointer, "cocoa-py.request", CocoaPyReleaseRequest);
        if (!capsule) CFBridgingRelease(pointer);
        return capsule;
    }
}

static PyObject *test_push(PyObject *, PyObject *args) {
    PyObject *capsule; long long value;
    if (!PyArg_ParseTuple(args, "OL", &capsule, &value)) return nullptr;
    CocoaPyRequest *request = CocoaPyGetRequest(capsule);
    if (!request) return nullptr;
    @autoreleasepool {
        Py_BEGIN_ALLOW_THREADS
        [request push:@(value)];
        Py_END_ALLOW_THREADS
    }
    Py_RETURN_NONE;
}

static PyObject *test_finish(PyObject *, PyObject *capsule) {
    CocoaPyRequest *request = CocoaPyGetRequest(capsule);
    if (!request) return nullptr;
    @autoreleasepool { [request finish:@42]; }
    Py_RETURN_NONE;
}

static PyObject *test_fail(PyObject *, PyObject *capsule) {
    CocoaPyRequest *request = CocoaPyGetRequest(capsule);
    if (!request) return nullptr;
    @autoreleasepool { [request fail:@"runtime" message:@"Native test failure"]; }
    Py_RETURN_NONE;
}

static PyObject *test_finish_on_main(PyObject *, PyObject *capsule) {
    CocoaPyRequest *request = CocoaPyGetRequest(capsule);
    if (!request) return nullptr;
    dispatch_async(dispatch_get_main_queue(), ^{ [request finish:@42]; });
    Py_RETURN_NONE;
}

static PyObject *test_drain_signals(PyObject *, PyObject *capsule) {
    CocoaPyRequest *request = CocoaPyGetRequest(capsule);
    if (!request) return nullptr;
    unsigned long count = 0;
    while (dispatch_semaphore_wait(request.signal, DISPATCH_TIME_NOW) == 0) ++count;
    return PyLong_FromUnsignedLong(count);
}

static PyObject *test_blocking_waits(PyObject *, PyObject *) {
    return PyLong_FromUnsignedLong(blockingWaits.load(std::memory_order_relaxed));
}

static PyMethodDef fixtureMethods[] = {
    {"start", test_start, METH_VARARGS, nullptr},
    {"poll", system_poll, METH_VARARGS, nullptr},
    {"close", system_close, METH_O, nullptr},
    {"push", test_push, METH_VARARGS, nullptr},
    {"finish", test_finish, METH_O, nullptr},
    {"fail", test_fail, METH_O, nullptr},
    {"finish_on_main", test_finish_on_main, METH_O, nullptr},
    {"drain_signals", test_drain_signals, METH_O, nullptr},
    {"blocking_waits", test_blocking_waits, METH_NOARGS, nullptr},
    {nullptr, nullptr, 0, nullptr},
};
static PyModuleDef fixtureModule = {
    PyModuleDef_HEAD_INIT, "_requests_fixture", nullptr, -1, fixtureMethods,
};
PyMODINIT_FUNC PyInit__requests_fixture(void) { return PyModule_Create(&fixtureModule); }
