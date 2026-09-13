#pragma once

@interface CocoaPyInputFormatter : NSFormatter
@property(nonatomic, weak) CocoaPySceneTextInput *inputOwner;
@end
@implementation CocoaPyInputFormatter
- (NSString *)stringForObjectValue:(id)value { return [value isKindOfClass:NSString.class] ? value : @""; }
- (BOOL)getObjectValue:(out id *)object forString:(NSString *)string errorDescription:(out NSString **)error {
    if (object) *object = string;
    return YES;
}
- (BOOL)isPartialStringValid:(NSString *)string newEditingString:(NSString **)replacement errorDescription:(NSString **)error {
    CocoaPySceneTextInput *owner = self.inputOwner;
    if (owner.suppress || owner.marking || owner.editorUndoManager.isUndoing || owner.editorUndoManager.isRedoing) return YES;
    NSInteger limit = owner.options[@"max_length"] == NSNull.null ? -1 : [owner.options[@"max_length"] integerValue];
    return [inputNormalize(string, false, limit) isEqual:inputNormalize(string, false, -1)];
}
@end

@implementation CocoaPyInputView
- (void)scrollWheel:(NSEvent *)event {
    CocoaPySceneTextInput *owner = self.inputOwner;
    BOOL forward = ![owner.options[@"multiline"] boolValue];
    if (!forward && owner.scrollView) {
        NSClipView *clip = owner.scrollView.contentView;
        CGFloat limit = MAX(0, self.bounds.size.height - clip.bounds.size.height);
        CGFloat offset = clip.bounds.origin.y;
        CGFloat delta = event.scrollingDeltaY;
        forward = fabs(event.scrollingDeltaX) > fabs(delta) ||
            (delta > 0 && offset <= .5) || (delta < 0 && offset >= limit - .5);
    }
    if (!owner.headless && forward) [owner.surface scrollWheel:event];
    else [super scrollWheel:event];
}
- (NSRect)firstRectForCharacterRange:(NSRange)range actualRange:(NSRangePointer)actualRange {
    CocoaPySceneTextInput *owner = self.inputOwner;
    if (!owner.headless) return [super firstRectForCharacterRange:range actualRange:actualRange];
    if (actualRange) *actualRange = range;
    NSRect rect = [owner.surface convertRect:owner.caretRect toView:nil];
    return [owner.surface.window convertRectToScreen:rect];
}
- (BOOL)becomeFirstResponder {
    BOOL accepted = [super becomeFirstResponder];
    if (accepted) [self.inputOwner editingBegan];
    return accepted;
}
- (BOOL)resignFirstResponder {
    BOOL accepted = [super resignFirstResponder];
    if (accepted) [self.inputOwner editingEnded];
    return accepted;
}
- (void)setMarkedText:(id)text selectedRange:(NSRange)selection replacementRange:(NSRange)replacement {
    ++self.inputOwner.marking;
    [super setMarkedText:text selectedRange:selection replacementRange:replacement];
    --self.inputOwner.marking;
    [self.inputOwner changed];
}
- (void)unmarkText {
    ++self.inputOwner.marking;
    [super unmarkText];
    --self.inputOwner.marking;
    [self.inputOwner changed];
}
- (void)insertText:(id)text replacementRange:(NSRange)replacement {
    BOOL composing = self.hasMarkedText;
    if (composing) ++self.inputOwner.marking;
    [super insertText:text replacementRange:replacement];
    if (composing) --self.inputOwner.marking;
    [self.inputOwner changed];
}
@end

