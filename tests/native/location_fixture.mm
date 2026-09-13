// Drive the production delegate with constructed samples. This executable
// never creates a location manager, requests access, or contacts a geocoder.
#include "../../native/system/SystemRequest.h"
#import <CoreLocation/CoreLocation.h>

// Replace only the network producer. The production request and completion
// handler still process results, cancellation, and errors.
static NSUInteger geocodeStarts = 0;
@interface TestGeocoder : NSObject
@property(nonatomic, copy) CLGeocodeCompletionHandler completion;
@property(nonatomic) NSUInteger cancellations;
@end
@implementation TestGeocoder
- (void)geocodeAddressString:(NSString *)address completionHandler:(CLGeocodeCompletionHandler)completion {
    geocodeStarts++; self.completion = completion;
}
- (void)reverseGeocodeLocation:(CLLocation *)location completionHandler:(CLGeocodeCompletionHandler)completion {
    geocodeStarts++; self.completion = completion;
}
- (void)cancelGeocode { self.cancellations++; }
@end
#define CLGeocoder TestGeocoder
#include "../../native/system/Location.h"
#undef CLGeocoder

@interface TestLocationManager : NSObject
@property(nonatomic) CLAuthorizationStatus authorizationStatus;
@property(nonatomic, weak) id delegate;
@property(nonatomic) NSUInteger starts;
@property(nonatomic) NSUInteger stops;
@end
@implementation TestLocationManager
- (void)startUpdatingLocation { self.starts++; }
- (void)stopUpdatingLocation { self.stops++; }
@end

@interface TestHeading : NSObject
@property(nonatomic) double magneticHeading;
@property(nonatomic) double trueHeading;
@property(nonatomic) double headingAccuracy;
@property(nonatomic, strong) NSDate *timestamp;
@end
@implementation TestHeading
@end

