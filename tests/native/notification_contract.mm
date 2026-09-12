#include "../../native/system/SystemRequest.h"
#include "../../native/system/Notification.h"
#include <cassert>
#include <cstdio>

// Only the OS service is replaced. Production validation, lifecycle code,
// serialization, and Apple's real notification/trigger objects run unchanged.
@interface FakeSettings : NSObject
@property UNAuthorizationStatus authorizationStatus;
@property UNNotificationSetting alertSetting, soundSetting, badgeSetting, notificationCenterSetting;
@property UNNotificationSetting lockScreenSetting, scheduledDeliverySetting, timeSensitiveSetting;
@property UNAlertStyle alertStyle;
@property UNShowPreviewsSetting showPreviewsSetting;
@end
@implementation FakeSettings
@end

@interface FakeDelivered : NSObject
@property(strong) UNNotificationRequest *request;
@property(strong) NSDate *date;
@end
@implementation FakeDelivered
@end

// System-restored content can omit empty text even though newly constructed
// UNMutableNotificationContent instances return empty strings on macOS.
@interface RestoredContent : NSObject
@property(nonatomic, copy) NSString *title, *subtitle, *body;
@property(copy) NSDictionary *userInfo;
@property(strong) UNNotificationSound *sound;
@property BOOL throwOnBody;
@end
@implementation RestoredContent
@synthesize body = _body;
- (NSString *)body {
    if (self.throwOnBody) [NSException raise:NSInvalidArgumentException format:@"Invalid restored notification content"];
    return _body;
}
@end

@interface RestoredNotice : NSObject
@property(copy) NSString *identifier;
@property(strong) RestoredContent *content;
@property(strong) UNNotificationTrigger *trigger;
@end
@implementation RestoredNotice
@end

@interface FakeCenter : NSObject
@property(strong) FakeSettings *settings;
@property(strong) NSMutableDictionary<NSString *, UNNotificationRequest *> *pending;
@property(strong) NSMutableDictionary<NSString *, FakeDelivered *> *delivered;
@property(strong) NSMutableArray *settingsCallbacks, *addCallbacks;
@property BOOL deferSettings, deferAdd;
@property BOOL deferRecords;
@property(copy) void (^pendingCallback)(NSArray *);
@property(copy) void (^deliveredCallback)(NSArray *);
@property(strong) NSError *addError;
@property NSUInteger prompts;
- (void)getNotificationSettingsWithCompletionHandler:(void (^)(UNNotificationSettings *))callback;
- (void)getPendingNotificationRequestsWithCompletionHandler:(void (^)(NSArray *))callback;
- (void)getDeliveredNotificationsWithCompletionHandler:(void (^)(NSArray *))callback;
- (void)addNotificationRequest:(UNNotificationRequest *)notice withCompletionHandler:(void (^)(NSError *))callback;
- (void)removePendingNotificationRequestsWithIdentifiers:(NSArray *)identifiers;
- (void)removeDeliveredNotificationsWithIdentifiers:(NSArray *)identifiers;
@end
@implementation FakeCenter
- (instancetype)init {
    if ((self = [super init])) {
        _settings = [FakeSettings new]; _settings.authorizationStatus = UNAuthorizationStatusAuthorized;
        _pending = [NSMutableDictionary dictionary]; _delivered = [NSMutableDictionary dictionary];
        _settingsCallbacks = [NSMutableArray array]; _addCallbacks = [NSMutableArray array];
    }
    return self;
}
- (void)getNotificationSettingsWithCompletionHandler:(void (^)(UNNotificationSettings *))callback {
    if (self.deferSettings) [self.settingsCallbacks addObject:[callback copy]];
    else callback((UNNotificationSettings *)self.settings);
}
- (void)requestAuthorizationWithOptions:(UNAuthorizationOptions)options completionHandler:(void (^)(BOOL, NSError *))callback {
    assert(options == (UNAuthorizationOptionAlert | UNAuthorizationOptionSound | UNAuthorizationOptionBadge));
    self.prompts++; callback(self.settings.authorizationStatus == UNAuthorizationStatusAuthorized, nil);
}
- (void)addNotificationRequest:(UNNotificationRequest *)notice withCompletionHandler:(void (^)(NSError *))callback {
    if (!self.addError) self.pending[notice.identifier] = notice;
    if (self.deferAdd) [self.addCallbacks addObject:[callback copy]];
    else callback(self.addError);
}
- (void)getPendingNotificationRequestsWithCompletionHandler:(void (^)(NSArray *))callback {
    if (self.deferRecords) self.pendingCallback = callback;
    else callback(self.pending.allValues);
}
- (void)getDeliveredNotificationsWithCompletionHandler:(void (^)(NSArray *))callback {
    if (self.deferRecords) self.deliveredCallback = callback;
    else callback(self.delivered.allValues);
}
- (void)removePendingNotificationRequestsWithIdentifiers:(NSArray *)identifiers { [self.pending removeObjectsForKeys:identifiers]; }
- (void)removeDeliveredNotificationsWithIdentifiers:(NSArray *)identifiers { [self.delivered removeObjectsForKeys:identifiers]; }
@end

