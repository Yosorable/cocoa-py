#pragma once

static PyObject *metal_create_window(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    const char *title = "Metal";
    const char *orientation = "auto";
    static char titleKeyword[] = "title";
    static char orientationKeyword[] = "orientation";
    static char *kwlist[] = {titleKeyword, orientationKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|ss", kwlist, &title, &orientation)) {
        return nullptr;
    }

    UIInterfaceOrientationMask orientationMask;
    if (strcmp(orientation, "auto") == 0) {
        orientationMask = UIInterfaceOrientationMaskAll;
    } else if (strcmp(orientation, "portrait") == 0) {
        orientationMask = UIInterfaceOrientationMaskPortrait;
    } else if (strcmp(orientation, "landscape") == 0) {
        orientationMask = UIInterfaceOrientationMaskLandscape;
    } else {
        PyErr_Format(PyExc_ValueError,
            "orientation must be 'auto', 'portrait', or 'landscape', got '%s'",
            orientation);
        return nullptr;
    }

    if (!ensureMetalContext()) {
        PyErr_SetString(PyExc_RuntimeError, "Metal device is unavailable on this device.");
        return nullptr;
    }

    NSString *windowTitle = [NSString stringWithUTF8String:title];
    __block long long handle = 0;
    runOnMainSync(^{
        UIWindowScene *scene = activeScene();
        if (!scene) {
            return;
        }

        handle = nextHandle();
        CocoaPyMetalViewController *controller = [[CocoaPyMetalViewController alloc] initWithTitle:windowTitle];
        controller.allowedOrientations = orientationMask;

        UIWindow *window = [[UIWindow alloc] initWithWindowScene:scene];
        window.frame = scene.coordinateSpace.bounds;
        window.rootViewController = controller;
        window.windowLevel = UIWindowLevelAlert;
        window.backgroundColor = UIColor.clearColor;
        [window makeKeyAndVisible];
        [controller.view layoutIfNeeded];

        [controller setNeedsUpdateOfSupportedInterfaceOrientations];
        UIWindowSceneGeometryPreferencesIOS *prefs =
            [[UIWindowSceneGeometryPreferencesIOS alloc] initWithInterfaceOrientations:orientationMask];
        [scene requestGeometryUpdateWithPreferences:prefs errorHandler:^(NSError *err){
            (void)err;
        }];

        controller.surfaceView.windowHandle = handle;

        CAMetalLayer *layer = (CAMetalLayer *)controller.surfaceView.layer;
        layer.device = gDevice;
        layer.pixelFormat = MTLPixelFormatBGRA8Unorm;
        layer.framebufferOnly = NO;
        layer.contentsScale = window.screen.scale;
        layer.drawableSize = CGSizeMake(
            controller.surfaceView.bounds.size.width * window.screen.scale,
            controller.surfaceView.bounds.size.height * window.screen.scale
        );

        dispatch_semaphore_t vsync = dispatch_semaphore_create(0);
        // Public gpu.Buffer and some scene internals can be mutated before
        // begin_frame(). Keep the window-wide fence conservative; higher
        // in-flight counts require every per-frame mutable buffer to be ringed.
        dispatch_semaphore_t frameSemaphore = dispatch_semaphore_create(1);
        controller.vsyncSemaphore = vsync;
        CADisplayLink *displayLink = [CADisplayLink displayLinkWithTarget:controller selector:@selector(vsyncFired)];
        [displayLink addToRunLoop:[NSRunLoop mainRunLoop] forMode:NSRunLoopCommonModes];

        WindowRecord record{};
        record.handle = handle; record.window = window; record.controller = controller;
        record.layer = layer; record.displayLink = displayLink;
        record.vsyncSemaphore = vsync; record.frameSemaphore = frameSemaphore;
        CocoaPySceneEventObserver *observer = [CocoaPySceneEventObserver new];
        observer.handle = handle; observer.window = window;
        record.eventObserver = observer;
        {
            std::lock_guard<std::mutex> lock(gStateMutex);
            gWindows.emplace(handle, std::move(record));
        }
        [observer start];
    });

    if (handle == 0) {
        PyErr_SetString(PyExc_RuntimeError, "_metal could not find an active UIWindowScene.");
        return nullptr;
    }
    return PyLong_FromLongLong(handle);
}