@implementation CocoaPyInputHost
- (BOOL)isFlipped { return YES; }
- (NSView *)hitTest:(NSPoint)point {
    NSPoint local = [self convertPoint:point fromView:self.superview];
    return self.inputOwner.headless || ![self.inputOwner containsClipPoint:local] ? nil : [super hitTest:point];
}
- (void)drawRect:(NSRect)dirty {
    CocoaPySceneTextInput *owner = self.inputOwner;
    if (owner.headless) return;
    if (owner.textValue.length || owner.field) return;
    NSArray *padding = owner.options[@"padding"];
    NSMutableParagraphStyle *paragraph = [NSMutableParagraphStyle new];
    paragraph.alignment = inputAlignment(owner.options[@"alignment"]);
    NSDictionary *attrs = @{NSFontAttributeName:owner.textView.font,
                            NSForegroundColorAttributeName:inputColor(owner.options[@"placeholder_color"]),
                            NSParagraphStyleAttributeName:paragraph};
    NSRect rect = NSMakeRect([padding[1] doubleValue], [padding[0] doubleValue],
        MAX(1, self.bounds.size.width - [padding[1] doubleValue] - [padding[3] doubleValue]),
        MAX(1, self.bounds.size.height - [padding[0] doubleValue] - [padding[2] doubleValue]));
    if (![owner.options[@"multiline"] boolValue]) {
        CGFloat lineHeight = [@"Mg" sizeWithAttributes:attrs].height;
        rect.origin.y += MAX(0, (rect.size.height - lineHeight) / 2);
    }
    [owner.options[@"placeholder"] drawWithRect:rect options:NSStringDrawingUsesLineFragmentOrigin attributes:attrs];
}
- (BOOL)performKeyEquivalent:(NSEvent *)event {
    CocoaPySceneTextInput *owner = self.inputOwner;
    if (!owner.focused || !(event.modifierFlags & NSEventModifierFlagCommand)) return [super performKeyEquivalent:event];
    NSTextView *editor = owner.textView ?: (NSTextView *)owner.field.currentEditor;
    NSString *key = event.charactersIgnoringModifiers.lowercaseString;
    if (([key isEqual:@"x"] || [key isEqual:@"v"] || [key isEqual:@"z"]) &&
        (![owner.options[@"enabled"] boolValue] || [owner.options[@"read_only"] boolValue])) return YES;
    if ([key isEqualToString:@"a"]) [editor selectAll:nil];
    else if ([key isEqualToString:@"c"]) { if (!owner.field) [editor copy:nil]; }
    else if ([key isEqualToString:@"x"]) { if (!owner.field) [editor cut:nil]; }
    else if ([key isEqualToString:@"v"]) [editor pasteAsPlainText:nil];
    else if ([key isEqualToString:@"z"]) {
        if (event.modifierFlags & NSEventModifierFlagShift) { if (editor.undoManager.canRedo) [editor.undoManager redo]; }
        else if (editor.undoManager.canUndo) [editor.undoManager undo];
    } else return [super performKeyEquivalent:event];
    [owner changed];
    return YES;
}
@end

@implementation CocoaPySceneTextInput (Platform)
- (void)buildEditor {
    self.inputUndoManager = [NSUndoManager new];
    self.host = [[CocoaPyInputHost alloc] initWithFrame:NSZeroRect];
    self.host.inputOwner = self;
    self.host.wantsLayer = YES;
    self.host.layer.masksToBounds = YES;
    self.host.hidden = YES;
    [self.surface addSubview:self.host];
    if ([self.options[@"secure"] boolValue]) {
        self.field = [[NSSecureTextField alloc] initWithFrame:NSZeroRect];
        self.field.bordered = NO;
        self.field.bezeled = NO;
        self.field.drawsBackground = NO;
        self.field.delegate = self;
        self.field.focusRingType = NSFocusRingTypeNone;
        CocoaPyInputFormatter *formatter = [CocoaPyInputFormatter new];
        formatter.inputOwner = self;
        self.field.formatter = formatter;
        [self.host addSubview:self.field];
    } else {
        self.scrollView = [[NSScrollView alloc] initWithFrame:NSZeroRect];
        self.scrollView.drawsBackground = NO;
        self.scrollView.contentView.drawsBackground = NO;
        self.scrollView.borderType = NSNoBorder;
        self.scrollView.hasVerticalScroller = [self.options[@"multiline"] boolValue];
        self.scrollView.autohidesScrollers = YES;
        self.textView = [[CocoaPyInputView alloc] initWithFrame:NSMakeRect(0, 0, 100, 30)];
        self.textView.inputOwner = self;
        self.textView.delegate = self;
        self.textView.richText = NO;
        self.textView.importsGraphics = NO;
        self.textView.drawsBackground = NO;
        self.textView.allowsUndo = YES;
        self.textView.textContainerInset = NSZeroSize;
        self.textView.textContainer.lineFragmentPadding = 0;
        self.textView.verticallyResizable = YES;
        self.textView.horizontallyResizable = ![self.options[@"multiline"] boolValue];
        self.textView.autoresizingMask = NSViewWidthSizable;
        self.scrollView.documentView = self.textView;
        [self.host addSubview:self.scrollView];
    }
    [self applyAppearance];
}

