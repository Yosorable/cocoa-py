// Run the production iOS motion coordinator against deterministic producers on
// macOS. No device, sensor, permission dialog, or simulator is involved.
#include "../../native/system/SystemRequest.h"
#import <CoreMotion/CoreMotion.h>

static NSUInteger managers = 0;
static BOOL usageDescription = YES, hardware = YES, throwNextStart = NO;
static CMAttitudeReferenceFrame frames = (CMAttitudeReferenceFrame)15;
@interface TestMotionManager : NSObject
@property(nonatomic) double accelerometerUpdateInterval, gyroUpdateInterval, magnetometerUpdateInterval, deviceMotionUpdateInterval;
@property(nonatomic) BOOL showsDeviceMovementDisplay;
@property(nonatomic) CMAttitudeReferenceFrame attitudeReferenceFrame;
@property(nonatomic, strong) NSMutableDictionary *handlers, *queues, *starts, *stops;
@property(nonatomic, readonly) BOOL accelerometerAvailable, gyroAvailable, magnetometerAvailable, deviceMotionAvailable;
+ (CMAttitudeReferenceFrame)availableAttitudeReferenceFrames;
@end
@implementation TestMotionManager
- (instancetype)init {
    if ((self = [super init])) {
        managers++; _handlers = [NSMutableDictionary dictionary]; _queues = [NSMutableDictionary dictionary];
        _starts = [NSMutableDictionary dictionary]; _stops = [NSMutableDictionary dictionary];
    }
    return self;
}
+ (CMAttitudeReferenceFrame)availableAttitudeReferenceFrames { return frames; }
- (BOOL)accelerometerAvailable { return hardware; }
- (BOOL)gyroAvailable { return hardware; }
- (BOOL)magnetometerAvailable { return hardware; }
- (BOOL)deviceMotionAvailable { return hardware; }
- (void)start:(NSString *)sensor queue:(NSOperationQueue *)queue handler:(id)handler {
    if (throwNextStart) { throwNextStart = NO; [NSException raise:@"FixtureStart" format:@"Start failed"]; }
    self.handlers[sensor] = [handler copy]; self.queues[sensor] = queue;
    self.starts[sensor] = @([self.starts[sensor] unsignedIntegerValue] + 1);
}
- (void)stop:(NSString *)sensor {
    [self.queues[sensor] cancelAllOperations];
    [self.handlers removeObjectForKey:sensor]; [self.queues removeObjectForKey:sensor];
    self.stops[sensor] = @([self.stops[sensor] unsignedIntegerValue] + 1);
}
- (void)startAccelerometerUpdatesToQueue:(NSOperationQueue *)q withHandler:(CMAccelerometerHandler)b { [self start:@"accelerometer" queue:q handler:b]; }
- (void)startGyroUpdatesToQueue:(NSOperationQueue *)q withHandler:(CMGyroHandler)b { [self start:@"gyroscope" queue:q handler:b]; }
- (void)startMagnetometerUpdatesToQueue:(NSOperationQueue *)q withHandler:(CMMagnetometerHandler)b { [self start:@"magnetometer" queue:q handler:b]; }
- (void)startDeviceMotionUpdatesUsingReferenceFrame:(CMAttitudeReferenceFrame)frame toQueue:(NSOperationQueue *)q withHandler:(CMDeviceMotionHandler)b {
    self.attitudeReferenceFrame = frame; [self start:@"device" queue:q handler:b];
}
- (void)stopAccelerometerUpdates { [self stop:@"accelerometer"]; }
- (void)stopGyroUpdates { [self stop:@"gyroscope"]; }
- (void)stopMagnetometerUpdates { [self stop:@"magnetometer"]; }
- (void)stopDeviceMotionUpdates { [self stop:@"device"]; }
@end
@interface TestAttitude : NSObject
@property(nonatomic) double roll, pitch, yaw;
@property(nonatomic) CMQuaternion quaternion;
@end
@implementation TestAttitude
@end
@interface TestMotionData : NSObject
@property(nonatomic) double timestamp;
@property(nonatomic) CMAcceleration acceleration, gravity, userAcceleration;
@property(nonatomic) CMRotationRate rotationRate;
@property(nonatomic, strong) TestAttitude *attitude;
@property(nonatomic) CMCalibratedMagneticField magneticField;
@end
@implementation TestMotionData
@end
// A raw magnetometer and device motion have different magneticField structs.
@interface TestMagnetometerData : NSObject
@property(nonatomic) double timestamp;
@property(nonatomic) CMMagneticField magneticField;
@end
@implementation TestMagnetometerData
@end
static BOOL TestUsageDescription(NSString *key) { return usageDescription; }
#undef COCOA_PY_UIKIT
#define COCOA_PY_UIKIT 1
#define CMMotionManager TestMotionManager
#define CocoaPyUsageKey TestUsageDescription
#include "../../native/system/Motion.h"
#undef CocoaPyUsageKey
#undef CMMotionManager

