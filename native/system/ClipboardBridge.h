#pragma once

#include "Clipboard.h"

static NSString *CocoaPyClipboardString(PyObject *object, BOOL allowEmpty) {
    if (!PyUnicode_Check(object)) {
        PyErr_SetString(PyExc_TypeError, "Expected a string."); return nil;
    }
    Py_ssize_t length;
    const char *value = PyUnicode_AsUTF8AndSize(object, &length);
    if (!value) return nil;
    if (!allowEmpty && (!length || memchr(value, 0, length))) {
        PyErr_SetString(PyExc_ValueError, "Expected a nonempty string without NUL characters."); return nil;
    }
    return [[NSString alloc] initWithBytes:value length:length encoding:NSUTF8StringEncoding];
}

static NSData *CocoaPyClipboardBuffer(PyObject *object) {
    Py_buffer view;
    if (PyObject_GetBuffer(object, &view, PyBUF_CONTIG_RO) < 0) return nil;
    // Copy while holding the GIL, before dispatching to the main thread. The
    // native block never borrows mutable Python storage or a Python object.
    NSData *data;
    @try { data = [NSData dataWithBytes:view.buf length:view.len]; }
    @finally { PyBuffer_Release(&view); }
    if (!data) PyErr_NoMemory();
    return data;
}

static PyObject *CocoaPyClipboardResult(id value) {
    if (!value || value == NSNull.null) Py_RETURN_NONE;
    if ([value isKindOfClass:NSData.class]) {
        NSData *data = value;
        if (data.length > PY_SSIZE_T_MAX) return PyErr_NoMemory();
        return PyBytes_FromStringAndSize((const char *)data.bytes, (Py_ssize_t)data.length);
    }
    if ([value isKindOfClass:NSString.class]) {
        NSData *data = [value dataUsingEncoding:NSUTF8StringEncoding];
        if (!data) {
            PyErr_SetString(PyExc_UnicodeError, "The clipboard contains invalid Unicode text."); return nullptr;
        }
        return PyUnicode_DecodeUTF8((const char *)data.bytes, data.length, "strict");
    }
    if ([value isKindOfClass:NSNumber.class]) {
        if (CFGetTypeID((__bridge CFTypeRef)value) == CFBooleanGetTypeID())
            return PyBool_FromLong([value boolValue]);
        return PyLong_FromLongLong([value longLongValue]);
    }
    if ([value isKindOfClass:NSArray.class]) {
        PyObject *result = PyList_New([value count]);
        if (!result) return nullptr;
        for (NSUInteger index = 0; index < [value count]; ++index) {
            PyObject *item = CocoaPyClipboardResult(value[index]);
            if (!item) { Py_DECREF(result); return nullptr; }
            PyList_SET_ITEM(result, index, item);
        }
        return result;
    }
    PyErr_SetString(PyExc_RuntimeError, "Unexpected clipboard result."); return nullptr;
}

