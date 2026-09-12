#pragma once

static PyObject *metal_create_window(PyObject *, PyObject *args, PyObject *kwargs) {
    const char *title = "Metal", *orientation = "auto";
    static const char *keywords[] = {"title", "orientation", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|ss", (char **)keywords, &title, &orientation)) return nullptr;
    if (strcmp(orientation, "auto") && strcmp(orientation, "portrait") && strcmp(orientation, "landscape")) {
        PyErr_SetString(PyExc_ValueError, "orientation must be 'auto', 'portrait', or 'landscape'."); return nullptr;
    }
    if (!NSThread.isMainThread && !NSApp.isRunning) {
        PyErr_SetString(PyExc_RuntimeError, "Create desktop scene windows on the main Python thread."); return nullptr;
    }
    if (!ensureMetalContext()) { PyErr_SetString(PyExc_RuntimeError, "Metal is unavailable."); return nullptr; }
    NSString *windowTitle = [NSString stringWithUTF8String:title];
    bool portrait = strcmp(orientation, "portrait") == 0;
    __block long long handle = 0;
    runOnMainSync(^{
        @autoreleasepool {
            CocoaPyPrepareApplication();
            auto controller = [[CocoaPyMetalViewController alloc] initWithTitle:windowTitle];
            NSRect rect = NSMakeRect(0, 0, portrait ? 540 : 960, portrait ? 800 : 640);
            NSWindow *window = [[NSWindow alloc] initWithContentRect:rect
                styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
                backing:NSBackingStoreBuffered defer:NO];
            window.releasedWhenClosed = NO;
            window.title = windowTitle;
            window.contentViewController = controller;
            window.delegate = controller;
            [window setContentSize:rect.size];
            [window center];
            [window makeKeyAndOrderFront:nil];
            [window makeFirstResponder:controller.surfaceView];
            [NSApp activate];
            [controller.view layoutSubtreeIfNeeded];
            handle = nextHandle();
            controller.surfaceView.windowHandle = handle;
            CAMetalLayer *layer = (CAMetalLayer *)controller.surfaceView.layer;
            layer.device = gDevice;
            layer.pixelFormat = MTLPixelFormatBGRA8Unorm;
            layer.framebufferOnly = NO;
            layer.contentsScale = window.backingScaleFactor;
            layer.drawableSize = CGSizeMake(rect.size.width * layer.contentsScale, rect.size.height * layer.contentsScale);
            auto semaphore = dispatch_semaphore_create(0);
            controller.vsyncSemaphore = semaphore;
            CADisplayLink *displayLink = [controller.surfaceView displayLinkWithTarget:controller selector:@selector(vsyncFired)];
            [displayLink addToRunLoop:NSRunLoop.mainRunLoop forMode:NSRunLoopCommonModes];
            WindowRecord record{};
            record.handle = handle; record.window = window; record.controller = controller;
            record.layer = layer; record.displayLink = displayLink;
            record.vsyncSemaphore = semaphore; record.frameSemaphore = dispatch_semaphore_create(1);
            std::lock_guard<std::mutex> lock(gStateMutex);
            gWindows.emplace(handle, std::move(record));
        }
    });
    return PyLong_FromLongLong(handle);
}

static PyObject *metal_close_window(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    void *pool = nullptr;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto record = windowRecord(handle);
        if (record) pool = std::exchange(record->frameAutoreleasePool, nullptr);
    }
    if (pool) objc_autoreleasePoolPop(pool);
    runOnMainSync(^{
        metalCloseTextInputsForWindow(handle);
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gWindows.find(handle);
        if (it == gWindows.end()) return;
        auto &record = it->second;
        [record.displayLink invalidate];
        record.controller.vsyncSemaphore = nil;
        if (record.vsyncSemaphore) dispatch_semaphore_signal(record.vsyncSemaphore);
        if (record.frameSemaphore) dispatch_semaphore_signal(record.frameSemaphore);
        record.window.delegate = nil;
        [record.window orderOut:nil];
        [record.window close];
        gWindows.erase(it);
    });
    Py_RETURN_NONE;
}

static PyObject *metalMacMetrics(long long handle, long long knownRevision = -1) {
    __block bool found = false;
    __block NSSize size = NSZeroSize;
    __block CGFloat scale = 1;
    __block long long revision = 0;
    runOnMainSync(^{
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto record = windowRecord(handle);
        if (!record) return;
        found = true;
        size = record->controller.surfaceView.bounds.size;
        scale = record->window.backingScaleFactor;
        revision = record->controller.layoutRevision;
    });
    if (!found) { PyErr_SetString(PyExc_KeyError, "Metal window handle not found."); return nullptr; }
    if (revision == knownRevision) Py_RETURN_NONE;
    return Py_BuildValue("{s:L,s:(dd),s:(dd),s:d,s:(dddd)}", "revision", revision,
        "size", size.width, size.height, "pixel_size", size.width * scale, size.height * scale,
        "scale", (double)scale, "safe_area", 0.0, 0.0, 0.0, 0.0);
}

static PyObject *metal_window_metrics(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    return metalMacMetrics(handle);
}
static PyObject *metal_window_metrics_if_changed(PyObject *, PyObject *args) {
    long long handle, revision;
    if (!PyArg_ParseTuple(args, "LL", &handle, &revision)) return nullptr;
    return metalMacMetrics(handle, revision);
}

static PyObject *metal_consume_actions(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    Py_BEGIN_ALLOW_THREADS
    metalMacPumpEvents();
    Py_END_ALLOW_THREADS
    __block int action = 0, close = 0;
    __block bool found = false;
    runOnMainSync(^{
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto record = windowRecord(handle);
        if (!record) return;
        found = true;
        [record->controller consumeActionCount:&action closeCount:&close];
    });
    if (!found) { PyErr_SetString(PyExc_KeyError, "Metal window handle not found."); return nullptr; }
    return Py_BuildValue("{s:i,s:i}", "action", action, "close", close);
}

static PyObject *metal_set_title(PyObject *, PyObject *args, PyObject *kwargs) {
    long long handle;
    const char *title;
    static const char *keywords[] = {"handle", "title", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "Ls", (char **)keywords, &handle, &title)) return nullptr;
    NSString *value = [NSString stringWithUTF8String:title];
    __block bool found = false;
    runOnMainSync(^{
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto record = windowRecord(handle);
        if (record) { record->window.title = value; record->controller.windowTitle = value; found = true; }
    });
    if (!found) { PyErr_SetString(PyExc_KeyError, "Metal window handle not found."); return nullptr; }
    Py_RETURN_NONE;
}

static PyObject *metal_set_action_label(PyObject *, PyObject *args, PyObject *kwargs) {
    long long handle;
    PyObject *label = Py_None;
    static const char *keywords[] = {"handle", "label", nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LO", (char **)keywords, &handle, &label)) return nullptr;
    const char *text = label == Py_None ? "" : PyUnicode_AsUTF8(label);
    if (!text) return nullptr;
    NSString *value = [NSString stringWithUTF8String:text];
    bool hidden = label == Py_None;
    __block bool found = false;
    runOnMainSync(^{
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto record = windowRecord(handle);
        if (!record) return;
        found = true;
        record->controller.actionButton.hidden = hidden;
        record->controller.actionButton.title = value;
        record->controller.view.needsLayout = YES;
    });
    if (!found) { PyErr_SetString(PyExc_KeyError, "Metal window handle not found."); return nullptr; }
    Py_RETURN_NONE;
}
