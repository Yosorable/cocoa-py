#pragma once

static UIButton *inputKeyboardButton(NSString *symbol, id target, SEL action) {
    UIButton *button = [UIButton buttonWithType:UIButtonTypeSystem];
    button.translatesAutoresizingMaskIntoConstraints = NO;
    button.tintColor = UIColor.labelColor;
    button.backgroundColor = UIColor.systemFillColor;
    button.layer.cornerRadius = 5;
    button.layer.shadowColor = UIColor.blackColor.CGColor;
    button.layer.shadowOpacity = 0.2;
    button.layer.shadowRadius = 0;
    button.layer.shadowOffset = CGSizeMake(0, 1);
    if (@available(iOS 26.0, *)) {
        button.layer.cornerRadius = 9;
        button.layer.shadowOpacity = 0.1;
        button.layer.shadowRadius = 5;
        button.layer.shadowOffset = CGSizeMake(0, 2);
    }
    UIImageSymbolConfiguration *configuration = [UIImageSymbolConfiguration configurationWithPointSize:14];
    [button setImage:[UIImage systemImageNamed:symbol withConfiguration:configuration] forState:UIControlStateNormal];
    [button addTarget:target action:action forControlEvents:UIControlEventTouchUpInside];
    [NSLayoutConstraint activateConstraints:@[
        [button.widthAnchor constraintEqualToConstant:32],
        [button.heightAnchor constraintEqualToConstant:42],
    ]];
    return button;
}

@implementation CocoaPyInputField
- (CGRect)caretRectForPosition:(UITextPosition *)position {
    CocoaPySceneTextInput *owner = self.inputOwner;
    return owner.headless ? [owner.surface convertRect:owner.caretRect toView:self] : [super caretRectForPosition:position];
}
- (CGRect)firstRectForRange:(UITextRange *)range {
    CocoaPySceneTextInput *owner = self.inputOwner;
    return owner.headless ? [owner.surface convertRect:owner.caretRect toView:self] : [super firstRectForRange:range];
}
- (void)setMarkedText:(NSString *)text selectedRange:(NSRange)selection {
    ++self.inputOwner.marking;
    [super setMarkedText:text selectedRange:selection];
    --self.inputOwner.marking;
    [self.inputOwner changed];
}
- (void)unmarkText {
    ++self.inputOwner.marking;
    [super unmarkText];
    --self.inputOwner.marking;
    [self.inputOwner changed];
}
- (void)insertText:(NSString *)text {
    BOOL composing = self.markedTextRange != nil;
    if (composing) ++self.inputOwner.marking;
    [super insertText:text];
    if (composing) --self.inputOwner.marking;
    [self.inputOwner changed];
}
@end

@implementation CocoaPyInputView
- (CGRect)caretRectForPosition:(UITextPosition *)position {
    CocoaPySceneTextInput *owner = self.inputOwner;
    return owner.headless ? [owner.surface convertRect:owner.caretRect toView:self] : [super caretRectForPosition:position];
}
- (CGRect)firstRectForRange:(UITextRange *)range {
    CocoaPySceneTextInput *owner = self.inputOwner;
    return owner.headless ? [owner.surface convertRect:owner.caretRect toView:self] : [super firstRectForRange:range];
}
- (void)setMarkedText:(NSString *)text selectedRange:(NSRange)selection {
    ++self.inputOwner.marking;
    [super setMarkedText:text selectedRange:selection];
    --self.inputOwner.marking;
    [self.inputOwner changed];
}
- (void)unmarkText {
    ++self.inputOwner.marking;
    [super unmarkText];
    --self.inputOwner.marking;
    [self.inputOwner changed];
}
- (void)insertText:(NSString *)text {
    BOOL composing = self.markedTextRange != nil;
    if (composing) ++self.inputOwner.marking;
    [super insertText:text];
    if (composing) --self.inputOwner.marking;
    [self.inputOwner changed];
}
@end

@implementation CocoaPyInputHost
- (BOOL)pointInside:(CGPoint)point withEvent:(UIEvent *)event {
    return !self.inputOwner.headless && [self.inputOwner containsClipPoint:point] && [super pointInside:point withEvent:event];
}
@end

