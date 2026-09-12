#include "../../native/system/SystemRequest.h"
#import <IOKit/ps/IOPowerSources.h>
#import <IOKit/ps/IOPSKeys.h>
#include <cstdio>
#include <cstdlib>

// Replace only IOKit's source snapshots; exercise production battery mapping
// without depending on the test machine's battery or charging settings.
static NSArray *sources;
static CFTypeRef TestCopyPowerSourcesInfo() {
    return (__bridge_retained CFTypeRef)sources;
}
static CFArrayRef TestCopyPowerSourcesList(CFTypeRef info) {
    return CFArrayCreateCopy(kCFAllocatorDefault, (CFArrayRef)info);
}
static CFDictionaryRef TestGetPowerSourceDescription(CFTypeRef, CFTypeRef source) {
    return (CFDictionaryRef)source;
}
#define IOPSCopyPowerSourcesInfo TestCopyPowerSourcesInfo
#define IOPSCopyPowerSourcesList TestCopyPowerSourcesList
#define IOPSGetPowerSourceDescription TestGetPowerSourceDescription
#include "../../native/system/Device.h"
#undef IOPSCopyPowerSourcesInfo
#undef IOPSCopyPowerSourcesList
#undef IOPSGetPowerSourceDescription

static NSDictionary *Battery(int level, BOOL plugged, BOOL charging, NSNumber *charged) {
    NSMutableDictionary *source = [@{
        @kIOPSTypeKey: @kIOPSInternalBatteryType,
        @kIOPSCurrentCapacityKey: @(level), @kIOPSMaxCapacityKey: @100,
        @kIOPSPowerSourceStateKey: plugged ? @kIOPSACPowerValue : @kIOPSBatteryPowerValue,
        @kIOPSIsChargingKey: @(charging),
    } mutableCopy];
    if (charged) source[@kIOPSIsChargedKey] = charged;
    return source;
}

static void Check(const char *name, NSDictionary *source, id level, NSString *state) {
    sources = source ? @[source] : @[];
    NSDictionary *actual = CocoaPyBattery();
    NSDictionary *expected = @{ @"level": level, @"state": state };
    if (![actual isEqualToDictionary:expected]) {
        std::fprintf(stderr, "%s: expected %s, got %s\n", name,
                     expected.description.UTF8String, actual.description.UTF8String);
        std::exit(1);
    }
}

int main() {
    @autoreleasepool {
        Check("charging on hold", Battery(80, YES, NO, @NO), @0.8, @"not_charging");
        Check("charged below 100 percent", Battery(95, YES, NO, @YES), @0.95, @"full");
        Check("fully charged", Battery(100, YES, NO, @YES), @1.0, @"full");
        Check("actively charging", Battery(40, YES, YES, @NO), @0.4, @"charging");
        Check("finishing charge", Battery(100, YES, YES, @YES), @1.0, @"charging");
        Check("battery power", Battery(80, NO, NO, @NO), @0.8, @"unplugged");
        Check("unplugged after charging", Battery(100, NO, NO, @YES), @1.0, @"unplugged");
        Check("no charged flag", Battery(100, YES, NO, nil), @1.0, @"not_charging");
        Check("no internal battery", nil, NSNull.null, @"unavailable");
        std::puts("9 device battery scenarios passed");
    }
}