static CocoaPyRequest *Call(FakeCenter *center, NSString *operation, NSDictionary *args = @{}) {
    return CocoaPyNotificationWithCenter([@"notification." stringByAppendingString:operation], args, (UNUserNotificationCenter *)center);
}
static NSMutableDictionary *Notice(NSString *identifier, NSDictionary *trigger) {
    return [@{ @"identifier": identifier, @"title": @"Title", @"subtitle": @"Subtitle", @"body": @"Body",
               @"sound": @YES, @"foreground": @YES, @"trigger": trigger } mutableCopy];
}
static void Deliver(FakeCenter *center, NSString *identifier) {
    FakeDelivered *item = [FakeDelivered new]; item.request = center.pending[identifier];
    item.date = [NSDate dateWithTimeIntervalSince1970:1234567890];
    center.delivered[identifier] = item;
}
static void Triggers() {
    NSString *error = nil;
    auto trigger = CocoaPyNotificationTrigger(@{ @"kind": @"immediate" }, &error);
    assert(!trigger && !error);
    auto interval = (UNTimeIntervalNotificationTrigger *)CocoaPyNotificationTrigger(
        @{ @"kind": @"interval", @"seconds": @60, @"repeat": @YES }, &error);
    assert(interval.repeats && interval.timeInterval == 60 && interval.nextTriggerDate && !error);
    auto daily = (UNCalendarNotificationTrigger *)CocoaPyNotificationTrigger(
        @{ @"kind": @"calendar", @"hour": @8, @"minute": @30, @"second": @0 }, &error);
    assert(!error && daily.repeats && daily.dateComponents.hour == 8 && daily.nextTriggerDate);
    assert(!daily.dateComponents.timeZone && !daily.dateComponents.calendar);
    for (int day = 0; day < 7; ++day) {
        auto weekly = (UNCalendarNotificationTrigger *)CocoaPyNotificationTrigger(
            @{ @"kind": @"calendar", @"hour": @9, @"minute": @0, @"second": @0,
               @"weekday": @(day), @"timezone": @"Asia/Shanghai" }, &error);
        assert(!error && weekly.repeats && weekly.nextTriggerDate);
        assert(weekly.dateComponents.weekday == (day + 1) % 7 + 1);
        assert([weekly.dateComponents.timeZone.name isEqual:@"Asia/Shanghai"]);
        assert([CocoaPyNotificationTriggerInfo(weekly)[@"weekday"] intValue] == day);
    }
    double stamp = std::ceil(NSDate.date.timeIntervalSince1970) + 3600;
    auto absolute = (UNCalendarNotificationTrigger *)CocoaPyNotificationTrigger(
        @{ @"kind": @"date", @"timestamp": @(stamp) }, &error);
    assert(!error && !absolute.repeats);
    assert(std::abs(absolute.nextTriggerDate.timeIntervalSince1970 - stamp) < 0.01);
    assert([CocoaPyNotificationTriggerInfo(absolute)[@"timestamp"] doubleValue] == stamp);
    NSArray *invalid = @[
        NSNull.null, @{}, @{ @"kind": @"unknown" },
        @{ @"kind": @"interval", @"seconds": @59, @"repeat": @YES },
        @{ @"kind": @"interval", @"seconds": @1, @"repeat": NSNull.null },
        @{ @"kind": @"interval", @"seconds": @YES, @"repeat": @NO },
        @{ @"kind": @"interval", @"seconds": @(-1), @"repeat": @NO },
        @{ @"kind": @"interval", @"seconds": @(INFINITY), @"repeat": @NO },
        @{ @"kind": @"date", @"timestamp": @1 }, @{ @"kind": @"date", @"timestamp": @(NAN) },
        @{ @"kind": @"calendar", @"hour": @24, @"minute": @0, @"second": @0 },
        @{ @"kind": @"calendar", @"hour": @9, @"minute": @0.5, @"second": @0 },
        @{ @"kind": @"calendar", @"hour": @9, @"minute": @0, @"second": @0, @"weekday": @7 },
        @{ @"kind": @"calendar", @"hour": @9, @"minute": @0, @"second": @0, @"timezone": @"Not/AZone" },
    ];
    for (id value in invalid) {
        assert(!CocoaPyNotificationTrigger(value, &error) && error);
    }
}
static void PermissionsAndRecords() {
    FakeCenter *center = [FakeCenter new];
    center.settings.alertSetting = UNNotificationSettingEnabled;
    center.settings.soundSetting = UNNotificationSettingDisabled;
    center.settings.alertStyle = UNAlertStyleBanner;
    center.settings.showPreviewsSetting = UNShowPreviewsSettingWhenAuthenticated;
    NSDictionary *settings = Call(center, @"settings").result;
    assert([settings[@"authorization"] isEqual:@"authorized"]);
    assert([settings[@"alert"] isEqual:@"enabled"] && [settings[@"sound"] isEqual:@"disabled"]);
    assert([settings[@"alert_style"] isEqual:@"banner"] && [settings[@"previews"] isEqual:@"when_authenticated"]);
    assert(center.prompts == 0 && [NSJSONSerialization isValidJSONObject:settings]);
    assert([Call(center, @"request_permission").result isEqual:@"authorized"] && center.prompts == 1);
    auto args = Notice(@"a", @{ @"kind": @"interval", @"seconds": @3600, @"repeat": @NO });
    center.settings.authorizationStatus = UNAuthorizationStatusDenied;
    assert([Call(center, @"schedule", args).failure[@"kind"] isEqual:@"permission"] && !center.pending.count);
    center.settings.authorizationStatus = UNAuthorizationStatusProvisional;
    assert([Call(center, @"schedule", args).result isEqual:@"a"]);
    NSArray *items = Call(center, @"pending").result;
    NSDictionary *item = items.firstObject;
    assert(items.count == 1 && [item[@"subtitle"] isEqual:@"Subtitle"] && [item[@"sound"] boolValue]);
    assert([item[@"next_date"] doubleValue] > NSDate.date.timeIntervalSince1970);
    assert([NSJSONSerialization isValidJSONObject:items]);
    [args setObject:@"Replacement" forKey:@"title"];
    assert(Call(center, @"schedule", args).done && center.pending.count == 1);
    assert([center.pending[@"cocoa-py:a"].content.title isEqual:@"Replacement"]);
    center.addError = [NSError errorWithDomain:@"test" code:1 userInfo:@{NSLocalizedDescriptionKey:@"Rejected"}];
    assert([Call(center, @"schedule", args).failure[@"kind"] isEqual:@"os"]);
    center.addError = nil;
    for (NSString *field in @[@"sound", @"foreground", @"identifier", @"subtitle", @"trigger"]) {
        NSMutableDictionary *invalid = [args mutableCopy]; invalid[field] = NSNull.null;
        assert([Call(center, @"schedule", invalid).failure[@"kind"] isEqual:@"value"]);
    }
}
static void NamespaceAndCleanup() {
    FakeCenter *center = [FakeCenter new];
    Call(center, @"schedule", Notice(@"a", @{ @"kind": @"immediate" }));
    Call(center, @"schedule", Notice(@"b", @{ @"kind": @"immediate" }));
    center.pending[@"host:own"] = [UNNotificationRequest requestWithIdentifier:@"host:own"
        content:[UNMutableNotificationContent new] trigger:nil];
    Deliver(center, @"cocoa-py:a"); Deliver(center, @"cocoa-py:b"); Deliver(center, @"host:own");
    NSArray *items = Call(center, @"delivered").result;
    assert(items.count == 2 && [NSJSONSerialization isValidJSONObject:items]);
    assert([items.firstObject[@"delivered_at"] doubleValue] == 1234567890);
    assert([(NSArray *)Call(center, @"pending").result count] == 2);
    Call(center, @"cancel_pending", @{ @"identifier": @"a" });
    assert(!center.pending[@"cocoa-py:a"] && center.delivered[@"cocoa-py:a"]);
    Call(center, @"remove_delivered", @{ @"identifier": @"b" });
    assert(center.pending[@"cocoa-py:b"] && !center.delivered[@"cocoa-py:b"]);
    Call(center, @"cancel_pending");
    assert(center.pending.count == 1 && center.pending[@"host:own"] && center.delivered.count == 2);
    Call(center, @"remove_delivered");
    assert(center.delivered.count == 1 && center.delivered[@"host:own"] && center.pending.count == 1);
    Call(center, @"schedule", Notice(@"a", @{ @"kind": @"immediate" })); Deliver(center, @"cocoa-py:a");
    Call(center, @"cancel", @{ @"identifier": @"a" });
    assert(center.pending.count == 1 && center.delivered.count == 1);
    assert([Call(center, @"cancel", @{ @"identifier": @"" }).failure[@"kind"] isEqual:@"value"]);
}
static void ClosedRequestsDoNotDestroyReplacements() {
    FakeCenter *center = [FakeCenter new];
    auto args = Notice(@"a", @{ @"kind": @"immediate" });
    Call(center, @"schedule", args);
    center.deferSettings = YES;
    CocoaPyRequest *waiting = Call(center, @"schedule", args);
    [waiting close];
    void (^settingsCallback)(UNNotificationSettings *) = center.settingsCallbacks.firstObject;
    settingsCallback((UNNotificationSettings *)center.settings);
    [center.settingsCallbacks removeAllObjects];
    assert(center.pending.count == 1 && center.pending[@"cocoa-py:a"]);
    center.deferSettings = NO; center.deferAdd = YES;
    CocoaPyRequest *submitted = Call(center, @"schedule", args);
    [submitted close];
    center.deferAdd = NO; args[@"title"] = @"Newer";
    Call(center, @"schedule", args);
    void (^addCallback)(NSError *) = center.addCallbacks.firstObject; addCallback(nil);
    [center.addCallbacks removeAllObjects];
    assert(submitted.closed && !submitted.done);
    assert([center.pending[@"cocoa-py:a"].content.title isEqual:@"Newer"]);
}
static void ForegroundPolicy() {
    UNMutableNotificationContent *content = [UNMutableNotificationContent new];
    auto options = [&](NSString *identifier) {
        return CocoaPyNotificationPresentation([UNNotificationRequest requestWithIdentifier:identifier content:content trigger:nil]);
    };
    assert(options(@"host:own") == UNNotificationPresentationOptionNone);
    assert(options(@"cocoa-py:old") == (UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList));
    content.sound = UNNotificationSound.defaultSound;
    assert(options(@"cocoa-py:a") & UNNotificationPresentationOptionSound);
    content.userInfo = @{CocoaPyNotificationForegroundKey: @NO};
    assert(options(@"cocoa-py:a") == UNNotificationPresentationOptionNone);
}

