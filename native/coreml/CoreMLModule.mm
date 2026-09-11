// CoreMLModule.mm — Python C extension for CoreML inference on iOS
//
// Built-in module registered via PyImport_AppendInittab before Py_Initialize().
// Provides model compilation, inspection, and single/batch inference.

#include <Python.h>

#define PY_ARRAY_UNIQUE_SYMBOL coreml_ARRAY_API
#define NPY_NO_DEPRECATED_API NPY_2_0_API_VERSION
#include <numpy/arrayobject.h>

#import <CoreML/CoreML.h>
#import <CoreVideo/CoreVideo.h>
#import <Foundation/Foundation.h>

#include "CoreMLModule.h"

// ============================================================================
// MARK: - CoreMLModel Python type
// ============================================================================

typedef struct {
    PyObject_HEAD
    MLModel * __strong model;
} CoreMLModelObject;

static void CoreMLModel_dealloc(PyObject *self) {
    CoreMLModelObject *obj = (CoreMLModelObject *)self;
    obj->model = nil;  // ARC releases
    Py_TYPE(self)->tp_free(self);
}

static PyType_Slot CoreMLModel_slots[] = {
    {Py_tp_dealloc, (void *)CoreMLModel_dealloc},
    {Py_tp_doc,     (void *)"CoreML model handle"},
    {0, NULL}
};

static PyType_Spec CoreMLModel_spec = {
    .name      = "coreml.Model",
    .basicsize = sizeof(CoreMLModelObject),
    .flags     = Py_TPFLAGS_DEFAULT,
    .slots     = CoreMLModel_slots,
};

static PyTypeObject *CoreMLModelType = NULL;


// ============================================================================
// MARK: - Helpers: numpy ↔ MLMultiArray
// ============================================================================

/// Map numpy dtype number to MLMultiArrayDataType. Returns -1 on unsupported type.
static MLMultiArrayDataType numpy_to_ml_dtype(int npy_type) {
    switch (npy_type) {
        case NPY_FLOAT:   return MLMultiArrayDataTypeFloat32;
        case NPY_DOUBLE:  return MLMultiArrayDataTypeDouble;
        case NPY_INT32:   return MLMultiArrayDataTypeInt32;
        case NPY_FLOAT16: return MLMultiArrayDataTypeFloat16;
        default:          return (MLMultiArrayDataType)-1;
    }
}

/// Map MLMultiArrayDataType to numpy dtype number.
static int ml_to_numpy_dtype(MLMultiArrayDataType mlType) {
    switch (mlType) {
        case MLMultiArrayDataTypeFloat32: return NPY_FLOAT;
        case MLMultiArrayDataTypeDouble:  return NPY_DOUBLE;
        case MLMultiArrayDataTypeInt32:   return NPY_INT32;
        case MLMultiArrayDataTypeFloat16: return NPY_FLOAT16;
        default:                          return -1;
    }
}

/// Size in bytes for an MLMultiArrayDataType.
static size_t ml_dtype_size(MLMultiArrayDataType mlType) {
    switch (mlType) {
        case MLMultiArrayDataTypeFloat16: return 2;
        case MLMultiArrayDataTypeFloat32: return 4;
        case MLMultiArrayDataTypeDouble:  return 8;
        case MLMultiArrayDataTypeInt32:   return 4;
        default:                          return 0;
    }
}

/// A borrowed numpy view, valid only during the MLMultiArray buffer handler.
static PyArrayObject *mlarray_view(void *bytes, NSInteger size,
                                  NSArray<NSNumber *> *shape,
                                  NSArray<NSNumber *> *elementStrides,
                                  MLMultiArrayDataType dataType, bool writable) {
    int dtype = ml_to_numpy_dtype(dataType);
    size_t itemSize = ml_dtype_size(dataType);
    if (dtype < 0 || shape.count == 0 || shape.count > NPY_MAXDIMS ||
        elementStrides.count != shape.count || size < 0 || !bytes) {
        PyErr_SetString(PyExc_TypeError, "Unsupported MLMultiArray type or storage layout.");
        return NULL;
    }

    npy_intp dims[NPY_MAXDIMS], strides[NPY_MAXDIMS];
    size_t extent = itemSize;
    for (NSUInteger i = 0; i < shape.count; i++) {
        long long dim = shape[i].longLongValue;
        long long stride = elementStrides[i].longLongValue;
        if (dim <= 0 || dim > NPY_MAX_INTP || stride < 0 ||
            stride > NPY_MAX_INTP / itemSize) {
            PyErr_SetString(PyExc_ValueError, "Invalid MLMultiArray shape or strides.");
            return NULL;
        }
        dims[i] = (npy_intp)dim;
        strides[i] = (npy_intp)stride * (npy_intp)itemSize;
        if (extent > (size_t)size ||
            (strides[i] && (size_t)(dim - 1) > ((size_t)size - extent) / (size_t)strides[i])) {
            PyErr_SetString(PyExc_ValueError, "MLMultiArray strides exceed its backing buffer.");
            return NULL;
        }
        extent += (size_t)(dim - 1) * (size_t)strides[i];
    }

    return (PyArrayObject *)PyArray_New(&PyArray_Type, (int)shape.count, dims,
                                       dtype, strides, bytes, 0,
                                       writable ? NPY_ARRAY_WRITEABLE : 0, NULL);
}

