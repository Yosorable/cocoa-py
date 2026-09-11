#pragma once
#import <UserNotifications/UserNotifications.h>

static NSString *const CocoaPyNotificationPrefix = @"cocoa-py:";
static BOOL CocoaPyNotificationsAvailable() {
    return NSBundle.mainBundle.bundleIdentifier.length > 0 &&
           [NSBundle.mainBundle.bundleURL.pathExtension.lowercaseString isEqual:@"app"];
}
static NSString *CocoaPyNotificationStatus(UNAuthorizationStatus status) {
    switch (status) {
        case UNAuthorizationStatusDenied: return @"denied";
        case UNAuthorizationStatusAuthorized: return @"authorized";
        case UNAuthorizationStatusProvisional: return @"provisional";
#if COCOA_PY_UIKIT
        case UNAuthorizationStatusEphemeral: return @"ephemeral";
#endif
        default: return @"not_determined";
    }
}
@interface CocoaPyNotificationRequest : CocoaPyRequest
@property(nonatomic, copy) NSString *identifier;
@end
@implementation CocoaPyNotificationRequest
- (void)close {
    BOOL pending;
    @synchronized(self) { pending = !self.done; }
    [super close];
    if (pending && self.identifier)
        [UNUserNotificationCenter.currentNotificationCenter removePendingNotificationRequestsWithIdentifiers:@[self.identifier]];
}
@end

static CocoaPyRequest *CocoaPyNotification(NSString *name, NSDictionary *args) {
    BOOL available = CocoaPyNotificationsAvailable();
    if ([name isEqual:@"notification.available"]) return CocoaPyValue(@(available));
    if (!available) return CocoaPyFailure(@"not_implemented", @"Local notifications require an app bundle. On macOS, use the cocoa-py launcher.");
    UNUserNotificationCenter *center = UNUserNotificationCenter.currentNotificationCenter;
    CocoaPyNotificationRequest *request = [CocoaPyNotificationRequest new];
    if ([name isEqual:@"notification.permission"]) {
        [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *settings) {
            [request finish:CocoaPyNotificationStatus(settings.authorizationStatus)];
        }];
    } else if ([name isEqual:@"notification.request_permission"]) {
        [center requestAuthorizationWithOptions:UNAuthorizationOptionAlert | UNAuthorizationOptionSound | UNAuthorizationOptionBadge
                             completionHandler:^(BOOL granted, NSError *error) {
            if (error) [request fail:@"os" message:error.localizedDescription];
            else [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *settings) {
                [request finish:CocoaPyNotificationStatus(settings.authorizationStatus)];
            }];
        }];
    } else if ([name isEqual:@"notification.schedule"]) {
        if (!CocoaPyString(args, @"identifier") || [args[@"identifier"] length] > 128 ||
            !CocoaPyString(args, @"title", YES) || !CocoaPyString(args, @"body", YES) ||
            !CocoaPyNumber(args, @"delay", [args[@"repeat"] boolValue] ? 60 : 0.1, 31536000))
            return CocoaPyFailure(@"value", @"A notification requires a valid identifier, text, and delay (at least 60 seconds when repeating).");
        request.identifier = [CocoaPyNotificationPrefix stringByAppendingString:args[@"identifier"]];
        UNMutableNotificationContent *content = [UNMutableNotificationContent new];
        content.title = args[@"title"]; content.body = args[@"body"];
        content.sound = [args[@"sound"] boolValue] ? UNNotificationSound.defaultSound : nil;
        UNTimeIntervalNotificationTrigger *trigger = [UNTimeIntervalNotificationTrigger triggerWithTimeInterval:[args[@"delay"] doubleValue]
                                                                                                      repeats:[args[@"repeat"] boolValue]];
        UNNotificationRequest *notice = [UNNotificationRequest requestWithIdentifier:request.identifier content:content trigger:trigger];
        [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *settings) {
            if (request.closed) return;
            UNAuthorizationStatus status = settings.authorizationStatus;
            if (status == UNAuthorizationStatusNotDetermined || status == UNAuthorizationStatusDenied) {
                [request fail:@"permission" message:@"Request notification permission before scheduling."]; return;
            }
            [center addNotificationRequest:notice withCompletionHandler:^(NSError *error) {
                @synchronized(request) {
                    if (request.closed) [UNUserNotificationCenter.currentNotificationCenter removePendingNotificationRequestsWithIdentifiers:@[request.identifier]];
                    else if (error) [request fail:@"os" message:error.localizedDescription];
                    else [request finish:args[@"identifier"]];
                }
            }];
        }];
    } else if ([name isEqual:@"notification.pending"]) {
        [center getPendingNotificationRequestsWithCompletionHandler:^(NSArray<UNNotificationRequest *> *notices) {
            NSMutableArray *values = [NSMutableArray array];
            for (UNNotificationRequest *notice in notices) {
                if (![notice.identifier hasPrefix:CocoaPyNotificationPrefix]) continue;
                NSDate *next = [notice.trigger isKindOfClass:UNCalendarNotificationTrigger.class] ?
                    [(UNCalendarNotificationTrigger *)notice.trigger nextTriggerDate] :
                    [notice.trigger isKindOfClass:UNTimeIntervalNotificationTrigger.class] ?
                    [(UNTimeIntervalNotificationTrigger *)notice.trigger nextTriggerDate] : nil;
                [values addObject:@{ @"identifier": [notice.identifier substringFromIndex:CocoaPyNotificationPrefix.length],
                    @"title": notice.content.title, @"body": notice.content.body,
                    @"repeat": @(notice.trigger.repeats), @"next_date": next ? @(next.timeIntervalSince1970) : NSNull.null }];
            }
            [request finish:values];
        }];
    } else if ([name isEqual:@"notification.cancel"]) {
        if (!CocoaPyString(args, @"identifier")) return CocoaPyFailure(@"value", @"identifier must be a nonempty string.");
        NSArray *identifiers = @[[CocoaPyNotificationPrefix stringByAppendingString:args[@"identifier"]]];
        [center removePendingNotificationRequestsWithIdentifiers:identifiers];
        [center removeDeliveredNotificationsWithIdentifiers:identifiers];
        [request finish:nil];
    } else return CocoaPyFailure(@"value", @"Unknown notification operation.");
    return request;
}
