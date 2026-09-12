#pragma once

// These conversions run with the GIL held. No Python object enters a UI block.
static NSString *inputString(PyObject *value) {
    Py_ssize_t size;
    const char *bytes = PyUnicode_AsUTF8AndSize(value, &size);
    if (!bytes) return nil;
    return [[NSString alloc] initWithBytes:bytes length:size encoding:NSUTF8StringEncoding];
}

static PyObject *inputPython(id value) {
    if (!value || value == NSNull.null) Py_RETURN_NONE;
    if ([value isKindOfClass:NSString.class]) {
        NSData *data = [value dataUsingEncoding:NSUTF8StringEncoding];
        if (!data) { PyErr_SetString(PyExc_ValueError, "Invalid Unicode from the native editor."); return nullptr; }
        return PyUnicode_DecodeUTF8((const char *)data.bytes, data.length, "strict");
    }
    if ([value isKindOfClass:NSNumber.class]) {
        if (CFGetTypeID((__bridge CFTypeRef)value) == CFBooleanGetTypeID()) return PyBool_FromLong([value boolValue]);
        return PyLong_FromUnsignedLongLong([value unsignedLongLongValue]);
    }
    if ([value isKindOfClass:NSArray.class]) {
        PyObject *list = PyList_New([value count]);
        if (!list) return nullptr;
        Py_ssize_t index = 0;
        for (id item in value) {
            PyObject *converted = inputPython(item);
            if (!converted) { Py_DECREF(list); return nullptr; }
            PyList_SET_ITEM(list, index++, converted);
        }
        return list;
    }
    PyObject *dict = PyDict_New();
    if (!dict) return nullptr;
    for (NSString *key in value) {
        PyObject *item = inputPython(value[key]);
        if (!item || PyDict_SetItemString(dict, key.UTF8String, item) < 0) {
            Py_XDECREF(item); Py_DECREF(dict); return nullptr;
        }
        Py_DECREF(item);
    }
    return dict;
}

static NSArray *inputNumbers(PyObject *value, Py_ssize_t count, bool indices = false) {
    // Numeric conversions can run Python and mutate a caller-owned list.
    PyObject *seq = PySequence_Tuple(value);
    if (!seq) return nil;
    NSMutableArray *result = [NSMutableArray new];
    if (PyTuple_GET_SIZE(seq) != count) PyErr_SetString(PyExc_ValueError, "Incorrect sequence length.");
    else for (Py_ssize_t i = 0; i < count; ++i) {
        PyObject *item = PyTuple_GET_ITEM(seq, i);
        if (indices) {
            PyObject *index = PyNumber_Index(item);
            if (!index) break;
            unsigned long long n = PyLong_AsUnsignedLongLong(index);
            Py_DECREF(index);
            if (PyErr_Occurred()) break;
            [result addObject:@(n)];
        } else {
            double n = PyFloat_AsDouble(item);
            if (PyErr_Occurred()) break;
            if (!std::isfinite(n)) { PyErr_SetString(PyExc_ValueError, "Numbers must be finite."); break; }
            [result addObject:@(n)];
        }
    }
    Py_DECREF(seq);
    return PyErr_Occurred() ? nil : result;
}

static NSInteger inputLimit(PyObject *value) {
    if (value == Py_None) return -1;
    PyObject *index = PyNumber_Index(value);
    if (!index) return -2;
    long long limit = PyLong_AsLongLong(index);
    Py_DECREF(index);
    if (PyErr_Occurred()) return -2;
    if (limit < 0 || limit > 10000000) {
        PyErr_SetString(PyExc_ValueError, "max_length must be in 0..10000000 or None."); return -2;
    }
    return limit;
}