/// Convert a numpy ndarray to an MLMultiArray. Returns nil and sets Python error on failure.
static MLMultiArray *numpy_to_mlarray(PyArrayObject *arr) {
    // Ensure C-contiguous
    PyArrayObject *contig = (PyArrayObject *)PyArray_ContiguousFromAny(
        (PyObject *)arr, PyArray_TYPE(arr), 0, 0);
    if (!contig) return nil;

    int ndim = PyArray_NDIM(contig);
    npy_intp *dims = PyArray_DIMS(contig);
    if (ndim == 0) {
        PyErr_SetString(PyExc_ValueError, "CoreML tensor input must have at least one dimension.");
        Py_DECREF(contig);
        return nil;
    }
    for (int i = 0; i < ndim; i++) {
        if (dims[i] <= 0) {
            PyErr_SetString(PyExc_ValueError, "CoreML tensor dimensions must be positive.");
            Py_DECREF(contig);
            return nil;
        }
    }

    MLMultiArrayDataType mlType = numpy_to_ml_dtype(PyArray_TYPE(contig));
    if ((int)mlType == -1) {
        PyErr_Format(PyExc_TypeError,
                     "Unsupported numpy dtype %d for CoreML. Use float16/32/64 or int32.",
                     PyArray_TYPE(contig));
        Py_DECREF(contig);
        return nil;
    }

    // Build shape array
    NSMutableArray<NSNumber *> *shape = [NSMutableArray arrayWithCapacity:ndim];
    for (int i = 0; i < ndim; i++) {
        [shape addObject:@(dims[i])];
    }

    NSError *error = nil;
    MLMultiArray *mlArray = [[MLMultiArray alloc] initWithShape:shape
                                                      dataType:mlType
                                                         error:&error];
    if (!mlArray) {
        PyErr_Format(PyExc_RuntimeError, "Failed to create MLMultiArray: %s",
                     [[error localizedDescription] UTF8String]);
        Py_DECREF(contig);
        return nil;
    }

    __block int copyStatus = -1;
    [mlArray getMutableBytesWithHandler:^(void *bytes, NSInteger size, NSArray<NSNumber *> *strides) {
        PyArrayObject *view = mlarray_view(bytes, size, shape, strides, mlType, true);
        if (view) {
            copyStatus = PyArray_CopyInto(view, contig);
            Py_DECREF(view);
        }
    }];
    Py_DECREF(contig);
    if (copyStatus < 0) return nil;
    return mlArray;
}

/// Python images use RGB/RGBA uint8, or 2D uint8/float16 grayscale.
/// Returns nil and sets Python error on failure.
static CVPixelBufferRef numpy_to_pixelbuffer(PyArrayObject *arr, MLImageConstraint *constraint) {
    OSType pixelFormat = constraint.pixelFormatType;
    bool color = pixelFormat == kCVPixelFormatType_32BGRA;
    int dtype;
    size_t bytesPerPixel;
    switch (pixelFormat) {
        case kCVPixelFormatType_32BGRA: dtype = NPY_UINT8; bytesPerPixel = 4; break;
        case kCVPixelFormatType_OneComponent8: dtype = NPY_UINT8; bytesPerPixel = 1; break;
        case kCVPixelFormatType_OneComponent16Half: dtype = NPY_FLOAT16; bytesPerPixel = 2; break;
        default:
            PyErr_Format(PyExc_TypeError, "Unsupported CoreML image pixel format: %u.",
                         (unsigned int)pixelFormat);
            return NULL;
    }
    if (PyArray_TYPE(arr) != dtype) {
        PyErr_Format(PyExc_TypeError, "Image input requires dtype %s.",
                     dtype == NPY_FLOAT16 ? "float16" : "uint8");
        return NULL;
    }
    int ndim = PyArray_NDIM(arr);
    npy_intp *dims = PyArray_DIMS(arr);
    if ((color && (ndim != 3 || (dims[2] != 3 && dims[2] != 4))) ||
        (!color && ndim != 2 && !(ndim == 3 && dims[2] == 1))) {
        PyErr_SetString(PyExc_ValueError, color
                        ? "Color image input must have shape (H, W, 3) RGB or (H, W, 4) RGBA."
                        : "Grayscale image input must have shape (H, W) or (H, W, 1).");
        return NULL;
    }
    npy_intp height = dims[0], width = dims[1];
    if (height <= 0 || width <= 0 || width > NPY_MAX_INTP / bytesPerPixel) {
        PyErr_SetString(PyExc_ValueError, "Image dimensions must be positive and fit in memory.");
        return NULL;
    }
    PyArrayObject *contig = (PyArrayObject *)PyArray_FROM_OTF(
        (PyObject *)arr, dtype, NPY_ARRAY_CARRAY_RO);
    if (!contig) return NULL;

    CVPixelBufferRef pixelBuffer = NULL;
    CVReturn status = CVPixelBufferCreate(kCFAllocatorDefault,
                                          (size_t)width, (size_t)height,
                                          pixelFormat, NULL, &pixelBuffer);
    if (status != kCVReturnSuccess || !pixelBuffer) {
        PyErr_SetString(PyExc_RuntimeError, "Failed to create CVPixelBuffer.");
        Py_DECREF(contig);
        return nil;
    }

    status = CVPixelBufferLockBaseAddress(pixelBuffer, 0);
    if (status != kCVReturnSuccess) {
        PyErr_Format(PyExc_RuntimeError, "Failed to lock image buffer: %d.", (int)status);
        CVPixelBufferRelease(pixelBuffer);
        Py_DECREF(contig);
        return NULL;
    }
    uint8_t *dst = (uint8_t *)CVPixelBufferGetBaseAddress(pixelBuffer);
    size_t bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer);
    uint8_t *src = (uint8_t *)PyArray_DATA(contig);
    if (!dst || bytesPerRow < (size_t)width * bytesPerPixel) {
        PyErr_SetString(PyExc_RuntimeError, "Invalid image buffer storage.");
        CVPixelBufferUnlockBaseAddress(pixelBuffer, 0);
        CVPixelBufferRelease(pixelBuffer);
        Py_DECREF(contig);
        return NULL;
    }
    if (!color) {
        for (npy_intp row = 0; row < height; row++) {
            memcpy(dst + row * bytesPerRow, src + row * width * bytesPerPixel,
                   width * bytesPerPixel);
        }
    } else {
        npy_intp channels = dims[2];
        for (npy_intp row = 0; row < height; row++) {
            uint8_t *dstRow = dst + row * bytesPerRow;
            uint8_t *srcRow = src + row * width * channels;
            for (npy_intp col = 0; col < width; col++) {
                dstRow[col * 4 + 0] = srcRow[col * channels + 2];
                dstRow[col * 4 + 1] = srcRow[col * channels + 1];
                dstRow[col * 4 + 2] = srcRow[col * channels + 0];
                dstRow[col * 4 + 3] = channels == 4 ? srcRow[col * channels + 3] : 255;
            }
        }
    }

    CVPixelBufferUnlockBaseAddress(pixelBuffer, 0);
    Py_DECREF(contig);
    return pixelBuffer;
}

