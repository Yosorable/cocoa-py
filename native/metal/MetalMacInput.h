#pragma once

@implementation CocoaPyMetalSurfaceView (Input)
- (void)cocoaPointer:(NSEvent *)event phase:(int)phase {
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gWindows.find(self.windowHandle);
    if (it == gWindows.end()) return;
    auto &record = it->second;
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
    if (record.touchQueue.size() >= 4096) record.touchQueue.erase(record.touchQueue.begin());
    record.touchQueue.push_back({phase, identifier, point.x, point.y, previous.x, previous.y, event.timestamp});
    if (phase >= 2) record.touchIdMap.erase(key);
}
- (void)mouseDown:(NSEvent *)event { [self cocoaPointer:event phase:0]; }
- (void)mouseDragged:(NSEvent *)event { [self cocoaPointer:event phase:1]; }
- (void)mouseUp:(NSEvent *)event { [self cocoaPointer:event phase:2]; }
- (void)rightMouseDown:(NSEvent *)event { [self cocoaPointer:event phase:0]; }
- (void)rightMouseDragged:(NSEvent *)event { [self cocoaPointer:event phase:1]; }
- (void)rightMouseUp:(NSEvent *)event { [self cocoaPointer:event phase:2]; }
- (void)keyDown:(NSEvent *)event {
    if (event.keyCode == 53) {
        CocoaPyMetalViewController *controller = (CocoaPyMetalViewController *)self.window.contentViewController;
        controller.closePressCount += 1;
    } else [super keyDown:event];
}
@end
