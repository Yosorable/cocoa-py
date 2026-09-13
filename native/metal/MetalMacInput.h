#pragma once
#import <IOKit/hidsystem/IOLLEvent.h>

// AppKit virtual key positions (HIToolbox/Events.h) to Keyboard/Keypad HID usages.
static unsigned metalMacHID(unsigned code) {
    static const unsigned codes[128] = {
        4,22,7,9,11,10,29,27,6,25,100,5,20,26,8,21,
        28,23,30,31,32,33,35,34,46,38,36,45,37,39,48,18,
        24,47,12,19,40,15,13,52,14,51,49,54,56,17,16,55,
        43,44,53,42,0,41,231,227,225,57,226,224,229,230,228,0,
        108,99,0,85,0,87,0,83,128,129,127,84,88,0,86,109,
        110,103,98,89,90,91,92,93,94,95,111,96,97,137,135,133,
        62,63,64,60,65,66,145,68,144,104,107,105,0,67,0,69,
        0,106,73,74,75,76,61,77,59,78,58,80,79,81,82,0,
    };
    return code < 128 ? codes[code] : 0;
}

static unsigned metalMacModifiers(NSEventModifierFlags flags) {
    return ((flags & NSEventModifierFlagShift) ? 1 : 0) |
           ((flags & NSEventModifierFlagControl) ? 2 : 0) |
           ((flags & NSEventModifierFlagOption) ? 4 : 0) |
           ((flags & NSEventModifierFlagCommand) ? 8 : 0) |
           ((flags & NSEventModifierFlagCapsLock) ? 16 : 0) |
           ((flags & NSEventModifierFlagFunction) ? 32 : 0) |
           ((flags & NSEventModifierFlagNumericPad) ? 64 : 0);
}

@implementation CocoaPyMetalSurfaceView (Input)
- (void)cocoaPointer:(NSEvent *)event phase:(int)phase {
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gWindows.find(self.windowHandle);
    if (it == gWindows.end()) return;
    auto &record = it->second;
    if (!record.active || !record.foreground || !record.focused) return;
    void *key = reinterpret_cast<void *>(static_cast<uintptr_t>(event.buttonNumber + 1));
    long long identifier;
    if (phase == 0) record.touchIdMap[key] = identifier = record.nextTouchId++;
    else {
        auto touch = record.touchIdMap.find(key);
        if (touch == record.touchIdMap.end()) return;
        identifier = touch->second;
    }
    NSPoint point = [self convertPoint:event.locationInWindow fromView:nil];
    NSPoint previous = phase == 0 ? point : self.previousPointer;
    self.previousPointer = point;
    if (record.touchQueue.size() >= 4096) { metalResetInput(record, "overflow"); return; }
    record.touchQueue.push_back({phase, identifier, point.x, point.y, previous.x, previous.y, event.timestamp, record.inputEpoch});
    if (phase >= 2) record.touchIdMap.erase(key);
}
- (void)mouseDown:(NSEvent *)event { [self cocoaPointer:event phase:0]; }
- (void)mouseDragged:(NSEvent *)event { [self cocoaPointer:event phase:1]; }
- (void)mouseUp:(NSEvent *)event { [self cocoaPointer:event phase:2]; }
- (void)rightMouseDown:(NSEvent *)event { [self cocoaPointer:event phase:0]; }
- (void)rightMouseDragged:(NSEvent *)event { [self cocoaPointer:event phase:1]; }
- (void)rightMouseUp:(NSEvent *)event { [self cocoaPointer:event phase:2]; }
- (void)scrollWheel:(NSEvent *)event {
    NSPoint point = [self convertPoint:event.locationInWindow fromView:nil];
    double unit = event.hasPreciseScrollingDeltas ? 1.0 : 16.0;
    ScrollEvent sample = {point.x, point.y, -event.scrollingDeltaX * unit,
        -event.scrollingDeltaY * unit, event.timestamp,
        (bool)event.hasPreciseScrollingDeltas, event.momentumPhase != NSEventPhaseNone};
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto window = gWindows.find(self.windowHandle);
    if (window == gWindows.end()) return;
    if (!window->second.active || !window->second.foreground || !window->second.focused) return;
    sample.epoch = window->second.inputEpoch;
    auto &queue = window->second.scrollQueue;
    if (queue.size() >= 4096) queue.erase(queue.begin());
    queue.push_back(sample);
}
- (void)keyDown:(NSEvent *)event {
    if (metalQueueKey(self.windowHandle, metalMacHID(event.keyCode), event.charactersIgnoringModifiers,
                      metalMacModifiers(event.modifierFlags), true, event.isARepeat, false, event.timestamp)) return;
    if (event.keyCode == 53) {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gWindows.find(self.windowHandle);
        if (it != gWindows.end() && !it->second.keyboardEvents) it->second.controller.closePressCount += 1;
    } else [super keyDown:event];
}
- (void)keyUp:(NSEvent *)event {
    if (!metalQueueKey(self.windowHandle, metalMacHID(event.keyCode), event.charactersIgnoringModifiers,
                       metalMacModifiers(event.modifierFlags), false, false, false, event.timestamp)) [super keyUp:event];
}
- (void)flagsChanged:(NSEvent *)event {
    unsigned code = metalMacHID(event.keyCode), flags = (unsigned)event.modifierFlags;
    unsigned mask = 0;
    switch (code) {
        case 224: mask = NX_DEVICELCTLKEYMASK; break;
        case 225: mask = NX_DEVICELSHIFTKEYMASK; break;
        case 226: mask = NX_DEVICELALTKEYMASK; break;
        case 227: mask = NX_DEVICELCMDKEYMASK; break;
        case 228: mask = NX_DEVICERCTLKEYMASK; break;
        case 229: mask = NX_DEVICERSHIFTKEYMASK; break;
        case 230: mask = NX_DEVICERALTKEYMASK; break;
        case 231: mask = NX_DEVICERCMDKEYMASK; break;
    }
    unsigned modifiers = metalMacModifiers(event.modifierFlags);
    if (code == 57) {
        metalQueueKey(self.windowHandle, code, @"", modifiers, true, false, false, event.timestamp);
        metalQueueKey(self.windowHandle, code, @"", modifiers, false, false, false, event.timestamp);
    } else if (mask) metalQueueKey(self.windowHandle, code, @"", modifiers, (flags & mask) != 0, false, false, event.timestamp);
    else [super flagsChanged:event];
}
@end
