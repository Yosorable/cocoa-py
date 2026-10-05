// Drive production requests with controlled producers. This executable never
// creates a real location manager, requests access, or contacts a geocoder.
#include "../../native/system/SystemRequest.h"
#import <CoreLocation/CoreLocation.h>
#include <atomic>

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
static std::atomic<unsigned> servicesQueries{0}, managerCreations{0};
static std::atomic<bool> servicesOnMain{false}, managerOffMain{false};
static BOOL servicesEnabled = YES, servicesThrows = NO, managerThrows = NO;
static CLAuthorizationStatus initialAuthorization = kCLAuthorizationStatusAuthorizedAlways;
static CLAccuracyAuthorization initialAccuracy = CLAccuracyAuthorizationFullAccuracy;
static dispatch_semaphore_t servicesGate;
static void checkManagerThread() {
    if (!NSThread.isMainThread) managerOffMain = true;
}
@interface TestLocationManager : NSObject
@property(nonatomic) CLAuthorizationStatus authorizationStatus;
@property(nonatomic) CLAccuracyAuthorization accuracyAuthorization;
@property(nonatomic) CLLocationAccuracy desiredAccuracy;
@property(nonatomic) CLLocationDistance distanceFilter;
@property(nonatomic, weak) id delegate;
@property(nonatomic) NSUInteger starts;
@property(nonatomic) NSUInteger stops;
+ (BOOL)locationServicesEnabled;
- (void)requestWhenInUseAuthorization;
- (void)startUpdatingLocation;
- (void)stopUpdatingLocation;
@end
@implementation TestLocationManager
@synthesize authorizationStatus = _authorizationStatus, accuracyAuthorization = _accuracyAuthorization;
+ (BOOL)locationServicesEnabled {
    dispatch_semaphore_t gate = servicesGate;
    servicesQueries++;
    if (NSThread.isMainThread) servicesOnMain = true;
    // A regression must fail assertions rather than deadlock this executable.
    else if (gate) dispatch_semaphore_wait(gate, dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC));
    if (servicesThrows) [NSException raise:@"TestServicesError" format:@"Service query failed."];
    return servicesEnabled;
}
- (instancetype)init {
    checkManagerThread(); managerCreations++;
    if (managerThrows) [NSException raise:@"TestManagerError" format:@"Manager creation failed."];
    if ((self = [super init])) {
        _authorizationStatus = initialAuthorization;
        _accuracyAuthorization = initialAccuracy;
    }
    return self;
}
- (CLAuthorizationStatus)authorizationStatus { checkManagerThread(); return _authorizationStatus; }
- (CLAccuracyAuthorization)accuracyAuthorization { checkManagerThread(); return _accuracyAuthorization; }
- (void)requestWhenInUseAuthorization { checkManagerThread(); }
- (void)startUpdatingLocation { checkManagerThread(); self.starts++; }
- (void)stopUpdatingLocation { checkManagerThread(); self.stops++; }
@end

