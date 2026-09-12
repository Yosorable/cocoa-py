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
            // External power alone does not mean the battery has finished charging.
            BOOL charged = [source[@kIOPSIsChargedKey] boolValue];
            value = @{ @"level": maximum > 0 ? @(current / maximum) : NSNull.null,
                       @"state": [source[@kIOPSIsChargingKey] boolValue] ? @"charging" :
                                  ac ? (charged ? @"full" : @"not_charging") : @"unplugged" };
            break;
        }
        CFRelease(sources);
    }
    if (info) CFRelease(info);
    return value;
#endif
}

static CocoaPyRequest *CocoaPyDevice(NSString *name, NSDictionary *args) {
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
    return CocoaPyFailure(@"value", @"Unknown device operation.");
}
