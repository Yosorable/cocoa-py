#pragma once
#import <CoreLocation/CoreLocation.h>

static BOOL CocoaPyHeadingAvailable() {
#if TARGET_OS_IOS
    return CLLocationManager.headingAvailable;
#else
    return NO;
#endif
}
static BOOL CocoaPyLocationNumber(NSDictionary *args, NSString *key, double minimum, double maximum) {
    return CocoaPyNumber(args, key, minimum, maximum) &&
           CFGetTypeID((__bridge CFTypeRef)args[key]) != CFBooleanGetTypeID();
}
static BOOL CocoaPyLocationCapacity(NSDictionary *args) {
    return CocoaPyLocationNumber(args, @"capacity", 1, 4096) &&
           floor([args[@"capacity"] doubleValue]) == [args[@"capacity"] doubleValue];
}
static BOOL CocoaPyLocationAddress(NSDictionary *args) {
    if (!CocoaPyString(args, @"address")) return NO;
    NSString *address = args[@"address"];
    return [address stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet].length > 0 &&
           [address rangeOfString:@"\0"].location == NSNotFound;
}
static NSString *CocoaPyLocationStatus(CLAuthorizationStatus status) {
    switch (status) {
        case kCLAuthorizationStatusAuthorizedAlways:
#if COCOA_PY_UIKIT
        case kCLAuthorizationStatusAuthorizedWhenInUse: return @"authorized";
#else
            return @"authorized";
#endif
        case kCLAuthorizationStatusDenied: return @"denied";
        case kCLAuthorizationStatusRestricted: return @"restricted";
        default: return @"not_determined";
    }
}
static NSDictionary *CocoaPyCoordinates(CLLocation *value) {
    BOOL hasVerticalAccuracy = std::isfinite(value.verticalAccuracy) && value.verticalAccuracy >= 0;
    return @{ @"latitude": @(value.coordinate.latitude), @"longitude": @(value.coordinate.longitude),
        @"altitude": hasVerticalAccuracy && std::isfinite(value.altitude) ? @(value.altitude) : NSNull.null,
        @"horizontal_accuracy": @(value.horizontalAccuracy),
        @"vertical_accuracy": hasVerticalAccuracy ? @(value.verticalAccuracy) : NSNull.null,
        @"speed": std::isfinite(value.speed) && value.speed >= 0 ? @(value.speed) : NSNull.null,
        @"course": std::isfinite(value.course) && value.course >= 0 && value.course < 360 ? @(value.course) : NSNull.null,
        @"timestamp": @(value.timestamp.timeIntervalSince1970) };
}

@interface CocoaPyLocationRequest : CocoaPyRequest <CLLocationManagerDelegate>
@property(nonatomic, strong) CLLocationManager *manager;
@property(nonatomic, strong) CLGeocoder *geocoder;
@property(nonatomic) BOOL permissionOnly;
@property(nonatomic) BOOL streaming;
@property(nonatomic) BOOL started;
@property(nonatomic) double maxAge;
@property(nonatomic) NSTimeInterval startedAt;
@property(nonatomic) BOOL headingMode;
@property(nonatomic) BOOL trueNorth;
- (void)beginUpdates;
- (void)stopUpdates;
- (BOOL)acceptsTimestamp:(NSDate *)timestamp;
@end