static CocoaPyMotionRequest *watch(NSString *sensor = @"accelerometer", double interval = 0.01, NSUInteger capacity = 128, NSString *frame = nil) {
    CocoaPyRequest *request = CocoaPyMotion(@"motion.watch", @{ @"sensor": sensor, @"interval": @(interval),
        @"capacity": @(capacity), @"reference_frame": frame ?: (id)NSNull.null });
    if (request.failure) [NSException raise:@"Fixture" format:@"Unexpected start failure: %@", request.failure];
    return (CocoaPyMotionRequest *)request;
}
static TestMotionData *sample(double timestamp) {
    TestMotionData *data = [TestMotionData new];
    data.timestamp = timestamp; data.acceleration = {1, 2, -1}; data.gravity = {0, 0, -1};
    data.userAcceleration = {0.1, 0.2, 0.3}; data.rotationRate = {0.1, 0.2, 0.3};
    data.attitude = [TestAttitude new]; data.attitude.roll = 0.5; data.attitude.pitch = -0.2;
    data.attitude.yaw = 0.4; data.attitude.quaternion = {0, 0, 0, 1};
    data.magneticField = {{10, 20, 30}, CMMagneticFieldCalibrationAccuracyHigh};
    return data;
}
static void emit(NSString *sensor, id value, NSError *error = nil) {
    void (^handler)(id, NSError *) = CocoaPyMotionHub.shared.manager.handlers[sensor];
    if (!handler) [NSException raise:@"Fixture" format:@"No handler for %@", sensor];
    handler(value, error);
}
static NSUInteger buffered(CocoaPyRequest *request) { return [[request snapshot:NO][@"buffered"] unsignedIntegerValue]; }
static NSDictionary *take(CocoaPyRequest *request) { return [request snapshot:YES][@"sample"]; }
static void pump() { for (int i = 0; i < 5; i++) CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.01, true); }

