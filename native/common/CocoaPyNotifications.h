#pragma once
#import <UserNotifications/UserNotifications.h>

// Hosts opt into this policy from their own notification delegate. Importing a
// Python module must never take ownership of a host's global delegate.
static NSString *const CocoaPyNotificationPrefix = @"cocoa-py:";
static NSString *const CocoaPyNotificationForegroundKey = @"cocoa-py.foreground";

static UNNotificationPresentationOptions CocoaPyNotificationPresentation(UNNotificationRequest *request) {
    if (![request.identifier hasPrefix:CocoaPyNotificationPrefix]) return UNNotificationPresentationOptionNone;
    id foreground = request.content.userInfo[CocoaPyNotificationForegroundKey];
    if ([foreground isKindOfClass:NSNumber.class] && ![foreground boolValue])
        return UNNotificationPresentationOptionNone;
    UNNotificationPresentationOptions options = UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList;
    if (request.content.sound) options |= UNNotificationPresentationOptionSound;
    return options;
}
