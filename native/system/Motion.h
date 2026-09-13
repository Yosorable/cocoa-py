#pragma once

static NSArray<NSString *> *CocoaPyMotionSensors() {
    return @[@"accelerometer", @"gyroscope", @"magnetometer", @"device"];
}
static NSArray<NSString *> *CocoaPyMotionFrameNames() {
    return @[@"arbitrary", @"arbitrary_corrected", @"magnetic_north", @"true_north"];
}
static BOOL CocoaPyMotionNumber(NSDictionary *args, NSString *key, double minimum, double maximum) {
    return CocoaPyNumber(args, key, minimum, maximum) &&
           CFGetTypeID((__bridge CFTypeRef)args[key]) != CFBooleanGetTypeID();
}

#if COCOA_PY_UIKIT
#import <CoreMotion/CoreMotion.h>

static CMAttitudeReferenceFrame CocoaPyMotionFrame(NSString *name) {
    return (CMAttitudeReferenceFrame)(1UL << [CocoaPyMotionFrameNames() indexOfObject:name]);
}
static NSArray *CocoaPyMotionFrames() {
    NSMutableArray *result = [NSMutableArray array];
    CMAttitudeReferenceFrame available = CMMotionManager.availableAttitudeReferenceFrames;
    for (NSString *name in CocoaPyMotionFrameNames())
        if (available & CocoaPyMotionFrame(name)) [result addObject:name];
    return result;
}
static NSDictionary *CocoaPyMotionVector(double x, double y, double z) {
    if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z)) return nil;
    return @{ @"x": @(x), @"y": @(y), @"z": @(z) };
}
static NSDictionary *CocoaPyMotionSample(NSString *sensor, id data, NSString *frame) {
    if (!data) return nil;
    double timestamp = [(CMLogItem *)data timestamp];
    if (!std::isfinite(timestamp) || timestamp < 0) return nil;
    NSDictionary *vector = nil;
    NSString *key = nil;
    if ([sensor isEqual:@"accelerometer"]) {
        CMAcceleration a = [(CMAccelerometerData *)data acceleration];
        vector = CocoaPyMotionVector(a.x * 9.80665, a.y * 9.80665, a.z * 9.80665); key = @"acceleration";
    } else if ([sensor isEqual:@"gyroscope"]) {
        CMRotationRate r = [(CMGyroData *)data rotationRate];
        vector = CocoaPyMotionVector(r.x, r.y, r.z); key = @"rotation_rate";
    } else if ([sensor isEqual:@"magnetometer"]) {
        CMMagneticField m = [(CMMagnetometerData *)data magneticField];
        vector = CocoaPyMotionVector(m.x, m.y, m.z); key = @"magnetic_field";
    } else {
        CMDeviceMotion *motion = data;
        CMAttitude *attitude = motion.attitude;
        if (!attitude) return nil;
        CMAcceleration a = motion.userAcceleration, g = motion.gravity;
        CMRotationRate r = motion.rotationRate;
        CMQuaternion q = attitude.quaternion;
        NSDictionary *acceleration = CocoaPyMotionVector(a.x * 9.80665, a.y * 9.80665, a.z * 9.80665);
        NSDictionary *gravity = CocoaPyMotionVector(g.x * 9.80665, g.y * 9.80665, g.z * 9.80665);
        NSDictionary *rotation = CocoaPyMotionVector(r.x, r.y, r.z);
        if (!acceleration || !gravity || !rotation || !std::isfinite(attitude.roll) ||
            !std::isfinite(attitude.pitch) || !std::isfinite(attitude.yaw) ||
            !std::isfinite(q.x) || !std::isfinite(q.y) || !std::isfinite(q.z) || !std::isfinite(q.w) ||
            (q.x == 0 && q.y == 0 && q.z == 0 && q.w == 0)) return nil;
        CMCalibratedMagneticField magnetic = motion.magneticField;
        NSDictionary *field = CocoaPyMotionVector(magnetic.field.x, magnetic.field.y, magnetic.field.z);
        NSString *accuracy = @"uncalibrated";
        if (field && magnetic.accuracy >= CMMagneticFieldCalibrationAccuracyLow &&
            magnetic.accuracy <= CMMagneticFieldCalibrationAccuracyHigh)
            accuracy = @[@"low", @"medium", @"high"][magnetic.accuracy];
        else field = nil;
        return @{ @"timestamp": @(timestamp), @"acceleration": acceleration, @"gravity": gravity,
            @"rotation_rate": rotation, @"attitude": @{ @"roll": @(attitude.roll), @"pitch": @(attitude.pitch), @"yaw": @(attitude.yaw) },
            @"quaternion": @{ @"x": @(q.x), @"y": @(q.y), @"z": @(q.z), @"w": @(q.w) },
            @"reference_frame": frame, @"magnetic_field": field ?: NSNull.null, @"magnetic_accuracy": accuracy };
    }
    return vector ? @{ @"timestamp": @(timestamp), key: vector } : nil;
}
static NSString *CocoaPyMotionErrorKind(NSError *error) {
    BOOL denied = [error.domain isEqual:CMErrorDomain] &&
        (error.code == CMErrorMotionActivityNotAuthorized || error.code == CMErrorNotAuthorized ||
         error.code == CMErrorMotionActivityNotEntitled || error.code == CMErrorNotEntitled);
    return denied ? @"permission" : @"os";
}

