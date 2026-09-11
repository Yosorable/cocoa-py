#pragma once

#import <Foundation/Foundation.h>
#import <CoreFoundation/CoreFoundation.h>
#import <TargetConditionals.h>
#include <chrono>
#include <thread>
#include <utility>

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

// Optional host hooks. Embedded hosts may manage security-scoped bookmarks;
// desktop extensions use Foundation's scoped URL access when no hook is linked.
extern "C" void *CocoaPyBeginFileAccess(const char *) __attribute__((weak_import));
extern "C" void CocoaPyEndFileAccess(void *) __attribute__((weak_import));

struct CocoaPyFileAccess {
    void *token = nullptr;
    __strong NSURL *scopedURL = nil;
    explicit CocoaPyFileAccess(NSURL *url) {
        if (CocoaPyBeginFileAccess && CocoaPyEndFileAccess)
            token = CocoaPyBeginFileAccess(url.path.UTF8String);
        else if ([url startAccessingSecurityScopedResource])
            scopedURL = url;
    }
    void close() {
        if (auto value = std::exchange(token, nullptr)) CocoaPyEndFileAccess(value);
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
    using Clock = std::chrono::steady_clock;
    auto deadline = Clock::now() + std::chrono::duration<double>(seconds);
    do {
        if (dispatch_semaphore_wait(semaphore, DISPATCH_TIME_NOW) == 0) return true;
        if (NSThread.isMainThread) {
#if !COCOA_PY_UIKIT
            CocoaPyPumpEvents();
#endif
            auto result = CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.01, true);
            if (result == kCFRunLoopRunFinished) std::this_thread::sleep_for(std::chrono::milliseconds(1));
        } else if (dispatch_semaphore_wait(semaphore,
                    dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_MSEC)) == 0) {
            return true;
        }
    } while (Clock::now() < deadline);
    return dispatch_semaphore_wait(semaphore, DISPATCH_TIME_NOW) == 0;
}
