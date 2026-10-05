#pragma once

@interface CocoaPyMetalSurfaceView : NSView
@property(nonatomic) long long windowHandle;
@property(nonatomic) NSPoint previousPointer;
@property(nonatomic, strong) NSTrackingArea *pointerTrackingArea;
@end

@implementation CocoaPyMetalSurfaceView
- (CALayer *)makeBackingLayer { return [CAMetalLayer layer]; }
- (BOOL)wantsUpdateLayer { return YES; }
- (BOOL)isFlipped { return YES; }
- (BOOL)acceptsFirstResponder { return YES; }
- (BOOL)acceptsFirstMouse:(NSEvent *)event { return YES; }
- (void)viewDidMoveToWindow {
    [super viewDidMoveToWindow];
    self.window.acceptsMouseMovedEvents = YES;
}
- (void)updateTrackingAreas {
    [super updateTrackingAreas];
    if (_pointerTrackingArea) [self removeTrackingArea:_pointerTrackingArea];
    _pointerTrackingArea = [[NSTrackingArea alloc] initWithRect:NSZeroRect
        options:NSTrackingMouseEnteredAndExited | NSTrackingMouseMoved | NSTrackingActiveInKeyWindow | NSTrackingInVisibleRect
        owner:self userInfo:nil];
    [self addTrackingArea:_pointerTrackingArea];
}
@end

@interface CocoaPyMetalViewController : NSViewController <NSWindowDelegate>
@property(nonatomic, strong) CocoaPyMetalSurfaceView *surfaceView;
@property(nonatomic, strong) NSButton *actionButton;
@property(nonatomic, copy) NSString *windowTitle;
@property(nonatomic) NSInteger actionPressCount, closePressCount;
@property(nonatomic) long long layoutRevision;
@property(nonatomic, strong) dispatch_semaphore_t vsyncSemaphore;
@property(nonatomic) BOOL fullscreenTarget, fullscreenTransition;
- (void)requestFullscreen:(BOOL)fullscreen;
- (instancetype)initWithTitle:(NSString *)title;
- (void)consumeActionCount:(int *)action closeCount:(int *)close;
- (void)vsyncFired;
@end

@implementation CocoaPyMetalViewController
- (instancetype)initWithTitle:(NSString *)title {
    if ((self = [super init])) { _windowTitle = [title copy]; _layoutRevision = 1; }
    return self;
}
- (void)loadView {
    _surfaceView = [[CocoaPyMetalSurfaceView alloc] initWithFrame:NSMakeRect(0, 0, 960, 640)];
    _surfaceView.wantsLayer = YES;
    self.view = _surfaceView;
    _actionButton = [NSButton buttonWithTitle:@"" target:self action:@selector(actionTapped:)];
    _actionButton.hidden = YES;
    _actionButton.bezelStyle = NSBezelStyleRounded;
    [self.view addSubview:_actionButton];
}
- (void)viewDidLayout {
    [super viewDidLayout];
    _layoutRevision += 1;
    [_actionButton sizeToFit];
    NSRect button = _actionButton.frame;
    button.origin = NSMakePoint(MAX(8, self.view.bounds.size.width - button.size.width - 12), 8);
    _actionButton.frame = button;
}
- (void)windowDidChangeBackingProperties:(NSNotification *)notification { _layoutRevision += 1; }
- (BOOL)windowShouldClose:(NSWindow *)sender { _closePressCount += 1; return NO; }
- (void)requestFullscreen:(BOOL)fullscreen {
    _fullscreenTarget = fullscreen;
    NSWindow *window = self.view.window;
    if (!window || window.delegate != self || _fullscreenTransition || ((window.styleMask & NSWindowStyleMaskFullScreen) != 0) == fullscreen) return;
    _fullscreenTransition = YES;
    [window toggleFullScreen:nil];
}
- (void)windowWillEnterFullScreen:(NSNotification *)notification {
    if (!_fullscreenTransition) _fullscreenTarget = YES;
    _fullscreenTransition = YES;
}
- (void)windowWillExitFullScreen:(NSNotification *)notification {
    if (!_fullscreenTransition) _fullscreenTarget = NO;
    _fullscreenTransition = YES;
}
- (void)windowDidEnterFullScreen:(NSNotification *)notification {
    _fullscreenTransition = NO;
    dispatch_async(dispatch_get_main_queue(), ^{ [self requestFullscreen:self.fullscreenTarget]; });
}
- (void)windowDidExitFullScreen:(NSNotification *)notification {
    _fullscreenTransition = NO;
    dispatch_async(dispatch_get_main_queue(), ^{ [self requestFullscreen:self.fullscreenTarget]; });
}
- (void)windowDidFailToEnterFullScreen:(NSWindow *)window {
    _fullscreenTransition = NO;
    _fullscreenTarget = (window.styleMask & NSWindowStyleMaskFullScreen) != 0;
}
- (void)windowDidFailToExitFullScreen:(NSWindow *)window {
    _fullscreenTransition = NO;
    _fullscreenTarget = (window.styleMask & NSWindowStyleMaskFullScreen) != 0;
}
- (void)actionTapped:(id)sender { _actionPressCount += 1; }
- (void)consumeActionCount:(int *)action closeCount:(int *)close {
    if (action) *action = (int)_actionPressCount;
    if (close) *close = (int)_closePressCount;
    _actionPressCount = _closePressCount = 0;
}
- (void)vsyncFired { if (_vsyncSemaphore) dispatch_semaphore_signal(_vsyncSemaphore); }
@end

// Used without the GIL and without gStateMutex. Plain desktop Python has no
// NSApplication.run() loop, so service AppKit events at scene frame boundaries.
static void metalMacPumpEvents() { CocoaPyPumpEvents(); }