// The injected board factory lets native contract tests use an isolated macOS
// pasteboard while exercising the exact production bridge and Python wrapper.
static PyObject *CocoaPyClipboardCall(PyObject *args, PyObject *kwargs,
                                      CocoaPyPasteboard *(*getBoard)()) {
    const char *name;
    PyObject *input = Py_None, *localOnly = Py_False, *expiration = Py_None;
    static const char *keywords[] = {"operation", "payload", "local_only", "expires_in", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "s|O$OO:clipboard",
            const_cast<char **>(keywords), &name, &input, &localOnly, &expiration)) return nullptr;
    @autoreleasepool {
        @try {
            NSString *operation = [NSString stringWithUTF8String:name];
            BOOL writing = [@[@"write_text", @"write_url", @"write_image", @"write_item"] containsObject:operation];
            BOOL typedRead = [operation isEqual:@"read_bytes"];
            if (!writing && !typedRead && ![@[@"read_text", @"read_url", @"read_image", @"types",
                    @"has_text", @"has_image", @"has_urls", @"change_count", @"clear"] containsObject:operation]) {
                PyErr_SetString(PyExc_ValueError, "Unknown clipboard operation."); return nullptr;
            }
            if (!writing && ((!typedRead && input != Py_None) || localOnly != Py_False || expiration != Py_None)) {
                PyErr_SetString(PyExc_TypeError, "This clipboard operation does not accept write options or data.");
                return nullptr;
            }
            if (!PyBool_Check(localOnly)) {
                PyErr_SetString(PyExc_TypeError, "local_only must be bool."); return nullptr;
            }
            double seconds = 0;
            if (expiration != Py_None) {
                if (PyBool_Check(expiration) || (!PyFloat_Check(expiration) && !PyLong_Check(expiration))) {
                    PyErr_SetString(PyExc_TypeError, "expires_in must be a number or None."); return nullptr;
                }
                seconds = PyFloat_AsDouble(expiration);
                if (PyErr_Occurred()) return nullptr;
                if (!std::isfinite(seconds) || seconds < 0.001 || seconds > 31536000) {
                    PyErr_SetString(PyExc_ValueError, "expires_in must be between 0.001 and 31536000 seconds."); return nullptr;
                }
#if !COCOA_PY_UIKIT
                PyErr_SetString(PyExc_NotImplementedError, "Clipboard expiration requires iOS."); return nullptr;
#endif
            }
            id payload = nil;
            if ([operation isEqual:@"write_text"] || [operation isEqual:@"write_url"] || typedRead) {
                payload = CocoaPyClipboardString(input, [operation isEqual:@"write_text"]);
                if (!payload) return nullptr;
            } else if ([operation isEqual:@"write_image"]) {
                payload = CocoaPyClipboardBuffer(input);
                if (!payload) return nullptr;
            } else if ([operation isEqual:@"write_item"]) {
                if (!PyDict_Check(input)) {
                    PyErr_SetString(PyExc_TypeError, "representations must be a dictionary."); return nullptr;
                }
                if (!PyDict_Size(input)) {
                    PyErr_SetString(PyExc_ValueError, "representations must not be empty; use clear()."); return nullptr;
                }
                NSMutableDictionary *item = [NSMutableDictionary dictionary];
                PyObject *key, *value; Py_ssize_t position = 0;
                while (PyDict_Next(input, &position, &key, &value)) {
                    NSString *type = CocoaPyClipboardString(key, NO);
                    if (!type) return nullptr;
                    NSData *data = CocoaPyClipboardBuffer(value);
                    if (!data) return nullptr;
                    item[type] = data;
                }
                payload = item;
            }
#if !COCOA_PY_UIKIT
            if (!NSThread.isMainThread && !NSApp.isRunning) {
                PyErr_SetString(PyExc_RuntimeError, "Call clipboard APIs on the Python main thread, or run an AppKit event loop.");
                return nullptr;
            }
#endif
            __block CocoaPyRequest *request;
            BOOL onlyHere = localOnly == Py_True;
            Py_BEGIN_ALLOW_THREADS
            CocoaPyRunOnMain(^{
                @try { request = CocoaPyClipboard(getBoard(), operation, payload, onlyHere, seconds); }
                @catch (NSException *exception) { request = CocoaPyFailure(@"runtime", exception.reason); }
            });
            Py_END_ALLOW_THREADS
            if (PyErr_CheckSignals() < 0) return nullptr;
            if (request.failure) {
                NSString *kind = request.failure[@"kind"];
                PyObject *error = [kind isEqual:@"value"] ? PyExc_ValueError :
                    [kind isEqual:@"not_implemented"] ? PyExc_NotImplementedError :
                    [kind isEqual:@"os"] ? PyExc_OSError : PyExc_RuntimeError;
                PyErr_SetString(error, [request.failure[@"message"] UTF8String]); return nullptr;
            }
            return CocoaPyClipboardResult(request.result);
        } @catch (NSException *exception) {
            PyErr_SetString(PyExc_RuntimeError, (exception.reason ?: @"Clipboard operation failed.").UTF8String);
            return nullptr;
        }
    }
}
