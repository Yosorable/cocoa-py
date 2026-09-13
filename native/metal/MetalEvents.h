// Window-scoped lifecycle and keyboard queues. Called with native values only;
// producers never acquire the GIL or call Python.
#pragma once

static PyObject *gWindowSuspendedError = nullptr;
static std::atomic<unsigned long long> gSceneEventObservers{0};
static NSString *const metalSuspendedMessage = @"The scene window is suspended.";

static bool metalGPUAllowed(const WindowRecord &window) {
#if COCOA_PY_UIKIT
    return window.active && window.foreground;
#else
    return window.foreground;
#endif
}

static bool metalRequireGPU(long long handle) {
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gWindows.find(handle);
    if (it == gWindows.end()) { PyErr_SetString(PyExc_KeyError, "Window handle not found."); return false; }
    if (!metalGPUAllowed(it->second)) {
        PyErr_SetString(gWindowSuspendedError, metalSuspendedMessage.UTF8String);
        return false;
    }
    return true;
}

static void metalCommit(WindowRecord &window, id<MTLCommandBuffer> command) {
    window.lastSubmittedCB = command;
    [command commit];
}

static void metalSetWindowError(NSString *message) {
    PyErr_SetString([message isEqualToString:metalSuspendedMessage] ? gWindowSuspendedError : PyExc_RuntimeError,
                    message.UTF8String);
}

// gStateMutex is held by callers of this helper.
static void metalResetInput(WindowRecord &window, const char *reason, bool queueEvent = true) {
    ++window.inputEpoch;
    window.touchQueue.clear();
    window.scrollQueue.clear();
    window.touchIdMap.clear();
    window.pressedKeys.clear();
    auto &queue = window.platformQueue;
    queue.erase(std::remove_if(queue.begin(), queue.end(), [](const auto &event) { return event.kind == 0; }), queue.end());
    if (queue.size() >= 4094) queue.clear();
    if (queueEvent) {
        ScenePlatformEvent event;
        event.kind = 2;
        event.epoch = window.inputEpoch;
        event.timestamp = NSProcessInfo.processInfo.systemUptime;
        event.reason = reason;
        event.sequence = ++window.platformSequence;
        queue.push_back(std::move(event));
    }
    if (window.vsyncSemaphore) dispatch_semaphore_signal(window.vsyncSemaphore);
}

static void metalPublishState(long long handle, bool active, bool foreground, bool focused) {
    id<MTLCommandBuffer> submitted = nil;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gWindows.find(handle);
        if (it == gWindows.end()) return;
        auto &window = it->second;
        if (window.active == active && window.foreground == foreground && window.focused == focused) return;
        if ((window.active && !active) || (window.foreground && !foreground) || (window.focused && !focused))
            metalResetInput(window, !foreground ? "background" : !active ? "inactive" : "focus", false);
        window.active = active;
        window.foreground = foreground;
        window.focused = focused;
        ScenePlatformEvent event;
        event.kind = 1;
        event.active = active; event.foreground = foreground; event.focused = focused;
        event.epoch = window.inputEpoch;
        event.timestamp = NSProcessInfo.processInfo.systemUptime;
        if (window.platformQueue.size() >= 4096) metalResetInput(window, "overflow");
        event.epoch = window.inputEpoch;
        event.sequence = ++window.platformSequence;
        window.platformQueue.push_back(std::move(event));
        if (window.vsyncSemaphore) dispatch_semaphore_signal(window.vsyncSemaphore);
        if (!metalGPUAllowed(window)) submitted = window.lastSubmittedCB;
    }
    // Apple requires previously committed work to be scheduled before the app
    // enters the background. Never wait under the state mutex.
    if (submitted && submitted.status < MTLCommandBufferStatusScheduled) [submitted waitUntilScheduled];
}

static void metalRestoreKeyboardResponder(long long handle) {
    dispatch_async(dispatch_get_main_queue(), ^{
        CocoaPyMetalSurfaceView *surface = nil;
        {
            std::lock_guard<std::mutex> lock(gStateMutex);
            auto it = gWindows.find(handle);
            if (it == gWindows.end()) return;
            auto &window = it->second;
            if (!window.active || !window.foreground || !window.focused || window.activeTextInput) return;
            surface = window.controller.surfaceView;
        }
#if COCOA_PY_UIKIT
        [surface becomeFirstResponder];
#else
        [surface.window makeFirstResponder:surface];
#endif
    });
}