@implementation CocoaPySceneTextInput (Platform)
- (void)buildEditor {
    self.host = [[CocoaPyInputHost alloc] initWithFrame:CGRectZero];
    self.host.inputOwner = self;
    self.host.clipsToBounds = YES;
    self.host.hidden = YES;
    self.host.userInteractionEnabled = !self.headless;
    [self.surface addSubview:self.host];
    if ([self.options[@"multiline"] boolValue]) {
        self.textView = [[CocoaPyInputView alloc] initWithFrame:CGRectZero];
        self.textView.inputOwner = self;
        self.textView.delegate = self;
        self.textView.backgroundColor = UIColor.clearColor;
        self.textView.textContainerInset = UIEdgeInsetsZero;
        self.textView.textContainer.lineFragmentPadding = 0;
        self.textView.contentInsetAdjustmentBehavior = UIScrollViewContentInsetAdjustmentNever;
        self.textView.keyboardDismissMode = UIScrollViewKeyboardDismissModeInteractive;
        self.textView.allowsEditingTextAttributes = NO;
        self.textView.dataDetectorTypes = UIDataDetectorTypeNone;
        [self.host addSubview:self.textView];
        self.placeholderLabel = [[UILabel alloc] initWithFrame:CGRectZero];
        self.placeholderLabel.numberOfLines = 0;
        self.placeholderLabel.userInteractionEnabled = NO;
        [self.host addSubview:self.placeholderLabel];
    } else {
        self.field = [[CocoaPyInputField alloc] initWithFrame:CGRectZero];
        self.field.inputOwner = self;
        self.field.delegate = self;
        self.field.backgroundColor = UIColor.clearColor;
        self.field.borderStyle = UITextBorderStyleNone;
        [self.field addTarget:self action:@selector(fieldChanged:) forControlEvents:UIControlEventEditingChanged];
        [self.host addSubview:self.field];
    }
    CGFloat toolbarHeight = 46;
    if (@available(iOS 26.0, *)) toolbarHeight = 66;
    UIInputView *toolbar = [[UIInputView alloc] initWithFrame:CGRectMake(0, 0, 0, toolbarHeight)
                                             inputViewStyle:UIInputViewStyleKeyboard];
    toolbar.backgroundColor = UIColor.clearColor;
    toolbar.autoresizingMask = UIViewAutoresizingFlexibleWidth;
    UIButton *dismiss = inputKeyboardButton(@"keyboard.chevron.compact.down", self, @selector(dismissInput:));
    [toolbar addSubview:dismiss];
    [NSLayoutConstraint activateConstraints:@[
        [dismiss.trailingAnchor constraintEqualToAnchor:toolbar.safeAreaLayoutGuide.trailingAnchor constant:-6],
        [dismiss.centerYAnchor constraintEqualToAnchor:toolbar.centerYAnchor],
    ]];
    if (!self.headless) {
        UIStackView *navigation = [[UIStackView alloc] initWithArrangedSubviews:@[
            inputKeyboardButton(@"chevron.up", self, @selector(previousInput:)),
            inputKeyboardButton(@"chevron.down", self, @selector(nextInput:)),
        ]];
        navigation.translatesAutoresizingMaskIntoConstraints = NO;
        navigation.spacing = 6;
        [toolbar addSubview:navigation];
        [NSLayoutConstraint activateConstraints:@[
            [navigation.leadingAnchor constraintEqualToAnchor:toolbar.safeAreaLayoutGuide.leadingAnchor constant:6],
            [navigation.centerYAnchor constraintEqualToAnchor:toolbar.centerYAnchor],
        ]];
    }
    if (self.field) self.field.inputAccessoryView = toolbar;
    else self.textView.inputAccessoryView = toolbar;
    NSValue *keyboard = gInputKeyboardFrames[@(self.windowHandle)];
    self.keyboardFrame = keyboard ? keyboard.CGRectValue : CGRectNull;
    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(keyboardChanged:)
        name:UIKeyboardWillChangeFrameNotification object:nil];
    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(keyboardChanged:)
        name:UIKeyboardWillHideNotification object:nil];
    [self applyAppearance];
}