@implementation CocoaPyLocationRequest
- (void)beginUpdates {
    if (self.closed || self.done) return;
    if (!self.headingMode || self.trueNorth) {
        CLAuthorizationStatus status = self.manager.authorizationStatus;
        if (status == kCLAuthorizationStatusNotDetermined) return;
        if (self.permissionOnly) { [self finish:CocoaPyLocationStatus(status)]; return; }
        if (status == kCLAuthorizationStatusDenied || status == kCLAuthorizationStatusRestricted) {
            [self stopUpdates];
            [self fail:@"permission" message:@"Location access was denied or restricted."]; return;
        }
    }
    if (self.started) return;
    self.started = YES;
    self.startedAt = NSDate.date.timeIntervalSince1970;
    if (!self.headingMode || self.trueNorth) [self.manager startUpdatingLocation];
#if TARGET_OS_IOS
    if (self.headingMode && !self.done && !self.closed) [self.manager startUpdatingHeading];
#endif
}
- (void)stopUpdates {
    if (!self.headingMode || self.trueNorth) [self.manager stopUpdatingLocation];
#if TARGET_OS_IOS
    if (self.headingMode) [self.manager stopUpdatingHeading];
#endif
}
- (BOOL)acceptsTimestamp:(NSDate *)timestamp {
    if (!timestamp) return NO;
    NSTimeInterval measured = timestamp.timeIntervalSince1970;
    if (!std::isfinite(measured)) return NO;
    // Zero rejects pre-start cache entries, not the time spent delivering a
    // newly measured sample. Start this window after any permission prompt.
    NSTimeInterval earliest = self.maxAge == 0 ? self.startedAt : NSDate.date.timeIntervalSince1970 - self.maxAge;
    return measured >= earliest;
}
- (void)locationManagerDidChangeAuthorization:(CLLocationManager *)manager {
    [self beginUpdates];
}
- (void)locationManager:(CLLocationManager *)manager didUpdateLocations:(NSArray<CLLocation *> *)locations {
    if (self.done || self.closed || self.headingMode) return;
    NSEnumerator *ordered = self.streaming ? locations.objectEnumerator : locations.reverseObjectEnumerator;
    for (CLLocation *value in ordered) {
        if (!CLLocationCoordinate2DIsValid(value.coordinate) || !std::isfinite(value.horizontalAccuracy) ||
            value.horizontalAccuracy < 0 || ![self acceptsTimestamp:value.timestamp]) continue;
        NSDictionary *coordinates = CocoaPyCoordinates(value);
        if (self.streaming) [self push:coordinates];
        else { [self finish:coordinates]; [self stopUpdates]; break; }
    }
}
- (void)locationManager:(CLLocationManager *)manager didUpdateHeading:(CLHeading *)value {
    if (self.done || self.closed || !self.headingMode) return;
    double magnetic = value.magneticHeading, actual = value.trueHeading, accuracy = value.headingAccuracy;
    if (!std::isfinite(magnetic) || magnetic < 0 || magnetic >= 360 ||
        !std::isfinite(accuracy) || accuracy < 0 || ![self acceptsTimestamp:value.timestamp]) return;
    BOOL hasTrue = std::isfinite(actual) && actual >= 0 && actual < 360;
    if (self.trueNorth && !hasTrue) return;
    NSDictionary *sample = @{ @"magnetic_heading": @(magnetic),
        @"true_heading": hasTrue ? @(actual) : NSNull.null,
        @"accuracy": @(accuracy), @"timestamp": @(value.timestamp.timeIntervalSince1970) };
    if (self.streaming) [self push:sample];
    else { [self finish:sample]; [self stopUpdates]; }
}
- (BOOL)locationManagerShouldDisplayHeadingCalibration:(CLLocationManager *)manager {
    // Invalid readings are skipped while the caller retains timeout control.
    return NO;
}
- (void)locationManager:(CLLocationManager *)manager didFailWithError:(NSError *)error {
    if (self.done || self.closed) return;
    if ([error.domain isEqual:kCLErrorDomain] && error.code == kCLErrorLocationUnknown) return;
    [self fail:([error.domain isEqual:kCLErrorDomain] && error.code == kCLErrorDenied) ? @"permission" : @"os"
        message:error.localizedDescription];
    [self stopUpdates];
}
- (void)close {
    [super close];
    [self stopUpdates]; self.manager.delegate = nil; self.manager = nil;
    [self.geocoder cancelGeocode]; self.geocoder = nil;
}
@end

