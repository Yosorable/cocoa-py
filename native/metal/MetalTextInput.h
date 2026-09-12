// Native text editing for Scene. Python conversion stays on the calling
// interpreter thread; editors, delegates and event queues stay on the main thread.
#pragma once

static NSString *inputNormalize(NSString *text, bool multiline, NSInteger limit) {
    text = [text stringByReplacingOccurrencesOfString:@"\r\n" withString:@"\n"];
    text = [text stringByReplacingOccurrencesOfString:@"\r" withString:@"\n"];
    if (!multiline) text = [text stringByReplacingOccurrencesOfString:@"\n" withString:@" "];
    if (limit < 0) return text;
    __block NSInteger count = 0;
    __block NSUInteger end = 0;
    [text enumerateSubstringsInRange:NSMakeRange(0, text.length)
                            options:NSStringEnumerationByComposedCharacterSequences
                         usingBlock:^(NSString *, NSRange range, NSRange, BOOL *stop) {
        if (count++ >= limit) { *stop = YES; return; }
        end = NSMaxRange(range);
    }];
    return end == text.length ? text : [text substringToIndex:end];
}

static NSUInteger inputUTF16Index(NSString *text, NSUInteger index) {
    NSUInteger offset = 0;
    while (offset < text.length && index > 0) {
        unichar unit = [text characterAtIndex:offset++];
        if (CFStringIsSurrogateHighCharacter(unit) && offset < text.length &&
            CFStringIsSurrogateLowCharacter([text characterAtIndex:offset])) ++offset;
        --index;
    }
    return index ? NSNotFound : offset;
}

static NSUInteger inputCodePointIndex(NSString *text, NSUInteger offset) {
    NSUInteger index = 0, pos = 0;
    offset = MIN(offset, text.length);
    while (pos < offset) {
        unichar unit = [text characterAtIndex:pos++];
        if (CFStringIsSurrogateHighCharacter(unit) && pos < text.length &&
            CFStringIsSurrogateLowCharacter([text characterAtIndex:pos])) {
            if (pos == offset) break;
            ++pos;
        }
        ++index;
    }
    return index;
}

static CocoaColor *inputColor(NSArray *color) {
#if COCOA_PY_UIKIT
    return [UIColor colorWithRed:[color[0] doubleValue] green:[color[1] doubleValue]
                           blue:[color[2] doubleValue] alpha:[color[3] doubleValue]];
#else
    return [NSColor colorWithSRGBRed:[color[0] doubleValue] green:[color[1] doubleValue]
                               blue:[color[2] doubleValue] alpha:[color[3] doubleValue]];
#endif
}

static NSTextAlignment inputAlignment(NSString *alignment) {
    if ([alignment isEqualToString:@"left"]) return NSTextAlignmentLeft;
    if ([alignment isEqualToString:@"center"]) return NSTextAlignmentCenter;
    if ([alignment isEqualToString:@"right"]) return NSTextAlignmentRight;
    return NSTextAlignmentNatural;
}