static void EmptyAndRestoredText() {
    FakeCenter *center = [FakeCenter new];
    auto args = Notice(@"empty", @{ @"kind": @"immediate" });
    args[@"title"] = @""; args[@"body"] = @""; args[@"subtitle"] = @"";
    assert(Call(center, @"schedule", args).done);
    NSArray *fresh = Call(center, @"pending").result;
    for (NSString *field in @[@"title", @"body", @"subtitle"]) assert([fresh.firstObject[field] isEqual:@""]);
    [center.pending removeAllObjects];

    RestoredNotice *notice = [RestoredNotice new];
    notice.identifier = @"cocoa-py:restored";
    notice.content = [RestoredContent new];
    center.pending[notice.identifier] = (UNNotificationRequest *)notice;
    Deliver(center, notice.identifier);
    notice.content.title = @"Preserved title";
    notice.content.subtitle = @"Preserved subtitle";
    NSArray *partial = Call(center, @"pending").result;
    assert([partial.firstObject[@"title"] isEqual:notice.content.title]);
    assert([partial.firstObject[@"body"] isEqual:@""]);
    assert([partial.firstObject[@"subtitle"] isEqual:notice.content.subtitle]);
    notice.content.title = nil; notice.content.subtitle = nil;
    for (NSString *operation in @[@"pending", @"delivered"]) {
        NSArray *items = Call(center, operation).result;
        assert(items.count == 1 && [NSJSONSerialization isValidJSONObject:items]);
        NSDictionary *item = items.firstObject;
        for (NSString *field in @[@"title", @"body", @"subtitle"]) assert([item[field] isEqual:@""]);
        assert([item[@"identifier"] isEqual:@"restored"] && ![item[@"sound"] boolValue]);
        assert([item[@"foreground"] boolValue] && ![item[@"repeat"] boolValue]);
    }
}