static NSMutableDictionary *inputOptions(PyObject *value, bool creating) {
    if (!PyDict_Check(value)) { PyErr_SetString(PyExc_TypeError, "Text input options must be a dict."); return nil; }
    // Own a snapshot before invoking any user-defined numeric conversion.
    PyObject *copy = PyDict_Copy(value);
    if (!copy) return nil;
    NSSet *strings = [NSSet setWithArray:@[@"text", @"placeholder", @"font"]];
    NSSet *booleans = [NSSet setWithArray:@[@"enabled", @"read_only", @"multiline", @"secure", @"autocorrection",
        @"spell_check", @"avoid_keyboard", @"select_all_on_focus"]];
    NSSet *positive = [NSSet setWithArray:@[@"width", @"height", @"font_size"]];
    NSSet *nonnegative = [NSSet setWithArray:@[@"border_width", @"corner_radius"]];
    NSSet *colors = [NSSet setWithArray:@[@"text_color", @"placeholder_color", @"background", @"border_color"]];
    NSDictionary *choices = @{
        @"alignment":@[@"natural", @"left", @"center", @"right"],
        @"keyboard_type":@[@"default", @"ascii", @"number", @"decimal", @"phone", @"email", @"url"],
        @"return_key":@[@"default", @"done", @"go", @"search", @"send", @"next"],
        @"autocapitalization":@[@"none", @"sentences", @"words", @"all"],
        @"content_type":@[NSNull.null, @"name", @"username", @"password", @"new_password", @"one_time_code", @"email", @"telephone", @"url"],
        @"keyboard_appearance":@[@"default", @"light", @"dark"],
        @"submit_behavior":@[@"blur", @"stay", @"next", @"newline"]};
    NSMutableSet *keys = [NSMutableSet new];
    for (NSSet *set in @[strings, booleans, positive, nonnegative, colors]) [keys unionSet:set];
    [keys addObjectsFromArray:choices.allKeys];
    [keys addObjectsFromArray:@[@"selection", @"max_length", @"padding"]];
    NSMutableDictionary *result = [NSMutableDictionary new];
    PyObject *key, *item;
    Py_ssize_t pos = 0;
    while (PyDict_Next(copy, &pos, &key, &item)) {
        NSString *name = inputString(key);
        if (!name) break;
        if (![keys containsObject:name] || (!creating && ([name isEqual:@"multiline"] || [name isEqual:@"secure"]))) {
            PyErr_Format(PyExc_ValueError, "Unknown or immutable text input option: %s", name.UTF8String); break;
        }
        id converted = nil;
        if ([strings containsObject:name]) converted = item == Py_None && [name isEqual:@"font"] ? NSNull.null : inputString(item);
        else if ([booleans containsObject:name]) {
            if (!PyBool_Check(item)) PyErr_SetString(PyExc_TypeError, "Boolean options require bool values.");
            else converted = @(item == Py_True);
        } else if ([positive containsObject:name] || [nonnegative containsObject:name]) {
            double number = PyFloat_AsDouble(item);
            if (!PyErr_Occurred()) {
                if (!std::isfinite(number) || number < 0 || number > 1e7 || ([positive containsObject:name] && number == 0))
                    PyErr_SetString(PyExc_ValueError, "Invalid text input dimension.");
                else converted = @(number);
            }
        } else if ([colors containsObject:name] || [name isEqual:@"padding"]) {
            converted = inputNumbers(item, 4);
            if (converted) for (NSNumber *number in converted) {
                if (number.doubleValue < 0 || number.doubleValue > 1e7 || ([colors containsObject:name] && number.doubleValue > 1)) {
                    PyErr_SetString(PyExc_ValueError, "Colors must be in 0..1; padding must be non-negative."); break;
                }
            }
        } else if ([name isEqual:@"selection"]) {
            converted = inputNumbers(item, 2, true);
            if (converted && [converted[0] unsignedLongLongValue] > [converted[1] unsignedLongLongValue])
                PyErr_SetString(PyExc_ValueError, "Selection start must not exceed its end.");
        } else if ([name isEqual:@"max_length"]) {
            NSInteger limit = inputLimit(item);
            if (limit != -2) converted = limit < 0 ? NSNull.null : @(limit);
        } else {
            converted = item == Py_None ? NSNull.null : inputString(item);
            if (converted && ![choices[name] containsObject:converted])
                PyErr_Format(PyExc_ValueError, "Invalid %s option.", name.UTF8String);
        }
        if (PyErr_Occurred() || !converted) break;
        result[name] = converted;
    }
    if (!PyErr_Occurred() && creating && result.count != keys.count)
        PyErr_SetString(PyExc_ValueError, "Missing text input configuration options.");
    Py_DECREF(copy);
    return PyErr_Occurred() ? nil : result;
}

