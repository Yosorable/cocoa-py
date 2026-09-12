#pragma once
#include "../common/CocoaPyNotifications.h"

static BOOL CocoaPyNotificationsAvailable() {
    return NSBundle.mainBundle.bundleIdentifier.length > 0 &&
           [NSBundle.mainBundle.bundleURL.pathExtension.lowercaseString isEqual:@"app"];
}
static NSString *CocoaPyNotificationStatus(UNAuthorizationStatus status) {
    switch (status) {
        case UNAuthorizationStatusNotDetermined: return @"not_determined";
        case UNAuthorizationStatusDenied: return @"denied";
        case UNAuthorizationStatusAuthorized: return @"authorized";
        case UNAuthorizationStatusProvisional: return @"provisional";
#if COCOA_PY_UIKIT
        case UNAuthorizationStatusEphemeral: return @"ephemeral";
#endif
        default: return @"unknown";
    }
}
static NSString *CocoaPyNotificationSetting(UNNotificationSetting value) {
    switch (value) {
        case UNNotificationSettingEnabled: return @"enabled";
        case UNNotificationSettingDisabled: return @"disabled";
        case UNNotificationSettingNotSupported: return @"not_supported";
        default: return @"unknown";
    }
}
static NSDictionary *CocoaPyNotificationSettings(UNNotificationSettings *settings) {
    NSString *style = @"unknown", *previews = @"unknown";
    switch (settings.alertStyle) {
        case UNAlertStyleNone: style = @"none"; break;
        case UNAlertStyleBanner: style = @"banner"; break;
        case UNAlertStyleAlert: style = @"alert"; break;
        default: break;
    }
    switch (settings.showPreviewsSetting) {
        case UNShowPreviewsSettingAlways: previews = @"always"; break;
        case UNShowPreviewsSettingWhenAuthenticated: previews = @"when_authenticated"; break;
        case UNShowPreviewsSettingNever: previews = @"never"; break;
        default: break;
    }
    return @{
        @"authorization": CocoaPyNotificationStatus(settings.authorizationStatus),
        @"alert": CocoaPyNotificationSetting(settings.alertSetting),
        @"sound": CocoaPyNotificationSetting(settings.soundSetting),
        @"badge": CocoaPyNotificationSetting(settings.badgeSetting),
        @"notification_center": CocoaPyNotificationSetting(settings.notificationCenterSetting),
        @"lock_screen": CocoaPyNotificationSetting(settings.lockScreenSetting),
        @"scheduled_delivery": CocoaPyNotificationSetting(settings.scheduledDeliverySetting),
        @"time_sensitive": CocoaPyNotificationSetting(settings.timeSensitiveSetting),
        @"alert_style": style, @"previews": previews,
    };
}
static BOOL CocoaPyNotificationBool(id value) {
    return [value isKindOfClass:NSNumber.class] && CFGetTypeID((__bridge CFTypeRef)value) == CFBooleanGetTypeID();
}
static BOOL CocoaPyNotificationNumber(NSDictionary *args, NSString *key, double minimum, double maximum) {
    return CocoaPyNumber(args, key, minimum, maximum) && !CocoaPyNotificationBool(args[key]);
}
static BOOL CocoaPyNotificationInteger(NSDictionary *args, NSString *key, int maximum) {
    return CocoaPyNotificationNumber(args, key, 0, maximum) &&
           std::floor([args[key] doubleValue]) == [args[key] doubleValue];
}
static BOOL CocoaPyNotificationIdentifier(NSDictionary *args) {
    return CocoaPyString(args, @"identifier") && [args[@"identifier"] length] <= 128;
}

// The system invokes these callbacks after system_start's exception boundary
// has returned. Convert failures here so Python receives an error instead of
// terminating the host process on a notification service queue.
static void CocoaPyNotificationCallback(CocoaPyRequest *request, void (^work)(void)) {
    @autoreleasepool {
        @try {
            if (!request.closed && !request.done) work();
        } @catch (NSException *exception) {
            [request fail:@"runtime" message:exception.reason ?: exception.name];
        }
    }
}