static void AsyncCallbackExceptionsBecomeFailures() {
    FakeCenter *center = [FakeCenter new];
    RestoredNotice *notice = [RestoredNotice new];
    notice.identifier = @"cocoa-py:invalid";
    notice.content = [RestoredContent new];
    notice.content.throwOnBody = YES;
    center.pending[notice.identifier] = (UNNotificationRequest *)notice;
    Deliver(center, notice.identifier);
    center.deferRecords = YES;
    for (NSString *operation in @[@"pending", @"delivered"]) {
        CocoaPyRequest *request = Call(center, operation);
        assert(!request.done);
        void (^callback)(NSArray *) = [operation isEqual:@"pending"] ? center.pendingCallback : center.deliveredCallback;
        NSArray *items = [operation isEqual:@"pending"] ? (NSArray *)center.pending.allValues : (NSArray *)center.delivered.allValues;
        __block NSException *escaped = nil;
        dispatch_sync(dispatch_get_global_queue(QOS_CLASS_DEFAULT, 0), ^{
            @try { callback(items); }
            @catch (NSException *exception) { escaped = exception; }
        });
        assert(!escaped && request.done);
        assert([request.failure[@"kind"] isEqual:@"runtime"]);
        assert([request.failure[@"message"] isEqual:@"Invalid restored notification content"]);
        assert([NSJSONSerialization isValidJSONObject:[request snapshot:NO]]);
        assert(dispatch_semaphore_wait(request.signal, DISPATCH_TIME_NOW) == 0);
    }
}

int main() {
    @autoreleasepool {
        @try {
            EmptyAndRestoredText(); AsyncCallbackExceptionsBecomeFailures();
        } @catch (NSException *exception) {
            std::fprintf(stderr, "Notification regression: %s: %s\n", exception.name.UTF8String, exception.reason.UTF8String);
            return 1;
        }
        Triggers(); PermissionsAndRecords(); NamespaceAndCleanup();
        ClosedRequestsDoNotDestroyReplacements(); ForegroundPolicy();
        std::puts("notification native contracts passed");
    }
}
