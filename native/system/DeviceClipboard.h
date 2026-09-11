#pragma once
#include <sys/utsname.h>
#if !COCOA_PY_UIKIT
#import <IOKit/ps/IOPowerSources.h>
#import <IOKit/ps/IOPSKeys.h>
#endif

static NSDictionary *CocoaPyBattery() {
#if COCOA_PY_UIKIT
    UIDevice *device = UIDevice.currentDevice;
    BOOL previous = device.batteryMonitoringEnabled;
    device.batteryMonitoringEnabled = YES;
    float level = device.batteryLevel;
    UIDeviceBatteryState state = device.batteryState;
    device.batteryMonitoringEnabled = previous;
    return @{ @"level": level >= 0 ? @(level) : NSNull.null,
              @"state": state == UIDeviceBatteryStateCharging ? @"charging" :
                         state == UIDeviceBatteryStateFull ? @"full" :
                         state == UIDeviceBatteryStateUnplugged ? @"unplugged" : @"unknown" };
#else
    CFTypeRef info = IOPSCopyPowerSourcesInfo();
    CFArrayRef sources = info ? IOPSCopyPowerSourcesList(info) : nullptr;
    NSDictionary *value = @{ @"level": NSNull.null, @"state": @"unavailable" };
    if (sources) {
        for (CFIndex i = 0; i < CFArrayGetCount(sources); i++) {
            NSDictionary *source = (__bridge NSDictionary *)IOPSGetPowerSourceDescription(info, CFArrayGetValueAtIndex(sources, i));
            if (![source[@kIOPSTypeKey] isEqual:@kIOPSInternalBatteryType]) continue;
            double maximum = [source[@kIOPSMaxCapacityKey] doubleValue];
            double current = [source[@kIOPSCurrentCapacityKey] doubleValue];
            BOOL ac = [source[@kIOPSPowerSourceStateKey] isEqual:@kIOPSACPowerValue];
            value = @{ @"level": maximum > 0 ? @(current / maximum) : NSNull.null,
                       @"state": [source[@kIOPSIsChargingKey] boolValue] ? @"charging" :
                                  ac ? @"full" : @"unplugged" };
            break;
        }
        CFRelease(sources);
    }
    if (info) CFRelease(info);
    return value;
#endif
}