/// Convert an MLMultiArray to a numpy ndarray. Returns NULL and sets Python error on failure.
static PyObject *mlarray_to_numpy(MLMultiArray *mlArray) {
    // Dynamic outputs (for example NMS with no detections) can have zero elements.
    // Preserve their shape and dtype without accessing an empty backing buffer.
    if (mlArray.count == 0) {
        NSArray<NSNumber *> *shape = mlArray.shape;
        int dtype = ml_to_numpy_dtype(mlArray.dataType);
        if (dtype < 0 || shape.count == 0 || shape.count > NPY_MAXDIMS) {
            PyErr_SetString(PyExc_TypeError, "Unsupported MLMultiArray type or rank.");
            return NULL;
        }
        npy_intp dims[NPY_MAXDIMS];
        for (NSUInteger i = 0; i < shape.count; i++) {
            long long dim = shape[i].longLongValue;
            if (dim < 0 || dim > NPY_MAX_INTP) {
                PyErr_SetString(PyExc_ValueError, "Invalid MLMultiArray shape.");
                return NULL;
            }
            dims[i] = (npy_intp)dim;
        }
        return PyArray_SimpleNew((int)shape.count, dims, dtype);
    }

    __block PyObject *result = NULL;
    [mlArray getBytesWithHandler:^(const void *bytes, NSInteger size) {
        PyArrayObject *view = mlarray_view((void *)bytes, size, mlArray.shape,
                                         mlArray.strides, mlArray.dataType, false);
        if (view) {
            result = (PyObject *)PyArray_NewCopy(view, NPY_CORDER);
            Py_DECREF(view);
        }
    }];
    return result;
}

static PyObject *pixelbuffer_to_numpy(CVPixelBufferRef buffer) {
    if (!buffer) {
        PyErr_SetString(PyExc_RuntimeError, "Image output has no pixel buffer.");
        return NULL;
    }
    OSType format = CVPixelBufferGetPixelFormatType(buffer);
    int dtype, channels;
    size_t bytesPerPixel;
    switch (format) {
        case kCVPixelFormatType_32BGRA:
            dtype = NPY_UINT8; channels = 4; bytesPerPixel = 4; break;
        case kCVPixelFormatType_OneComponent8:
            dtype = NPY_UINT8; channels = 1; bytesPerPixel = 1; break;
        case kCVPixelFormatType_OneComponent16Half:
            dtype = NPY_FLOAT16; channels = 1; bytesPerPixel = 2; break;
        default:
            PyErr_Format(PyExc_TypeError, "Unsupported CoreML image output pixel format: %u.",
                         (unsigned int)format);
            return NULL;
    }
    size_t width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer);
    if (CVPixelBufferIsPlanar(buffer) || !width || !height ||
        width > NPY_MAX_INTP / bytesPerPixel || height > NPY_MAX_INTP) {
        PyErr_SetString(PyExc_ValueError, "Invalid CoreML image output dimensions or layout.");
        return NULL;
    }
    CVReturn status = CVPixelBufferLockBaseAddress(buffer, kCVPixelBufferLock_ReadOnly);
    if (status != kCVReturnSuccess) {
        PyErr_Format(PyExc_RuntimeError, "Failed to lock image output: %d.", (int)status);
        return NULL;
    }
    size_t bytesPerRow = CVPixelBufferGetBytesPerRow(buffer);
    const uint8_t *src = (const uint8_t *)CVPixelBufferGetBaseAddress(buffer);
    if (!src || bytesPerRow < width * bytesPerPixel) {
        PyErr_SetString(PyExc_RuntimeError, "Invalid CoreML image output storage.");
        CVPixelBufferUnlockBaseAddress(buffer, kCVPixelBufferLock_ReadOnly);
        return NULL;
    }
    npy_intp dims[] = {(npy_intp)height, (npy_intp)width, channels};
    PyObject *result = PyArray_SimpleNew(channels == 1 ? 2 : 3, dims, dtype);
    if (result) {
        uint8_t *dst = (uint8_t *)PyArray_DATA((PyArrayObject *)result);
        for (size_t row = 0; row < height; row++) {
            const uint8_t *srcRow = src + row * bytesPerRow;
            uint8_t *dstRow = dst + row * width * bytesPerPixel;
            if (channels == 4) {
                for (size_t col = 0; col < width; col++) {
                    dstRow[col * 4 + 0] = srcRow[col * 4 + 2];
                    dstRow[col * 4 + 1] = srcRow[col * 4 + 1];
                    dstRow[col * 4 + 2] = srcRow[col * 4 + 0];
                    dstRow[col * 4 + 3] = srcRow[col * 4 + 3];
                }
            } else {
                memcpy(dstRow, srcRow, width * bytesPerPixel);
            }
        }
    }
    CVPixelBufferUnlockBaseAddress(buffer, kCVPixelBufferLock_ReadOnly);
    return result;
}

