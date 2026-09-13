// Exercise AppKit input adapters directly, without posting system events.
#include <Python.h>
#import <AppKit/AppKit.h>

@interface CocoaPyFixtureScrollEvent : NSEvent
@property(nonatomic) NSPoint fixtureLocation;
@property(nonatomic) double fixtureDeltaX, fixtureDeltaY;
@end
@implementation CocoaPyFixtureScrollEvent
- (NSPoint)locationInWindow { return self.fixtureLocation; }
- (CGFloat)scrollingDeltaX { return self.fixtureDeltaX; }
- (CGFloat)scrollingDeltaY { return self.fixtureDeltaY; }
- (BOOL)hasPreciseScrollingDeltas { return YES; }
- (NSEventPhase)momentumPhase { return NSEventPhaseNone; }
- (NSTimeInterval)timestamp { return NSProcessInfo.processInfo.systemUptime; }
@end

static NSView *findSurface(NSView *view, long long handle) {
    if ([NSStringFromClass(view.class) isEqual:@"CocoaPyMetalSurfaceView"] &&
        [[view valueForKey:@"windowHandle"] longLongValue] == handle) return view;
    for (NSView *child in view.subviews) {
        NSView *surface = findSurface(child, handle);
        if (surface) return surface;
    }
    return nil;
}

static PyObject *scroll(PyObject *, PyObject *args) {
    long long handle;
    double x, y, dx, dy;
    if (!PyArg_ParseTuple(args, "Ldddd", &handle, &x, &y, &dx, &dy)) return nullptr;
    @autoreleasepool {
        __block NSString *error = nil;
        dispatch_block_t work = ^{
            @try {
                NSView *surface = nil;
                for (NSWindow *window in NSApp.windows) {
                    surface = findSurface(window.contentView, handle);
                    if (surface) break;
                }
                if (!surface) { error = @"Scene surface not found"; return; }
                CocoaPyFixtureScrollEvent *event = [CocoaPyFixtureScrollEvent new];
                event.fixtureLocation = [surface convertPoint:NSMakePoint(x, y) toView:nil];
                event.fixtureDeltaX = dx;
                event.fixtureDeltaY = dy;
                [surface scrollWheel:event];
            } @catch (NSException *exception) { error = exception.reason; }
        };
        if (NSThread.isMainThread) work();
        else {
            Py_BEGIN_ALLOW_THREADS
            dispatch_sync(dispatch_get_main_queue(), work);
            Py_END_ALLOW_THREADS
        }
        if (error) { PyErr_SetString(PyExc_RuntimeError, error.UTF8String); return nullptr; }
    }
    Py_RETURN_NONE;
}

static PyObject *keyboard(PyObject *, PyObject *args) {
    long long handle;
    unsigned code, flags;
    const char *phase, *characters;
    int repeat;
    if (!PyArg_ParseTuple(args, "LIssIp", &handle, &code, &phase, &characters, &flags, &repeat)) return nullptr;
    @autoreleasepool {
        __block NSString *error = nil;
        NSString *text = [NSString stringWithUTF8String:characters];
        dispatch_block_t work = ^{
            @try {
                NSView *surface = nil;
                for (NSWindow *window in NSApp.windows) {
                    surface = findSurface(window.contentView, handle);
                    if (surface) break;
                }
                if (!surface) { error = @"Scene surface not found"; return; }
                NSEventType type = !strcmp(phase, "down") ? NSEventTypeKeyDown : !strcmp(phase, "up") ? NSEventTypeKeyUp : NSEventTypeFlagsChanged;
                NSEvent *event = [NSEvent keyEventWithType:type location:NSZeroPoint modifierFlags:flags
                    timestamp:NSProcessInfo.processInfo.systemUptime windowNumber:surface.window.windowNumber
                    context:nil characters:text charactersIgnoringModifiers:text isARepeat:repeat keyCode:code];
                if (type == NSEventTypeKeyDown) [surface keyDown:event];
                else if (type == NSEventTypeKeyUp) [surface keyUp:event];
                else [surface flagsChanged:event];
            } @catch (NSException *exception) { error = exception.reason; }
        };
        if (NSThread.isMainThread) work();
        else {
            Py_BEGIN_ALLOW_THREADS
            dispatch_sync(dispatch_get_main_queue(), work);
            Py_END_ALLOW_THREADS
        }
        if (error) { PyErr_SetString(PyExc_RuntimeError, error.UTF8String); return nullptr; }
    }
    Py_RETURN_NONE;
}

static PyObject *lifecycle(PyObject *, PyObject *args) {
    long long handle;
    const char *kind;
    if (!PyArg_ParseTuple(args, "Ls", &handle, &kind)) return nullptr;
    @autoreleasepool {
        __block NSString *error = nil;
        dispatch_block_t work = ^{
            NSView *surface = nil;
            for (NSWindow *window in NSApp.windows) {
                surface = findSurface(window.contentView, handle);
                if (surface) break;
            }
            if (!surface) { error = @"Scene surface not found"; return; }
            NSNotificationName name;
            id object = surface.window;
            if (!strcmp(kind, "focus")) name = NSWindowDidBecomeKeyNotification;
            else if (!strcmp(kind, "blur")) name = NSWindowDidResignKeyNotification;
            else if (!strcmp(kind, "background")) name = NSWindowDidMiniaturizeNotification;
            else if (!strcmp(kind, "foreground")) name = NSWindowDidDeminiaturizeNotification;
            else if (!strcmp(kind, "active")) { name = NSApplicationDidBecomeActiveNotification; object = NSApp; }
            else if (!strcmp(kind, "inactive")) { name = NSApplicationWillResignActiveNotification; object = NSApp; }
            else { error = @"Unknown lifecycle event"; return; }
            [NSNotificationCenter.defaultCenter postNotificationName:name object:object];
        };
        if (NSThread.isMainThread) work();
        else {
            Py_BEGIN_ALLOW_THREADS
            dispatch_sync(dispatch_get_main_queue(), work);
            Py_END_ALLOW_THREADS
        }
        if (error) { PyErr_SetString(PyExc_RuntimeError, error.UTF8String); return nullptr; }
    }
    Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
    {"scroll", scroll, METH_VARARGS, nullptr},
    {"keyboard", keyboard, METH_VARARGS, nullptr},
    {"lifecycle", lifecycle, METH_VARARGS, nullptr}, {nullptr}};
static PyModuleDef module = {PyModuleDef_HEAD_INIT, "_scene_ui_fixture", nullptr, -1, methods};
PyMODINIT_FUNC PyInit__scene_ui_fixture(void) { return PyModule_Create(&module); }