static CocoaPyRequest *CocoaPyDeviceClipboard(NSString *name, NSDictionary *args) {
    if ([name isEqual:@"device.info"]) {
        struct utsname hardware; uname(&hardware);
        NSProcessInfo *process = NSProcessInfo.processInfo;
        NSOperatingSystemVersion version = process.operatingSystemVersion;
        NSString *thermal = @[@"nominal", @"fair", @"serious", @"critical"][MIN((NSUInteger)process.thermalState, 3UL)];
        return CocoaPyValue(@{ @"platform": COCOA_PY_UIKIT ? @"ios" : @"macos",
            @"system_version": [NSString stringWithFormat:@"%ld.%ld.%ld", version.majorVersion, version.minorVersion, version.patchVersion],
            @"machine": [NSString stringWithUTF8String:hardware.machine],
            @"cpu_count": @(process.processorCount), @"active_cpu_count": @(process.activeProcessorCount),
            @"physical_memory": @(process.physicalMemory), @"uptime": @(process.systemUptime),
            @"low_power": @(process.lowPowerModeEnabled), @"thermal_state": thermal });
    }
    if ([name isEqual:@"device.battery"]) return CocoaPyValue(CocoaPyBattery());
    if ([name isEqual:@"device.storage"]) {
        if (!CocoaPyString(args, @"path")) return CocoaPyFailure(@"value", @"path must be a nonempty string.");
        NSURL *url = [NSURL fileURLWithPath:args[@"path"]];
        CocoaPyFileAccess access(url);
        NSError *error;
        NSDictionary *attributes = [NSFileManager.defaultManager attributesOfFileSystemForPath:url.path error:&error];
        if (!attributes) return CocoaPyFailure(@"os", error.localizedDescription);
        return CocoaPyValue(@{ @"total": attributes[NSFileSystemSize], @"free": attributes[NSFileSystemFreeSize] });
    }
#if COCOA_PY_UIKIT
    UIPasteboard *pasteboard = UIPasteboard.generalPasteboard;
#else
    NSPasteboard *pasteboard = NSPasteboard.generalPasteboard;
#endif
    if ([name isEqual:@"clipboard.read_text"]) {
#if COCOA_PY_UIKIT
        return CocoaPyValue(pasteboard.string);
#else
        return CocoaPyValue([pasteboard stringForType:NSPasteboardTypeString]);
#endif
    }
    if ([name isEqual:@"clipboard.types"]) {
#if COCOA_PY_UIKIT
        return CocoaPyValue(pasteboard.pasteboardTypes ?: @[]);
#else
        return CocoaPyValue(pasteboard.types ?: @[]);
#endif
    }
    if ([name isEqual:@"clipboard.clear"]) {
#if COCOA_PY_UIKIT
        pasteboard.items = @[];
#else
        [pasteboard clearContents];
#endif
        return CocoaPyValue(nil);
    }
    if ([name isEqual:@"clipboard.write_text"]) {
        if (!CocoaPyString(args, @"text", YES)) return CocoaPyFailure(@"value", @"text must be a string.");
#if COCOA_PY_UIKIT
        NSMutableDictionary *options = [NSMutableDictionary dictionary];
        options[UIPasteboardOptionLocalOnly] = @([args[@"local_only"] boolValue]);
        if (args[@"expires_in"] != NSNull.null) {
            if (!CocoaPyNumber(args, @"expires_in", 0.001, 31536000)) return CocoaPyFailure(@"value", @"expires_in must be positive.");
            options[UIPasteboardOptionExpirationDate] = [NSDate dateWithTimeIntervalSinceNow:[args[@"expires_in"] doubleValue]];
        }
        [pasteboard setItems:@[@{ @"public.utf8-plain-text": args[@"text"] }] options:options];
#else
        if ([args[@"local_only"] boolValue] || (args[@"expires_in"] && args[@"expires_in"] != NSNull.null))
            return CocoaPyFailure(@"not_implemented", @"Clipboard expiration and local_only require iOS.");
        [pasteboard clearContents];
        if (![pasteboard setString:args[@"text"] forType:NSPasteboardTypeString])
            return CocoaPyFailure(@"os", @"The clipboard rejected the text.");
#endif
        return CocoaPyValue(nil);
    }
    if ([name isEqual:@"clipboard.read_bytes"]) {
        if (!CocoaPyString(args, @"type")) return CocoaPyFailure(@"value", @"type must be a nonempty string.");
#if COCOA_PY_UIKIT
        NSData *data = [pasteboard dataForPasteboardType:args[@"type"]];
#else
        NSData *data = [pasteboard dataForType:args[@"type"]];
#endif
        return CocoaPyValue(data ? [data base64EncodedStringWithOptions:0] : nil);
    }
    if ([name isEqual:@"clipboard.write_bytes"]) {
        if (!CocoaPyString(args, @"type") || !CocoaPyString(args, @"data", YES))
            return CocoaPyFailure(@"value", @"type and base64 data must be strings.");
        NSData *data = [[NSData alloc] initWithBase64EncodedString:args[@"data"] options:0];
        if (!data) return CocoaPyFailure(@"value", @"Invalid base64 data.");
#if COCOA_PY_UIKIT
        pasteboard.items = @[@{ args[@"type"]: data }];
#else
        [pasteboard clearContents];
        if (![pasteboard setData:data forType:args[@"type"]]) return CocoaPyFailure(@"os", @"The clipboard rejected the data.");
#endif
        return CocoaPyValue(nil);
    }
    return CocoaPyFailure(@"value", @"Unknown device or clipboard operation.");
}