// ============================================================================
// MARK: - Convert MLFeatureValue to Python object
// ============================================================================

static PyObject *feature_value_to_python(MLFeatureValue *value) {
    switch (value.type) {
        case MLFeatureTypeMultiArray:
            return mlarray_to_numpy(value.multiArrayValue);

        case MLFeatureTypeString:
            return PyUnicode_FromString([value.stringValue UTF8String]);

        case MLFeatureTypeDouble:
            return PyFloat_FromDouble(value.doubleValue);

        case MLFeatureTypeInt64:
            return PyLong_FromLongLong(value.int64Value);

        case MLFeatureTypeDictionary: {
            NSDictionary *dict = value.dictionaryValue;
            PyObject *pyDict = PyDict_New();
            for (id key in dict) {
                PyObject *pyKey = nil;
                if ([key isKindOfClass:[NSString class]]) {
                    pyKey = PyUnicode_FromString([key UTF8String]);
                } else if ([key isKindOfClass:[NSNumber class]]) {
                    pyKey = PyLong_FromLongLong([key longLongValue]);
                } else {
                    pyKey = PyUnicode_FromString([[key description] UTF8String]);
                }
                NSNumber *val = dict[key];
                PyObject *pyVal = PyFloat_FromDouble([val doubleValue]);
                PyDict_SetItem(pyDict, pyKey, pyVal);
                Py_DECREF(pyKey);
                Py_DECREF(pyVal);
            }
            return pyDict;
        }

        case MLFeatureTypeImage:
            return pixelbuffer_to_numpy(value.imageBufferValue);

        default:
            PyErr_Format(PyExc_TypeError, "Unsupported CoreML output feature type: %d.", (int)value.type);
            return NULL;
    }
}

// ============================================================================
// MARK: - Module functions
// ============================================================================

static NSString *python_path(PyObject *value) {
    PyObject *bytes = NULL;
    if (!PyUnicode_FSConverter(value, &bytes)) return nil;
    NSString *path = [[NSFileManager defaultManager]
        stringWithFileSystemRepresentation:PyBytes_AS_STRING(bytes)
        length:(NSUInteger)PyBytes_GET_SIZE(bytes)];
    Py_DECREF(bytes);
    if (!path) PyErr_SetString(PyExc_ValueError, "Invalid filesystem path.");
    return path;
}

