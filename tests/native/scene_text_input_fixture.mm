// Drive actual AppKit editors without exposing test hooks in the runtime.
#include <Python.h>
#import <AppKit/AppKit.h>

@interface NSObject (SceneTextInputFixture)
- (NSDictionary *)state;
- (void)changed;
- (void)submit;
@end

static id findOwner(NSView *view, long long handle) {
    if ([NSStringFromClass(view.class) isEqual:@"CocoaPyInputHost"]) {
        id owner = [view valueForKey:@"inputOwner"];
        if ([[owner valueForKey:@"handle"] longLongValue] == handle) return owner;
    }
    for (NSView *child in view.subviews) {
        id owner = findOwner(child, handle);
        if (owner) return owner;
    }
    return nil;
}

static PyObject *perform(PyObject *, PyObject *args) {
    long long handle;
    const char *command, *text = "";
    Py_ssize_t length = 0;
    unsigned long long start = 0, count = 0;
    if (!PyArg_ParseTuple(args, "Ls|s#KK", &handle, &command, &text, &length, &start, &count)) return nullptr;
    @autoreleasepool {
        NSString *action = @(command);
        NSString *value = [[NSString alloc] initWithBytes:text length:length encoding:NSUTF8StringEncoding];
        __block NSString *error = nil, *json = nil;
        dispatch_block_t work = ^{
            @try {
                NSObject *owner = nil;
                for (NSWindow *window in NSApp.windows) {
                    owner = findOwner(window.contentView, handle);
                    if (owner) break;
                }
                if (!owner) { error = @"Editor not found"; return; }
                NSTextField *field = [owner valueForKey:@"field"];
                NSTextView *editor = [owner valueForKey:@"textView"] ?: (NSTextView *)field.currentEditor;
                NSView *host = [owner valueForKey:@"host"];
                if ([action isEqual:@"insert"]) [editor insertText:value replacementRange:NSMakeRange(NSNotFound, 0)];
                else if ([action isEqual:@"marked"]) [editor setMarkedText:value selectedRange:NSMakeRange(start, count)
                    replacementRange:NSMakeRange(NSNotFound, 0)];
                else if ([action isEqual:@"commit"]) [editor unmarkText];
                else if ([action isEqual:@"selection"]) editor.selectedRange = NSMakeRange(start, count);
                else if ([action isEqual:@"command"]) [editor doCommandBySelector:NSSelectorFromString(value)];
                else if ([action isEqual:@"submit"]) [owner submit];
                else if ([action isEqual:@"scroll"]) {
                    NSScrollView *scroll = [owner valueForKey:@"scrollView"];
                    [scroll.contentView scrollToPoint:NSMakePoint(0, start)];
                } else if (![action isEqual:@"inspect"]) { error = @"Unknown fixture command"; return; }
                NSMutableDictionary *state = [[owner state] mutableCopy];
                state[@"hidden"] = @(host.hidden);
                state[@"alpha"] = @(host.alphaValue);
                state[@"headless"] = [owner valueForKey:@"headless"];
                NSPoint hitPoint = [host convertPoint:NSMakePoint(10, 10) toView:host.superview];
                state[@"accepts_pointer"] = @([host hitTest:hitPoint] != nil);
                if ([[owner valueForKey:@"headless"] boolValue] && editor) {
                    NSRect caret = [editor firstRectForCharacterRange:NSMakeRange(0, 0) actualRange:nullptr];
                    caret = [host.window convertRectFromScreen:caret];
                    NSView *surface = [owner valueForKey:@"surface"];
                    caret = [surface convertRect:caret fromView:nil];
                    state[@"caret_rect"] = @[@(caret.origin.x), @(caret.origin.y), @(caret.size.width), @(caret.size.height)];
                }
                state[@"frame"] = @[@(host.frame.origin.x), @(host.frame.origin.y), @(host.frame.size.width), @(host.frame.size.height)];
                state[@"rotation"] = @(host.frameCenterRotation);
                state[@"first_responder"] = @(editor && host.window.firstResponder == editor);
                state[@"can_undo"] = @(editor.undoManager.canUndo);
                state[@"can_redo"] = @(editor.undoManager.canRedo);
                state[@"utf16_selection"] = @[@(editor.selectedRange.location), @(editor.selectedRange.length)];
                NSData *data = [NSJSONSerialization dataWithJSONObject:state options:0 error:nil];
                json = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
            } @catch (NSException *exception) { error = exception.reason; }
        };
        if (NSThread.isMainThread) work();
        else {
            Py_BEGIN_ALLOW_THREADS
            dispatch_sync(dispatch_get_main_queue(), work);
            Py_END_ALLOW_THREADS
        }
        if (error) { PyErr_SetString(PyExc_RuntimeError, error.UTF8String); return nullptr; }
        return PyUnicode_FromString(json.UTF8String);
    }
}

static PyMethodDef methods[] = {{"perform", perform, METH_VARARGS, nullptr}, {nullptr}};
static PyModuleDef module = {PyModuleDef_HEAD_INIT, "_scene_text_input_fixture", nullptr, -1, methods};
PyMODINIT_FUNC PyInit__scene_text_input_fixture(void) { return PyModule_Create(&module); }