- (void)applyAppearance {
    NSDictionary *o = self.options;
    CGFloat size = [o[@"font_size"] doubleValue];
    NSFont *font = [o[@"font"] isKindOfClass:NSString.class] ? [NSFont fontWithName:o[@"font"] size:size] : nil;
    font = font ?: [NSFont systemFontOfSize:size];
    self.host.layer.backgroundColor = inputColor(o[@"background"]).CGColor;
    self.host.layer.borderColor = inputColor(o[@"border_color"]).CGColor;
    self.host.layer.borderWidth = [o[@"border_width"] doubleValue];
    self.host.layer.cornerRadius = [o[@"corner_radius"] doubleValue];
    CGFloat width = [o[@"width"] doubleValue], height = [o[@"height"] doubleValue];
    NSArray *padding = o[@"padding"];
    NSRect inner = NSMakeRect([padding[1] doubleValue], [padding[0] doubleValue],
        MAX(1, width - [padding[1] doubleValue] - [padding[3] doubleValue]),
        MAX(1, height - [padding[0] doubleValue] - [padding[2] doubleValue]));
    if (![o[@"multiline"] boolValue]) {
        CGFloat lineHeight = [@"Mg" sizeWithAttributes:@{NSFontAttributeName:font}].height;
        inner.origin.y += MAX(0, (inner.size.height - lineHeight) / 2);
        inner.size.height = MIN(inner.size.height, lineHeight);
    }
    BOOL editable = [o[@"enabled"] boolValue] && ![o[@"read_only"] boolValue];
    if (self.field) {
        self.field.frame = inner;
        self.field.font = font;
        self.field.textColor = inputColor(o[@"text_color"]);
        self.field.alignment = inputAlignment(o[@"alignment"]);
        self.field.enabled = [o[@"enabled"] boolValue];
        self.field.editable = editable;
        self.field.selectable = [o[@"enabled"] boolValue];
        self.field.placeholderAttributedString = [[NSAttributedString alloc] initWithString:o[@"placeholder"]
            attributes:@{NSForegroundColorAttributeName:inputColor(o[@"placeholder_color"]), NSFontAttributeName:font}];
    } else {
        self.scrollView.frame = inner;
        self.textView.font = font;
        self.textView.textColor = inputColor(o[@"text_color"]);
        self.textView.insertionPointColor = inputColor(o[@"text_color"]);
        self.textView.alignment = inputAlignment(o[@"alignment"]);
        self.textView.editable = editable;
        self.textView.selectable = [o[@"enabled"] boolValue];
        self.textView.continuousSpellCheckingEnabled = [o[@"spell_check"] boolValue];
        self.textView.automaticSpellingCorrectionEnabled = [o[@"autocorrection"] boolValue];
        self.textView.automaticQuoteSubstitutionEnabled = [o[@"autocorrection"] boolValue];
        self.textView.automaticDashSubstitutionEnabled = [o[@"autocorrection"] boolValue];
        self.textView.textContainer.widthTracksTextView = [o[@"multiline"] boolValue];
        self.textView.textContainer.containerSize = NSMakeSize([o[@"multiline"] boolValue] ? inner.size.width : 1e7, 1e7);
        self.textView.minSize = NSMakeSize(inner.size.width, inner.size.height);
        self.textView.maxSize = NSMakeSize(1e7, 1e7);
        [self.textView setFrameSize:NSMakeSize(inner.size.width, MAX(inner.size.height, self.textView.frame.size.height))];
    }
    if (![o[@"enabled"] boolValue]) [self endEditing];
    [self.host setNeedsDisplay:YES];
}

- (NSString *)textValue { return self.field ? self.field.stringValue : self.textView.string; }
- (void)setTextValue:(NSString *)text {
    if (self.field) {
        self.field.stringValue = text;
        if (self.field.currentEditor) self.field.currentEditor.string = text;
    } else {
        if (self.textView.hasMarkedText) [self.textView unmarkText];
        self.textView.string = text;
    }
    [self.editorUndoManager removeAllActions];
}
- (NSRange)selectionRange {
    NSText *editor = self.field ? self.field.currentEditor : self.textView;
    return editor ? editor.selectedRange : self.savedSelection;
}
- (void)setSelectionRange:(NSRange)range {
    self.savedSelection = range;
    NSTextView *editor = self.textView ?: (NSTextView *)self.field.currentEditor;
    if (editor) { editor.selectedRange = range; [editor scrollRangeToVisible:range]; }
}
- (NSRange)markedRange {
    NSTextView *editor = self.textView ?: (NSTextView *)self.field.currentEditor;
    return editor && editor.hasMarkedText ? editor.markedRange : NSMakeRange(NSNotFound, 0);
}
- (void)beginEditing:(BOOL)selectAll {
    if (!self.shown || ![self.options[@"enabled"] boolValue]) return;
    self.activating = YES;
    self.host.hidden = NO;
    [self applyPlacement];
    if ([self.surface.window makeFirstResponder:self.field ?: self.textView]) {
        [self editingBegan];
        [self setSelectionRange:selectAll ? NSMakeRange(0, self.textValue.length) : self.savedSelection];
    }
    self.activating = NO;
    [self applyPlacement];
}
- (void)endEditing {
    if (!self.focused) return;
    [self.surface.window makeFirstResponder:self.surface];
    [self editingEnded];
}
- (void)finishComposition {
    NSTextView *editor = self.textView ?: (NSTextView *)self.field.currentEditor;
    if (editor.hasMarkedText) [editor unmarkText];
}
- (void)replaceRange:(NSRange)range withString:(NSString *)text {
    NSTextView *editor = self.textView ?: (NSTextView *)self.field.currentEditor;
    if (editor) {
        [editor breakUndoCoalescing];
        [editor insertText:text replacementRange:range];
    } else if ([self shouldChangeRange:range replacement:text]) {
        [self setTextValue:[self.textValue stringByReplacingCharactersInRange:range withString:text]];
        self.savedSelection = NSMakeRange(range.location + text.length, 0);
        [self changed];
    }
}
- (NSUndoManager *)editorUndoManager { return (self.textView ?: (NSTextView *)self.field.currentEditor).undoManager; }
- (NSUndoManager *)undoManagerForTextView:(NSTextView *)view { return self.inputUndoManager; }