static PyObject *coreml_load(PyObject *self, PyObject *args, PyObject *kwargs) {
    PyObject *path;
    const char *compute_units = "all";
    static const char *keywords[] = {"path", "compute_units", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O|s:load", keywords, &path, &compute_units))
        return NULL;

    NSString *nsPath = python_path(path);
    if (!nsPath) return NULL;
    NSURL *url = [NSURL fileURLWithPath:nsPath];

    // Check path exists
    if (![[NSFileManager defaultManager] fileExistsAtPath:nsPath]) {
        PyErr_Format(PyExc_FileNotFoundError, "Model not found: %s", nsPath.UTF8String);
        return NULL;
    }

    // Configure compute units
    MLModelConfiguration *config = [[MLModelConfiguration alloc] init];
    if (strcmp(compute_units, "all") == 0)
        config.computeUnits = MLComputeUnitsAll;
    else if (strcmp(compute_units, "cpuAndNeuralEngine") == 0)
        config.computeUnits = MLComputeUnitsCPUAndNeuralEngine;
    else if (strcmp(compute_units, "cpuAndGPU") == 0)
        config.computeUnits = MLComputeUnitsCPUAndGPU;
    else if (strcmp(compute_units, "cpuOnly") == 0)
        config.computeUnits = MLComputeUnitsCPUOnly;
    else {
        PyErr_Format(PyExc_ValueError,
                     "Invalid compute_units: '%s'. Use 'all', 'cpuOnly', 'cpuAndGPU', or 'cpuAndNeuralEngine'.",
                     compute_units);
        return NULL;
    }

    // Load model (release GIL — can be slow)
    __block MLModel *model = nil;
    __block NSError *error = nil;

    Py_BEGIN_ALLOW_THREADS
    model = [MLModel modelWithContentsOfURL:url configuration:config error:&error];
    Py_END_ALLOW_THREADS

    if (!model) {
        PyErr_Format(PyExc_RuntimeError, "Failed to load model: %s",
                     [[error localizedDescription] UTF8String]);
        return NULL;
    }

    // Create Python wrapper
    // tp_alloc uses calloc (zero-init), safe for ARC __strong fields
    CoreMLModelObject *obj = (CoreMLModelObject *)CoreMLModelType->tp_alloc(CoreMLModelType, 0);
    if (!obj) return NULL;
    obj->model = model;  // ARC retains (tp_alloc zero-inited, so old value is nil)
    return (PyObject *)obj;
}

static bool set_description_item(PyObject *info, const char *key, PyObject *value) {
    if (!value) return false;
    int status = PyDict_SetItemString(info, key, value);
    Py_DECREF(value);
    return status == 0;
}

static PyObject *feature_description(MLFeatureDescription *feature) {
    const char *typeName = "unknown";
    switch (feature.type) {
        case MLFeatureTypeMultiArray: typeName = "multiArray"; break;
        case MLFeatureTypeImage: typeName = "image"; break;
        case MLFeatureTypeString: typeName = "string"; break;
        case MLFeatureTypeDouble: typeName = "double"; break;
        case MLFeatureTypeInt64: typeName = "int64"; break;
        case MLFeatureTypeDictionary: typeName = "dictionary"; break;
        case MLFeatureTypeSequence: typeName = "sequence"; break;
        default: break;
    }
    PyObject *info = Py_BuildValue("{s:s,s:s,s:O}",
                                 "name", feature.name.UTF8String,
                                 "type", typeName,
                                 "is_optional", feature.isOptional ? Py_True : Py_False);
    if (!info) return NULL;

    if (feature.type == MLFeatureTypeMultiArray && feature.multiArrayConstraint) {
        MLMultiArrayConstraint *constraint = feature.multiArrayConstraint;
        PyObject *shape = PyList_New(constraint.shape.count);
        if (!shape) { Py_DECREF(info); return NULL; }
        for (NSUInteger i = 0; i < constraint.shape.count; i++) {
            PyObject *dim = PyLong_FromLongLong(constraint.shape[i].longLongValue);
            if (!dim) { Py_DECREF(shape); Py_DECREF(info); return NULL; }
            PyList_SET_ITEM(shape, i, dim);
        }
        if (!set_description_item(info, "shape", shape)) { Py_DECREF(info); return NULL; }
        const char *dtype = "unsupported";
        switch (constraint.dataType) {
            case MLMultiArrayDataTypeFloat16: dtype = "float16"; break;
            case MLMultiArrayDataTypeFloat32: dtype = "float32"; break;
            case MLMultiArrayDataTypeDouble: dtype = "float64"; break;
            case MLMultiArrayDataTypeInt32: dtype = "int32"; break;
            default: break;
        }
        if (!set_description_item(info, "dtype", PyUnicode_FromString(dtype))) {
            Py_DECREF(info); return NULL;
        }
    }
    if (feature.type == MLFeatureTypeImage && feature.imageConstraint) {
        MLImageConstraint *constraint = feature.imageConstraint;
        bool color = constraint.pixelFormatType == kCVPixelFormatType_32BGRA;
        bool gray8 = constraint.pixelFormatType == kCVPixelFormatType_OneComponent8;
        bool gray16 = constraint.pixelFormatType == kCVPixelFormatType_OneComponent16Half;
        PyObject *shape = color
            ? Py_BuildValue("[lli]", (long)constraint.pixelsHigh, (long)constraint.pixelsWide, 4)
            : Py_BuildValue("[ll]", (long)constraint.pixelsHigh, (long)constraint.pixelsWide);
        const char *dtype = gray16 ? "float16" : (color || gray8 ? "uint8" : "unsupported");
        const char *layout = color ? "RGBA" : (gray8 || gray16 ? "GRAYSCALE" : "unsupported");
        if (!set_description_item(info, "shape", shape) ||
            !set_description_item(info, "dtype", PyUnicode_FromString(dtype)) ||
            !set_description_item(info, "color_layout", PyUnicode_FromString(layout))) {
            Py_DECREF(info); return NULL;
        }
    }
    return info;
}

static PyObject *describe_features(NSDictionary<NSString *, MLFeatureDescription *> *features) {
    PyObject *result = PyList_New(0);
    if (!result) return NULL;
    for (NSString *name in [features.allKeys sortedArrayUsingSelector:@selector(compare:)]) {
        PyObject *info = feature_description(features[name]);
        if (!info) { Py_DECREF(result); return NULL; }
        int status = PyList_Append(result, info);
        Py_DECREF(info);
        if (status < 0) { Py_DECREF(result); return NULL; }
    }
    return result;
}

static PyObject *coreml_describe(PyObject *self, PyObject *args) {
    CoreMLModelObject *modelObj;
    if (!PyArg_ParseTuple(args, "O!", CoreMLModelType, &modelObj)) return NULL;
    MLModelDescription *desc = modelObj->model.modelDescription;
    PyObject *inputs = describe_features(desc.inputDescriptionsByName);
    if (!inputs) return NULL;
    PyObject *outputs = describe_features(desc.outputDescriptionsByName);
    if (!outputs) { Py_DECREF(inputs); return NULL; }
    PyObject *result = Py_BuildValue("{s:O,s:O}", "inputs", inputs, "outputs", outputs);
    Py_DECREF(inputs);
    Py_DECREF(outputs);
    return result;
}

// Forward declarations for helpers defined after coreml_compile
static MLDictionaryFeatureProvider *dict_to_provider(CoreMLModelObject *modelObj, PyObject *inputDict);
static PyObject *output_to_dict(id<MLFeatureProvider> output);

static PyObject *coreml_predict(PyObject *self, PyObject *args) {
    CoreMLModelObject *modelObj;
    PyObject *inputDict;
    if (!PyArg_ParseTuple(args, "O!O!", CoreMLModelType, &modelObj, &PyDict_Type, &inputDict))
        return NULL;

    @autoreleasepool {
        MLDictionaryFeatureProvider *provider = dict_to_provider(modelObj, inputDict);
        if (!provider) return NULL;

        __block id<MLFeatureProvider> output = nil;
        __block NSError *predError = nil;

        Py_BEGIN_ALLOW_THREADS
        output = [modelObj->model predictionFromFeatures:provider error:&predError];
        Py_END_ALLOW_THREADS

        if (!output) {
            PyErr_Format(PyExc_RuntimeError, "Prediction failed: %s",
                         [[predError localizedDescription] UTF8String]);
            return NULL;
        }

        return output_to_dict(output);
    }
}

static PyObject *coreml_compile(PyObject *self, PyObject *args, PyObject *kwargs) {
    PyObject *srcPath;
    PyObject *dstDir = Py_None;
    static const char *keywords[] = {"model_path", "output_dir", NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "O|O:compile", keywords, &srcPath, &dstDir))
        return NULL;

    NSString *nsSrcPath = python_path(srcPath);
    if (!nsSrcPath) return NULL;
    NSString *nsDstDir = dstDir == Py_None ? nil : python_path(dstDir);
    if (dstDir != Py_None && !nsDstDir) return NULL;
    if (![[NSFileManager defaultManager] fileExistsAtPath:nsSrcPath]) {
        PyErr_Format(PyExc_FileNotFoundError, "Model not found: %s", nsSrcPath.UTF8String);
        return NULL;
    }

    NSURL *srcURL = [NSURL fileURLWithPath:nsSrcPath];

    // Compile (release GIL — can be slow)
    __block NSURL *compiledURL = nil;
    __block NSError *error = nil;

    Py_BEGIN_ALLOW_THREADS
    compiledURL = [MLModel compileModelAtURL:srcURL error:&error];
    Py_END_ALLOW_THREADS

    if (!compiledURL) {
        PyErr_Format(PyExc_RuntimeError, "Failed to compile model: %s",
                     [[error localizedDescription] UTF8String]);
        return NULL;
    }

    // Determine destination
    NSString *dstPath;
    if (nsDstDir) {
        NSString *filename = [[nsSrcPath lastPathComponent]
                              stringByDeletingPathExtension];
        filename = [filename stringByAppendingString:@".mlmodelc"];
        dstPath = [nsDstDir stringByAppendingPathComponent:filename];
    } else {
        // Same directory as source
        NSString *filename = [[nsSrcPath lastPathComponent]
                              stringByDeletingPathExtension];
        filename = [filename stringByAppendingString:@".mlmodelc"];
        dstPath = [[nsSrcPath stringByDeletingLastPathComponent]
                   stringByAppendingPathComponent:filename];
    }

    // Remove existing and copy
    NSFileManager *fm = [NSFileManager defaultManager];
    [fm removeItemAtPath:dstPath error:nil];
    NSError *copyError = nil;
    BOOL copied = [fm copyItemAtURL:compiledURL toURL:[NSURL fileURLWithPath:dstPath] error:&copyError];
    [fm removeItemAtURL:compiledURL error:nil];
    if (!copied) {
        PyErr_Format(PyExc_RuntimeError, "Failed to copy compiled model: %s",
                     [[copyError localizedDescription] UTF8String]);
        return NULL;
    }

    return PyUnicode_FromString([dstPath UTF8String]);
}