static PyObject *metal_close_window(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) {
        return nullptr;
    }

    void *framePool = nullptr;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *window = windowRecord(handle);
        if (window) {
            framePool = window->frameAutoreleasePool;
            window->frameAutoreleasePool = nullptr;
        }
    }
    if (framePool) {
        objc_autoreleasePoolPop(framePool);
    }

    runOnMainSync(^{
        CocoaPySceneEventObserver *observer = nil;
        {
            std::lock_guard<std::mutex> lock(gStateMutex);
            auto it = gWindows.find(handle);
            if (it != gWindows.end()) observer = it->second.eventObserver;
        }
        [observer stop];
        metalCloseTextInputsForWindow(handle);
        WindowRecord record{};
        {
            std::lock_guard<std::mutex> lock(gStateMutex);
            auto it = gWindows.find(handle);
            if (it == gWindows.end()) return;
            record = std::move(it->second);
            gWindows.erase(it);
        }
        UIWindowScene *scene = record.window.windowScene;
        [record.displayLink invalidate];
        record.displayLink = nil;
        if (record.vsyncSemaphore) {
            dispatch_semaphore_signal(record.vsyncSemaphore);
        }
        record.vsyncSemaphore = nil;
        if (record.frameSemaphore) {
            dispatch_semaphore_signal(record.frameSemaphore);
        }
        record.frameSemaphore = nil;
        record.encoder = nil;
        record.commandBuffer = nil;
        record.drawable = nil;
        record.targetTexture = nil;
        record.layer = nil;
        record.window.hidden = YES;
        record.window.rootViewController = nil;
        record.window.windowScene = nil;
        record.window = nil;
        record.controller = nil;

        // Trigger re-query of the now-key window's supported orientations,
        // so any scene-imposed lock is released cleanly.
        if (scene) {
            UIWindowSceneGeometryPreferencesIOS *prefs =
                [[UIWindowSceneGeometryPreferencesIOS alloc]
                    initWithInterfaceOrientations:UIInterfaceOrientationMaskAll];
            [scene requestGeometryUpdateWithPreferences:prefs errorHandler:^(NSError *err){
                (void)err;
            }];
        }
    });

    Py_RETURN_NONE;
}

static PyObject *metal_window_metrics(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) {
        return nullptr;
    }

    __block CGRect bounds = CGRectZero;
    __block CGFloat scale = 1.0;
    __block UIEdgeInsets insets = UIEdgeInsetsZero;
    __block long long revision = 0;
    __block bool found = false;

    runOnMainSync(^{
        @autoreleasepool {
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *record = windowRecord(handle);
        if (!record) return;
        bounds = record->controller.surfaceView.bounds;
        scale = record->window.screen.scale;
        insets = record->controller.view.safeAreaInsets;
        revision = record->controller.layoutRevision;
        found = true;
        }
    });

    if (!found) {
        PyErr_SetString(PyExc_KeyError, "_metal window handle not found.");
        return nullptr;
    }

    PyObject *dict = PyDict_New();
    PyObject *revisionValue = PyLong_FromLongLong(revision);
    PyObject *sizeValue = Py_BuildValue("(ff)", bounds.size.width, bounds.size.height);
    PyObject *pixelValue = Py_BuildValue("(ff)", bounds.size.width * scale, bounds.size.height * scale);
    PyObject *scaleValue = PyFloat_FromDouble(scale);
    PyObject *safeValue = Py_BuildValue("(ffff)", insets.top, insets.left, insets.bottom, insets.right);
    PyDict_SetItemString(dict, "revision", revisionValue);
    PyDict_SetItemString(dict, "size", sizeValue);
    PyDict_SetItemString(dict, "pixel_size", pixelValue);
    PyDict_SetItemString(dict, "scale", scaleValue);
    PyDict_SetItemString(dict, "safe_area", safeValue);
    Py_DECREF(revisionValue);
    Py_DECREF(sizeValue);
    Py_DECREF(pixelValue);
    Py_DECREF(scaleValue);
    Py_DECREF(safeValue);
    return dict;
}