static NSString *inputApplyOptions(CocoaPySceneTextInput *input, NSDictionary *changes) {
    NSMutableDictionary *options = [input.options mutableCopy];
    [options addEntriesFromDictionary:changes];
    if ([options[@"secure"] boolValue] && [options[@"multiline"] boolValue]) return @"Secure multiline input is unsupported.";
    if (![options[@"multiline"] boolValue] && [options[@"submit_behavior"] isEqual:@"newline"])
        return @"The newline submit behavior requires TextView.";
    NSString *text = changes[@"text"] ?: input.textValue;
    BOOL replaceText = changes[@"text"] != nil || ![options[@"max_length"] isEqual:input.options[@"max_length"]];
    if (replaceText) text = inputNormalize(text, [options[@"multiline"] boolValue],
        options[@"max_length"] == NSNull.null ? -1 : [options[@"max_length"] integerValue]);
    NSRange selection = input.selectionRange;
    if (changes[@"selection"]) {
        NSUInteger start = inputUTF16Index(text, [changes[@"selection"][0] unsignedLongLongValue]);
        NSUInteger end = inputUTF16Index(text, [changes[@"selection"][1] unsignedLongLongValue]);
        if (start == NSNotFound || end == NSNotFound) return @"Selection exceeds the text length.";
        selection = NSMakeRange(start, end - start);
    } else {
        selection.location = MIN(selection.location, text.length);
        selection.length = MIN(selection.length, text.length - selection.location);
    }
    [options removeObjectForKey:@"text"];
    [options removeObjectForKey:@"selection"];
#if COCOA_PY_UIKIT
    BOOL reloadKeyboard = NO;
    for (NSString *key in @[@"keyboard_type", @"return_key", @"autocapitalization", @"autocorrection",
                            @"spell_check", @"content_type", @"keyboard_appearance"])
        if (![options[key] isEqual:input.options[key]]) reloadKeyboard = YES;
#endif
    input.options = options;
    BOOL wasFocused = input.focused;
    input.suppress = YES;
    @try {
        [input applyAppearance];
        if (replaceText) [input setTextValue:text];
        if (replaceText || changes[@"selection"]) [input setSelectionRange:selection];
        [input applyPlacement];
#if COCOA_PY_UIKIT
        if (reloadKeyboard && input.focused) [(input.field ?: input.textView) reloadInputViews];
#endif
    } @finally { input.suppress = NO; }
    if (wasFocused && !input.focused) [input enqueue:@"blur"];
    input.lastText = input.textValue;
    input.lastSelection = input.selectionRange;
    input.lastMarked = input.markedRange;
    // A programmatic update establishes a barrier against older queued edits.
    ++input.revision;
    return nil;
}

static PyObject *inputInvoke(long long handle, id (^operation)(CocoaPySceneTextInput *)) {
    __block id result = nil;
    __block NSString *error = nil;
    __block bool missing = false;
    runOnMainSync(^{ @autoreleasepool {
        CocoaPySceneTextInput *input = gTextInputs[@(handle)];
        if (!input || input.closed) { missing = true; return; }
        @try { result = operation(input); }
        @catch (NSException *exception) { error = exception.reason ?: @"Native text input failed."; }
    }});
    if (missing) { PyErr_SetString(PyExc_KeyError, "Text input handle not found."); return nullptr; }
    if (error) { PyErr_SetString(PyExc_RuntimeError, error.UTF8String); return nullptr; }
    return inputPython(result);
}

static PyObject *metal_text_input_normalize(PyObject *, PyObject *args) {
    PyObject *value, *maximum;
    int multiline;
    if (!PyArg_ParseTuple(args, "OpO", &value, &multiline, &maximum)) return nullptr;
    @autoreleasepool {
        NSString *text = inputString(value);
        if (!text) return nullptr;
        NSInteger limit = inputLimit(maximum);
        if (limit == -2) return nullptr;
        return inputPython(inputNormalize(text, multiline, limit));
    }
}