#define CLGeocoder TestGeocoder
#define CLLocationManager TestLocationManager
#include "../../native/system/Location.h"
#undef CLLocationManager
#undef CLGeocoder

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
    value.manager = manager;
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
static void pumpUntil(BOOL (^ready)(void)) {
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:3];
    while (!ready() && deadline.timeIntervalSinceNow > 0)
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.001, true);
    if (!ready()) [NSException raise:@"TestTimeout" format:@"An asynchronous request did not make progress."];
}
static NSDictionary *servicesCase(NSString *operation, BOOL enabled, BOOL cancel = NO,
                                  BOOL queryError = NO, BOOL managerError = NO) {
    servicesQueries = 0; managerCreations = 0; servicesOnMain = false; managerOffMain = false;
    servicesEnabled = enabled; servicesThrows = queryError; managerThrows = managerError;
    initialAuthorization = cancel ? kCLAuthorizationStatusNotDetermined : kCLAuthorizationStatusAuthorizedAlways;
    initialAccuracy = CLAccuracyAuthorizationReducedAccuracy;
    servicesGate = dispatch_semaphore_create(0);
    NSDictionary *options = @{ @"max_age": @15, @"capacity": @2, @"accuracy": @10, @"distance_filter": @25 };
    CocoaPyRequest *pending = CocoaPyLocation(operation, options);
    __block BOOL servicedUI = NO;
    dispatch_async(dispatch_get_main_queue(), ^{ servicedUI = YES; });
    pumpUntil(^BOOL { return servicedUI && servicesQueries.load() > 0; });
    BOOL pendingWhileUIRan = !pending.done && managerCreations == 0;
    NSDictionary *snapshot;
    NSUInteger starts = 0;
    BOOL released = NO;
    if (cancel) {
        [pending close];
        snapshot = [pending snapshot:NO];
        __weak CocoaPyRequest *weakPending = pending;
        pending = nil;
        dispatch_semaphore_signal(servicesGate);
        pumpUntil(^BOOL { return weakPending == nil; });
        released = YES;
    } else {
        dispatch_semaphore_signal(servicesGate);
        pumpUntil(^BOOL {
            return pending.done || ([pending isKindOfClass:CocoaPyLocationRequest.class] &&
                                    ((CocoaPyLocationRequest *)pending).started);
        });
        if ([pending isKindOfClass:CocoaPyLocationRequest.class]) {
            CocoaPyLocationRequest *location = (CocoaPyLocationRequest *)pending;
            starts = location.manager.starts;
            if (location.started) [location locationManager:(id)location.manager didUpdateLocations:@[fix(42, 0)]];
        }
        snapshot = [pending snapshot:NO];
        [pending close];
    }
    servicesGate = nil;
    servicesThrows = NO; managerThrows = NO;
    initialAuthorization = kCLAuthorizationStatusAuthorizedAlways;
    initialAccuracy = CLAccuracyAuthorizationFullAccuracy;
    return @{ @"pending_while_ui_ran": @(pendingWhileUIRan), @"queries": @(servicesQueries.load()),
        @"queried_on_main": @(servicesOnMain.load()), @"manager_off_main": @(managerOffMain.load()),
        @"managers": @(managerCreations.load()), @"starts": @(starts), @"released": @(released), @"state": snapshot };
}
static NSDictionary *mainQueueCase(NSString *operation) {
    __block NSDictionary *result;
    dispatch_async(dispatch_get_main_queue(), ^{
        servicesQueries = 0; servicesOnMain = false; managerOffMain = false;
        servicesEnabled = YES;
        CocoaPyRequest *pending = CocoaPyLocation(operation,
            @{ @"max_age": @15, @"capacity": @2, @"accuracy": @10, @"distance_filter": @25 });
        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:0.5];
        while (!pending.done && deadline.timeIntervalSinceNow > 0) {
            // Use the production wait, as Python does while releasing the GIL.
            CocoaPyWaitSemaphore(pending.signal, 0.01);
            if ([pending isKindOfClass:CocoaPyLocationRequest.class]) {
                CocoaPyLocationRequest *location = (CocoaPyLocationRequest *)pending;
                if (location.started)
                    [location locationManager:(id)location.manager didUpdateLocations:@[fix(42, 0)]];
            }
        }
        result = @{ @"state": [pending snapshot:NO], @"queries": @(servicesQueries.load()),
            @"queried_on_main": @(servicesOnMain.load()), @"manager_off_main": @(managerOffMain.load()) };
        [pending close];
    });
    pumpUntil(^BOOL { return result != nil; });
    return result;
}
int main() {
    @autoreleasepool {
        NSMutableDictionary *results = [NSMutableDictionary dictionary];
        CocoaPyLocationRequest *value = request();
        TestLocationManager *manager = (TestLocationManager *)(id)value.manager;
        [value locationManager:(id)value.manager didUpdateLocations:@[fix(1, 10), fix(2, 0.1)]];
        results[@"latest"] = @{ @"sample": value.result, @"stops": @(manager.stops) };
        [value close];

        value = request();
        [value locationManager:(id)value.manager didUpdateLocations:@[fix(1, 0.5), fix(2, 0.1, -1)]];
        results[@"invalid_latest"] = value.result;
        [value close];

        value = request(); value.maxAge = 0;
        [value locationManager:(id)value.manager didUpdateLocations:@[fix(1, 2)]];
        BOOL rejectedCache = !value.done;
        [value locationManager:(id)value.manager didUpdateLocations:@[fix(2, 0.25)]];
        results[@"zero_age"] = @{ @"rejected_cache": @(rejectedCache), @"state": state(value) };
        [value close];

        value = request(YES); value.maxAge = 0; value.capacity = 2;
        [value locationManager:(id)value.manager didUpdateLocations:@[fix(0, 3), fix(1, 0.5), fix(2, 0.3), fix(3, 0.1)]];
        results[@"stream"] = @{ @"latitudes": latitudes(value), @"state": state(value) };
        [value close];
        [value locationManager:nil didUpdateLocations:@[fix(4, 0.1)]];
        results[@"closed_stream"] = @{ @"state": state(value), @"released_manager": @(value.manager == nil) };

        value = request(); value.maxAge = 0.5;
        [value locationManager:(id)value.manager didUpdateLocations:@[fix(1, 2), fix(91, 0.1), fix(2, 0.1, NAN)]];
        results[@"invalid_locations_skipped"] = @(!value.done && !value.samples.count);
        [value close];

        value = request(NO, YES);
        [value locationManager:(id)value.manager didUpdateHeading:heading(123, -1)];
        results[@"magnetic"] = value.result;
        [value close];

        value = request(NO, YES, YES); value.maxAge = 0;
        manager = (TestLocationManager *)(id)value.manager;
        [value locationManager:(id)value.manager didUpdateHeading:heading(120, -1)];
        BOOL waitsForTrue = !value.done;
        [value locationManager:(id)value.manager didUpdateLocations:@[fix(1, 0.1)]];
        BOOL ignoresFix = !value.done;
        [value locationManager:(id)value.manager didUpdateHeading:heading(120, 125, 3, 2)];
        BOOL rejectsOld = !value.done;
        [value locationManager:(id)value.manager didUpdateHeading:heading(120, 125, 3, 0.1)];
        results[@"true_north"] = @{ @"waits_for_true": @(waitsForTrue), @"ignores_fix": @(ignoresFix),
            @"rejects_old": @(rejectsOld), @"sample": value.result, @"location_stops": @(manager.stops) };
        [value close];

        value = request(YES, YES); value.capacity = 2;
        for (CLHeading *sample in @[heading(1, -1, -1), heading(NAN), heading(-1), heading(360),
                                   heading(1, -1, INFINITY), heading(1, -1, 5, 30)])
            [value locationManager:(id)value.manager didUpdateHeading:sample];
        BOOL rejectsInvalid = value.samples.count == 0;
        for (int index = 1; index <= 3; index++)
            [value locationManager:(id)value.manager didUpdateHeading:heading(index)];
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
        [value locationManager:(id)value.manager didFailWithError:[NSError errorWithDomain:kCLErrorDomain code:kCLErrorLocationUnknown userInfo:nil]];
        BOOL ignoredTransient = !value.done;
        [value locationManager:(id)value.manager didFailWithError:[NSError errorWithDomain:kCLErrorDomain code:kCLErrorHeadingFailure userInfo:nil]];
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
        [value locationManagerDidChangeAuthorization:(id)manager];
        results[@"cancelled_permission"] = @{ @"was_pending": @(pendingPermission),
            @"starts": @(manager.starts), @"released_manager": @(value.manager == nil), @"state": state(value) };

        value = request();
        CLLocation *nonfinite = [[CLLocation alloc] initWithCoordinate:CLLocationCoordinate2DMake(1, 2)
            altitude:NAN horizontalAccuracy:5 verticalAccuracy:INFINITY course:INFINITY speed:INFINITY
            timestamp:NSDate.date];
        [value locationManager:(id)value.manager didUpdateLocations:@[nonfinite]];
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

        results[@"services_status"] = servicesCase(@"location.status", YES);
        results[@"services_status_disabled"] = servicesCase(@"location.status", NO);
        results[@"services_current"] = servicesCase(@"location.current", YES);
        results[@"services_watch"] = servicesCase(@"location.watch", YES);
        results[@"services_disabled"] = servicesCase(@"location.current", NO);
        results[@"services_cancelled"] = servicesCase(@"location.current", YES, YES);
        results[@"services_status_cancelled"] = servicesCase(@"location.status", YES, YES);
        results[@"services_error"] = servicesCase(@"location.status", YES, NO, YES);
        results[@"services_manager_error"] = servicesCase(@"location.current", YES, NO, NO, YES);
        results[@"main_queue_status"] = mainQueueCase(@"location.status");
        results[@"main_queue_current"] = mainQueueCase(@"location.current");
        servicesQueries = 0; servicesEnabled = NO; initialAuthorization = kCLAuthorizationStatusDenied;
        value = (CocoaPyLocationRequest *)CocoaPyLocation(@"location.request_permission", @{});
        results[@"permission_without_services_query"] = @{ @"queries": @(servicesQueries.load()), @"state": state(value) };
        [value close];

        NSData *data = [NSJSONSerialization dataWithJSONObject:results options:NSJSONWritingPrettyPrinted error:nil];
        if (!data) return 1;
        printf("%s\n", [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding].UTF8String);
    }
}