- (void)applyAppearance {
    NSDictionary *o = self.options;
    CGFloat size = [o[@"font_size"] doubleValue];
    UIFont *font = [o[@"font"] isKindOfClass:NSString.class] ? [UIFont fontWithName:o[@"font"] size:size] : nil;
    font = font ?: [UIFont systemFontOfSize:size];
    self.host.backgroundColor = inputColor(o[@"background"]);
    self.host.layer.borderColor = inputColor(o[@"border_color"]).CGColor;
    self.host.layer.borderWidth = [o[@"border_width"] doubleValue];
    self.host.layer.cornerRadius = [o[@"corner_radius"] doubleValue];
    CGFloat width = [o[@"width"] doubleValue], height = [o[@"height"] doubleValue];
    NSArray *padding = o[@"padding"];
    CGRect inner = CGRectMake([padding[1] doubleValue], [padding[0] doubleValue],
        MAX(1, width - [padding[1] doubleValue] - [padding[3] doubleValue]),
        MAX(1, height - [padding[0] doubleValue] - [padding[2] doubleValue]));
    BOOL readOnly = [o[@"read_only"] boolValue], enabled = [o[@"enabled"] boolValue];
    id<UITextInputTraits> traits = self.field ? (id)self.field : (id)self.textView;
    NSDictionary *keyboards = @{@"default":@(UIKeyboardTypeDefault), @"ascii":@(UIKeyboardTypeASCIICapable),
        @"number":@(UIKeyboardTypeNumberPad), @"decimal":@(UIKeyboardTypeDecimalPad),
        @"phone":@(UIKeyboardTypePhonePad), @"email":@(UIKeyboardTypeEmailAddress), @"url":@(UIKeyboardTypeURL)};
    NSDictionary *returns = @{@"default":@(UIReturnKeyDefault), @"done":@(UIReturnKeyDone),
        @"go":@(UIReturnKeyGo), @"search":@(UIReturnKeySearch), @"send":@(UIReturnKeySend), @"next":@(UIReturnKeyNext)};
    NSDictionary *capitals = @{@"none":@(UITextAutocapitalizationTypeNone), @"sentences":@(UITextAutocapitalizationTypeSentences),
        @"words":@(UITextAutocapitalizationTypeWords), @"all":@(UITextAutocapitalizationTypeAllCharacters)};
    NSDictionary *appearances = @{@"default":@(UIKeyboardAppearanceDefault), @"light":@(UIKeyboardAppearanceLight), @"dark":@(UIKeyboardAppearanceDark)};
    NSDictionary *contents = @{@"name":UITextContentTypeName, @"username":UITextContentTypeUsername,
        @"password":UITextContentTypePassword, @"new_password":UITextContentTypeNewPassword,
        @"one_time_code":UITextContentTypeOneTimeCode, @"email":UITextContentTypeEmailAddress,
        @"telephone":UITextContentTypeTelephoneNumber, @"url":UITextContentTypeURL};
    traits.keyboardType = (UIKeyboardType)[keyboards[o[@"keyboard_type"]] integerValue];
    traits.returnKeyType = (UIReturnKeyType)[returns[o[@"return_key"]] integerValue];
    traits.autocapitalizationType = (UITextAutocapitalizationType)[capitals[o[@"autocapitalization"]] integerValue];
    traits.autocorrectionType = [o[@"autocorrection"] boolValue] ? UITextAutocorrectionTypeYes : UITextAutocorrectionTypeNo;
    traits.spellCheckingType = [o[@"spell_check"] boolValue] ? UITextSpellCheckingTypeYes : UITextSpellCheckingTypeNo;
    traits.keyboardAppearance = (UIKeyboardAppearance)[appearances[o[@"keyboard_appearance"]] integerValue];
    UIView *accessory = self.field ? self.field.inputAccessoryView : self.textView.inputAccessoryView;
    accessory.overrideUserInterfaceStyle = traits.keyboardAppearance == UIKeyboardAppearanceDark ? UIUserInterfaceStyleDark :
        (traits.keyboardAppearance == UIKeyboardAppearanceLight ? UIUserInterfaceStyleLight : UIUserInterfaceStyleUnspecified);
    traits.textContentType = o[@"content_type"] == NSNull.null ? nil : contents[o[@"content_type"]];
    if (self.field) {
        self.field.frame = inner;
        self.field.font = font;
        self.field.textColor = inputColor(o[@"text_color"]);
        self.field.tintColor = inputColor(o[@"text_color"]);
        self.field.textAlignment = inputAlignment(o[@"alignment"]);
        self.field.enabled = enabled;
        if (self.field.secureTextEntry != [o[@"secure"] boolValue])
            self.field.secureTextEntry = [o[@"secure"] boolValue];
        if (readOnly != (self.field.inputView != nil)) {
            self.field.inputView = readOnly ? [[UIView alloc] initWithFrame:CGRectZero] : nil;
            if (self.field.isFirstResponder) [self.field reloadInputViews];
        }
        self.field.attributedPlaceholder = [[NSAttributedString alloc] initWithString:o[@"placeholder"]
            attributes:@{NSForegroundColorAttributeName:inputColor(o[@"placeholder_color"]), NSFontAttributeName:font}];
        if ([o[@"secure"] boolValue]) {
            self.field.autocapitalizationType = UITextAutocapitalizationTypeNone;
            self.field.autocorrectionType = UITextAutocorrectionTypeNo;
            self.field.spellCheckingType = UITextSpellCheckingTypeNo;
        }
    } else {
        self.textView.frame = inner;
        self.textView.font = font;
        self.textView.textColor = inputColor(o[@"text_color"]);
        self.textView.tintColor = inputColor(o[@"text_color"]);
        self.textView.textAlignment = inputAlignment(o[@"alignment"]);
        self.textView.editable = enabled && !readOnly;
        self.textView.selectable = enabled;
        self.placeholderLabel.font = font;
        self.placeholderLabel.textColor = inputColor(o[@"placeholder_color"]);
        self.placeholderLabel.textAlignment = inputAlignment(o[@"alignment"]);
        self.placeholderLabel.text = o[@"placeholder"];
        self.placeholderLabel.frame = inner;
        [self.placeholderLabel sizeToFit];
        self.placeholderLabel.hidden = self.textValue.length != 0;
    }
    if (!enabled) [self endEditing];
}