static PyObject *metal_text_input_create(PyObject *, PyObject *args) {
    long long window;
    PyObject *config;
    int headless = 0;
    if (!PyArg_ParseTuple(args, "LO|p", &window, &config, &headless)) return nullptr;
    @autoreleasepool {
        NSDictionary *options = inputOptions(config, true);
        if (!options) return nullptr;
        __block long long handle = 0;
        __block NSString *error = nil;
        runOnMainSync(^{ @autoreleasepool {
            CocoaPyMetalSurfaceView *surface;
            { std::lock_guard<std::mutex> lock(gStateMutex);
                WindowRecord *record = windowRecord(window);
                surface = record ? record->controller.surfaceView : nil;
            }
            if (!surface) { error = @"Metal window handle not found."; return; }
            CocoaPySceneTextInput *input = [CocoaPySceneTextInput new];
            @try {
                input.options = [options mutableCopy];
                input.events = [NSMutableArray new];
                input.surface = surface;
                input.windowHandle = window;
                input.headless = headless;
                input.caretRect = CGRectMake(0, 0, 1, 20);
                input.placement = CGAffineTransformIdentity;
                input.opacity = 1;
                input.lastText = @"";
                input.lastMarked = NSMakeRange(NSNotFound, 0);
                input.suppress = YES;
                [input buildEditor];
                [NSNotificationCenter.defaultCenter addObserver:input selector:@selector(undoFinished:)
                    name:NSUndoManagerDidUndoChangeNotification object:nil];
                [NSNotificationCenter.defaultCenter addObserver:input selector:@selector(undoFinished:)
                    name:NSUndoManagerDidRedoChangeNotification object:nil];
                error = inputApplyOptions(input, options);
                if (error) { [input close]; return; }
                handle = input.handle = nextHandle();
                if (!gTextInputs) gTextInputs = [NSMutableDictionary new];
                gTextInputs[@(handle)] = input;
                ++gTextInputCount;
            } @catch (NSException *exception) { [input close]; error = exception.reason; }
        }});
        if (error) { PyErr_SetString(PyExc_ValueError, error.UTF8String); return nullptr; }
        return PyLong_FromLongLong(handle);
    }
}

static PyObject *metal_text_input_update(PyObject *, PyObject *args) {
    long long handle;
    PyObject *config;
    if (!PyArg_ParseTuple(args, "LO", &handle, &config)) return nullptr;
    @autoreleasepool {
        NSDictionary *options = inputOptions(config, false);
        if (!options) return nullptr;
        __block NSString *validation = nil;
        PyObject *result = inputInvoke(handle, ^id(CocoaPySceneTextInput *input) {
            validation = inputApplyOptions(input, options);
            return input.state;
        });
        if (validation) { Py_XDECREF(result); PyErr_SetString(PyExc_ValueError, validation.UTF8String); return nullptr; }
        return result;
    }
}

static PyObject *metal_text_input_command(PyObject *, PyObject *args) {
    long long handle;
    const char *name;
    PyObject *value = Py_None;
    if (!PyArg_ParseTuple(args, "Ls|O", &handle, &name, &value)) return nullptr;
    @autoreleasepool {
        NSString *command = @(name);
        if (![@[@"focus", @"blur", @"commit", @"insert", @"undo", @"redo"] containsObject:command]) {
            PyErr_SetString(PyExc_ValueError, "Unknown text input command."); return nullptr;
        }
        NSString *text = [command isEqual:@"insert"] ? inputString(value) : nil;
        if (PyErr_Occurred()) return nullptr;
        int all = [command isEqual:@"focus"] ? PyObject_IsTrue(value) : 0;
        if (all < 0) return nullptr;
        return inputInvoke(handle, ^id(CocoaPySceneTextInput *input) {
            if ([command isEqual:@"focus"]) [input beginEditing:all];
            else if ([command isEqual:@"blur"]) [input endEditing];
            else if ([command isEqual:@"commit"]) { [input finishComposition]; [input changed]; }
            else if ([input.options[@"enabled"] boolValue] && ![input.options[@"read_only"] boolValue]) {
                if ([command isEqual:@"insert"]) {
                    NSString *replacement = inputNormalize(text, [input.options[@"multiline"] boolValue], -1);
                    [input replaceRange:input.selectionRange withString:replacement];
                } else if ([command isEqual:@"undo"] && input.editorUndoManager.canUndo) [input.editorUndoManager undo];
                else if ([command isEqual:@"redo"] && input.editorUndoManager.canRedo) [input.editorUndoManager redo];
                [input changed];
            }
            return input.state;
        });
    }
}

static PyObject *metal_text_input_state(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    return inputInvoke(handle, ^id(CocoaPySceneTextInput *input) { return input.state; });
}