@class CocoaPyMotionSource;
@interface CocoaPyMotionRequest : CocoaPyRequest
@property(nonatomic, strong) CocoaPyMotionSource *source;
@property(nonatomic) double interval;
// Accessed only while holding the source lock.
@property(nonatomic) double nextTimestamp;
@end

@interface CocoaPyMotionSource : NSObject
@property(nonatomic, strong) CMMotionManager *manager;
@property(nonatomic, copy) NSString *sensor;
@property(nonatomic, copy) NSString *referenceFrame;
@property(nonatomic, strong) NSOperationQueue *queue;
@property(nonatomic, strong) NSHashTable<CocoaPyMotionRequest *> *subscribers;
@property(nonatomic, readonly) BOOL running;
@property(nonatomic, strong, readonly) NSError *failure;
- (NSArray<CocoaPyMotionRequest *> *)watches;
- (void)add:(CocoaPyMotionRequest *)request;
- (void)remove:(CocoaPyMotionRequest *)request;
- (void)updateInterval;
- (void)start;
- (void)stop;
- (void)receive:(id)data error:(NSError *)error;
@end

@interface CocoaPyMotionHub : NSObject
@property(nonatomic, strong) CMMotionManager *manager;
@property(nonatomic, strong) NSMutableDictionary<NSString *, CocoaPyMotionSource *> *sources;
+ (instancetype)shared;
- (NSDictionary *)available;
- (void)detach:(CocoaPyMotionRequest *)request;
@end

@implementation CocoaPyMotionRequest
- (void)close {
    [super close];
    [[CocoaPyMotionHub shared] detach:self];
}
@end