int main() {
    @autoreleasepool {
        NSMutableDictionary *results = [NSMutableDictionary dictionary];
        NSMutableArray *invalid = [NSMutableArray array];
        for (NSDictionary *options in @[
            @{ @"interval": @YES, @"capacity": @2, @"sensor": @"accelerometer" },
            @{ @"interval": @0.1, @"capacity": @YES, @"sensor": @"accelerometer" },
            @{ @"interval": @0.1, @"capacity": @1.5, @"sensor": @"accelerometer" },
            @{ @"interval": @0.009, @"capacity": @2, @"sensor": @"accelerometer" },
            @{ @"interval": @0.1, @"capacity": @4097, @"sensor": @"accelerometer" },
            @{ @"interval": @0.1, @"capacity": @2, @"sensor": @"device", @"reference_frame": @YES },
            @{ @"interval": @0.1, @"capacity": @2, @"sensor": @"device", @"reference_frame": @"unknown" },
            @{ @"interval": @0.1, @"capacity": @2, @"sensor": @"gyroscope", @"reference_frame": @"arbitrary" }]) {
            CocoaPyRequest *request = CocoaPyMotion(@"motion.watch", options);
            [invalid addObject:request.failure[@"kind"] ?: @"accepted"];
            [request close];
        }
        results[@"validation"] = @{ @"errors": invalid, @"managers_created": @(managers) };
        CocoaPyMotionHub *hub = CocoaPyMotionHub.shared;
        TestMotionManager *manager = hub.manager;
        [CocoaPyMotion(@"motion.available", @{}) close]; [CocoaPyMotion(@"motion.available", @{}) close];
        results[@"queries"] = @{ @"managers": @(managers), @"starts": @(manager.starts.count),
            @"frames": CocoaPyMotion(@"motion.reference_frames", @{}).result };

        CocoaPyMotionRequest *fast = watch(), *slow = watch(@"accelerometer", 0.05);
        double fastest = manager.accelerometerUpdateInterval;
        for (int i = 0; i <= 30; i++) emit(@"accelerometer", sample(100 + i * 0.01));
        results[@"fanout"] = @{ @"same_source": @(fast.source == slow.source), @"native_starts": manager.starts[@"accelerometer"],
            @"interval": @(fastest), @"fast_samples": @(buffered(fast)), @"slow_samples": @(buffered(slow)),
            @"acceleration": take(fast)[@"acceleration"] };
        [fast close];
        NSUInteger before = buffered(slow);
        emit(@"accelerometer", sample(101));
        results[@"close_fast"] = @{ @"interval": @(manager.accelerometerUpdateInterval),
            @"slow_continues": @(buffered(slow) == before + 1), @"fast_detached": @(fast.source == nil) };
        [slow close]; [slow close];
        results[@"last_close"] = @{ @"sources": @(hub.sources.count), @"handlers": @(manager.handlers.count),
            @"stops": manager.stops[@"accelerometer"] };

        CocoaPyMotionRequest *accel = watch(), *gyro = watch(@"gyroscope");
        NSOperationQueue *gyroQueue = gyro.source.queue;
        BOOL shared = accel.source.manager == gyro.source.manager;
        BOOL separate = accel.source.queue != gyroQueue;
        [accel close]; emit(@"gyroscope", sample(200));
        results[@"independent_sensors"] = @{ @"shared_manager": @(shared), @"separate_queues": @(separate),
            @"gyro_running": @(gyro.source.running), @"gyro_samples": @(buffered(gyro)), @"rotation": take(gyro)[@"rotation_rate"] };
        [gyro close];

        CocoaPyMotionRequest *magnet = watch(@"magnetometer");
        TestMagnetometerData *magnetic = [TestMagnetometerData new];
        magnetic.timestamp = 250; magnetic.magneticField = {10, -20, 30};
        emit(@"magnetometer", magnetic); results[@"raw_magnetic"] = take(magnet);
        magnetic.timestamp = 251; magnetic.magneticField = {NAN, 0, 0}; emit(@"magnetometer", magnetic);
        results[@"invalid_magnetic_skipped"] = @(buffered(magnet) == 0);
        [magnet close];

        CocoaPyMotionRequest *bounded = watch(@"accelerometer", 0.01, 2);
        for (int i = 0; i < 5; i++) emit(@"accelerometer", sample(300 + i * 0.02));
        results[@"bounded"] = @{ @"state": [bounded snapshot:NO], @"first": take(bounded)[@"timestamp"], @"second": take(bounded)[@"timestamp"] };
        [bounded close];

        CocoaPyMotionRequest *device = watch(@"device", 0.01, 128, @"magnetic_north");
        emit(@"device", sample(400)); results[@"device"] = take(device);
        results[@"native_frame"] = @(manager.attitudeReferenceFrame);
        CocoaPyRequest *conflict = CocoaPyMotion(@"motion.watch", @{ @"sensor": @"device", @"interval": @0.1, @"capacity": @2 });
        results[@"conflict"] = @{ @"error": conflict.failure[@"kind"], @"still_running": @(device.source.running) };
        [conflict close];
        CocoaPyMotionRequest *peer = watch(@"device", 0.02, 128, @"magnetic_north");
        results[@"same_frame"] = @(peer.source == device.source);
        [peer close];
        NSMutableArray *optional = [NSMutableArray array];
        for (int i = 0; i < 3; i++) {
            TestMotionData *data = sample(401 + i);
            data.magneticField = i == 0 ? CMCalibratedMagneticField{{1, 2, 3}, CMMagneticFieldCalibrationAccuracyUncalibrated}
                : (i == 1 ? CMCalibratedMagneticField{{NAN, 2, 3}, CMMagneticFieldCalibrationAccuracyHigh}
                          : CMCalibratedMagneticField{{1, 2, 3}, (CMMagneticFieldCalibrationAccuracy)99});
            emit(@"device", data); [optional addObject:take(device)];
        }
        results[@"optional_magnetic"] = optional;
        TestMotionData *invalidAttitude = sample(410); invalidAttitude.attitude.quaternion = {NAN, 0, 0, 1}; emit(@"device", invalidAttitude);
        invalidAttitude = sample(411); invalidAttitude.attitude = nil; emit(@"device", invalidAttitude);
        invalidAttitude = sample(412); invalidAttitude.gravity = {INFINITY, 0, 0}; emit(@"device", invalidAttitude);
        invalidAttitude = sample(413); invalidAttitude.attitude.quaternion = {0, 0, 0, 0}; emit(@"device", invalidAttitude);
        results[@"invalid_device_skipped"] = @(buffered(device) == 0);
        [device close]; device = watch(@"device", 0.01, 2, @"true_north");
        results[@"frame_after_close"] = @(manager.attitudeReferenceFrame);
        [device close];
        device = watch(@"device");
        emit(@"device", nil, [NSError errorWithDomain:CMErrorDomain code:CMErrorTrueNorthNotAvailable userInfo:nil]);
        pump(); results[@"unavailable_north"] = @{ @"state": [device snapshot:NO], @"detached": @(device.source == nil) };
        [device close];
        frames = CMAttitudeReferenceFrameXArbitraryZVertical;
        CocoaPyRequest *unsupported = CocoaPyMotion(@"motion.watch", @{ @"sensor": @"device", @"interval": @0.1, @"capacity": @2, @"reference_frame": @"true_north" });
        results[@"unsupported_frame"] = unsupported.failure[@"kind"]; [unsupported close]; frames = (CMAttitudeReferenceFrame)15;

        accel = watch();
        TestMotionData *bad = sample(NAN); emit(@"accelerometer", bad);
        bad = sample(-1); emit(@"accelerometer", bad);
        bad = sample(500); bad.acceleration = {INFINITY, 0, 0}; emit(@"accelerometer", bad);
        emit(@"accelerometer", nil);
        results[@"invalid_acceleration_skipped"] = @(buffered(accel) == 0);
        emit(@"accelerometer", sample(501)); emit(@"accelerometer", sample(501)); emit(@"accelerometer", sample(500));
        results[@"out_of_order_skipped"] = @(buffered(accel) == 1);
        CocoaPyMotionSource *oldSource = accel.source;
        void (^late)(id, NSError *) = manager.handlers[@"accelerometer"];
        [accel close]; accel = watch();
        late(sample(510), [NSError errorWithDomain:CMErrorDomain code:CMErrorNotAuthorized userInfo:nil]); pump();
        emit(@"accelerometer", sample(511));
        results[@"late_callbacks"] = @{ @"old_stopped": @(!oldSource.running), @"new_sample": @(buffered(accel)), @"new_error": accel.failure ?: NSNull.null };
        [accel close];

        accel = watch(); peer = watch(); gyro = watch(@"gyroscope");
        emit(@"accelerometer", nil, [NSError errorWithDomain:CMErrorDomain code:CMErrorNotAuthorized userInfo:nil]);
        CocoaPyRequest *duringFailure = CocoaPyMotion(@"motion.watch", @{ @"sensor": @"accelerometer", @"interval": @0.1, @"capacity": @2 });
        results[@"join_failing_source"] = duringFailure.failure[@"kind"]; [duringFailure close];
        pump(); emit(@"gyroscope", sample(600));
        results[@"error_cleanup"] = @{ @"first": [accel snapshot:NO], @"second": [peer snapshot:NO],
            @"detached": @(accel.source == nil && peer.source == nil), @"other_sensor_continues": @(buffered(gyro) == 1) };
        [accel close]; [peer close]; [gyro close];
        accel = watch(); emit(@"accelerometer", sample(601)); results[@"restart_after_error"] = @(buffered(accel) == 1); [accel close];

        throwNextStart = YES;
        CocoaPyRequest *failedStart = CocoaPyMotion(@"motion.watch", @{ @"sensor": @"device", @"interval": @0.1, @"capacity": @2 });
        results[@"start_exception"] = @{ @"error": failedStart.failure[@"kind"], @"sources": @(hub.sources.count) }; [failedStart close];
        usageDescription = NO;
        CocoaPyRequest *missingUsage = CocoaPyMotion(@"motion.watch", @{ @"sensor": @"accelerometer", @"interval": @0.1, @"capacity": @2 });
        results[@"missing_usage"] = missingUsage.failure[@"kind"]; [missingUsage close]; usageDescription = YES;
        hardware = NO;
        CocoaPyRequest *missingHardware = CocoaPyMotion(@"motion.watch", @{ @"sensor": @"accelerometer", @"interval": @0.1, @"capacity": @2 });
        results[@"missing_hardware"] = missingHardware.failure[@"kind"]; [missingHardware close]; hardware = YES;

        accel = watch(); CocoaPyMotionSource *concurrentSource = accel.source;
        dispatch_group_t producers = dispatch_group_create();
        for (int worker = 0; worker < 2; worker++) dispatch_group_async(producers, dispatch_get_global_queue(QOS_CLASS_DEFAULT, 0), ^{
            @autoreleasepool { for (int i = 0; i < 100; i++) [concurrentSource receive:sample(700 + i * 0.02) error:nil]; }
        });
        [accel close]; dispatch_group_wait(producers, DISPATCH_TIME_FOREVER);
        results[@"concurrent_close"] = @(accel.closed && buffered(accel) == 0 && accel.source == nil);
        results[@"final"] = @{ @"managers": @(managers), @"sources": @(hub.sources.count), @"handlers": @(manager.handlers.count) };
        NSData *json = [NSJSONSerialization dataWithJSONObject:results options:NSJSONWritingPrettyPrinted error:nil];
        if (!json) return 2;
        printf("%s\n", [[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding].UTF8String);
    }
}
