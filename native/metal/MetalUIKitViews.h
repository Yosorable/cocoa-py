#pragma once

@interface CocoaPyMetalSurfaceView : UIView
@property (nonatomic, assign) long long windowHandle;
@end

@implementation CocoaPyMetalSurfaceView
+ (Class)layerClass { return [CAMetalLayer class]; }
- (BOOL)canBecomeFirstResponder { return YES; }
@end

/* Overlay buttons animate scale and background on press. Keep the background
 * on the view: UIButtonConfiguration would own a separate highlight background. */
@interface CocoaPyOverlayButton : UIButton
@end
@implementation CocoaPyOverlayButton
- (void)setHighlighted:(BOOL)highlighted {
    [super setHighlighted:highlighted];
    [UIView animateWithDuration:0.12 delay:0
                        options:UIViewAnimationOptionBeginFromCurrentState | UIViewAnimationOptionAllowUserInteraction
                     animations:^{
        self.transform = highlighted ? CGAffineTransformMakeScale(0.88, 0.88) : CGAffineTransformIdentity;
        self.backgroundColor = [[UIColor blackColor] colorWithAlphaComponent:(highlighted ? 0.55 : 0.28)];
    } completion:nil];
}
@end

@interface CocoaPyMetalViewController : UIViewController
@property (nonatomic, strong) CocoaPyMetalSurfaceView *surfaceView;
@property (nonatomic, strong) UILabel *titleLabel;
@property (nonatomic, strong) UIButton *actionButton;
@property (nonatomic, strong) UIButton *closeButton;
@property (nonatomic, copy) NSString *windowTitle;
@property (nonatomic, assign) NSInteger actionPressCount;
@property (nonatomic, assign) NSInteger closePressCount;
@property (nonatomic, assign) long long layoutRevision;
@property (nonatomic, assign) dispatch_semaphore_t vsyncSemaphore;
@property (nonatomic, assign) UIInterfaceOrientationMask allowedOrientations;
- (instancetype)initWithTitle:(NSString *)windowTitle;
- (void)consumeActionCount:(int *)actionCount closeCount:(int *)closeCount;
- (void)vsyncFired;
@end

@implementation CocoaPyMetalViewController

- (instancetype)initWithTitle:(NSString *)windowTitle {
    self = [super init];
    if (self) {
        _windowTitle = [windowTitle copy];
        _layoutRevision = 1;
        _allowedOrientations = UIInterfaceOrientationMaskAll;
    }
    return self;
}

- (UIInterfaceOrientationMask)supportedInterfaceOrientations {
    return _allowedOrientations;
}

- (void)loadView {
    self.view = [[UIView alloc] initWithFrame:CGRectZero];
    self.view.backgroundColor = UIColor.blackColor;
}

