#pragma once
#import <CoreLocation/CoreLocation.h>

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
    return @{ @"latitude": @(value.coordinate.latitude), @"longitude": @(value.coordinate.longitude),
        @"altitude": value.verticalAccuracy >= 0 ? @(value.altitude) : NSNull.null,
        @"horizontal_accuracy": @(value.horizontalAccuracy),
        @"vertical_accuracy": value.verticalAccuracy >= 0 ? @(value.verticalAccuracy) : NSNull.null,
        @"speed": value.speed >= 0 ? @(value.speed) : NSNull.null,
        @"course": value.course >= 0 ? @(value.course) : NSNull.null,
        @"timestamp": @(value.timestamp.timeIntervalSince1970) };
}

@interface CocoaPyLocationRequest : CocoaPyRequest <CLLocationManagerDelegate>
@property(nonatomic, strong) CLLocationManager *manager;
@property(nonatomic, strong) CLGeocoder *geocoder;
@property(nonatomic) BOOL permissionOnly;
@property(nonatomic) BOOL streaming;
@property(nonatomic) BOOL started;
@property(nonatomic) double maxAge;
- (void)beginUpdates;
@end

@implementation CocoaPyLocationRequest
- (void)beginUpdates {
    if (self.closed || self.done) return;
    CLAuthorizationStatus status = self.manager.authorizationStatus;
    if (status == kCLAuthorizationStatusNotDetermined) return;
    if (self.permissionOnly) { [self finish:CocoaPyLocationStatus(status)]; return; }
    if (status == kCLAuthorizationStatusDenied || status == kCLAuthorizationStatusRestricted) {
        [self.manager stopUpdatingLocation];
        [self fail:@"permission" message:@"Location access was denied or restricted."]; return;
    }
    if (self.started) return;
    self.started = YES;
    [self.manager startUpdatingLocation];
}
- (void)locationManagerDidChangeAuthorization:(CLLocationManager *)manager {
    [self beginUpdates];
}
- (void)locationManager:(CLLocationManager *)manager didUpdateLocations:(NSArray<CLLocation *> *)locations {
    if (self.done || self.closed) return;
    for (CLLocation *value in locations) {
        if (value.horizontalAccuracy < 0 || -value.timestamp.timeIntervalSinceNow > self.maxAge) continue;
        NSDictionary *coordinates = CocoaPyCoordinates(value);
        if (self.streaming) [self push:coordinates];
        else { [self finish:coordinates]; [manager stopUpdatingLocation]; break; }
    }
}
- (void)locationManager:(CLLocationManager *)manager didFailWithError:(NSError *)error {
    if ([error.domain isEqual:kCLErrorDomain] && error.code == kCLErrorLocationUnknown) return;
    [self fail:([error.domain isEqual:kCLErrorDomain] && error.code == kCLErrorDenied) ? @"permission" : @"os"
        message:error.localizedDescription];
    [manager stopUpdatingLocation];
}
- (void)close {
    [self.manager stopUpdatingLocation]; self.manager.delegate = nil; self.manager = nil;
    [self.geocoder cancelGeocode]; self.geocoder = nil;
    [super close];
}
@end

static CocoaPyRequest *CocoaPyLocation(NSString *name, NSDictionary *args) {
    if ([name isEqual:@"location.status"]) {
        CLLocationManager *manager = [CLLocationManager new];
        return CocoaPyValue(@{ @"permission": CocoaPyLocationStatus(manager.authorizationStatus),
            @"enabled": @(CLLocationManager.locationServicesEnabled),
            @"precise": @(manager.accuracyAuthorization == CLAccuracyAuthorizationFullAccuracy) });
    }
    if ([name isEqual:@"location.geocode"] || [name isEqual:@"location.reverse_geocode"]) {
        BOOL reverse = [name isEqual:@"location.reverse_geocode"];
        if ((reverse && (!CocoaPyNumber(args, @"latitude", -90, 90) || !CocoaPyNumber(args, @"longitude", -180, 180))) ||
            (!reverse && !CocoaPyString(args, @"address")))
            return CocoaPyFailure(@"value", @"A nonempty address or valid coordinates are required.");
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
    BOOL stream = [name isEqual:@"location.watch"];
    if (!permission && !stream && ![name isEqual:@"location.current"])
        return CocoaPyFailure(@"value", @"Unknown location operation.");
    if (!permission && (!CocoaPyNumber(args, @"accuracy", 0.1, 10000) ||
        !CocoaPyNumber(args, @"max_age", 0, 86400) || !CocoaPyNumber(args, @"distance_filter", 0, 100000) ||
        !CocoaPyNumber(args, @"capacity", 1, 4096)))
        return CocoaPyFailure(@"value", @"Invalid location sampling options.");
    if (!CLLocationManager.locationServicesEnabled && !permission)
        return CocoaPyFailure(@"permission", @"Location Services are disabled.");
    CocoaPyLocationRequest *request = [CocoaPyLocationRequest new];
    request.permissionOnly = permission; request.streaming = stream;
    request.maxAge = [args[@"max_age"] doubleValue];
    request.capacity = permission ? 1 : [args[@"capacity"] unsignedIntegerValue];
    request.manager = [CLLocationManager new];
    request.manager.desiredAccuracy = permission ? kCLLocationAccuracyBest : [args[@"accuracy"] doubleValue];
    request.manager.distanceFilter = [args[@"distance_filter"] doubleValue] ?: kCLDistanceFilterNone;
    if (request.manager.authorizationStatus == kCLAuthorizationStatusNotDetermined) {
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