@class CocoaPySceneTextInput;
#if COCOA_PY_UIKIT
@interface CocoaPyInputField : UITextField
@property(nonatomic, weak) CocoaPySceneTextInput *inputOwner;
@end
@interface CocoaPyInputView : UITextView
@property(nonatomic, weak) CocoaPySceneTextInput *inputOwner;
@end
@interface CocoaPySceneTextInput : NSObject <UITextFieldDelegate, UITextViewDelegate>
@property(nonatomic, strong) UIView *host;
@property(nonatomic, strong) CocoaPyInputField *field;
@property(nonatomic, strong) CocoaPyInputView *textView;
@property(nonatomic, strong) UILabel *placeholderLabel;
@property(nonatomic) CGRect keyboardFrame;
#else
@interface CocoaPyInputView : NSTextView
@property(nonatomic, weak) CocoaPySceneTextInput *inputOwner;
@end
@interface CocoaPyInputHost : NSView
@property(nonatomic, weak) CocoaPySceneTextInput *inputOwner;
@end
@interface CocoaPySceneTextInput : NSObject <NSTextFieldDelegate, NSTextViewDelegate>
@property(nonatomic, strong) CocoaPyInputHost *host;
@property(nonatomic, strong) NSSecureTextField *field;
@property(nonatomic, strong) CocoaPyInputView *textView;
@property(nonatomic, strong) NSScrollView *scrollView;
@property(nonatomic, strong) NSUndoManager *inputUndoManager;
#endif
@property(nonatomic, weak) CocoaPyMetalSurfaceView *surface;
@property(nonatomic, strong) NSMutableDictionary *options;
@property(nonatomic, strong) NSMutableArray<NSDictionary *> *events;
@property(nonatomic) long long handle, windowHandle;
@property(nonatomic) unsigned long long revision;
@property(nonatomic) BOOL closed, suppress, shown, focused, activating;
@property(nonatomic) BOOL headless;
@property(nonatomic) CGRect caretRect;
@property(nonatomic) NSInteger marking;
@property(nonatomic) NSRange savedSelection;
@property(nonatomic) CGAffineTransform placement;
@property(nonatomic) CGFloat opacity;
@property(nonatomic, copy) NSString *lastText;
@property(nonatomic) NSRange lastSelection, lastMarked;
- (void)changed;
- (void)undoFinished:(NSNotification *)notification;
- (void)selectionChanged;
- (void)editingBegan;
- (void)editingEnded;
- (void)submit;
- (void)enqueue:(NSString *)kind;
- (NSDictionary *)state;
- (BOOL)shouldChangeRange:(NSRange)range replacement:(NSString *)replacement;
- (void)close;
@end

@interface CocoaPySceneTextInput (Platform)
- (void)buildEditor;
- (void)applyAppearance;
- (NSString *)textValue;
- (void)setTextValue:(NSString *)text;
- (NSRange)selectionRange;
- (void)setSelectionRange:(NSRange)range;
- (NSRange)markedRange;
- (void)beginEditing:(BOOL)selectAll;
- (void)endEditing;
- (void)finishComposition;
- (void)replaceRange:(NSRange)range withString:(NSString *)text;
- (void)applyPlacement;
- (NSUndoManager *)editorUndoManager;
@end

static NSMutableDictionary<NSNumber *, CocoaPySceneTextInput *> *gTextInputs;
static std::atomic<unsigned long long> gTextInputCount{0};
#if COCOA_PY_UIKIT
static NSMutableDictionary<NSNumber *, NSValue *> *gInputKeyboardFrames;
#endif

#if COCOA_PY_UIKIT
#include "MetalTextInputUIKit.h"
#else
#include "MetalTextInputMac.h"
#endif

@implementation CocoaPySceneTextInput

- (NSDictionary *)state {
    NSString *text = self.textValue ?: @"";
    NSRange selection = self.selectionRange;
    NSRange marked = self.markedRange;
    id markedValue = marked.location == NSNotFound ? NSNull.null :
        @[@(inputCodePointIndex(text, marked.location)), @(inputCodePointIndex(text, NSMaxRange(marked)))];
    return @{@"text":text, @"selection":@[@(inputCodePointIndex(text, selection.location)),
                                           @(inputCodePointIndex(text, NSMaxRange(selection)))],
             @"marked_range":markedValue, @"focused":@(self.focused), @"revision":@(self.revision)};
}

- (void)enqueue:(NSString *)kind {
    if (self.closed || self.suppress) return;
    ++self.revision;
    NSMutableDictionary *event = [self.state mutableCopy];
    event[@"kind"] = kind;
    // Coalesce adjacent updates without moving edits across focus/submit events.
    NSDictionary *last = self.events.lastObject;
    if (([kind isEqualToString:@"change"] || [kind isEqualToString:@"selection"]) &&
        [last[@"kind"] isEqual:kind]) [self.events removeLastObject];
    if (self.events.count >= 256) [self.events removeObjectAtIndex:0];
    [self.events addObject:event];
}

- (BOOL)shouldChangeRange:(NSRange)range replacement:(NSString *)replacement {
    if (self.suppress) return YES;
    if (![self.options[@"enabled"] boolValue] || [self.options[@"read_only"] boolValue]) return NO;
    if (self.editorUndoManager.isUndoing || self.editorUndoManager.isRedoing) return YES;
    if (self.marking || self.markedRange.location != NSNotFound) return YES;
    NSString *value = self.textValue;
    if (range.location > value.length || range.length > value.length - range.location) return NO;
    NSString *proposed = [value stringByReplacingCharactersInRange:range withString:replacement ?: @""];
    NSInteger limit = self.options[@"max_length"] == NSNull.null ? -1 : [self.options[@"max_length"] integerValue];
    bool multiline = [self.options[@"multiline"] boolValue];
    return [inputNormalize(proposed, multiline, limit) isEqualToString:inputNormalize(proposed, multiline, -1)];
}