// A nil trigger is valid for immediate delivery. The separate error output
// distinguishes it from rejected input before calling Apple's constructors.
static UNNotificationTrigger *CocoaPyNotificationTrigger(NSDictionary *value, NSString **error) {
    *error = nil;
    if (![value isKindOfClass:NSDictionary.class] || !CocoaPyString(value, @"kind")) {
        *error = @"trigger must describe an immediate, interval, date, or calendar trigger."; return nil;
    }
    NSString *kind = value[@"kind"];
    if ([kind isEqual:@"immediate"]) return nil;
    if ([kind isEqual:@"interval"]) {
        if (!CocoaPyNotificationBool(value[@"repeat"]) ||
            !CocoaPyNotificationNumber(value, @"seconds", [value[@"repeat"] boolValue] ? 60 : 0, 31536000) ||
            [value[@"seconds"] doubleValue] <= 0) {
            *error = @"Interval seconds must be positive and at most one year; repeating intervals require at least 60 seconds."; return nil;
        }
        return [UNTimeIntervalNotificationTrigger triggerWithTimeInterval:[value[@"seconds"] doubleValue]
                                                                 repeats:[value[@"repeat"] boolValue]];
    }
    NSDateComponents *components = [NSDateComponents new];
    // Absolute dates use Gregorian UTC components, independent of user settings.
    NSCalendar *calendar = [[NSCalendar alloc] initWithCalendarIdentifier:NSCalendarIdentifierGregorian];
    if ([kind isEqual:@"date"]) {
        if (!CocoaPyNotificationNumber(value, @"timestamp", 0, 253402300799) ||
            [value[@"timestamp"] doubleValue] <= NSDate.date.timeIntervalSince1970) {
            *error = @"The notification date must be in the future and before year 10000."; return nil;
        }
        calendar.timeZone = [NSTimeZone timeZoneForSecondsFromGMT:0];
        NSDate *date = [NSDate dateWithTimeIntervalSince1970:std::ceil([value[@"timestamp"] doubleValue])];
        components = [calendar components:NSCalendarUnitYear | NSCalendarUnitMonth | NSCalendarUnitDay |
                      NSCalendarUnitHour | NSCalendarUnitMinute | NSCalendarUnitSecond fromDate:date];
        components.calendar = calendar;
        components.timeZone = calendar.timeZone;
        return [UNCalendarNotificationTrigger triggerWithDateMatchingComponents:components repeats:NO];
    }
    if ([kind isEqual:@"calendar"]) {
        if (!CocoaPyNotificationInteger(value, @"hour", 23) ||
            !CocoaPyNotificationInteger(value, @"minute", 59) ||
            !CocoaPyNotificationInteger(value, @"second", 59) ||
            (value[@"weekday"] && !CocoaPyNotificationInteger(value, @"weekday", 6))) {
            *error = @"Calendar times require hour 0..23, minute/second 0..59, and optional weekday 0..6 (Monday first)."; return nil;
        }
        if (value[@"timezone"]) {
            NSTimeZone *zone = CocoaPyString(value, @"timezone") ? [NSTimeZone timeZoneWithName:value[@"timezone"]] : nil;
            if (!zone) { *error = @"timezone must be a recognized IANA timezone name."; return nil; }
            components.timeZone = zone;
        }
        // Leave both calendar and timezone unspecified for local recurring times;
        // embedding a calendar would snapshot its timezone when archived by the OS.
        components.hour = [value[@"hour"] integerValue];
        components.minute = [value[@"minute"] integerValue];
        components.second = [value[@"second"] integerValue];
        if (value[@"weekday"]) components.weekday = ([value[@"weekday"] integerValue] + 1) % 7 + 1;
        return [UNCalendarNotificationTrigger triggerWithDateMatchingComponents:components repeats:YES];
    }
    *error = @"Unknown notification trigger kind."; return nil;
}

static NSDictionary *CocoaPyNotificationTriggerInfo(UNNotificationTrigger *trigger) {
    if (!trigger) return @{ @"kind": @"immediate" };
    if ([trigger isKindOfClass:UNTimeIntervalNotificationTrigger.class]) {
        return @{ @"kind": @"interval", @"seconds": @(((UNTimeIntervalNotificationTrigger *)trigger).timeInterval),
                  @"repeat": @(trigger.repeats) };
    }
    if ([trigger isKindOfClass:UNCalendarNotificationTrigger.class]) {
        NSDateComponents *c = ((UNCalendarNotificationTrigger *)trigger).dateComponents;
        if (!trigger.repeats && c.year != NSDateComponentUndefined) {
            NSDate *date = [c.calendar dateFromComponents:c];
            return @{ @"kind": @"date", @"timestamp": date ? @(date.timeIntervalSince1970) : NSNull.null };
        }
        return @{ @"kind": @"calendar", @"hour": @(c.hour), @"minute": @(c.minute), @"second": @(c.second),
                  @"weekday": c.weekday == NSDateComponentUndefined ? NSNull.null : @((c.weekday + 5) % 7),
                  @"timezone": c.timeZone.name ?: (id)NSNull.null };
    }
    return @{ @"kind": @"unknown" };
}
static NSMutableDictionary *CocoaPyNotificationInfo(UNNotificationRequest *notice) {
    UNNotificationContent *content = notice.content;
    id foreground = content.userInfo[CocoaPyNotificationForegroundKey];
    // System-restored content may use nil for an omitted text field. Keep the
    // public schema string-valued and never insert nil into a dictionary literal.
    return [@{
        @"identifier": [notice.identifier substringFromIndex:CocoaPyNotificationPrefix.length],
        @"title": content.title ?: @"", @"body": content.body ?: @"", @"subtitle": content.subtitle ?: @"",
        @"sound": @(content.sound != nil),
        @"foreground": @(![foreground isKindOfClass:NSNumber.class] || [foreground boolValue]),
        @"repeat": @(notice.trigger.repeats), @"trigger": CocoaPyNotificationTriggerInfo(notice.trigger),
    } mutableCopy];
}