@implementation CocoaPyMotionSource {
    BOOL _running;
    NSError *_failure;
    double _lastTimestamp;
}
- (instancetype)init {
    if ((self = [super init])) {
        _subscribers = [NSHashTable weakObjectsHashTable];
        _lastTimestamp = -1;
    }
    return self;
}
- (BOOL)running { @synchronized(self) { return _running; } }
- (NSError *)failure { @synchronized(self) { return _failure; } }
- (NSArray *)watches { @synchronized(self) { return self.subscribers.allObjects; } }
- (void)add:(CocoaPyMotionRequest *)request {
    @synchronized(self) { [self.subscribers addObject:request]; }
}
- (void)remove:(CocoaPyMotionRequest *)request {
    @synchronized(self) { [self.subscribers removeObject:request]; }
}
- (void)updateInterval {
    double interval = 1;
    for (CocoaPyMotionRequest *request in self.watches) interval = fmin(interval, request.interval);
    if ([self.sensor isEqual:@"accelerometer"]) self.manager.accelerometerUpdateInterval = interval;
    else if ([self.sensor isEqual:@"gyroscope"]) self.manager.gyroUpdateInterval = interval;
    else if ([self.sensor isEqual:@"magnetometer"]) self.manager.magnetometerUpdateInterval = interval;
    else self.manager.deviceMotionUpdateInterval = interval;
}
- (void)start {
    [self updateInterval];
    @synchronized(self) { if (_running) return; _running = YES; }
    // Core Motion can cancel operations on a sensor's queue when that sensor
    // stops. Keep separate queues so closing one sensor cannot cancel another.
    self.queue = [NSOperationQueue new];
    self.queue.maxConcurrentOperationCount = 1;
    self.queue.qualityOfService = NSQualityOfServiceUserInitiated;
    __weak CocoaPyMotionSource *weakSource = self;
    if ([self.sensor isEqual:@"accelerometer"])
        [self.manager startAccelerometerUpdatesToQueue:self.queue withHandler:^(CMAccelerometerData *data, NSError *error) {
            [weakSource receive:data error:error];
        }];
    else if ([self.sensor isEqual:@"gyroscope"])
        [self.manager startGyroUpdatesToQueue:self.queue withHandler:^(CMGyroData *data, NSError *error) {
            [weakSource receive:data error:error];
        }];
    else if ([self.sensor isEqual:@"magnetometer"])
        [self.manager startMagnetometerUpdatesToQueue:self.queue withHandler:^(CMMagnetometerData *data, NSError *error) {
            [weakSource receive:data error:error];
        }];
    else
        [self.manager startDeviceMotionUpdatesUsingReferenceFrame:CocoaPyMotionFrame(self.referenceFrame)
            toQueue:self.queue withHandler:^(CMDeviceMotion *data, NSError *error) {
                [weakSource receive:data error:error];
            }];
}
- (void)stop {
    @synchronized(self) { if (!_running) return; _running = NO; }
    if ([self.sensor isEqual:@"accelerometer"]) [self.manager stopAccelerometerUpdates];
    else if ([self.sensor isEqual:@"gyroscope"]) [self.manager stopGyroUpdates];
    else if ([self.sensor isEqual:@"magnetometer"]) [self.manager stopMagnetometerUpdates];
    else [self.manager stopDeviceMotionUpdates];
    [self.queue cancelAllOperations]; self.queue = nil;
}
- (void)receive:(id)data error:(NSError *)error {
    @synchronized(self) {
        if (!_running || _failure) return;
        if (error) {
            _failure = error;
            // Stop and unsubscribe on the same thread used by start/close.
            // A closed source cannot deliver its error to a reopened sensor.
            dispatch_async(dispatch_get_main_queue(), ^{
                if (!self.running) return;
                for (CocoaPyMotionRequest *request in self.watches) {
                    [request fail:CocoaPyMotionErrorKind(error) message:error.localizedDescription];
                    [request close];
                }
            });
            return;
        }
        NSDictionary *sample = CocoaPyMotionSample(self.sensor, data, self.referenceFrame);
        if (!sample) return;
        double timestamp = [sample[@"timestamp"] doubleValue];
        if (timestamp <= _lastTimestamp) return;
        _lastTimestamp = timestamp;
        for (CocoaPyMotionRequest *request in self.subscribers.allObjects) {
            if (request.closed || request.done) continue;
            if (request.nextTimestamp >= 0 && timestamp + 1e-6 < request.nextTimestamp) continue;
            // Advance on a fixed timeline so harmless timestamp jitter does
            // not halve the delivery rate. Never generate catch-up samples.
            if (request.nextTimestamp < 0) request.nextTimestamp = timestamp + request.interval;
            else request.nextTimestamp += (floor(fmax(0, timestamp - request.nextTimestamp) / request.interval) + 1) * request.interval;
            [request push:sample];
        }
    }
}
@end

