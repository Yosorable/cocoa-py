// Synchronous image readback and encoding shared by macOS and iPhoneOS.
// Included after the Metal resource records and lookup helpers are defined.

static PyObject *metal_prepare_image_capture(PyObject *, PyObject *args) {
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    @autoreleasepool {
        __block NSString *error = nil;
        __block id<MTLCommandBuffer> command = nil;
        runOnMainSync(^{
            std::lock_guard<std::mutex> lock(gStateMutex);
            WindowRecord *window = windowRecord(handle);
            if (!window) {
                error = @"Capture requires an open scene window.";
                return;
            }
            if (!metalGPUAllowed(*window)) { error = metalSuspendedMessage; return; }
            if (window->frameActive || window->blitActive || window->computeActive) {
                error = @"Capture must run between render, blit and compute passes.";
                return;
            }
            command = window->offscreenCB ?: [gCommandQueue commandBuffer];
            if (!command) {
                error = @"Failed to create the capture synchronization command.";
                return;
            }
            metalCommit(*window, command);
            window->offscreenCB = nil;
        });
        if (error) {
            metalSetWindowError(error);
            return nullptr;
        }
        // Fence earlier use of mutable particle and shader buffers before
        // capture renders them again. Never wait while holding the state lock.
        Py_BEGIN_ALLOW_THREADS
        [command waitUntilCompleted];
        Py_END_ALLOW_THREADS
        if (PyErr_CheckSignals() < 0) return nullptr;
        if (command.status != MTLCommandBufferStatusCompleted) {
            PyErr_SetString(PyExc_RuntimeError,
                            (command.error.localizedDescription ?: @"Capture synchronization failed.").UTF8String);
            return nullptr;
        }
    }
    Py_RETURN_NONE;
}

static PyObject *metal_read_texture_image(PyObject *, PyObject *args, PyObject *kwargs) {
    long long windowHandle = 0, textureHandle = 0;
    int unpremultiply = 1;
    static char wk[] = "window", tk[] = "texture", uk[] = "unpremultiply";
    static char *keywords[] = {wk, tk, uk, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LL|p", keywords,
                                    &windowHandle, &textureHandle, &unpremultiply)) return nullptr;

    @autoreleasepool {
        __block NSString *error = nil;
        __block id<MTLBuffer> pixels = nil;
        __block id<MTLCommandBuffer> command = nil;
        __block id<MTLCommandBuffer> rendered = nil;
        __block NSUInteger width = 0, height = 0, rowBytes = 0;
        __block bool bgra = false;
        runOnMainSync(^{
            std::lock_guard<std::mutex> lock(gStateMutex);
            WindowRecord *window = windowRecord(windowHandle);
            TextureRecord *record = textureRecord(textureHandle);
            if (!window || !record) {
                error = @"Image readback requires an open window and texture.";
                return;
            }
            if (!metalGPUAllowed(*window)) { error = metalSuspendedMessage; return; }
            if (window->frameActive || window->blitActive || window->computeActive) {
                error = @"End the active render, blit or compute pass before reading an image.";
                return;
            }
            if (record->format != MTLPixelFormatBGRA8Unorm && record->format != MTLPixelFormatRGBA8Unorm) {
                error = @"Image readback supports bgra8 and rgba8 textures only.";
                return;
            }
            width = record->width;
            height = record->height;
            if (!width || !height || width > (NSUInteger)PY_SSIZE_T_MAX / 4 - 255) {
                error = @"Image dimensions are out of range.";
                return;
            }
            rowBytes = (width * 4 + 255) & ~(NSUInteger)255;
            if (height > (NSUInteger)PY_SSIZE_T_MAX / rowBytes) {
                error = @"Image dimensions are out of range.";
                return;
            }
            bgra = record->format == MTLPixelFormatBGRA8Unorm;
            pixels = [gDevice newBufferWithLength:rowBytes * height options:MTLResourceStorageModeShared];
            command = [gCommandQueue commandBuffer];
            id<MTLBlitCommandEncoder> encoder = [command blitCommandEncoder];
            if (!pixels || !command || !encoder) {
                if (encoder) [encoder endEncoding];
                error = @"Failed to allocate image readback resources.";
                return;
            }
            // Submit this window's queued offscreen draws before the copy.
            // All commands use the same queue, so the copy observes those draws.
            if (window->offscreenCB) {
                rendered = window->offscreenCB;
                metalCommit(*window, rendered);
                window->offscreenCB = nil;
            }
            [encoder copyFromTexture:record->texture sourceSlice:0 sourceLevel:0
                        sourceOrigin:MTLOriginMake(0, 0, 0)
                          sourceSize:MTLSizeMake(width, height, 1)
                            toBuffer:pixels destinationOffset:0
              destinationBytesPerRow:rowBytes destinationBytesPerImage:rowBytes * height];
            [encoder endEncoding];
            metalCommit(*window, command);
        });
        if (error) {
            metalSetWindowError(error);
            return nullptr;
        }
        // Completion handlers may take gStateMutex or need the Python thread.
        // Retained command/buffer objects outlive any concurrent window closure.
        Py_BEGIN_ALLOW_THREADS
        [command waitUntilCompleted];
        Py_END_ALLOW_THREADS
        if (PyErr_CheckSignals() < 0) return nullptr;
        if (rendered && rendered.status != MTLCommandBufferStatusCompleted) {
            PyErr_SetString(PyExc_RuntimeError,
                            (rendered.error.localizedDescription ?: @"Image rendering failed.").UTF8String);
            return nullptr;
        }
        if (command.status != MTLCommandBufferStatusCompleted) {
            NSString *message = command.error.localizedDescription ?: @"Metal image readback failed.";
            PyErr_SetString(PyExc_RuntimeError, message.UTF8String);
            return nullptr;
        }
        PyObject *data = PyBytes_FromStringAndSize(nullptr, (Py_ssize_t)(width * height * 4));
        if (!data) return nullptr;
        auto *destination = (unsigned char *)PyBytes_AS_STRING(data);
        const auto *source = (const unsigned char *)pixels.contents;
        Py_BEGIN_ALLOW_THREADS
        for (NSUInteger y = 0; y < height; ++y) {
            const unsigned char *row = source + y * rowBytes;
            unsigned char *output = destination + y * width * 4;
            for (NSUInteger x = 0; x < width; ++x) {
                unsigned int a = row[x * 4 + 3];
                for (unsigned int c = 0; c < 3; ++c) {
                    unsigned int value = row[x * 4 + (bgra ? 2 - c : c)];
                    if (unpremultiply) value = a ? std::min(255u, (value * 255u + a / 2u) / a) : 0;
                    output[x * 4 + c] = (unsigned char)value;
                }
                output[x * 4 + 3] = (unsigned char)a;
            }
        }
        Py_END_ALLOW_THREADS
        return Py_BuildValue("(nnN)", (Py_ssize_t)width, (Py_ssize_t)height, data);
    }
}