- (void)applyPlacement {
    CGFloat width = [self.options[@"width"] doubleValue], height = [self.options[@"height"] doubleValue];
    CGAffineTransform t = self.placement;
    CGFloat sx = hypot(t.a, t.b), sy = hypot(t.c, t.d);
    if (sx == 0 || sy == 0) { self.host.hidden = YES; return; }
    self.host.frameRotation = 0;
    self.host.boundsRotation = 0;
    self.host.frame = NSMakeRect(t.tx - width * sx / 2, t.ty - height * sy / 2, width * sx, height * sy);
    self.host.bounds = NSMakeRect(0, 0, width, height);
    self.host.frameCenterRotation = atan2(t.b, t.a) * 180.0 / M_PI;
    // AppKit's frame-center rotation can shift a view with scaled bounds.
    // Align its actual converted center after applying both transformations.
    NSPoint center = [self.host convertPoint:NSMakePoint(NSMidX(self.host.bounds), NSMidY(self.host.bounds))
                                     toView:self.surface];
    NSPoint origin = self.host.frame.origin;
    [self.host setFrameOrigin:NSMakePoint(origin.x + t.tx - center.x, origin.y + t.ty - center.y)];
    self.host.alphaValue = self.headless ? 0 : self.opacity;
    self.host.hidden = !self.shown || !(self.focused || self.activating);
    [self applyClips];
}

- (BOOL)textView:(NSTextView *)view shouldChangeTextInRange:(NSRange)range replacementString:(NSString *)text {
    return [self shouldChangeRange:range replacement:text];
}
- (void)textDidChange:(NSNotification *)notification { [self changed]; }
- (void)textViewDidChangeSelection:(NSNotification *)notification { [self selectionChanged]; }
- (void)controlTextDidChange:(NSNotification *)notification { [self changed]; }
- (void)controlTextDidBeginEditing:(NSNotification *)notification {
    [NSNotificationCenter.defaultCenter addObserver:self selector:@selector(textViewDidChangeSelection:)
        name:NSTextViewDidChangeSelectionNotification object:self.field.currentEditor];
    [self editingBegan];
}
- (void)controlTextDidEndEditing:(NSNotification *)notification {
    [NSNotificationCenter.defaultCenter removeObserver:self name:NSTextViewDidChangeSelectionNotification object:nil];
    [self editingEnded];
}
- (BOOL)inputEditor:(NSTextView *)editor command:(SEL)command {
    if (editor.hasMarkedText) return NO;
    if (command == @selector(insertNewline:) && ![self.options[@"submit_behavior"] isEqualToString:@"newline"]) {
        [self submit]; return YES;
    }
    if (command == @selector(insertTab:) || command == @selector(insertBacktab:)) {
        [self enqueue:command == @selector(insertTab:) ? @"next" : @"previous"]; return YES;
    }
    if (command == @selector(cancelOperation:)) { [self endEditing]; return YES; }
    return NO;
}
- (BOOL)textView:(NSTextView *)view doCommandBySelector:(SEL)command { return [self inputEditor:view command:command]; }
- (BOOL)control:(NSControl *)control textView:(NSTextView *)view doCommandBySelector:(SEL)command { return [self inputEditor:view command:command]; }
@end