// The center is supplied separately so native contract tests can use an isolated
// fake service while still exercising Apple's actual trigger/content objects.
static CocoaPyRequest *CocoaPyNotificationWithCenter(NSString *name, NSDictionary *args, UNUserNotificationCenter *center) {
    CocoaPyRequest *request = [CocoaPyRequest new];
    if ([name isEqual:@"notification.permission"] || [name isEqual:@"notification.settings"]) {
        [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *settings) {
            CocoaPyNotificationCallback(request, ^{
                [request finish:[name isEqual:@"notification.permission"] ?
                    (id)CocoaPyNotificationStatus(settings.authorizationStatus) : CocoaPyNotificationSettings(settings)];
            });
        }];
    } else if ([name isEqual:@"notification.request_permission"]) {
        [center requestAuthorizationWithOptions:UNAuthorizationOptionAlert | UNAuthorizationOptionSound | UNAuthorizationOptionBadge
                             completionHandler:^(BOOL granted, NSError *error) {
            CocoaPyNotificationCallback(request, ^{
                if (error) [request fail:@"os" message:error.localizedDescription];
                else [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *settings) {
                    CocoaPyNotificationCallback(request, ^{
                        [request finish:CocoaPyNotificationStatus(settings.authorizationStatus)];
                    });
                }];
            });
        }];
    } else if ([name isEqual:@"notification.schedule"]) {
        if (!CocoaPyNotificationIdentifier(args) || !CocoaPyString(args, @"title", YES) ||
            !CocoaPyString(args, @"body", YES) || !CocoaPyString(args, @"subtitle", YES) ||
            !CocoaPyNotificationBool(args[@"sound"]) || !CocoaPyNotificationBool(args[@"foreground"]))
            return CocoaPyFailure(@"value", @"A notification requires an identifier of 1..128 UTF-16 code units, text, and boolean sound/foreground flags.");
        NSString *error = nil;
        UNNotificationTrigger *trigger = CocoaPyNotificationTrigger(args[@"trigger"], &error);
        if (error) return CocoaPyFailure(@"value", error);
        NSString *identifier = [CocoaPyNotificationPrefix stringByAppendingString:args[@"identifier"]];
        UNMutableNotificationContent *content = [UNMutableNotificationContent new];
        content.title = args[@"title"]; content.body = args[@"body"]; content.subtitle = args[@"subtitle"];
        content.sound = [args[@"sound"] boolValue] ? UNNotificationSound.defaultSound : nil;
        content.userInfo = @{ CocoaPyNotificationForegroundKey: args[@"foreground"] };
        UNNotificationRequest *notice = [UNNotificationRequest requestWithIdentifier:identifier content:content trigger:trigger];
        [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *settings) {
            CocoaPyNotificationCallback(request, ^{
                @synchronized(request) {
                    if (request.closed) return;
                    UNAuthorizationStatus status = settings.authorizationStatus;
                    if (status != UNAuthorizationStatusAuthorized && status != UNAuthorizationStatusProvisional
#if COCOA_PY_UIKIT
                        && status != UNAuthorizationStatusEphemeral
#endif
                        ) {
                        [request fail:@"permission" message:@"Request notification permission before scheduling."]; return;
                    }
                    [center addNotificationRequest:notice withCompletionHandler:^(NSError *failure) {
                        // Once submitted, the OS owns this schedule. Closing or timing
                        // out the waiter must not delete a newer same-ID replacement.
                        CocoaPyNotificationCallback(request, ^{
                            if (failure) [request fail:@"os" message:failure.localizedDescription];
                            else [request finish:args[@"identifier"]];
                        });
                    }];
                }
            });
        }];
    } else if ([name isEqual:@"notification.pending"]) {
        [center getPendingNotificationRequestsWithCompletionHandler:^(NSArray<UNNotificationRequest *> *notices) {
            CocoaPyNotificationCallback(request, ^{
                NSMutableArray *values = [NSMutableArray array];
                for (UNNotificationRequest *notice in notices) {
                    if (![notice.identifier hasPrefix:CocoaPyNotificationPrefix]) continue;
                    NSDate *next = [notice.trigger isKindOfClass:UNCalendarNotificationTrigger.class] ?
                        [(UNCalendarNotificationTrigger *)notice.trigger nextTriggerDate] :
                        [notice.trigger isKindOfClass:UNTimeIntervalNotificationTrigger.class] ?
                        [(UNTimeIntervalNotificationTrigger *)notice.trigger nextTriggerDate] : nil;
                    NSMutableDictionary *value = CocoaPyNotificationInfo(notice);
                    value[@"next_date"] = next ? @(next.timeIntervalSince1970) : NSNull.null;
                    [values addObject:value];
                }
                [request finish:values];
            });
        }];
    } else if ([name isEqual:@"notification.delivered"]) {
        [center getDeliveredNotificationsWithCompletionHandler:^(NSArray<UNNotification *> *notices) {
            CocoaPyNotificationCallback(request, ^{
                NSMutableArray *values = [NSMutableArray array];
                for (UNNotification *notice in notices) {
                    if (![notice.request.identifier hasPrefix:CocoaPyNotificationPrefix]) continue;
                    NSMutableDictionary *value = CocoaPyNotificationInfo(notice.request);
                    value[@"delivered_at"] = @(notice.date.timeIntervalSince1970);
                    [values addObject:value];
                }
                [request finish:values];
            });
        }];
    } else if ([name isEqual:@"notification.cancel"]) {
        if (!CocoaPyNotificationIdentifier(args)) return CocoaPyFailure(@"value", @"identifier must contain 1..128 UTF-16 code units.");
        NSArray *identifiers = @[[CocoaPyNotificationPrefix stringByAppendingString:args[@"identifier"]]];
        [center removePendingNotificationRequestsWithIdentifiers:identifiers];
        [center removeDeliveredNotificationsWithIdentifiers:identifiers];
        [request finish:nil];
    } else if ([name isEqual:@"notification.cancel_pending"] || [name isEqual:@"notification.remove_delivered"]) {
        BOOL pending = [name isEqual:@"notification.cancel_pending"];
        if (args[@"identifier"]) {
            if (!CocoaPyNotificationIdentifier(args)) return CocoaPyFailure(@"value", @"identifier must contain 1..128 UTF-16 code units.");
            NSArray *identifiers = @[[CocoaPyNotificationPrefix stringByAppendingString:args[@"identifier"]]];
            if (pending) [center removePendingNotificationRequestsWithIdentifiers:identifiers];
            else [center removeDeliveredNotificationsWithIdentifiers:identifiers];
            [request finish:nil];
        } else {
            void (^remove)(NSArray *) = ^(NSArray *notices) {
                CocoaPyNotificationCallback(request, ^{
                    @synchronized(request) {
                        if (request.closed) return;
                        NSMutableArray *identifiers = [NSMutableArray array];
                        for (id item in notices) {
                            UNNotificationRequest *notice = pending ? item : ((UNNotification *)item).request;
                            if ([notice.identifier hasPrefix:CocoaPyNotificationPrefix]) [identifiers addObject:notice.identifier];
                        }
                        if (pending) [center removePendingNotificationRequestsWithIdentifiers:identifiers];
                        else [center removeDeliveredNotificationsWithIdentifiers:identifiers];
                        [request finish:nil];
                    }
                });
            };
            if (pending) [center getPendingNotificationRequestsWithCompletionHandler:remove];
            else [center getDeliveredNotificationsWithCompletionHandler:remove];
        }
    } else return CocoaPyFailure(@"value", @"Unknown notification operation.");
    return request;
}

static CocoaPyRequest *CocoaPyNotification(NSString *name, NSDictionary *args) {
    BOOL available = CocoaPyNotificationsAvailable();
    if ([name isEqual:@"notification.available"]) return CocoaPyValue(@(available));
    if (!available) return CocoaPyFailure(@"not_implemented", @"Local notifications require an app bundle. On macOS, the optional cocoa-py launcher provides one.");
    return CocoaPyNotificationWithCenter(name, args, UNUserNotificationCenter.currentNotificationCenter);
}