static PyObject *metal_encode_png(PyObject *, PyObject *args) {
    Py_ssize_t width = 0, height = 0;
    Py_buffer pixels{};
    if (!PyArg_ParseTuple(args, "nny*", &width, &height, &pixels)) return nullptr;
    if (width <= 0 || height <= 0 || width > PY_SSIZE_T_MAX / 4 ||
        height > PY_SSIZE_T_MAX / (width * 4) || pixels.len != width * height * 4) {
        PyBuffer_Release(&pixels);
        PyErr_SetString(PyExc_ValueError, "PNG input must be width * height * 4 RGBA bytes.");
        return nullptr;
    }
    @autoreleasepool {
        NSMutableData *encoded = [NSMutableData data];
        bool success = false;
        // Keep the exported buffer alive through CGImage and ImageIO teardown.
        Py_BEGIN_ALLOW_THREADS
        CGColorSpaceRef colorSpace = CGColorSpaceCreateWithName(kCGColorSpaceSRGB);
        CGDataProviderRef provider = CGDataProviderCreateWithData(nullptr, pixels.buf, pixels.len, nullptr);
        CGImageRef image = (provider && colorSpace) ? CGImageCreate(
            width, height, 8, 32, width * 4, colorSpace,
            kCGBitmapByteOrder32Big | kCGImageAlphaLast, provider, nullptr, false,
            kCGRenderingIntentDefault) : nullptr;
        CGImageDestinationRef writer = image ? CGImageDestinationCreateWithData(
            (__bridge CFMutableDataRef)encoded, CFSTR("public.png"), 1, nullptr) : nullptr;
        if (writer) {
            CGImageDestinationAddImage(writer, image, nullptr);
            success = CGImageDestinationFinalize(writer);
            CFRelease(writer);
        }
        if (image) CGImageRelease(image);
        if (provider) CGDataProviderRelease(provider);
        if (colorSpace) CGColorSpaceRelease(colorSpace);
        Py_END_ALLOW_THREADS
        PyBuffer_Release(&pixels);
        if (!success) {
            PyErr_SetString(PyExc_RuntimeError, "Failed to encode PNG image.");
            return nullptr;
        }
        return PyBytes_FromStringAndSize((const char *)encoded.bytes, (Py_ssize_t)encoded.length);
    }
}