/// Helper: convert a single input dict to MLDictionaryFeatureProvider.
static MLDictionaryFeatureProvider *dict_to_provider(
    CoreMLModelObject *modelObj, PyObject *inputDict)
{
    NSDictionary<NSString *, MLFeatureDescription *> *inputDescs =
        modelObj->model.modelDescription.inputDescriptionsByName;
    NSMutableDictionary<NSString *, MLFeatureValue *> *features = [NSMutableDictionary new];

    PyObject *key, *value;
    Py_ssize_t pos = 0;
    while (PyDict_Next(inputDict, &pos, &key, &value)) {
        if (!PyUnicode_Check(key)) {
            PyErr_SetString(PyExc_TypeError, "CoreML input names must be strings.");
            return nil;
        }
        const char *keyStr = PyUnicode_AsUTF8(key);
        if (!keyStr) return nil;

        MLFeatureValue *fv = nil;
        MLFeatureDescription *desc = inputDescs[@(keyStr)];
        if (!desc) {
            PyErr_Format(PyExc_KeyError, "Unknown CoreML input: '%s'.", keyStr);
            return nil;
        }
        if (value == Py_None && desc.isOptional) continue;
        bool expectsImage = desc.type == MLFeatureTypeImage;

        if (PyArray_Check(value) && expectsImage) {
            CVPixelBufferRef pb = numpy_to_pixelbuffer((PyArrayObject *)value, desc.imageConstraint);
            if (!pb) return nil;
            fv = [MLFeatureValue featureValueWithPixelBuffer:pb];
            CVPixelBufferRelease(pb);
        } else if (PyArray_Check(value)) {
            MLMultiArray *mlArr = numpy_to_mlarray((PyArrayObject *)value);
            if (!mlArr) return nil;
            fv = [MLFeatureValue featureValueWithMultiArray:mlArr];
        } else if (PyFloat_Check(value)) {
            fv = [MLFeatureValue featureValueWithDouble:PyFloat_AsDouble(value)];
        } else if (PyLong_Check(value)) {
            long long integer = PyLong_AsLongLong(value);
            if (PyErr_Occurred()) return nil;
            fv = [MLFeatureValue featureValueWithInt64:integer];
        } else if (PyUnicode_Check(value)) {
            const char *s = PyUnicode_AsUTF8(value);
            if (!s) return nil;
            fv = [MLFeatureValue featureValueWithString:@(s)];
        } else {
            PyErr_Format(PyExc_TypeError,
                         "Unsupported input type for key '%s'.", keyStr);
            return nil;
        }
        if (![desc isAllowedValue:fv]) {
            PyErr_Format(PyExc_ValueError,
                         "Input '%s' does not match its CoreML type, dtype, or shape. See coreml.describe(model).",
                         keyStr);
            return nil;
        }
        features[@(keyStr)] = fv;
    }

    for (NSString *name in inputDescs) {
        if (!inputDescs[name].isOptional && !features[name]) {
            PyErr_Format(PyExc_KeyError, "Missing required CoreML input: '%s'.", name.UTF8String);
            return nil;
        }
    }

    NSError *error = nil;
    MLDictionaryFeatureProvider *provider =
        [[MLDictionaryFeatureProvider alloc] initWithDictionary:features error:&error];
    if (!provider) {
        PyErr_Format(PyExc_RuntimeError, "Failed to create feature provider: %s",
                     [[error localizedDescription] UTF8String]);
    }
    return provider;
}

