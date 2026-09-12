#pragma once

#include "SystemRequest.h"

static NSData *CocoaPyShareBuffer(PyObject *object) {
    Py_buffer view;
    if (PyObject_GetBuffer(object, &view, PyBUF_CONTIG_RO) < 0) return nil;
    NSData *data;
    // Own the bytes before releasing the GIL or starting an asynchronous service.
    @try { data = [NSData dataWithBytes:view.buf length:view.len]; }
    @finally { PyBuffer_Release(&view); }
    if (!data) PyErr_NoMemory();
    return data;
}

static NSString *CocoaPyShareFilename(PyObject *object) {
    if (!PyUnicode_Check(object)) {
        PyErr_SetString(PyExc_TypeError, "Attachment filenames must be strings."); return nil;
    }
    Py_ssize_t length;
    const char *value = PyUnicode_AsUTF8AndSize(object, &length);
    if (!value) return nil;
    if (!length || memchr(value, 0, length) || memchr(value, '/', length) || memchr(value, '\\', length) ||
        (length == 1 && value[0] == '.') || (length == 2 && value[0] == '.' && value[1] == '.')) {
        PyErr_SetString(PyExc_ValueError, "Attachment names must be plain filenames without separators or NUL.");
        return nil;
    }
    return [[NSString alloc] initWithBytes:value length:length encoding:NSUTF8StringEncoding];
}

// Add owned native data to the existing string/path payload. No Python object
// crosses into the main-thread block or remains in the native request.
static bool CocoaPyShareBuffers(PyObject *buffers, NSMutableDictionary *payload) {
    @try {
        NSMutableArray *images = [NSMutableArray array], *attachments = [NSMutableArray array];
        if (buffers != Py_None) {
            if (!PyDict_Check(buffers)) {
                PyErr_SetString(PyExc_TypeError, "Share buffers must be a dictionary."); return false;
            }
            // A sequence subclass can run Python while being copied and remove
            // either input from buffers. Retain each input as soon as it is read.
            std::unique_ptr<PyObject, decltype(&Py_DecRef)> imageInput(
                Py_XNewRef(PyDict_GetItemString(buffers, "images")), Py_DecRef);
            std::unique_ptr<PyObject, decltype(&Py_DecRef)> fileInput(
                Py_XNewRef(PyDict_GetItemString(buffers, "attachments")), Py_DecRef);
            if (!imageInput || !fileInput || PyDict_Size(buffers) != 2 ||
                !(PyList_Check(imageInput.get()) || PyTuple_Check(imageInput.get())) || !PyDict_Check(fileInput.get())) {
                PyErr_SetString(PyExc_TypeError, "Share buffers require an images sequence and an attachments dictionary.");
                return false;
            }
            // Snapshot containers before calling a buffer exporter, which may
            // execute Python and mutate its original container.
            std::unique_ptr<PyObject, decltype(&Py_DecRef)> imageItems(PySequence_Tuple(imageInput.get()), Py_DecRef);
            if (!imageItems) return false;
            std::unique_ptr<PyObject, decltype(&Py_DecRef)> fileItems(PyDict_Items(fileInput.get()), Py_DecRef);
            if (!fileItems) return false;
            for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(imageItems.get()); ++index) {
                NSData *data = CocoaPyShareBuffer(PyTuple_GET_ITEM(imageItems.get(), index));
                if (!data) return false;
                [images addObject:data];
            }
            for (Py_ssize_t index = 0; index < PyList_GET_SIZE(fileItems.get()); ++index) {
                PyObject *pair = PyList_GET_ITEM(fileItems.get(), index);
                NSString *name = CocoaPyShareFilename(PyTuple_GET_ITEM(pair, 0));
                if (!name) return false;
                NSData *data = CocoaPyShareBuffer(PyTuple_GET_ITEM(pair, 1));
                if (!data) return false;
                [attachments addObject:@{@"name": name, @"data": data}];
            }
        }
        payload[@"images"] = images; payload[@"attachments"] = attachments;
        return true;
    } @catch (NSException *exception) {
        PyErr_SetString(PyExc_RuntimeError, (exception.reason ?: @"Cannot prepare share data.").UTF8String);
        return false;
    }
}