static CLLocation *fix(double latitude, double age, double accuracy = 5) {
    return [[CLLocation alloc] initWithCoordinate:CLLocationCoordinate2DMake(latitude, 20)
        altitude:10 horizontalAccuracy:accuracy verticalAccuracy:5
        timestamp:[NSDate dateWithTimeIntervalSinceNow:-age]];
}
static CLHeading *heading(double magnetic, double actual = -1, double accuracy = 5, double age = 0.01) {
    TestHeading *value = [TestHeading new];
    value.magneticHeading = magnetic; value.trueHeading = actual;
    value.headingAccuracy = accuracy; value.timestamp = [NSDate dateWithTimeIntervalSinceNow:-age];
    return (CLHeading *)(id)value;
}
static CocoaPyLocationRequest *request(BOOL stream = NO, BOOL compass = NO, BOOL trueNorth = NO) {
    CocoaPyLocationRequest *value = [CocoaPyLocationRequest new];
    TestLocationManager *manager = [TestLocationManager new];
    manager.authorizationStatus = kCLAuthorizationStatusAuthorizedAlways;
    value.manager = (CLLocationManager *)(id)manager;
    value.streaming = stream; value.headingMode = compass; value.trueNorth = trueNorth;
    value.maxAge = 15; value.startedAt = NSDate.date.timeIntervalSince1970 - 1;
    return value;
}
static NSDictionary *state(CocoaPyLocationRequest *value) {
    return [value snapshot:NO];
}
static NSArray *latitudes(CocoaPyLocationRequest *value) {
    return [value.samples valueForKey:@"latitude"];
}
int main() {
    @autoreleasepool {
        NSMutableDictionary *results = [NSMutableDictionary dictionary];
        CocoaPyLocationRequest *value = request();
        TestLocationManager *manager = (TestLocationManager *)(id)value.manager;
        [value locationManager:value.manager didUpdateLocations:@[fix(1, 10), fix(2, 0.1)]];
        results[@"latest"] = @{ @"sample": value.result, @"stops": @(manager.stops) };
        [value close];

        value = request();
        [value locationManager:value.manager didUpdateLocations:@[fix(1, 0.5), fix(2, 0.1, -1)]];
        results[@"invalid_latest"] = value.result;
        [value close];

        value = request(); value.maxAge = 0;
        [value locationManager:value.manager didUpdateLocations:@[fix(1, 2)]];
        BOOL rejectedCache = !value.done;
        [value locationManager:value.manager didUpdateLocations:@[fix(2, 0.25)]];
        results[@"zero_age"] = @{ @"rejected_cache": @(rejectedCache), @"state": state(value) };
        [value close];

        value = request(YES); value.maxAge = 0; value.capacity = 2;
        [value locationManager:value.manager didUpdateLocations:@[fix(0, 3), fix(1, 0.5), fix(2, 0.3), fix(3, 0.1)]];
        results[@"stream"] = @{ @"latitudes": latitudes(value), @"state": state(value) };
        [value close];
        [value locationManager:nil didUpdateLocations:@[fix(4, 0.1)]];
        results[@"closed_stream"] = @{ @"state": state(value), @"released_manager": @(value.manager == nil) };

        value = request(); value.maxAge = 0.5;
        [value locationManager:value.manager didUpdateLocations:@[fix(1, 2), fix(91, 0.1), fix(2, 0.1, NAN)]];
        results[@"invalid_locations_skipped"] = @(!value.done && !value.samples.count);
        [value close];

        value = request(NO, YES);
        [value locationManager:value.manager didUpdateHeading:heading(123, -1)];
        results[@"magnetic"] = value.result;
        [value close];

        value = request(NO, YES, YES); value.maxAge = 0;
        manager = (TestLocationManager *)(id)value.manager;
        [value locationManager:value.manager didUpdateHeading:heading(120, -1)];
        BOOL waitsForTrue = !value.done;
        [value locationManager:value.manager didUpdateLocations:@[fix(1, 0.1)]];
        BOOL ignoresFix = !value.done;
        [value locationManager:value.manager didUpdateHeading:heading(120, 125, 3, 2)];
        BOOL rejectsOld = !value.done;
        [value locationManager:value.manager didUpdateHeading:heading(120, 125, 3, 0.1)];
        results[@"true_north"] = @{ @"waits_for_true": @(waitsForTrue), @"ignores_fix": @(ignoresFix),
            @"rejects_old": @(rejectsOld), @"sample": value.result, @"location_stops": @(manager.stops) };
        [value close];

        value = request(YES, YES); value.capacity = 2;
        for (CLHeading *sample in @[heading(1, -1, -1), heading(NAN), heading(-1), heading(360),
                                   heading(1, -1, INFINITY), heading(1, -1, 5, 30)])
            [value locationManager:value.manager didUpdateHeading:sample];
        BOOL rejectsInvalid = value.samples.count == 0;
        for (int index = 1; index <= 3; index++)
            [value locationManager:value.manager didUpdateHeading:heading(index)];
        results[@"heading_stream"] = @{ @"rejects_invalid": @(rejectsInvalid),
            @"angles": [value.samples valueForKey:@"magnetic_heading"], @"state": state(value) };
        [value close];
        [value locationManager:nil didUpdateHeading:heading(4)];
        results[@"closed_heading"] = @{ @"state": state(value), @"released_manager": @(value.manager == nil) };

        value = request(NO, YES);
        manager = (TestLocationManager *)(id)value.manager;
        manager.authorizationStatus = kCLAuthorizationStatusDenied;
        [value beginUpdates];
        results[@"magnetic_without_location"] = @{ @"started": @(value.started),
            @"location_starts": @(manager.starts), @"state": state(value) };
        [value close];

        value = request(YES, YES, YES);
        manager = (TestLocationManager *)(id)value.manager;
        manager.authorizationStatus = kCLAuthorizationStatusNotDetermined;
        [value beginUpdates];
        BOOL waited = !value.started;
        manager.authorizationStatus = kCLAuthorizationStatusAuthorizedAlways;
        [value beginUpdates]; [value beginUpdates];
        double startedAt = value.startedAt;
        manager.authorizationStatus = kCLAuthorizationStatusDenied;
        [value beginUpdates];
        results[@"authorization"] = @{ @"waited": @(waited), @"starts": @(manager.starts),
            @"stops": @(manager.stops), @"started_at": @(startedAt), @"state": state(value) };
        [value close];

        value = request(YES, YES, YES);
        manager = (TestLocationManager *)(id)value.manager;
        [value locationManager:value.manager didFailWithError:[NSError errorWithDomain:kCLErrorDomain code:kCLErrorLocationUnknown userInfo:nil]];
        BOOL ignoredTransient = !value.done;
        [value locationManager:value.manager didFailWithError:[NSError errorWithDomain:kCLErrorDomain code:kCLErrorHeadingFailure userInfo:nil]];
        results[@"failure"] = @{ @"ignored_transient": @(ignoredTransient), @"stops": @(manager.stops), @"state": state(value) };
        [value close];

        NSMutableArray *permissions = [NSMutableArray array];
        for (NSNumber *authorization in @[@(kCLAuthorizationStatusDenied), @(kCLAuthorizationStatusRestricted)]) {
            value = request(); manager = (TestLocationManager *)(id)value.manager;
            manager.authorizationStatus = (CLAuthorizationStatus)authorization.intValue;
            [value beginUpdates];
            [permissions addObject:@{ @"state": state(value), @"starts": @(manager.starts) }];
            [value close];
        }
        results[@"refused_permissions"] = permissions;

        value = request(); value.permissionOnly = YES;
        manager = (TestLocationManager *)(id)value.manager;
        manager.authorizationStatus = kCLAuthorizationStatusNotDetermined;
        [value beginUpdates];
        BOOL pendingPermission = !value.done && manager.starts == 0;
        [value close]; [value close];
        manager.authorizationStatus = kCLAuthorizationStatusAuthorizedAlways;
        [value locationManagerDidChangeAuthorization:(CLLocationManager *)(id)manager];
        results[@"cancelled_permission"] = @{ @"was_pending": @(pendingPermission),
            @"starts": @(manager.starts), @"released_manager": @(value.manager == nil), @"state": state(value) };

        value = request();
        CLLocation *nonfinite = [[CLLocation alloc] initWithCoordinate:CLLocationCoordinate2DMake(1, 2)
            altitude:NAN horizontalAccuracy:5 verticalAccuracy:INFINITY course:INFINITY speed:INFINITY
            timestamp:NSDate.date];
        [value locationManager:value.manager didUpdateLocations:@[nonfinite]];
        BOOL serializable = [NSJSONSerialization isValidJSONObject:state(value)];
        results[@"nonfinite_optional"] = @{ @"serializable": @(serializable),
            @"sample": serializable ? value.result : NSNull.null };
        [value close];

        NSMutableArray *invalidGeocoding = [NSMutableArray array];
        for (NSDictionary *options in @[@{ @"address": @" \n\t" }, @{ @"address": @"one\0two" },
                @{ @"latitude": @YES, @"longitude": @0 }, @{ @"latitude": @0, @"longitude": @NO }]) {
            NSUInteger before = geocodeStarts;
            CocoaPyRequest *invalid = CocoaPyLocation(options[@"address"] ? @"location.geocode" : @"location.reverse_geocode", options);
            [invalidGeocoding addObject:@{ @"started": @(geocodeStarts != before), @"state": [invalid snapshot:NO] }];
            [invalid close];
        }
        results[@"invalid_geocoding"] = invalidGeocoding;

        value = (CocoaPyLocationRequest *)CocoaPyLocation(@"location.geocode", @{ @"address": @"Beijing" });
        TestGeocoder *geocoder = value.geocoder;
        [value close]; [value close];
        geocoder.completion(@[], nil);
        results[@"cancelled_geocoding"] = @{ @"cancellations": @(geocoder.cancellations),
            @"released_geocoder": @(value.geocoder == nil), @"state": state(value) };

        value = (CocoaPyLocationRequest *)CocoaPyLocation(@"location.reverse_geocode", @{ @"latitude": @1, @"longitude": @2 });
        value.geocoder.completion(nil, [NSError errorWithDomain:kCLErrorDomain code:kCLErrorNetwork userInfo:nil]);
        results[@"geocoding_failure"] = state(value);
        [value close];

        NSData *data = [NSJSONSerialization dataWithJSONObject:results options:NSJSONWritingPrettyPrinted error:nil];
        if (!data) return 1;
        printf("%s\n", [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding].UTF8String);
    }
}