- (NSString *)textValue { return self.field ? self.field.text : self.textView.text; }
- (void)setTextValue:(NSString *)text {
    if (self.field) { [self.field unmarkText]; self.field.text = text; }
    else { [self.textView unmarkText]; self.textView.text = text; }
    [self.editorUndoManager removeAllActions];
}
- (NSRange)selectionRange {
    id<UITextInput> editor = self.field ?: self.textView;
    UITextRange *range = editor.selectedTextRange;
    if (!range) return self.savedSelection;
    NSInteger start = [editor offsetFromPosition:editor.beginningOfDocument toPosition:range.start];
    NSInteger end = [editor offsetFromPosition:editor.beginningOfDocument toPosition:range.end];
    return NSMakeRange(MAX(0, start), MAX(0, end - start));
}
- (void)setSelectionRange:(NSRange)range {
    self.savedSelection = range;
    id<UITextInput> editor = self.field ?: self.textView;
    UITextPosition *start = [editor positionFromPosition:editor.beginningOfDocument offset:range.location];
    UITextPosition *end = [editor positionFromPosition:editor.beginningOfDocument offset:NSMaxRange(range)];
    if (start && end) editor.selectedTextRange = [editor textRangeFromPosition:start toPosition:end];
    if (self.textView) [self.textView scrollRangeToVisible:range];
}
- (NSRange)markedRange {
    id<UITextInput> editor = self.field ?: self.textView;
    UITextRange *range = editor.markedTextRange;
    if (!range) return NSMakeRange(NSNotFound, 0);
    NSInteger start = [editor offsetFromPosition:editor.beginningOfDocument toPosition:range.start];
    NSInteger end = [editor offsetFromPosition:editor.beginningOfDocument toPosition:range.end];
    return NSMakeRange(MAX(0, start), MAX(0, end - start));
}
- (void)beginEditing:(BOOL)selectAll {
    if (!self.shown || ![self.options[@"enabled"] boolValue]) return;
    self.activating = YES;
    [self applyPlacement];
    UIResponder *editor = self.field ?: self.textView;
    if ([editor becomeFirstResponder]) {
        [self editingBegan];
        [self setSelectionRange:selectAll ? NSMakeRange(0, self.textValue.length) : self.savedSelection];
    }
    self.activating = NO;
    [self applyPlacement];
}
- (void)endEditing {
    if (!self.focused) return;
    [(self.field ?: self.textView) resignFirstResponder];
    [self editingEnded];
}
- (void)finishComposition {
    id<UITextInput> editor = self.field ? (id)self.field : (id)self.textView;
    if (editor.markedTextRange) [editor unmarkText];
}
- (void)replaceRange:(NSRange)range withString:(NSString *)text {
    if (![self shouldChangeRange:range replacement:text]) return;
    id<UITextInput> editor = self.field ?: self.textView;
    UITextPosition *start = [editor positionFromPosition:editor.beginningOfDocument offset:range.location];
    UITextPosition *end = [editor positionFromPosition:editor.beginningOfDocument offset:NSMaxRange(range)];
    if (start && end) {
        [editor replaceRange:[editor textRangeFromPosition:start toPosition:end] withText:text];
        [self setSelectionRange:NSMakeRange(range.location + text.length, 0)];
        [self changed];
    }
}
- (NSUndoManager *)editorUndoManager { return (self.field ?: self.textView).undoManager; }