static PyObject *metal_window_metrics_if_changed(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    long long knownRevision = 0;
    if (!PyArg_ParseTuple(args, "LL", &handle, &knownRevision)) {
        return nullptr;
    }

    __block CGRect bounds = CGRectZero;
    __block CGFloat scale = 1.0;
    __block UIEdgeInsets insets = UIEdgeInsetsZero;
    __block long long revision = 0;
    __block bool found = false;

    runOnMainSync(^{
        @autoreleasepool {
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *record = windowRecord(handle);
        if (!record) return;
        found = true;
        revision = record->controller.layoutRevision;
        if (revision == knownRevision) {
            return;
        }
        bounds = record->controller.surfaceView.bounds;
        scale = record->window.screen.scale;
        insets = record->controller.view.safeAreaInsets;
        }
    });

    if (!found) {
        PyErr_SetString(PyExc_KeyError, "_metal window handle not found.");
        return nullptr;
    }

    if (revision == knownRevision) {
        Py_RETURN_NONE;
    }

    PyObject *dict = PyDict_New();
    PyObject *revisionValue = PyLong_FromLongLong(revision);
    PyObject *sizeValue = Py_BuildValue("(ff)", bounds.size.width, bounds.size.height);
    PyObject *pixelValue = Py_BuildValue("(ff)", bounds.size.width * scale, bounds.size.height * scale);
    PyObject *scaleValue = PyFloat_FromDouble(scale);
    PyObject *safeValue = Py_BuildValue("(ffff)", insets.top, insets.left, insets.bottom, insets.right);
    PyDict_SetItemString(dict, "revision", revisionValue);
    PyDict_SetItemString(dict, "size", sizeValue);
    PyDict_SetItemString(dict, "pixel_size", pixelValue);
    PyDict_SetItemString(dict, "scale", scaleValue);
    PyDict_SetItemString(dict, "safe_area", safeValue);
    Py_DECREF(revisionValue);
    Py_DECREF(sizeValue);
    Py_DECREF(pixelValue);
    Py_DECREF(scaleValue);
    Py_DECREF(safeValue);
    return dict;
}

static PyObject *metal_consume_actions(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) {
        return nullptr;
    }

    __block int actionCount = 0;
    __block int closeCount = 0;
    __block bool found = false;
    runOnMainSync(^{
        @autoreleasepool {
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *record = windowRecord(handle);
        if (!record) return;
        found = true;
        [record->controller consumeActionCount:&actionCount closeCount:&closeCount];
        }
    });

    if (!found) {
        PyErr_SetString(PyExc_KeyError, "_metal window handle not found.");
        return nullptr;
    }

    return Py_BuildValue("{s:i,s:i}", "action", actionCount, "close", closeCount);
}

static PyObject *metal_set_title(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long handle = 0;
    const char *title = nullptr;
    static char handleKeyword[] = "handle";
    static char titleKeyword[] = "title";
    static char *kwlist[] = {handleKeyword, titleKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "Ls", kwlist, &handle, &title)) {
        return nullptr;
    }

    NSString *nsTitle = [NSString stringWithUTF8String:title];
    __block bool found = false;
    runOnMainSync(^{
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *record = windowRecord(handle);
        if (!record) return;
        found = true;
        record->controller.windowTitle = nsTitle;
        record->controller.titleLabel.text = nsTitle;
    });

    if (!found) {
        PyErr_SetString(PyExc_KeyError, "_metal window handle not found.");
        return nullptr;
    }
    Py_RETURN_NONE;
}

static PyObject *metal_set_action_label(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long handle = 0;
    PyObject *labelObject = Py_None;
    static char handleKeyword[] = "handle";
    static char labelKeyword[] = "label";
    static char *kwlist[] = {handleKeyword, labelKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LO", kwlist, &handle, &labelObject)) {
        return nullptr;
    }

    NSString *label = nil;
    if (labelObject != Py_None) {
        const char *utf8 = PyUnicode_AsUTF8(labelObject);
        if (!utf8) {
            return nullptr;
        }
        label = [NSString stringWithUTF8String:utf8];
    }

    __block bool found = false;
    runOnMainSync(^{
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *record = windowRecord(handle);
        if (!record) return;
        found = true;
        if (label == nil) {
            record->controller.actionButton.hidden = YES;
            [record->controller.actionButton setTitle:nil forState:UIControlStateNormal];
            return;
        }
        record->controller.actionButton.hidden = NO;
        [record->controller.actionButton setTitle:label forState:UIControlStateNormal];
    });

    if (!found) {
        PyErr_SetString(PyExc_KeyError, "_metal window handle not found.");
        return nullptr;
    }
    Py_RETURN_NONE;
}
