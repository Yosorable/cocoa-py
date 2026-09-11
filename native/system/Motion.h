#pragma once
#if COCOA_PY_UIKIT
#import <CoreMotion/CoreMotion.h>

@interface CocoaPyMotionRequest : CocoaPyRequest
@property(nonatomic, strong) CMMotionManager *manager;
@property(nonatomic, strong) NSOperationQueue *queue;
@end
@implementation CocoaPyMotionRequest
- (void)close {
    [super close];
    [self.manager stopAccelerometerUpdates]; [self.manager stopGyroUpdates];
    [self.manager stopMagnetometerUpdates]; [self.manager stopDeviceMotionUpdates];
    [self.queue cancelAllOperations]; self.manager = nil; self.queue = nil;
}
@end
static NSDictionary *CocoaPyVector(double x, double y, double z) { return @{ @"x": @(x), @"y": @(y), @"z": @(z) }; }
#endif

static CocoaPyRequest *CocoaPyMotion(NSString *name, NSDictionary *args) {
#if COCOA_PY_UIKIT
    CMMotionManager *manager = [CMMotionManager new];
    NSDictionary *available = @{ @"accelerometer": @(manager.accelerometerAvailable), @"gyroscope": @(manager.gyroAvailable),
                                @"magnetometer": @(manager.magnetometerAvailable), @"device": @(manager.deviceMotionAvailable) };
    if ([name isEqual:@"motion.available"]) return CocoaPyValue(available);
    if (![name isEqual:@"motion.watch"] || !CocoaPyString(args, @"sensor") ||
        !CocoaPyNumber(args, @"interval", 0.01, 1) || !CocoaPyNumber(args, @"capacity", 1, 4096))
        return CocoaPyFailure(@"value", @"Invalid motion sampling options.");
    NSString *sensor = args[@"sensor"];
    if (!available[sensor]) return CocoaPyFailure(@"value", @"Unknown motion sensor.");
    if (![available[sensor] boolValue]) return CocoaPyFailure(@"not_implemented", @"This device does not provide the requested motion sensor.");
    if (!CocoaPyUsageKey(@"NSMotionUsageDescription"))
        return CocoaPyFailure(@"runtime", @"The host app must provide NSMotionUsageDescription.");
    CocoaPyMotionRequest *request = [CocoaPyMotionRequest new];
    request.manager = manager; request.queue = [NSOperationQueue new]; request.queue.maxConcurrentOperationCount = 1;
    request.capacity = [args[@"capacity"] unsignedIntegerValue];
    double interval = [args[@"interval"] doubleValue];
    manager.accelerometerUpdateInterval = interval; manager.gyroUpdateInterval = interval;
    manager.magnetometerUpdateInterval = interval; manager.deviceMotionUpdateInterval = interval;
    __weak CocoaPyMotionRequest *weakRequest = request;
    void (^deliver)(NSDictionary *, NSError *) = ^(NSDictionary *sample, NSError *error) {
        CocoaPyMotionRequest *target = weakRequest;
        if (error) {
            BOOL denied = [error.domain isEqual:CMErrorDomain] &&
                (error.code == CMErrorMotionActivityNotAuthorized || error.code == CMErrorNotAuthorized ||
                 error.code == CMErrorMotionActivityNotEntitled || error.code == CMErrorNotEntitled);
            [target fail:denied ? @"permission" : @"os" message:error.localizedDescription];
            dispatch_async(dispatch_get_main_queue(), ^{ [target close]; });
        } else if (sample) [target push:sample];
    };
    if ([sensor isEqual:@"accelerometer"]) {
        [manager startAccelerometerUpdatesToQueue:request.queue withHandler:^(CMAccelerometerData *data, NSError *error) {
            CMAcceleration a = data.acceleration;
            deliver(data ? @{ @"timestamp": @(data.timestamp), @"acceleration": CocoaPyVector(a.x * 9.80665, a.y * 9.80665, a.z * 9.80665) } : nil, error);
        }];
    } else if ([sensor isEqual:@"gyroscope"]) {
        [manager startGyroUpdatesToQueue:request.queue withHandler:^(CMGyroData *data, NSError *error) {
            CMRotationRate r = data.rotationRate;
            deliver(data ? @{ @"timestamp": @(data.timestamp), @"rotation_rate": CocoaPyVector(r.x, r.y, r.z) } : nil, error);
        }];
    } else if ([sensor isEqual:@"magnetometer"]) {
        [manager startMagnetometerUpdatesToQueue:request.queue withHandler:^(CMMagnetometerData *data, NSError *error) {
            CMMagneticField m = data.magneticField;
            deliver(data ? @{ @"timestamp": @(data.timestamp), @"magnetic_field": CocoaPyVector(m.x, m.y, m.z) } : nil, error);
        }];
    } else {
        [manager startDeviceMotionUpdatesToQueue:request.queue withHandler:^(CMDeviceMotion *data, NSError *error) {
            CMAcceleration a = data.userAcceleration, g = data.gravity;
            CMRotationRate r = data.rotationRate; CMQuaternion q = data.attitude.quaternion;
            deliver(data ? @{ @"timestamp": @(data.timestamp),
                @"acceleration": CocoaPyVector(a.x * 9.80665, a.y * 9.80665, a.z * 9.80665),
                @"gravity": CocoaPyVector(g.x * 9.80665, g.y * 9.80665, g.z * 9.80665),
                @"rotation_rate": CocoaPyVector(r.x, r.y, r.z),
                @"attitude": @{ @"roll": @(data.attitude.roll), @"pitch": @(data.attitude.pitch), @"yaw": @(data.attitude.yaw) },
                @"quaternion": @{ @"x": @(q.x), @"y": @(q.y), @"z": @(q.z), @"w": @(q.w) } } : nil, error);
        }];
    }
    return request;
#else
    if ([name isEqual:@"motion.available"]) return CocoaPyValue(@{ @"accelerometer": @NO, @"gyroscope": @NO, @"magnetometer": @NO, @"device": @NO });
    return CocoaPyFailure(@"not_implemented", @"Core Motion device sensors are not available in native macOS Python.");
#endif
}