/// Helper: convert MLFeatureProvider output to Python dict.
static PyObject *output_to_dict(id<MLFeatureProvider> output) {
    PyObject *result = PyDict_New();
    for (NSString *name in output.featureNames) {
        MLFeatureValue *fv = [output featureValueForName:name];
        PyObject *pyVal = feature_value_to_python(fv);
        if (!pyVal) {
            Py_DECREF(result);
            return NULL;
        }
        PyDict_SetItemString(result, [name UTF8String], pyVal);
        Py_DECREF(pyVal);
    }
    return result;
}

static PyObject *coreml_batch_predict(PyObject *self, PyObject *args) {
    CoreMLModelObject *modelObj;
    PyObject *inputList;
    if (!PyArg_ParseTuple(args, "O!O!", CoreMLModelType, &modelObj, &PyList_Type, &inputList))
        return NULL;

    Py_ssize_t count = PyList_Size(inputList);
    if (count == 0) {
        return PyList_New(0);
    }

    @autoreleasepool {
        NSMutableArray<id<MLFeatureProvider>> *providers =
            [NSMutableArray arrayWithCapacity:(NSUInteger)count];

        for (Py_ssize_t i = 0; i < count; i++) {
            PyObject *inputDict = PyList_GetItem(inputList, i);
            if (!PyDict_Check(inputDict)) {
                PyErr_Format(PyExc_TypeError,
                             "batch_predict expects a list of dicts, item %zd is not a dict.", i);
                return NULL;
            }
            MLDictionaryFeatureProvider *provider = dict_to_provider(modelObj, inputDict);
            if (!provider) return NULL;
            [providers addObject:provider];
        }

        MLArrayBatchProvider *batch =
            [[MLArrayBatchProvider alloc] initWithFeatureProviderArray:providers];

        __block id<MLBatchProvider> batchOutput = nil;
        __block NSError *error = nil;

        Py_BEGIN_ALLOW_THREADS
        batchOutput = [modelObj->model predictionsFromBatch:batch error:&error];
        Py_END_ALLOW_THREADS

        if (!batchOutput) {
            PyErr_Format(PyExc_RuntimeError, "Batch prediction failed: %s",
                         [[error localizedDescription] UTF8String]);
            return NULL;
        }

        PyObject *resultList = PyList_New(batchOutput.count);
        for (NSInteger i = 0; i < batchOutput.count; i++) {
            id<MLFeatureProvider> output = [batchOutput featuresAtIndex:i];
            PyObject *resultDict = output_to_dict(output);
            if (!resultDict) {
                Py_DECREF(resultList);
                return NULL;
            }
            PyList_SET_ITEM(resultList, i, resultDict);
        }

        return resultList;
    }
}