- (void)changed {
    if (self.suppress || self.closed || self.marking) return;
    // An undo group can temporarily restore over-limit composed text before
    // undoing its original insertion. Correct only the completed group.
    if (self.editorUndoManager.isUndoing || self.editorUndoManager.isRedoing) return;
    NSString *value = self.textValue;
    if (self.markedRange.location == NSNotFound) {
        NSInteger limit = self.options[@"max_length"] == NSNull.null ? -1 : [self.options[@"max_length"] integerValue];
        NSString *normalized = inputNormalize(value, [self.options[@"multiline"] boolValue], limit);
        if (![normalized isEqualToString:value]) {
            // Composition is allowed to exceed the limit provisionally. Apply
            // the final correction through the editor's ordinary undo mechanism.
            NSRange selection = self.selectionRange;
            self.suppress = YES;
            [self replaceRange:NSMakeRange(0, value.length) withString:normalized];
            [self setSelectionRange:NSMakeRange(MIN(selection.location, normalized.length), 0)];
            self.suppress = NO;
            value = self.textValue;
        }
    }
    if (![value isEqualToString:self.lastText] || !NSEqualRanges(self.lastMarked, self.markedRange)) {
        self.lastText = value;
        self.lastMarked = self.markedRange;
        [self enqueue:@"change"];
    }
    [self selectionChanged];
#if COCOA_PY_UIKIT
    self.placeholderLabel.hidden = value.length != 0;
#else
    [self.host setNeedsDisplay:YES];
#endif
}

- (void)undoFinished:(NSNotification *)notification {
    if (notification.object != self.editorUndoManager) return;
    if (self.editorUndoManager.isUndoing || self.editorUndoManager.isRedoing) {
        __weak CocoaPySceneTextInput *weakSelf = self;
        dispatch_async(dispatch_get_main_queue(), ^{ [weakSelf changed]; });
    } else [self changed];
}

- (void)selectionChanged {
    if (self.suppress || self.closed || self.marking) return;
    if (self.editorUndoManager.isUndoing || self.editorUndoManager.isRedoing) return;
    NSRange range = self.selectionRange;
    if (!NSEqualRanges(range, self.lastSelection)) {
        self.lastSelection = range;
        self.savedSelection = range;
        [self enqueue:@"selection"];
    }
}

- (void)editingBegan {
    if (self.closed || self.focused) return;
    self.focused = YES;
    [self applyPlacement];
    [self enqueue:@"focus"];
}

- (void)editingEnded {
    if (self.closed || !self.focused) return;
    [self finishComposition];
    [self changed];
    self.savedSelection = self.selectionRange;
    self.focused = NO;
    self.host.hidden = YES;
    [self enqueue:@"blur"];
}

- (void)submit {
    if (self.marking || self.markedRange.location != NSNotFound) return;
    [self changed];
    [self enqueue:@"submit"];
    NSString *behavior = self.options[@"submit_behavior"];
    if ([behavior isEqualToString:@"blur"]) [self endEditing];
}

- (void)close {
    if (self.closed) return;
    self.suppress = YES;
    [self endEditing];
    self.closed = YES;
    self.field.delegate = nil;
    self.textView.delegate = nil;
    [self.host removeFromSuperview];
    [NSNotificationCenter.defaultCenter removeObserver:self];
    [self.events removeAllObjects];
}
@end

static void metalCloseTextInputsForWindow(long long window) {
    for (NSNumber *key in [gTextInputs.allKeys copy]) {
        CocoaPySceneTextInput *input = gTextInputs[key];
        if (input.windowHandle == window) {
            [input close];
            [gTextInputs removeObjectForKey:key];
            --gTextInputCount;
        }
    }
#if COCOA_PY_UIKIT
    [gInputKeyboardFrames removeObjectForKey:@(window)];
#endif
}