- (void)viewDidLoad {
    [super viewDidLoad];

    _surfaceView = [[CocoaPyMetalSurfaceView alloc] initWithFrame:CGRectZero];
    _surfaceView.translatesAutoresizingMaskIntoConstraints = NO;
    _surfaceView.multipleTouchEnabled = YES;
    [self.view addSubview:_surfaceView];

    _titleLabel = nil;

    _actionButton = [CocoaPyOverlayButton buttonWithType:UIButtonTypeCustom];
    _actionButton.translatesAutoresizingMaskIntoConstraints = NO;
    _actionButton.hidden = YES;
    _actionButton.backgroundColor = [[UIColor blackColor] colorWithAlphaComponent:0.28];
    _actionButton.tintColor = [UIColor colorWithWhite:1.0 alpha:0.94];
    _actionButton.layer.cornerRadius = 15.0;
    _actionButton.layer.borderWidth = 0.75;
    _actionButton.layer.borderColor = [[UIColor whiteColor] colorWithAlphaComponent:0.14].CGColor;
    _actionButton.titleLabel.font = [UIFont systemFontOfSize:12 weight:UIFontWeightSemibold];
    [_actionButton setTitleColor:[UIColor colorWithWhite:1.0 alpha:0.94] forState:UIControlStateNormal];
    /* Keep text insets independent of UIButtonConfiguration. */
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    _actionButton.contentEdgeInsets = UIEdgeInsetsMake(7, 12, 7, 12);
#pragma clang diagnostic pop
    [_actionButton addTarget:self action:@selector(actionTapped) forControlEvents:UIControlEventTouchUpInside];
    [self.view addSubview:_actionButton];

    _closeButton = [CocoaPyOverlayButton buttonWithType:UIButtonTypeCustom];
    _closeButton.translatesAutoresizingMaskIntoConstraints = NO;
    if (@available(iOS 13.0, *)) {
        UIImageSymbolConfiguration *config = [UIImageSymbolConfiguration configurationWithPointSize:13 weight:UIImageSymbolWeightSemibold];
        UIImage *image = [UIImage systemImageNamed:@"xmark"];
        [_closeButton setImage:[image imageByApplyingSymbolConfiguration:config] forState:UIControlStateNormal];
    } else {
        [_closeButton setTitle:@"×" forState:UIControlStateNormal];
    }
    _closeButton.backgroundColor = [[UIColor blackColor] colorWithAlphaComponent:0.28];
    _closeButton.tintColor = [UIColor colorWithWhite:1.0 alpha:0.94];
    _closeButton.layer.cornerRadius = 15.0;
    _closeButton.layer.borderWidth = 0.75;
    _closeButton.layer.borderColor = [[UIColor whiteColor] colorWithAlphaComponent:0.14].CGColor;
    _closeButton.titleLabel.font = [UIFont systemFontOfSize:13 weight:UIFontWeightSemibold];
    [_closeButton addTarget:self action:@selector(closeTapped) forControlEvents:UIControlEventTouchUpInside];
    [self.view addSubview:_closeButton];

    UILayoutGuide *safe = self.view.safeAreaLayoutGuide;
    [NSLayoutConstraint activateConstraints:@[
        [_surfaceView.leadingAnchor constraintEqualToAnchor:self.view.leadingAnchor],
        [_surfaceView.trailingAnchor constraintEqualToAnchor:self.view.trailingAnchor],
        [_surfaceView.topAnchor constraintEqualToAnchor:self.view.topAnchor],
        [_surfaceView.bottomAnchor constraintEqualToAnchor:self.view.bottomAnchor],
        [_closeButton.trailingAnchor constraintEqualToAnchor:safe.trailingAnchor constant:-14],
        [_closeButton.topAnchor constraintEqualToAnchor:safe.topAnchor constant:8],
        [_closeButton.widthAnchor constraintEqualToConstant:30],
        [_closeButton.heightAnchor constraintEqualToConstant:30],
        [_actionButton.trailingAnchor constraintEqualToAnchor:_closeButton.leadingAnchor constant:-8],
        [_actionButton.centerYAnchor constraintEqualToAnchor:_closeButton.centerYAnchor],
        [_actionButton.heightAnchor constraintEqualToConstant:30],
    ]];
}

- (void)viewDidLayoutSubviews {
    [super viewDidLayoutSubviews];
    self.layoutRevision += 1;
}

- (void)actionTapped {
    self.actionPressCount += 1;
}

- (void)closeTapped {
    self.closePressCount += 1;
}

- (void)consumeActionCount:(int *)actionCount closeCount:(int *)closeCount {
    if (actionCount != nullptr) {
        *actionCount = (int)self.actionPressCount;
    }
    if (closeCount != nullptr) {
        *closeCount = (int)self.closePressCount;
    }
    self.actionPressCount = 0;
    self.closePressCount = 0;
}

- (void)vsyncFired {
    if (self.vsyncSemaphore) {
        dispatch_semaphore_signal(self.vsyncSemaphore);
    }
}

@end