static void metalTextFocusChanged(long long windowHandle, long long inputHandle, bool focused) {
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gWindows.find(windowHandle);
        if (it == gWindows.end()) return;
        auto &window = it->second;
        if (focused) {
            if (window.activeTextInput != inputHandle) metalResetInput(window, "text_input");
            window.activeTextInput = inputHandle;
        } else if (window.activeTextInput == inputHandle) window.activeTextInput = 0;
    }
    if (!focused) metalRestoreKeyboardResponder(windowHandle);
}

static std::string metalKeyName(unsigned code, NSString *characters) {
    switch (code) {
        case 40: case 88: return "enter";
        case 41: return "escape";
        case 42: return "backspace";
        case 43: return "tab";
        case 44: return "space";
        case 57: return "caps_lock";
        case 70: return "print_screen";
        case 71: return "scroll_lock";
        case 72: return "pause";
        case 73: return "insert";
        case 74: return "home";
        case 75: return "page_up";
        case 76: return "delete";
        case 77: return "end";
        case 78: return "page_down";
        case 79: return "right";
        case 80: return "left";
        case 81: return "down";
        case 82: return "up";
        case 83: return "num_lock";
        case 224: return "left_control";
        case 225: return "left_shift";
        case 226: return "left_alt";
        case 227: return "left_command";
        case 228: return "right_control";
        case 229: return "right_shift";
        case 230: return "right_alt";
        case 231: return "right_command";
    }
    if (code >= 58 && code <= 69) return "f" + std::to_string(code - 57);
    if (code >= 104 && code <= 115) return "f" + std::to_string(code - 91);
    NSString *value = characters.lowercaseString;
    if (value.length && [value rangeOfCharacterFromSet:NSCharacterSet.controlCharacterSet].location == NSNotFound)
        return value.UTF8String ?: "";
    return "key_" + std::to_string(code);
}

// Returns false for keys that should continue through the native responder chain.
static bool metalQueueKey(long long handle, unsigned code, NSString *characters,
                          unsigned modifiers, bool down, bool repeat, bool cancelled, double timestamp) {
    if (!code) return false;
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gWindows.find(handle);
    if (it == gWindows.end()) return false;
    auto &window = it->second;
    auto pressed = window.pressedKeys.find(code);
    if (!window.keyboardEvents || window.activeTextInput || !window.active || !window.foreground || !window.focused)
        return false;
    // Command shortcuts belong to the application/OS. A release for an already
    // captured key is still delivered, even if Command was pressed afterward.
    if ((modifiers & 8) && code < 224 && (down || pressed == window.pressedKeys.end())) return false;
    if (!down && pressed == window.pressedKeys.end()) return true;
    if (window.platformQueue.size() >= 4096) {
        metalResetInput(window, "overflow");
        return true;
    }
    SceneKeyEvent key;
    key.code = code; key.modifiers = modifiers; key.down = down;
    key.timestamp = timestamp; key.cancelled = cancelled;
    key.repeat = down && (repeat || pressed != window.pressedKeys.end());
    key.key = pressed != window.pressedKeys.end() ? pressed->second.key : metalKeyName(code, characters);
    if (down) window.pressedKeys[code] = key;
    else window.pressedKeys.erase(code);
    ScenePlatformEvent event;
    event.kind = 0; event.key = std::move(key);
    event.timestamp = timestamp; event.epoch = window.inputEpoch;
    event.sequence = ++window.platformSequence;
    window.platformQueue.push_back(std::move(event));
    if (window.vsyncSemaphore) dispatch_semaphore_signal(window.vsyncSemaphore);
    return true;
}

@interface CocoaPySceneEventObserver : NSObject
@property(nonatomic) long long handle;
@property(nonatomic) BOOL active, foreground, focused;
@property(nonatomic, strong) NSMutableArray *tokens;
#if COCOA_PY_UIKIT
@property(nonatomic, weak) UIWindow *window;
#else
@property(nonatomic, weak) NSWindow *window;
#endif
- (void)start;
- (void)stop;
@end