@implementation CocoaPyMotionHub
+ (instancetype)shared {
    static CocoaPyMotionHub *hub;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ hub = [self new]; });
    return hub;
}
- (instancetype)init {
    if ((self = [super init])) {
        _manager = [CMMotionManager new];
        _manager.showsDeviceMovementDisplay = NO;
        _sources = [NSMutableDictionary dictionary];
    }
    return self;
}
- (NSDictionary *)available {
    return @{ @"accelerometer": @(self.manager.accelerometerAvailable), @"gyroscope": @(self.manager.gyroAvailable),
              @"magnetometer": @(self.manager.magnetometerAvailable), @"device": @(self.manager.deviceMotionAvailable) };
}
- (void)detach:(CocoaPyMotionRequest *)request {
    CocoaPyMotionSource *source = request.source;
    if (!source) return;
    [source remove:request]; request.source = nil;
    if (!source.watches.count) {
        [source stop];
        if (self.sources[source.sensor] == source) [self.sources removeObjectForKey:source.sensor];
    } else if (!source.failure) [source updateInterval];
}
@end
#endif

static CocoaPyRequest *CocoaPyMotion(NSString *name, NSDictionary *args) {
    if ([name isEqual:@"motion.available"]) {
#if COCOA_PY_UIKIT
        return CocoaPyValue(CocoaPyMotionHub.shared.available);
#else
        return CocoaPyValue(@{ @"accelerometer": @NO, @"gyroscope": @NO, @"magnetometer": @NO, @"device": @NO });
#endif
    }
    if ([name isEqual:@"motion.reference_frames"]) {
#if COCOA_PY_UIKIT
        return CocoaPyValue(CocoaPyMotionFrames());
#else
        return CocoaPyValue(@[]);
#endif
    }
    if (![name isEqual:@"motion.watch"] || !CocoaPyString(args, @"sensor") ||
        ![CocoaPyMotionSensors() containsObject:args[@"sensor"]] ||
        !CocoaPyMotionNumber(args, @"interval", 0.01, 1) || !CocoaPyMotionNumber(args, @"capacity", 1, 4096) ||
        floor([args[@"capacity"] doubleValue]) != [args[@"capacity"] doubleValue])
        return CocoaPyFailure(@"value", @"Invalid motion sampling options.");
    NSString *sensor = args[@"sensor"];
    id frame = args[@"reference_frame"];
    BOOL device = [sensor isEqual:@"device"];
    if (frame == NSNull.null) frame = nil;
    if (frame && (!device || ![frame isKindOfClass:NSString.class] || ![CocoaPyMotionFrameNames() containsObject:frame]))
        return CocoaPyFailure(@"value", @"reference_frame is only supported for device motion and must name a known frame.");
    if (device && !frame) frame = @"arbitrary";
#if COCOA_PY_UIKIT
    CocoaPyMotionHub *hub = CocoaPyMotionHub.shared;
    if (![hub.available[sensor] boolValue])
        return CocoaPyFailure(@"not_implemented", @"This device does not provide the requested motion sensor.");
    if (device && ![CocoaPyMotionFrames() containsObject:frame])
        return CocoaPyFailure(@"not_implemented", @"This device does not provide the requested attitude reference frame.");
    if (!CocoaPyUsageKey(@"NSMotionUsageDescription"))
        return CocoaPyFailure(@"runtime", @"The host app must provide NSMotionUsageDescription.");
    CocoaPyMotionSource *source = hub.sources[sensor];
    NSError *failure = source.failure;
    if (failure) return CocoaPyFailure(CocoaPyMotionErrorKind(failure), failure.localizedDescription);
    if (source && device && ![source.referenceFrame isEqual:frame])
        return CocoaPyFailure(@"value", @"Device motion is already using another reference_frame. Close its watches before changing frames.");
    if (!source) {
        source = [CocoaPyMotionSource new]; source.manager = hub.manager;
        source.sensor = sensor; source.referenceFrame = frame;
        hub.sources[sensor] = source;
    }
    CocoaPyMotionRequest *request = [CocoaPyMotionRequest new];
    request.source = source; request.interval = [args[@"interval"] doubleValue];
    request.capacity = [args[@"capacity"] unsignedIntegerValue]; request.nextTimestamp = -1;
    [source add:request];
    @try { [source start]; }
    @catch (NSException *exception) {
        [request close];
        return CocoaPyFailure(@"runtime", exception.reason);
    }
    return request;
#else
    return CocoaPyFailure(@"not_implemented", @"Core Motion device sensors are not available in native macOS Python.");
#endif
}