- (void)applyPlacement {
    CGFloat width = [self.options[@"width"] doubleValue], height = [self.options[@"height"] doubleValue];
    CGAffineTransform t = self.placement;
    BOOL avoiding = !self.headless && !self.sceneManagedPlacement && (self.focused || self.activating) && [self.options[@"avoid_keyboard"] boolValue] && !CGRectIsNull(self.keyboardFrame);
    CGRect original = CGRectApplyAffineTransform(CGRectMake(-width / 2, -height / 2, width, height), t);
    avoiding = avoiding && CGRectIntersectsRect(original, self.keyboardFrame);
    if (avoiding && self.textView && fabs(t.b) < 1e-6 && fabs(t.c) < 1e-6 && fabs(t.d) > 1e-6) {
        CGFloat available = MAX(44, CGRectGetMinY(self.keyboardFrame) - self.surface.safeAreaInsets.top - 16);
        height = MIN(height, available / fabs(t.d));
    }
    if (self.textView) {
        NSArray *padding = self.options[@"padding"];
        CGRect inner = self.textView.frame;
        BOOL changedHeight = inner.size.height != MAX(1, height - [padding[0] doubleValue] - [padding[2] doubleValue]);
        inner.size.height = MAX(1, height - [padding[0] doubleValue] - [padding[2] doubleValue]);
        self.textView.frame = inner;
        if (changedHeight && self.focused) [self.textView scrollRangeToVisible:self.selectionRange];
    }
    self.host.transform = CGAffineTransformIdentity;
    self.host.bounds = CGRectMake(0, 0, width, height);
    self.host.center = CGPointMake(t.tx, t.ty);
    self.host.transform = CGAffineTransformMake(t.a, t.b, t.c, t.d, 0, 0);
    if (avoiding) {
        CGRect rect = [self.host convertRect:self.host.bounds toView:self.surface];
        if (CGRectIntersectsRect(rect, self.keyboardFrame)) {
            CGFloat dy = MIN(0, CGRectGetMinY(self.keyboardFrame) - 8 - CGRectGetMaxY(rect));
            self.host.center = CGPointMake(self.host.center.x, self.host.center.y + dy);
        }
    }
    self.host.alpha = self.headless ? 0 : self.opacity;
    self.host.hidden = !self.shown || !(self.focused || self.activating);
    [self applyClips];
}
- (void)keyboardChanged:(NSNotification *)notification {
    if (!self.surface.window.isKeyWindow || [notification.name isEqualToString:UIKeyboardWillHideNotification]) {
        self.keyboardFrame = CGRectNull;
    } else {
        CGRect frame = [notification.userInfo[UIKeyboardFrameEndUserInfoKey] CGRectValue];
        UIWindow *window = self.surface.window;
        frame = [window convertRect:frame fromCoordinateSpace:window.screen.coordinateSpace];
        frame = [self.surface convertRect:frame fromView:window];
        self.keyboardFrame = CGRectIntersection(frame, self.surface.bounds);
    }
    if (!gInputKeyboardFrames) gInputKeyboardFrames = [NSMutableDictionary new];
    gInputKeyboardFrames[@(self.windowHandle)] = [NSValue valueWithCGRect:self.keyboardFrame];
    // Automatic avoidance updates geometry immediately, for both standalone
    // editors and scroll-managed editors. UIKit owns the keyboard's animation.
    [UIView performWithoutAnimation:^{ [self applyPlacement]; }];
}

- (void)fieldChanged:(id)sender { [self changed]; }
- (void)previousInput:(id)sender { [self enqueue:@"previous"]; }
- (void)nextInput:(id)sender { [self enqueue:@"next"]; }
- (void)dismissInput:(id)sender {
    [self finishComposition];
    [self changed];
    [self endEditing];
}
- (BOOL)textField:(UITextField *)field shouldChangeCharactersInRange:(NSRange)range replacementString:(NSString *)text {
    return [self shouldChangeRange:range replacement:text];
}
- (BOOL)textFieldShouldReturn:(UITextField *)field { [self submit]; return NO; }
- (void)textFieldDidBeginEditing:(UITextField *)field { [self editingBegan]; }
- (void)textFieldDidEndEditing:(UITextField *)field { [self editingEnded]; }
- (void)textFieldDidChangeSelection:(UITextField *)field { [self selectionChanged]; }
- (BOOL)textView:(UITextView *)view shouldChangeTextInRange:(NSRange)range replacementText:(NSString *)text {
    if ([text isEqualToString:@"\n"] && ![self.options[@"submit_behavior"] isEqualToString:@"newline"] && !self.marking && self.markedRange.location == NSNotFound) {
        [self submit]; return NO;
    }
    return [self shouldChangeRange:range replacement:text];
}
- (void)textViewDidBeginEditing:(UITextView *)view { [self editingBegan]; }
- (void)textViewDidEndEditing:(UITextView *)view { [self editingEnded]; }
- (void)textViewDidChange:(UITextView *)view { [self changed]; }
- (void)textViewDidChangeSelection:(UITextView *)view { [self selectionChanged]; }
@end