@implementation CocoaPySceneEventObserver
- (instancetype)init {
    if ((self = [super init])) ++gSceneEventObservers;
    return self;
}
- (void)observe:(NSNotificationName)name object:(id)object update:(void (^)(CocoaPySceneEventObserver *))update {
    __weak CocoaPySceneEventObserver *weakSelf = self;
    id token = [NSNotificationCenter.defaultCenter addObserverForName:name object:object queue:nil
        usingBlock:^(NSNotification *) {
            CocoaPySceneEventObserver *owner = weakSelf;
            if (!owner) return;
            update(owner);
            metalPublishState(owner.handle, owner.active, owner.foreground, owner.focused);
            if (owner.active && owner.foreground && owner.focused) metalRestoreKeyboardResponder(owner.handle);
        }];
    [self.tokens addObject:token];
}
- (void)start {
    self.tokens = [NSMutableArray array];
#if COCOA_PY_UIKIT
    UIScene *scene = self.window.windowScene;
    self.active = scene.activationState == UISceneActivationStateForegroundActive;
    self.foreground = scene.activationState == UISceneActivationStateForegroundActive || scene.activationState == UISceneActivationStateForegroundInactive;
    self.focused = self.window.isKeyWindow;
    [self observe:UISceneDidActivateNotification object:scene update:^(CocoaPySceneEventObserver *owner) { owner.active = YES; }];
    [self observe:UISceneWillDeactivateNotification object:scene update:^(CocoaPySceneEventObserver *owner) { owner.active = NO; }];
    [self observe:UISceneWillEnterForegroundNotification object:scene update:^(CocoaPySceneEventObserver *owner) { owner.foreground = YES; }];
    [self observe:UISceneDidEnterBackgroundNotification object:scene update:^(CocoaPySceneEventObserver *owner) { owner.foreground = NO; owner.active = NO; }];
    [self observe:UIWindowDidBecomeKeyNotification object:self.window update:^(CocoaPySceneEventObserver *owner) { owner.focused = YES; }];
    [self observe:UIWindowDidResignKeyNotification object:self.window update:^(CocoaPySceneEventObserver *owner) { owner.focused = NO; }];
#else
    self.active = NSApp.isActive;
    self.foreground = !NSApp.hidden && !self.window.miniaturized;
    self.focused = self.window.isKeyWindow;
    [self observe:NSApplicationDidBecomeActiveNotification object:NSApp update:^(CocoaPySceneEventObserver *owner) { owner.active = YES; }];
    [self observe:NSApplicationWillResignActiveNotification object:NSApp update:^(CocoaPySceneEventObserver *owner) { owner.active = NO; }];
    [self observe:NSApplicationDidHideNotification object:NSApp update:^(CocoaPySceneEventObserver *owner) { owner.foreground = NO; }];
    [self observe:NSApplicationDidUnhideNotification object:NSApp update:^(CocoaPySceneEventObserver *owner) { owner.foreground = !owner.window.miniaturized; }];
    [self observe:NSWindowDidBecomeKeyNotification object:self.window update:^(CocoaPySceneEventObserver *owner) { owner.focused = YES; }];
    [self observe:NSWindowDidResignKeyNotification object:self.window update:^(CocoaPySceneEventObserver *owner) { owner.focused = NO; }];
    [self observe:NSWindowDidMiniaturizeNotification object:self.window update:^(CocoaPySceneEventObserver *owner) { owner.foreground = NO; }];
    [self observe:NSWindowDidDeminiaturizeNotification object:self.window update:^(CocoaPySceneEventObserver *owner) { owner.foreground = !NSApp.hidden; }];
#endif
    metalPublishState(self.handle, self.active, self.foreground, self.focused);
    metalRestoreKeyboardResponder(self.handle);
}
- (void)stop {
    for (id token in self.tokens) [NSNotificationCenter.defaultCenter removeObserver:token];
    [self.tokens removeAllObjects];
}
- (void)dealloc { [self stop]; --gSceneEventObservers; }
@end

