#pragma once

static unsigned metalUIKitModifiers(UIKeyModifierFlags flags) {
    return ((flags & UIKeyModifierShift) ? 1 : 0) |
           ((flags & UIKeyModifierControl) ? 2 : 0) |
           ((flags & UIKeyModifierAlternate) ? 4 : 0) |
           ((flags & UIKeyModifierCommand) ? 8 : 0) |
           ((flags & UIKeyModifierAlphaShift) ? 16 : 0) |
           ((flags & UIKeyModifierNumericPad) ? 64 : 0);
}

@implementation CocoaPyMetalSurfaceView (Keyboard)
- (NSSet<UIPress *> *)cocoaKeys:(NSSet<UIPress *> *)presses down:(BOOL)down cancelled:(BOOL)cancelled {
    NSMutableSet *unhandled = [NSMutableSet set];
    for (UIPress *press in presses) {
        UIKey *key = press.key;
        if (!key || !metalQueueKey(self.windowHandle, (unsigned)key.keyCode, key.charactersIgnoringModifiers,
                metalUIKitModifiers(key.modifierFlags), down, false, cancelled, press.timestamp)) [unhandled addObject:press];
    }
    return unhandled;
}
- (void)pressesBegan:(NSSet<UIPress *> *)presses withEvent:(UIPressesEvent *)event {
    NSSet *unhandled = [self cocoaKeys:presses down:YES cancelled:NO];
    if (unhandled.count) [super pressesBegan:unhandled withEvent:event];
}
- (void)pressesEnded:(NSSet<UIPress *> *)presses withEvent:(UIPressesEvent *)event {
    NSSet *unhandled = [self cocoaKeys:presses down:NO cancelled:NO];
    if (unhandled.count) [super pressesEnded:unhandled withEvent:event];
}
- (void)pressesCancelled:(NSSet<UIPress *> *)presses withEvent:(UIPressesEvent *)event {
    NSSet *unhandled = [self cocoaKeys:presses down:NO cancelled:YES];
    if (unhandled.count) [super pressesCancelled:unhandled withEvent:event];
}
@end