static PyObject *coreml_metadata(PyObject *self, PyObject *args) {
    CoreMLModelObject *modelObj;
    if (!PyArg_ParseTuple(args, "O!", CoreMLModelType, &modelObj))
        return NULL;

    MLModelDescription *desc = modelObj->model.modelDescription;
    NSDictionary<NSString *, NSString *> *meta = desc.metadata;

    PyObject *result = PyDict_New();

    // Standard metadata keys
    struct { NSString *key; const char *pyKey; } fields[] = {
        { MLModelDescriptionKey, "description" },
        { MLModelVersionStringKey, "version" },
        { MLModelAuthorKey, "author" },
        { MLModelLicenseKey, "license" },
        { MLModelCreatorDefinedKey, "creatorDefined" },
    };

    for (int i = 0; i < 5; i++) {
        id val = meta[fields[i].key];
        if (val) {
            if ([val isKindOfClass:[NSString class]]) {
                PyObject *pyVal = PyUnicode_FromString([val UTF8String]);
                PyDict_SetItemString(result, fields[i].pyKey, pyVal);
                Py_DECREF(pyVal);
            } else if ([val isKindOfClass:[NSDictionary class]]) {
                // creatorDefined is a dict
                NSDictionary *dictVal = (NSDictionary *)val;
                PyObject *pyDict = PyDict_New();
                for (NSString *k in dictVal) {
                    PyObject *pyK = PyUnicode_FromString([k UTF8String]);
                    PyObject *pyV = PyUnicode_FromString([[dictVal[k] description] UTF8String]);
                    PyDict_SetItem(pyDict, pyK, pyV);
                    Py_DECREF(pyK);
                    Py_DECREF(pyV);
                }
                PyDict_SetItemString(result, fields[i].pyKey, pyDict);
                Py_DECREF(pyDict);
            }
        }
    }

    // Prediction function type
    NSString *predictedFeatureName = desc.predictedFeatureName;
    if (predictedFeatureName) {
        PyObject *pyVal = PyUnicode_FromString([predictedFeatureName UTF8String]);
        PyDict_SetItemString(result, "predictedFeatureName", pyVal);
        Py_DECREF(pyVal);
    }

    NSString *predictedProbabilitiesName = desc.predictedProbabilitiesName;
    if (predictedProbabilitiesName) {
        PyObject *pyVal = PyUnicode_FromString([predictedProbabilitiesName UTF8String]);
        PyDict_SetItemString(result, "predictedProbabilitiesName", pyVal);
        Py_DECREF(pyVal);
    }

    return result;
}

// ============================================================================
// MARK: - Module definition
// ============================================================================

static PyMethodDef CoreMLMethods[] = {
    {"load",     (PyCFunction)coreml_load,     METH_VARARGS | METH_KEYWORDS,
     "load(path, compute_units='all') -> Model\n"
     "Load a compiled CoreML model (.mlmodelc); path accepts str or os.PathLike.\n"
     "compute_units: 'all', 'cpuOnly', 'cpuAndGPU', or 'cpuAndNeuralEngine'"},
    {"predict",  coreml_predict,  METH_VARARGS,
     "predict(model, inputs) -> dict\n"
     "Run prediction. inputs is a dict mapping input names to numpy arrays/scalars."},
    {"describe", coreml_describe, METH_VARARGS,
     "describe(model) -> dict\n"
     "Get model input/output description."},
    {"compile",  (PyCFunction)coreml_compile,  METH_VARARGS | METH_KEYWORDS,
     "compile(model_path, output_dir=None) -> str\n"
     "Compile a .mlmodel or .mlpackage to .mlmodelc. Paths accept str or os.PathLike.\n"
     "Returns the output path as a string.\n"
     "If output_dir is None, saves next to the source file."},
    {"batch_predict", coreml_batch_predict, METH_VARARGS,
     "batch_predict(model, inputs_list) -> list[dict]\n"
     "Run batch prediction. inputs_list is a list of input dicts."},
    {"metadata", coreml_metadata, METH_VARARGS,
     "metadata(model) -> dict\n"
     "Get model metadata (author, version, license, description)."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef coremlmodule = {
    PyModuleDef_HEAD_INIT,
    "coreml",
    "CoreML hardware-accelerated inference on iOS (Neural Engine / GPU / CPU)",
    -1,
    CoreMLMethods
};

PyMODINIT_FUNC PyInit_coreml(void) {
    import_array();

    CoreMLModelType = (PyTypeObject *)PyType_FromSpec(&CoreMLModel_spec);
    if (!CoreMLModelType)
        return NULL;

    PyObject *m = PyModule_Create(&coremlmodule);
    if (!m) return NULL;

    Py_INCREF(CoreMLModelType);
    if (PyModule_AddObject(m, "Model", (PyObject *)CoreMLModelType) < 0) {
        Py_DECREF(CoreMLModelType);
        Py_DECREF(m);
        return NULL;
    }

    return m;
}

// ============================================================================
// MARK: - Registration (called from Swift before Py_Initialize)
// ============================================================================

void registerCoreMLModule(void) {
    PyImport_AppendInittab("coreml", PyInit_coreml);
}