static CocoaPyRequest *CocoaPyLocation(NSString *name, NSDictionary *args) {
    if ([name isEqual:@"location.heading_available"]) return CocoaPyValue(@(CocoaPyHeadingAvailable()));
    if ([name isEqual:@"location.status"]) {
        CLLocationManager *manager = [CLLocationManager new];
        return CocoaPyValue(@{ @"permission": CocoaPyLocationStatus(manager.authorizationStatus),
            @"enabled": @(CLLocationManager.locationServicesEnabled),
            @"precise": @(manager.accuracyAuthorization == CLAccuracyAuthorizationFullAccuracy) });
    }
    if ([name isEqual:@"location.geocode"] || [name isEqual:@"location.reverse_geocode"]) {
        BOOL reverse = [name isEqual:@"location.reverse_geocode"];
        if ((reverse && (!CocoaPyLocationNumber(args, @"latitude", -90, 90) || !CocoaPyLocationNumber(args, @"longitude", -180, 180))) ||
            (!reverse && !CocoaPyLocationAddress(args)))
            return CocoaPyFailure(@"value", @"An address containing non-whitespace text without NUL, or valid numeric coordinates, is required.");
        CocoaPyLocationRequest *request = [CocoaPyLocationRequest new];
        request.geocoder = [CLGeocoder new];
        __weak CocoaPyLocationRequest *weakRequest = request;
        CLGeocodeCompletionHandler completion = ^(NSArray<CLPlacemark *> *places, NSError *error) {
            CocoaPyLocationRequest *target = weakRequest;
            if (!target) return;
            if (error) { [target fail:@"os" message:error.localizedDescription]; return; }
            NSMutableArray *values = [NSMutableArray array];
            for (CLPlacemark *place in places) {
                [values addObject:@{
                    @"latitude": place.location ? @(place.location.coordinate.latitude) : NSNull.null,
                    @"longitude": place.location ? @(place.location.coordinate.longitude) : NSNull.null,
                    @"name": place.name ?: NSNull.null,
                    @"street": place.thoroughfare ?: NSNull.null,
                    @"street_number": place.subThoroughfare ?: NSNull.null,
                    @"city": place.locality ?: NSNull.null,
                    @"region": place.administrativeArea ?: NSNull.null,
                    @"country": place.country ?: NSNull.null,
                    @"country_code": place.ISOcountryCode ?: NSNull.null,
                    @"postal_code": place.postalCode ?: NSNull.null,
                    @"time_zone": place.timeZone.name ?: NSNull.null }];
            }
            [target finish:values];
        };
        if (reverse) {
            CLLocation *coordinate = [[CLLocation alloc] initWithLatitude:[args[@"latitude"] doubleValue]
                                                               longitude:[args[@"longitude"] doubleValue]];
            [request.geocoder reverseGeocodeLocation:coordinate completionHandler:completion];
        } else [request.geocoder geocodeAddressString:args[@"address"] completionHandler:completion];
        return request;
    }
    BOOL permission = [name isEqual:@"location.request_permission"];
    BOOL heading = [name isEqual:@"location.heading"] || [name isEqual:@"location.watch_heading"];
    BOOL stream = [name isEqual:@"location.watch"] || [name isEqual:@"location.watch_heading"];
    if (!permission && !stream && !heading && ![name isEqual:@"location.current"])
        return CocoaPyFailure(@"value", @"Unknown location operation.");
    if (!permission && (!CocoaPyLocationNumber(args, @"max_age", 0, 86400) || !CocoaPyLocationCapacity(args)))
        return CocoaPyFailure(@"value", @"Invalid location sampling options.");
    NSNumber *orientation = nil;
    if (heading) {
        NSDictionary *orientations = @{ @"portrait": @(CLDeviceOrientationPortrait),
            @"portrait_upside_down": @(CLDeviceOrientationPortraitUpsideDown),
            @"landscape_left": @(CLDeviceOrientationLandscapeLeft),
            @"landscape_right": @(CLDeviceOrientationLandscapeRight) };
        if (CocoaPyString(args, @"orientation")) orientation = orientations[args[@"orientation"]];
        if (!orientation || !CocoaPyLocationNumber(args, @"angle_filter", 0, 180) ||
            (args[@"true_north"] != (__bridge id)kCFBooleanTrue && args[@"true_north"] != (__bridge id)kCFBooleanFalse))
            return CocoaPyFailure(@"value", @"Invalid compass sampling options.");
        if (!CocoaPyHeadingAvailable())
            return CocoaPyFailure(@"not_implemented", @"This device does not provide compass headings.");
    } else if (!permission && (!CocoaPyLocationNumber(args, @"accuracy", 0.1, 10000) ||
                              !CocoaPyLocationNumber(args, @"distance_filter", 0, 100000)))
        return CocoaPyFailure(@"value", @"Invalid location sampling options.");
    BOOL needsLocation = !heading || [args[@"true_north"] boolValue];
    if (needsLocation && !CLLocationManager.locationServicesEnabled && !permission)
        return CocoaPyFailure(@"permission", @"Location Services are disabled.");
    CocoaPyLocationRequest *request = [CocoaPyLocationRequest new];
    request.permissionOnly = permission; request.streaming = stream;
    request.headingMode = heading; request.trueNorth = heading && needsLocation;
    request.maxAge = [args[@"max_age"] doubleValue];
    request.capacity = permission ? 1 : [args[@"capacity"] unsignedIntegerValue];
    request.manager = [CLLocationManager new];
    request.manager.desiredAccuracy = heading ? kCLLocationAccuracyKilometer :
        (permission ? kCLLocationAccuracyBest : [args[@"accuracy"] doubleValue]);
    request.manager.distanceFilter = [args[@"distance_filter"] doubleValue] ?: kCLDistanceFilterNone;
#if TARGET_OS_IOS
    if (heading) {
        request.manager.headingFilter = [args[@"angle_filter"] doubleValue] ?: kCLHeadingFilterNone;
        request.manager.headingOrientation = (CLDeviceOrientation)orientation.integerValue;
    }
#endif
    if (needsLocation && request.manager.authorizationStatus == kCLAuthorizationStatusNotDetermined) {
#if COCOA_PY_UIKIT
        NSString *key = @"NSLocationWhenInUseUsageDescription";
#else
        NSString *key = @"NSLocationUsageDescription";
        CocoaPyPrepareApplication();
#endif
        if (!CocoaPyUsageKey(key)) {
            [request fail:@"runtime" message:[NSString stringWithFormat:@"The host app must provide %@. On macOS, use the cocoa-py launcher.", key]];
            return request;
        }
        request.manager.delegate = request;
        [request.manager requestWhenInUseAuthorization];
    } else {
        request.manager.delegate = request;
        [request beginUpdates];
    }
    return request;
}
