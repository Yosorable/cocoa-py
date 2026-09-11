#pragma once

#include "../common/CocoaPlatform.h"
#include <memory>
#include <vector>
#include <cmath>
#if COCOA_PY_UIKIT
#import <UIKit/UIKit.h>
#endif

// All delegate callbacks contain native values only. Python polls snapshots
// with the GIL held; closing a request always releases its native producer.
@interface CocoaPyRequest : NSObject
@property(nonatomic) BOOL done;
@property(nonatomic) BOOL closed;
@property(nonatomic, strong) id result;
@property(nonatomic, strong) NSDictionary *failure;
@property(nonatomic, strong) NSMutableArray *samples;
@property(nonatomic) NSUInteger capacity;
@property(nonatomic) NSUInteger dropped;
@property(nonatomic, strong) dispatch_semaphore_t signal;
- (void)finish:(id)value;
- (void)fail:(NSString *)kind message:(NSString *)message;
- (void)push:(id)value;
- (NSDictionary *)snapshot:(BOOL)consume;
- (void)close;
@end

@implementation CocoaPyRequest
- (instancetype)init {
    if ((self = [super init])) {
        _samples = [NSMutableArray array]; _capacity = 128;
        _signal = dispatch_semaphore_create(0);
    }
    return self;
}
- (void)finish:(id)value {
    @synchronized(self) {
        if (_done || _closed) return;
        _result = value ?: NSNull.null; _done = YES;
    }
    dispatch_semaphore_signal(_signal);
}
- (void)fail:(NSString *)kind message:(NSString *)message {
    @synchronized(self) {
        if (_done || _closed) return;
        _failure = @{ @"kind": kind, @"message": message ?: @"Apple framework operation failed." };
        _done = YES;
    }
    dispatch_semaphore_signal(_signal);
}
- (void)push:(id)value {
    BOOL wasEmpty;
    @synchronized(self) {
        if (_closed || _done) return;
        wasEmpty = !_samples.count;
        if (_samples.count == _capacity) { [_samples removeObjectAtIndex:0]; _dropped++; }
        [_samples addObject:value];
    }
    if (wasEmpty) dispatch_semaphore_signal(_signal);
}
- (NSDictionary *)snapshot:(BOOL)consume {
    @synchronized(self) {
        id sample = _samples.firstObject ?: NSNull.null;
        if (consume && _samples.count) [_samples removeObjectAtIndex:0];
        return @{ @"done": @(_done), @"closed": @(_closed),
                  @"result": _result ?: NSNull.null, @"error": _failure ?: NSNull.null,
                  @"sample": sample, @"buffered": @(_samples.count),
                  @"capacity": @(_capacity), @"dropped": @(_dropped) };
    }
}
- (void)close {
    @synchronized(self) { _closed = YES; [_samples removeAllObjects]; }
    dispatch_semaphore_signal(_signal);
}
@end

static CocoaPyRequest *CocoaPyFailure(NSString *kind, NSString *message) {
    CocoaPyRequest *request = [CocoaPyRequest new];
    [request fail:kind message:message]; return request;
}
static CocoaPyRequest *CocoaPyValue(id value) {
    CocoaPyRequest *request = [CocoaPyRequest new];
    [request finish:value]; return request;
}
static BOOL CocoaPyNumber(NSDictionary *args, NSString *key, double minimum, double maximum) {
    id value = args[key];
    return [value isKindOfClass:NSNumber.class] && std::isfinite([value doubleValue]) &&
           [value doubleValue] >= minimum && [value doubleValue] <= maximum;
}
static BOOL CocoaPyString(NSDictionary *args, NSString *key, BOOL allowEmpty = NO) {
    return [args[key] isKindOfClass:NSString.class] && (allowEmpty || [args[key] length] > 0);
}
static BOOL CocoaPyUsageKey(NSString *key) {
    id value = [NSBundle.mainBundle objectForInfoDictionaryKey:key];
    return [value isKindOfClass:NSString.class] && [value length] > 0;
}

#if COCOA_PY_UIKIT
static UIViewController *CocoaPyPresenter() {
    for (UIScene *scene in UIApplication.sharedApplication.connectedScenes) {
        if (![scene isKindOfClass:UIWindowScene.class] ||
            scene.activationState != UISceneActivationStateForegroundActive) continue;
        UIWindow *window = nil;
        for (UIWindow *candidate in ((UIWindowScene *)scene).windows) {
            if (!candidate.hidden && candidate.rootViewController &&
                (!window || candidate.isKeyWindow)) window = candidate;
        }
        UIViewController *controller = window.rootViewController;
        while (controller.presentedViewController) controller = controller.presentedViewController;
        if (controller && !controller.isBeingDismissed) return controller;
    }
    return nil;
}
#endif