static PyObject *metalWindowState(const WindowRecord &window) {
    return Py_BuildValue("{s:O,s:O,s:O,s:O,s:O,s:K,s:K}",
        "active", window.active ? Py_True : Py_False,
        "foreground", window.foreground ? Py_True : Py_False,
        "focused", window.focused ? Py_True : Py_False,
        "gpu_allowed", metalGPUAllowed(window) ? Py_True : Py_False,
        "native_repeat", COCOA_PY_UIKIT ? Py_False : Py_True,
        "epoch", window.inputEpoch, "sequence", window.platformSequence);
}

static PyObject *metal_window_state(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gWindows.find(handle);
    if (it == gWindows.end()) { PyErr_SetString(PyExc_KeyError, "Window handle not found."); return nullptr; }
    return metalWindowState(it->second);
}

static PyObject *metal_keyboard_events(PyObject *, PyObject *args) {
    long long handle;
    int enabled;
    if (!PyArg_ParseTuple(args, "Lp", &handle, &enabled)) return nullptr;
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gWindows.find(handle);
    if (it == gWindows.end()) { PyErr_SetString(PyExc_KeyError, "Window handle not found."); return nullptr; }
    it->second.keyboardEvents = enabled;
    if (!enabled) metalResetInput(it->second, "disabled");
    Py_RETURN_NONE;
}

static PyObject *metal_reset_window_input(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gWindows.find(handle);
    if (it == gWindows.end()) { PyErr_SetString(PyExc_KeyError, "Window handle not found."); return nullptr; }
    metalResetInput(it->second, "scene");
    Py_RETURN_NONE;
}

static PyObject *metal_discard_pending_draws(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gWindows.find(handle);
    if (it == gWindows.end()) { PyErr_SetString(PyExc_KeyError, "Window handle not found."); return nullptr; }
    if (it->second.frameActive) {
        PyErr_SetString(PyExc_RuntimeError, "End the active pass before discarding pending draws.");
        return nullptr;
    }
    it->second.offscreenCB = nil;
    it->second.commandBuffer = nil;
    it->second.drawable = nil;
    it->second.targetTexture = nil;
    Py_RETURN_NONE;
}

static PyObject *metal_consume_platform_events(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::vector<ScenePlatformEvent> events;
    PyObject *state = nullptr;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gWindows.find(handle);
        if (it == gWindows.end()) { PyErr_SetString(PyExc_KeyError, "Window handle not found."); return nullptr; }
        state = metalWindowState(it->second);
        if (!state) return nullptr;
        events.swap(it->second.platformQueue);
    }
    PyObject *list = PyList_New(events.size());
    if (!list) { Py_DECREF(state); return nullptr; }
    for (size_t i = 0; i < events.size(); ++i) {
        const auto &event = events[i];
        PyObject *item;
        if (event.kind == 0) {
            const auto &key = event.key;
            item = Py_BuildValue("{s:s,s:s,s:I,s:I,s:O,s:O,s:s,s:d,s:K}",
                "kind", "key", "key", key.key.c_str(), "code", key.code, "modifiers", key.modifiers,
                "repeat", key.repeat ? Py_True : Py_False, "cancelled", key.cancelled ? Py_True : Py_False,
                "phase", key.down ? "down" : "up", "timestamp", event.timestamp, "epoch", event.epoch);
        } else if (event.kind == 1) {
            item = Py_BuildValue("{s:s,s:O,s:O,s:O,s:d,s:K}", "kind", "state",
                "active", event.active ? Py_True : Py_False, "foreground", event.foreground ? Py_True : Py_False,
                "focused", event.focused ? Py_True : Py_False, "timestamp", event.timestamp, "epoch", event.epoch);
        } else {
            item = Py_BuildValue("{s:s,s:s,s:d,s:K}", "kind", "reset", "reason", event.reason.c_str(),
                "timestamp", event.timestamp, "epoch", event.epoch);
        }
        if (!item) { Py_DECREF(state); Py_DECREF(list); return nullptr; }
        PyObject *sequence = PyLong_FromUnsignedLongLong(event.sequence);
        if (!sequence || PyDict_SetItemString(item, "sequence", sequence) < 0) {
            Py_XDECREF(sequence); Py_DECREF(item); Py_DECREF(state); Py_DECREF(list); return nullptr;
        }
        Py_DECREF(sequence);
        PyList_SET_ITEM(list, i, item);
    }
    return Py_BuildValue("{s:N,s:N}", "state", state, "events", list);
}
