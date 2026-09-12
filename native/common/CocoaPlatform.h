#pragma once

#import <Foundation/Foundation.h>
#import <CoreFoundation/CoreFoundation.h>
#import <TargetConditionals.h>
#include <chrono>
#include <thread>
#include <utility>
#include <dlfcn.h>

#define COCOA_PY_UIKIT (TARGET_OS_IOS || TARGET_OS_TV || TARGET_OS_VISION)

#if !COCOA_PY_UIKIT
#import <AppKit/AppKit.h>

static inline void CocoaPyPrepareApplication() {
    if (!NSApp) {
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
        [NSApp finishLaunching];
    }
    [NSApp activate];
}

static inline void CocoaPyPumpEvents() {
    if (!NSThread.isMainThread || !NSApp) return;
    @autoreleasepool {
        for (unsigned i = 0; i < 128; ++i) {
            NSEvent *event = [NSApp nextEventMatchingMask:NSEventMaskAny untilDate:NSDate.distantPast
                                                 inMode:NSDefaultRunLoopMode dequeue:YES];
            if (!event) break;
            [NSApp sendEvent:event];
        }
        [NSApp updateWindows];
    }
}
#endif

// Resolve optional host hooks at runtime. Wheels must not link against a host
// application's symbols. Hosts export both functions before using the library.
struct CocoaPyFileAccessHooks {
    using Begin = void *(*)(const char *);
    using End = void (*)(void *);
    Begin begin;
    End end;
};

static inline const CocoaPyFileAccessHooks &CocoaPyGetFileAccessHooks() {
    static const CocoaPyFileAccessHooks hooks = {
        reinterpret_cast<CocoaPyFileAccessHooks::Begin>(dlsym(RTLD_DEFAULT, "CocoaPyBeginFileAccess")),
        reinterpret_cast<CocoaPyFileAccessHooks::End>(dlsym(RTLD_DEFAULT, "CocoaPyEndFileAccess")),
    };
    return hooks;
}

struct CocoaPyFileAccess {
    void *token = nullptr;
    CocoaPyFileAccessHooks::End end = nullptr;
    __strong NSURL *scopedURL = nil;
    explicit CocoaPyFileAccess(NSURL *url) {
        const auto &hooks = CocoaPyGetFileAccessHooks();
        if (hooks.begin && hooks.end) {
            end = hooks.end;
            token = hooks.begin(url.path.UTF8String);
        } else if ([url startAccessingSecurityScopedResource])
            scopedURL = url;
    }
    void close() {
        if (auto value = std::exchange(token, nullptr)) end(value);
        if (scopedURL) { [scopedURL stopAccessingSecurityScopedResource]; scopedURL = nil; }
    }
    ~CocoaPyFileAccess() { close(); }
    CocoaPyFileAccess(const CocoaPyFileAccess &) = delete;
    CocoaPyFileAccess &operator=(const CocoaPyFileAccess &) = delete;
};

static inline void CocoaPyRunOnMain(dispatch_block_t block) {
    if (NSThread.isMainThread) block();
    else dispatch_sync(dispatch_get_main_queue(), block);
}

// Call without the GIL. A desktop Python script normally occupies the main
// thread, so asynchronous Apple APIs need its run loop serviced while waiting.
static inline bool CocoaPyWaitSemaphore(dispatch_semaphore_t semaphore, double seconds) {
    // Background threads need no run-loop pumping or intermediate timeouts.
    if (!NSThread.isMainThread)
        return dispatch_semaphore_wait(semaphore,
            dispatch_time(DISPATCH_TIME_NOW, (int64_t)(seconds * NSEC_PER_SEC))) == 0;

    using Clock = std::chrono::steady_clock;
    auto deadline = Clock::now() + std::chrono::duration<double>(seconds);
    do {
        if (dispatch_semaphore_wait(semaphore, DISPATCH_TIME_NOW) == 0) return true;
#if !COCOA_PY_UIKIT
        CocoaPyPumpEvents();
#endif
        auto result = CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.01, true);
        if (result == kCFRunLoopRunFinished) std::this_thread::sleep_for(std::chrono::milliseconds(1));
    } while (Clock::now() < deadline);
    return dispatch_semaphore_wait(semaphore, DISPATCH_TIME_NOW) == 0;
}