static PyObject *metal_text_input_frame(PyObject *, PyObject *args) {
    long long handle;
    PyObject *matrix;
    double opacity;
    int visible;
    if (!PyArg_ParseTuple(args, "LOdp", &handle, &matrix, &opacity, &visible)) return nullptr;
    @autoreleasepool {
        NSArray *values = inputNumbers(matrix, 6);
        if (!values) return nullptr;
        if (!std::isfinite(opacity) || opacity < 0 || opacity > 1) {
            PyErr_SetString(PyExc_ValueError, "Opacity must be finite and in 0..1."); return nullptr;
        }
        CGAffineTransform t = CGAffineTransformMake([values[0] doubleValue], [values[1] doubleValue],
            [values[2] doubleValue], [values[3] doubleValue], [values[4] doubleValue], [values[5] doubleValue]);
        __block bool invalid = false;
        PyObject *result = inputInvoke(handle, ^id(CocoaPySceneTextInput *input) {
            CGFloat w = [input.options[@"width"] doubleValue], h = [input.options[@"height"] doubleValue];
            double extentX = fabs(t.tx) + fabs(t.a) * w / 2 + fabs(t.c) * h / 2;
            double extentY = fabs(t.ty) + fabs(t.b) * w / 2 + fabs(t.d) * h / 2;
            if (!std::isfinite(extentX) || !std::isfinite(extentY) || extentX > 1e7 || extentY > 1e7) {
                invalid = true; return nil;
            }
            input.placement = t;
            input.opacity = opacity;
            input.shown = visible && opacity > 0.001 && std::isfinite(t.a * t.d - t.b * t.c) && t.a * t.d != t.b * t.c;
            if (!input.shown) [input endEditing];
            [input applyPlacement];
            return nil;
        });
        if (invalid) {
            Py_XDECREF(result); PyErr_SetString(PyExc_ValueError, "Text input placement exceeds supported coordinates."); return nullptr;
        }
        return result;
    }
}

static PyObject *metal_text_input_anchor(PyObject *, PyObject *args) {
    long long handle;
    PyObject *rect;
    if (!PyArg_ParseTuple(args, "LO", &handle, &rect)) return nullptr;
    @autoreleasepool {
        NSArray *values = inputNumbers(rect, 4);
        if (!values) return nullptr;
        double x = [values[0] doubleValue], y = [values[1] doubleValue];
        double w = [values[2] doubleValue], h = [values[3] doubleValue];
        if (w <= 0 || h <= 0 || fabs(x) + w > 1e7 || fabs(y) + h > 1e7) {
            PyErr_SetString(PyExc_ValueError, "Invalid input-method caret rectangle."); return nullptr;
        }
        __block bool invalid = false;
        PyObject *result = inputInvoke(handle, ^id(CocoaPySceneTextInput *input) {
            if (!input.headless) { invalid = true; return nil; }
            input.caretRect = CGRectMake(x, y, w, h);
#if !COCOA_PY_UIKIT
            [input.textView.inputContext invalidateCharacterCoordinates];
#endif
            return nil;
        });
        if (invalid) {
            Py_XDECREF(result); PyErr_SetString(PyExc_ValueError, "Caret anchors require an input session."); return nullptr;
        }
        return result;
    }
}

static PyObject *metal_text_input_events(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    return inputInvoke(handle, ^id(CocoaPySceneTextInput *input) {
        NSArray *events = [input.events copy];
        [input.events removeAllObjects];
        return events;
    });
}

static PyObject *metal_text_input_close(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    runOnMainSync(^{
        CocoaPySceneTextInput *input = gTextInputs[@(handle)];
        if (!input) return;
        [input close];
        [gTextInputs removeObjectForKey:@(handle)];
        --gTextInputCount;
    });
    Py_RETURN_NONE;
}

static PyObject *metal_text_input_keyboard(PyObject *, PyObject *args) {
    long long window;
    if (!PyArg_ParseTuple(args, "L", &window)) return nullptr;
#if COCOA_PY_UIKIT
    __block CGRect frame = CGRectNull;
    runOnMainSync(^{
        for (CocoaPySceneTextInput *input in gTextInputs.allValues) {
            if (input.windowHandle == window && input.focused) { frame = input.keyboardFrame; break; }
        }
    });
    if (!CGRectIsNull(frame) && !CGRectIsEmpty(frame))
        return Py_BuildValue("(dddd)", frame.origin.x, frame.origin.y, frame.size.width, frame.size.height);
#endif
    Py_RETURN_NONE;
}
