#include <Python.h>

#import <AVFoundation/AVFoundation.h>
#import <Photos/Photos.h>
#import <PhotosUI/PhotosUI.h>
#include "../common/CocoaPlatform.h"
#import <ImageIO/ImageIO.h>
#include <memory>
#include <cmath>
#if COCOA_PY_UIKIT
#import <UIKit/UIKit.h>
#endif
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>

#include "PhotosModule.h"

typedef void (^CocoaPyPhotoPickCompletion)(NSArray<NSDictionary *> *results, NSString *errorMessage, BOOL cancelled);

#if COCOA_PY_UIKIT
@interface CocoaPyPhotoPickerDelegate : NSObject <PHPickerViewControllerDelegate, UIAdaptivePresentationControllerDelegate>
#else
@interface CocoaPyPhotoPickerDelegate : NSObject <PHPickerViewControllerDelegate, NSWindowDelegate>
@property(nonatomic, strong) NSWindow *presentationWindow;
#endif
@property(nonatomic, weak) PHPickerViewController *picker;
- (void)cancel;
- (void)dismissPicker;
- (instancetype)initWithAllowImages:(BOOL)allowImages
                        allowVideos:(BOOL)allowVideos
                         completion:(CocoaPyPhotoPickCompletion)completion;
@end

static NSString *CocoaPySanitizedBaseName(NSString *candidate, NSString *fallback) {
    NSString *base = candidate.length > 0 ? candidate : fallback;
    NSCharacterSet *allowed = [NSCharacterSet characterSetWithCharactersInString:@"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_ "];
    NSMutableString *out = [NSMutableString stringWithCapacity:base.length];
    for (NSUInteger i = 0; i < base.length; i++) {
        unichar c = [base characterAtIndex:i];
        if ([allowed characterIsMember:c]) {
            [out appendFormat:@"%C", c];
        } else {
            [out appendString:@"_"];
        }
    }
    NSString *trimmed = [out stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
    return trimmed.length > 0 ? trimmed : fallback;
}

static NSURL *CocoaPyCopyToTempURL(NSItemProvider *provider, NSURL *srcURL, NSString *folder, NSString *defaultExt, NSError **errorOut) {
    NSFileManager *fm = [NSFileManager defaultManager];
    [fm createDirectoryAtPath:folder withIntermediateDirectories:YES attributes:nil error:errorOut];
    if (errorOut && *errorOut) return nil;

    NSString *ext = srcURL.pathExtension.length > 0 ? srcURL.pathExtension.lowercaseString : defaultExt;
    NSString *base = CocoaPySanitizedBaseName(provider.suggestedName, @"picked_media");
    NSString *filename = [NSString stringWithFormat:@"%@_%@.%@", base, NSUUID.UUID.UUIDString.lowercaseString, ext];
    NSString *dstPath = [folder stringByAppendingPathComponent:filename];
    NSURL *dstURL = [NSURL fileURLWithPath:dstPath];

    NSError *moveError = nil;
    if ([fm moveItemAtURL:srcURL toURL:dstURL error:&moveError]) {
        return dstURL;
    }

    NSError *copyError = nil;
    [fm copyItemAtURL:srcURL toURL:dstURL error:&copyError];
    if (copyError) {
        if (errorOut) {
            *errorOut = copyError;
        }
        return nil;
    }
    return dstURL;
}

@implementation CocoaPyPhotoPickerDelegate {
    CocoaPyPhotoPickCompletion _completion;
    BOOL _allowImages;
    BOOL _allowVideos;
    BOOL _didFinishPicking;
}

- (instancetype)initWithAllowImages:(BOOL)allowImages
                        allowVideos:(BOOL)allowVideos
                         completion:(CocoaPyPhotoPickCompletion)completion {
    self = [super init];
    if (self) {
        _allowImages = allowImages;
        _allowVideos = allowVideos;
        _completion = [completion copy];
    }
    return self;
}

- (void)finishWithResults:(NSArray<NSDictionary *> *)results errorMessage:(NSString *)errorMessage cancelled:(BOOL)cancelled {
    if (!_completion) return;
    CocoaPyPhotoPickCompletion completion = _completion;
    _completion = nil;
    completion(results, errorMessage, cancelled);
}

- (void)picker:(PHPickerViewController *)picker didFinishPicking:(NSArray<PHPickerResult *> *)results {
    _didFinishPicking = YES;
    [self dismissPicker];
    if (results.count == 0) {
        [self finishWithResults:nil errorMessage:nil cancelled:YES];
        return;
    }

    NSString *pickerDir = [NSTemporaryDirectory() stringByAppendingPathComponent:@"cocoa_py_photos"];
    NSUInteger totalCount = results.count;
    NSMutableArray *entries = [NSMutableArray arrayWithCapacity:totalCount];
    for (NSUInteger i = 0; i < totalCount; i++) {
        [entries addObject:[NSNull null]];
    }
    NSMutableArray<NSString *> *errors = [NSMutableArray array];
    dispatch_group_t group = dispatch_group_create();

    for (NSUInteger index = 0; index < totalCount; index++) {
        PHPickerResult *item = results[index];
        NSItemProvider *provider = item.itemProvider;
        const BOOL isImage = [provider hasItemConformingToTypeIdentifier:UTTypeImage.identifier];
        const BOOL isVideo = [provider hasItemConformingToTypeIdentifier:UTTypeMovie.identifier];
        const BOOL chooseVideo = _allowVideos && isVideo && !_allowImages;
        const BOOL chooseImage = _allowImages && isImage && !_allowVideos;
        const BOOL chooseBoth = _allowVideos && _allowImages;

        if (!(chooseVideo || chooseImage || chooseBoth)) {
            continue;
        }

        if (_allowVideos && isVideo && (!(_allowImages && isImage))) {
            dispatch_group_enter(group);
            [provider loadFileRepresentationForTypeIdentifier:UTTypeMovie.identifier
                                            completionHandler:^(NSURL *url, NSError *error) {
                @autoreleasepool {
                    if (error || !url) {
                        @synchronized (errors) { [errors addObject:(error.localizedDescription ?: @"Failed to load selected video.")]; }
                        dispatch_group_leave(group);
                        return;
                    }
                    NSError *copyError = nil;
                    NSURL *dstURL = CocoaPyCopyToTempURL(provider, url, pickerDir, @"mov", &copyError);
                    if (!dstURL) {
                        @synchronized (errors) { [errors addObject:(copyError.localizedDescription ?: @"Failed to copy selected video.")]; }
                        dispatch_group_leave(group);
                        return;
                    }

                    AVURLAsset *asset = [AVURLAsset URLAssetWithURL:dstURL options:nil];
                    Float64 duration = CMTimeGetSeconds(asset.duration);
                    if (!isfinite(duration) || duration < 0) duration = 0;

                    NSDictionary *entry = @{
                        @"type": @"video",
                        @"path": dstURL.path ?: @"",
                        @"filename": dstURL.lastPathComponent ?: @"",
                        @"duration": @(duration),
                    };
                    @synchronized (entries) { entries[index] = entry; }
                    dispatch_group_leave(group);
                }
            }];
            continue;
        }

        if (_allowImages && isImage) {
            dispatch_group_enter(group);
            [provider loadFileRepresentationForTypeIdentifier:UTTypeImage.identifier
                                            completionHandler:^(NSURL *url, NSError *error) {
                @autoreleasepool {
                    if (error || !url) {
                        @synchronized (errors) { [errors addObject:(error.localizedDescription ?: @"Failed to load selected image.")]; }
                        dispatch_group_leave(group);
                        return;
                    }
                    NSError *copyError = nil;
                    NSURL *dstURL = CocoaPyCopyToTempURL(provider, url, pickerDir, @"jpg", &copyError);
                    if (!dstURL) {
                        @synchronized (errors) { [errors addObject:(copyError.localizedDescription ?: @"Failed to copy selected image.")]; }
                        dispatch_group_leave(group);
                        return;
                    }

                    CGImageSourceRef source = CGImageSourceCreateWithURL((__bridge CFURLRef)dstURL, nullptr);
                    NSDictionary *properties = source ? CFBridgingRelease(CGImageSourceCopyPropertiesAtIndex(source, 0, nullptr)) : nil;
                    if (source) CFRelease(source);
                    if (!properties) {
                        @synchronized (errors) { [errors addObject:@"Failed to decode selected image."]; }
                        dispatch_group_leave(group);
                        return;
                    }
                    size_t width = [properties[(__bridge NSString *)kCGImagePropertyPixelWidth] unsignedLongLongValue];
                    size_t height = [properties[(__bridge NSString *)kCGImagePropertyPixelHeight] unsignedLongLongValue];

                    NSDictionary *entry = @{
                        @"type": @"image",
                        @"path": dstURL.path ?: @"",
                        @"filename": dstURL.lastPathComponent ?: @"",
                        @"width": @(width),
                        @"height": @(height),
                    };
                    @synchronized (entries) { entries[index] = entry; }
                    dispatch_group_leave(group);
                }
            }];
            continue;
        }
    }

    dispatch_group_notify(group, dispatch_get_main_queue(), ^{
        // Filter out NSNull (skipped or failed items), preserving order
        NSMutableArray<NSDictionary *> *ordered = [NSMutableArray arrayWithCapacity:totalCount];
        for (id obj in entries) {
            if ([obj isKindOfClass:[NSDictionary class]]) {
                [ordered addObject:obj];
            }
        }
        if (ordered.count > 0) {
            [self finishWithResults:ordered errorMessage:nil cancelled:NO];
            return;
        }
        if (errors.count > 0) {
            [self finishWithResults:nil errorMessage:errors.firstObject cancelled:NO];
            return;
        }
        [self finishWithResults:nil errorMessage:nil cancelled:YES];
    });
}

- (void)dismissPicker {
#if COCOA_PY_UIKIT
    [self.picker dismissViewControllerAnimated:YES completion:nil];
#else
    self.presentationWindow.delegate = nil;
    [self.presentationWindow orderOut:nil];
    [self.presentationWindow close];
    self.presentationWindow = nil;
#endif
}
- (void)cancel {
    _didFinishPicking = YES;
    [self dismissPicker];
    [self finishWithResults:nil errorMessage:nil cancelled:YES];
}
#if COCOA_PY_UIKIT
- (void)presentationControllerDidDismiss:(UIPresentationController *)presentationController {
    if (!_didFinishPicking) [self finishWithResults:nil errorMessage:nil cancelled:YES];
}
#else
- (BOOL)windowShouldClose:(NSWindow *)sender { [self cancel]; return NO; }
#endif

@end

static NSMutableArray<CocoaPyPhotoPickerDelegate *> *activeDelegates(void) {
    static NSMutableArray<CocoaPyPhotoPickerDelegate *> *store = nil;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        store = [NSMutableArray new];
    });
    return store;
}

static BOOL ensurePhotoAddPermission(NSString **errorMessage) {
    __block PHAuthorizationStatus status = [PHPhotoLibrary authorizationStatusForAccessLevel:PHAccessLevelAddOnly];
    if (status == PHAuthorizationStatusNotDetermined) {
        NSBundle *bundle = NSBundle.mainBundle;
        if (![bundle objectForInfoDictionaryKey:@"NSPhotoLibraryAddUsageDescription"] &&
            ![bundle objectForInfoDictionaryKey:@"NSPhotoLibraryUsageDescription"]) {
            if (errorMessage) *errorMessage = @"The host must provide a Photos usage description before requesting access.";
            return NO;
        }
#if !COCOA_PY_UIKIT
        CocoaPyRunOnMain(^{ CocoaPyPrepareApplication(); });
#endif
        dispatch_semaphore_t sema = dispatch_semaphore_create(0);
        [PHPhotoLibrary requestAuthorizationForAccessLevel:PHAccessLevelAddOnly handler:^(PHAuthorizationStatus s) {
            status = s;
            dispatch_semaphore_signal(sema);
        }];
        if (!CocoaPyWaitSemaphore(sema, 120.0)) {
            if (errorMessage) *errorMessage = @"Photos permission request timed out.";
            return NO;
        }
    }

    if (status == PHAuthorizationStatusAuthorized || status == PHAuthorizationStatusLimited) {
        return YES;
    }

    if (errorMessage) {
        *errorMessage = @"No permission to save to Photos.";
    }
    return NO;
}

#if COCOA_PY_UIKIT
static UIViewController *topViewController(void) {
    UIWindowScene *targetScene = nil;
    for (UIScene *scene in UIApplication.sharedApplication.connectedScenes) {
        if (![scene isKindOfClass:[UIWindowScene class]]) continue;
        UIWindowScene *ws = (UIWindowScene *)scene;
        if (ws.activationState == UISceneActivationStateForegroundActive) {
            targetScene = ws;
            break;
        }
        if (!targetScene) targetScene = ws;
    }
    if (!targetScene) return nil;

    UIWindow *window = nil;
    for (UIWindow *w in targetScene.windows) {
        if (w.isKeyWindow) {
            window = w;
            break;
        }
        if (!window) window = w;
    }
    if (!window) return nil;

    UIViewController *vc = window.rootViewController;
    while (vc.presentedViewController) {
        vc = vc.presentedViewController;
    }
    return vc;
}

#endif

static void parseMediaTypes(PyObject *typesObj, BOOL *allowImages, BOOL *allowVideos) {
    *allowImages = YES;
    *allowVideos = NO;
    if (typesObj == NULL || typesObj == Py_None) {
        return;
    }

    *allowImages = NO;
    *allowVideos = NO;

    void (^applyOne)(PyObject *) = ^(PyObject *obj) {
        if (!PyUnicode_Check(obj)) return;
        const char *c = PyUnicode_AsUTF8(obj);
        if (!c) return;
        NSString *s = [@(c) lowercaseString];
        if ([s isEqualToString:@"image"] || [s isEqualToString:@"images"] || [s isEqualToString:@"photo"]) {
            *allowImages = YES;
        } else if ([s isEqualToString:@"video"] || [s isEqualToString:@"videos"] || [s isEqualToString:@"movie"]) {
            *allowVideos = YES;
        }
    };

    if (PyUnicode_Check(typesObj)) {
        applyOne(typesObj);
    } else if (PySequence_Check(typesObj)) {
        Py_ssize_t n = PySequence_Size(typesObj);
        for (Py_ssize_t i = 0; i < n; i++) {
            PyObject *item = PySequence_GetItem(typesObj, i);
            if (!item) continue;
            applyOne(item);
            Py_DECREF(item);
        }
    }

    if (!*allowImages && !*allowVideos) {
        *allowImages = YES;
    }
}

static PyObject *entryToPyDict(NSDictionary *entry) {
    PyObject *dict = PyDict_New();
    if (!dict) return NULL;
    for (id key in entry) {
        if (![key isKindOfClass:[NSString class]]) continue;
        NSString *k = (NSString *)key;
        id value = entry[k];
        PyObject *pyValue = NULL;
        if ([value isKindOfClass:[NSString class]]) {
            pyValue = PyUnicode_FromString([(NSString *)value UTF8String] ?: "");
        } else if ([value isKindOfClass:[NSNumber class]]) {
            const char *ctype = [(NSNumber *)value objCType];
            if (strcmp(ctype, @encode(float)) == 0 || strcmp(ctype, @encode(double)) == 0) {
                pyValue = PyFloat_FromDouble([(NSNumber *)value doubleValue]);
            } else {
                pyValue = PyLong_FromLongLong([(NSNumber *)value longLongValue]);
            }
        } else {
            continue;
        }
        if (!pyValue) {
            Py_DECREF(dict);
            return NULL;
        }
        PyDict_SetItemString(dict, k.UTF8String ?: "", pyValue);
        Py_DECREF(pyValue);
    }
    return dict;
}

static PyObject *py_photos_pick_media(PyObject *self, PyObject *args, PyObject *kwargs) {
    static const char *kwlist[] = {"types", "limit", "timeout", NULL};
    PyObject *typesObj = Py_None;
    int limit = 1;
    double timeout = 300.0;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|Oid", (char **)kwlist, &typesObj, &limit, &timeout)) {
        return NULL;
    }

    if (!std::isfinite(timeout) || timeout <= 0 || timeout > 3600 || limit < 0) {
        PyErr_SetString(PyExc_ValueError, "limit must be nonnegative and timeout must be in (0, 3600]."); return nullptr;
    }
    BOOL allowImages = YES;
    BOOL allowVideos = NO;
    parseMediaTypes(typesObj, &allowImages, &allowVideos);

#if COCOA_PY_UIKIT
    if (NSThread.isMainThread) {
        PyErr_SetString(PyExc_RuntimeError, "Open the photo picker from the Python execution thread, not a UI callback."); return nullptr;
    }
#else
    if (!NSThread.isMainThread && !NSApp.isRunning) {
        PyErr_SetString(PyExc_RuntimeError, "Open desktop photo pickers on the main Python thread."); return nullptr;
    }
#endif

    __block NSArray<NSDictionary *> *pickedEntries = nil;
    __block NSString *errorMessage = nil;
    __block BOOL cancelled = NO;
    dispatch_semaphore_t sema = dispatch_semaphore_create(0);
    __block __weak CocoaPyPhotoPickerDelegate *weakDelegate = nil;

    dispatch_async(dispatch_get_main_queue(), ^{
        @try {
#if COCOA_PY_UIKIT
        UIViewController *top = topViewController();
        if (!top) {
            errorMessage = @"No active view controller available to present photo picker.";
            dispatch_semaphore_signal(sema);
            return;
        }

#else
        CocoaPyPrepareApplication();
#endif
        PHPickerConfiguration *config = [[PHPickerConfiguration alloc] init];
        config.selectionLimit = limit <= 0 ? 0 : limit;
        if (allowImages && allowVideos) {
            config.filter = [PHPickerFilter anyFilterMatchingSubfilters:@[
                [PHPickerFilter imagesFilter],
                [PHPickerFilter videosFilter],
            ]];
        } else if (allowVideos) {
            config.filter = [PHPickerFilter videosFilter];
        } else {
            config.filter = [PHPickerFilter imagesFilter];
        }

        PHPickerViewController *picker = [[PHPickerViewController alloc] initWithConfiguration:config];

        CocoaPyPhotoPickerDelegate *delegate = [[CocoaPyPhotoPickerDelegate alloc] initWithAllowImages:allowImages
                                                               allowVideos:allowVideos
                                                                completion:^(NSArray<NSDictionary *> *results, NSString *error, BOOL wasCancelled) {
            pickedEntries = results;
            errorMessage = error;
            cancelled = wasCancelled;
            CocoaPyPhotoPickerDelegate *finished = weakDelegate;
            if (finished) {
                @synchronized (activeDelegates()) { [activeDelegates() removeObject:finished]; }
            }
            dispatch_semaphore_signal(sema);
        }];

        weakDelegate = delegate;
        delegate.picker = picker;
        picker.delegate = delegate;
#if COCOA_PY_UIKIT
        picker.presentationController.delegate = delegate;
#endif
        @synchronized (activeDelegates()) {
            [activeDelegates() addObject:delegate];
        }
#if COCOA_PY_UIKIT
        if (top.isBeingDismissed || top.isBeingPresented || top.view.window == nil) {
            @synchronized (activeDelegates()) {
                [activeDelegates() removeObject:delegate];
            }
            errorMessage = @"Photo picker cannot be presented right now. Please try again.";
            dispatch_semaphore_signal(sema);
            return;
        }
        [top presentViewController:picker animated:YES completion:nil];
#else
        NSWindow *window = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 860, 620)
            styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
            backing:NSBackingStoreBuffered defer:NO];
        window.releasedWhenClosed = NO;
        window.title = @"Choose Photos or Videos";
        window.contentViewController = picker;
        window.delegate = delegate;
        delegate.presentationWindow = window;
        [window center];
        [window makeKeyAndOrderFront:nil];
#endif
        } @catch (NSException *exception) {
            errorMessage = exception.reason ?: @"Cannot open the photo picker.";
            CocoaPyPhotoPickerDelegate *failed = weakDelegate;
            if (failed) {
                [failed dismissPicker];
                @synchronized (activeDelegates()) { [activeDelegates() removeObject:failed]; }
            }
            dispatch_semaphore_signal(sema);
        }
    });

    bool finished = false;
    Py_BEGIN_ALLOW_THREADS
    finished = CocoaPyWaitSemaphore(sema, timeout);
    if (!finished) CocoaPyRunOnMain(^{ [weakDelegate cancel]; });
    Py_END_ALLOW_THREADS
    if (!finished) { PyErr_SetString(PyExc_TimeoutError, "Photo selection timed out."); return nullptr; }

    if (cancelled) {
        return PyList_New(0);
    }
    if (errorMessage) {
        PyErr_SetString(PyExc_RuntimeError, errorMessage.UTF8String ?: "Photo picker failed.");
        return NULL;
    }
    if (!pickedEntries || pickedEntries.count == 0) {
        return PyList_New(0);
    }

    PyObject *list = PyList_New((Py_ssize_t)pickedEntries.count);
    if (!list) return NULL;
    for (NSUInteger i = 0; i < pickedEntries.count; i++) {
        PyObject *entry = entryToPyDict(pickedEntries[i]);
        if (!entry) {
            Py_DECREF(list);
            return NULL;
        }
        PyList_SET_ITEM(list, (Py_ssize_t)i, entry); // steals
    }
    return list;
}

static PyObject *py_photos_pick_image(PyObject *self, PyObject *args) {
    if (!PyArg_ParseTuple(args, "")) return NULL;
    PyObject *emptyArgs = PyTuple_New(0);
    if (!emptyArgs) return NULL;
    PyObject *kwargs = PyDict_New();
    if (!kwargs) {
        Py_DECREF(emptyArgs);
        return NULL;
    }
    PyObject *types = PyUnicode_FromString("image");
    PyObject *limit = PyLong_FromLong(1);
    if (!types || !limit) {
        Py_XDECREF(types);
        Py_XDECREF(limit);
        Py_DECREF(kwargs);
        Py_DECREF(emptyArgs);
        return NULL;
    }
    PyDict_SetItemString(kwargs, "types", types);
    PyDict_SetItemString(kwargs, "limit", limit);
    Py_DECREF(types);
    Py_DECREF(limit);

    PyObject *list = py_photos_pick_media(self, emptyArgs, kwargs);
    Py_DECREF(kwargs);
    Py_DECREF(emptyArgs);
    if (!list) return NULL;
    if (!PyList_Check(list) || PyList_Size(list) == 0) {
        Py_DECREF(list);
        Py_RETURN_NONE;
    }
    PyObject *first = PyList_GetItem(list, 0); // borrowed
    Py_INCREF(first);
    Py_DECREF(list);
    return first;
}

static PyObject *py_photos_save_image(PyObject *self, PyObject *args, PyObject *kwargs) {
    static const char *kwlist[] = {"path", "data", "format", NULL};
    PyObject *pathObj = Py_None;
    PyObject *dataObj = Py_None;
    PyObject *formatObj = Py_None;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|OOO", (char **)kwlist, &pathObj, &dataObj, &formatObj)) {
        return NULL;
    }

    const BOOL hasPath = pathObj != Py_None;
    const BOOL hasData = dataObj != Py_None;
    if ((hasPath && hasData) || (!hasPath && !hasData)) {
        PyErr_SetString(PyExc_ValueError, "save_image requires exactly one of: path or data.");
        return NULL;
    }

    NSString *path = nil;
    std::shared_ptr<CocoaPyFileAccess> fileAccess;
    NSData *rawData = nil;
    if (hasPath) {
        if (!PyUnicode_Check(pathObj)) {
            PyErr_SetString(PyExc_TypeError, "path must be a string.");
            return NULL;
        }
        const char *cpath = PyUnicode_AsUTF8(pathObj);
        if (!cpath) return NULL;
        path = @(cpath);
        fileAccess = std::make_shared<CocoaPyFileAccess>([NSURL fileURLWithPath:path]);
        if (![[NSFileManager defaultManager] fileExistsAtPath:path]) {
            PyErr_SetString(PyExc_FileNotFoundError, "Image path does not exist.");
            return NULL;
        }
    } else {
        Py_buffer view;
        if (PyObject_GetBuffer(dataObj, &view, PyBUF_SIMPLE) != 0) {
            PyErr_SetString(PyExc_TypeError, "data must be bytes-like.");
            return NULL;
        }
        rawData = [NSData dataWithBytes:view.buf length:(NSUInteger)view.len];
        PyBuffer_Release(&view);
        if (!rawData || rawData.length == 0) {
            PyErr_SetString(PyExc_ValueError, "data is empty.");
            return NULL;
        }
    }

    NSString *format = nil;
    if (formatObj != Py_None) {
        if (!PyUnicode_Check(formatObj)) {
            PyErr_SetString(PyExc_TypeError, "format must be a string.");
            return NULL;
        }
        const char *cfmt = PyUnicode_AsUTF8(formatObj);
        if (!cfmt) return NULL;
        format = [@(cfmt) lowercaseString];
    }
    NSString *normalizedFormat = format;
    if ([normalizedFormat isEqualToString:@"jpg"]) {
        normalizedFormat = @"jpeg";
    }

    NSString *permissionError = nil;
    __block BOOL allowed = NO;
    Py_BEGIN_ALLOW_THREADS
    allowed = ensurePhotoAddPermission(&permissionError);
    Py_END_ALLOW_THREADS
    if (!allowed) {
        PyErr_SetString(PyExc_PermissionError, permissionError.UTF8String ?: "No permission to save to Photos.");
        return NULL;
    }

    __block BOOL success = NO;
    __block NSError *saveError = nil;
    __block NSString *localIdentifier = nil;
    dispatch_semaphore_t sema = dispatch_semaphore_create(0);

    [[PHPhotoLibrary sharedPhotoLibrary] performChanges:^{
        PHAssetCreationRequest *request = [PHAssetCreationRequest creationRequestForAsset];
        localIdentifier = request.placeholderForCreatedAsset.localIdentifier;

        if (hasPath) {
            NSURL *fileURL = [NSURL fileURLWithPath:path];
            [request addResourceWithType:PHAssetResourceTypePhoto fileURL:fileURL options:nil];
        } else {
            PHAssetResourceCreationOptions *options = [PHAssetResourceCreationOptions new];
            if (normalizedFormat.length > 0) {
                options.uniformTypeIdentifier = [@"public." stringByAppendingString:normalizedFormat];
            }
            [request addResourceWithType:PHAssetResourceTypePhoto data:rawData options:options];
        }
    } completionHandler:^(BOOL ok, NSError * _Nullable error) {
        (void)fileAccess; // Retain host file access through asynchronous completion.
        success = ok;
        saveError = error;
        dispatch_semaphore_signal(sema);
    }];

    bool finished = false;
    Py_BEGIN_ALLOW_THREADS
    finished = CocoaPyWaitSemaphore(sema, 300.0);
    Py_END_ALLOW_THREADS
    if (!finished) {
        PyErr_SetString(PyExc_TimeoutError, "Photos save has not completed; it may still finish in the background."); return nullptr;
    }

    if (!success) {
        PyErr_SetString(PyExc_RuntimeError, saveError.localizedDescription.UTF8String ?: "Failed to save image.");
        return NULL;
    }

    PyObject *result = PyDict_New();
    if (!result) return NULL;
    PyObject *okObj = PyBool_FromLong(1);
    PyObject *idObj = PyUnicode_FromString(localIdentifier.UTF8String ?: "");
    if (!okObj || !idObj) {
        Py_XDECREF(okObj);
        Py_XDECREF(idObj);
        Py_DECREF(result);
        return NULL;
    }
    PyDict_SetItemString(result, "ok", okObj);
    PyDict_SetItemString(result, "local_id", idObj);
    Py_DECREF(okObj);
    Py_DECREF(idObj);
    return result;
}

static PyObject *py_photos_save_video(PyObject *self, PyObject *args, PyObject *kwargs) {
    static const char *kwlist[] = {"path", NULL};
    const char *pathCString = NULL;
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "s", (char **)kwlist, &pathCString)) {
        return NULL;
    }
    NSString *path = @(pathCString);
    auto fileAccess = std::make_shared<CocoaPyFileAccess>([NSURL fileURLWithPath:path]);
    if (![[NSFileManager defaultManager] fileExistsAtPath:path]) {
        PyErr_SetString(PyExc_FileNotFoundError, "Video path does not exist.");
        return NULL;
    }

    NSString *permissionError = nil;
    __block BOOL allowed = NO;
    Py_BEGIN_ALLOW_THREADS
    allowed = ensurePhotoAddPermission(&permissionError);
    Py_END_ALLOW_THREADS
    if (!allowed) {
        PyErr_SetString(PyExc_PermissionError, permissionError.UTF8String ?: "No permission to save to Photos.");
        return NULL;
    }

    __block BOOL success = NO;
    __block NSError *saveError = nil;
    __block NSString *localIdentifier = nil;
    dispatch_semaphore_t sema = dispatch_semaphore_create(0);

    [[PHPhotoLibrary sharedPhotoLibrary] performChanges:^{
        PHAssetCreationRequest *request = [PHAssetCreationRequest creationRequestForAsset];
        localIdentifier = request.placeholderForCreatedAsset.localIdentifier;
        [request addResourceWithType:PHAssetResourceTypeVideo fileURL:[NSURL fileURLWithPath:path] options:nil];
    } completionHandler:^(BOOL ok, NSError * _Nullable error) {
        (void)fileAccess; // Retain host file access through asynchronous completion.
        success = ok;
        saveError = error;
        dispatch_semaphore_signal(sema);
    }];

    bool finished = false;
    Py_BEGIN_ALLOW_THREADS
    finished = CocoaPyWaitSemaphore(sema, 300.0);
    Py_END_ALLOW_THREADS
    if (!finished) {
        PyErr_SetString(PyExc_TimeoutError, "Photos save has not completed; it may still finish in the background."); return nullptr;
    }

    if (!success) {
        PyErr_SetString(PyExc_RuntimeError, saveError.localizedDescription.UTF8String ?: "Failed to save video.");
        return NULL;
    }
    PyObject *result = PyDict_New();
    if (!result) return NULL;
    PyObject *okObj = PyBool_FromLong(1);
    PyObject *idObj = PyUnicode_FromString(localIdentifier.UTF8String ?: "");
    if (!okObj || !idObj) {
        Py_XDECREF(okObj);
        Py_XDECREF(idObj);
        Py_DECREF(result);
        return NULL;
    }
    PyDict_SetItemString(result, "ok", okObj);
    PyDict_SetItemString(result, "local_id", idObj);
    Py_DECREF(okObj);
    Py_DECREF(idObj);
    return result;
}

static PyMethodDef PhotosMethods[] = {
    {"pick_media", (PyCFunction)py_photos_pick_media, METH_VARARGS | METH_KEYWORDS,
     "pick_media(types=('image',), limit=1) -> list[dict]\n"
     "Pick media from Photos. Returns a list of entries."},
    {"pick_image", py_photos_pick_image, METH_VARARGS,
     "pick_image() -> dict | None\n"
     "Open the system photo picker and select one image.\n"
     "Returns {'path', 'filename', 'width', 'height'} or None when cancelled."},
    {"save_image", (PyCFunction)py_photos_save_image, METH_VARARGS | METH_KEYWORDS,
     "save_image(path=None, data=None, format=None) -> dict\n"
     "Save an image to Photos. Provide exactly one of path or data.\n"
     "Returns {'ok': True, 'local_id': str}."},
    {"save_video", (PyCFunction)py_photos_save_video, METH_VARARGS | METH_KEYWORDS,
     "save_video(path) -> dict\n"
     "Save a video file to Photos. Returns {'ok': True, 'local_id': str}."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef photosmodule = {
    PyModuleDef_HEAD_INIT,
    "_photos",
    "Native Photos integration for cocoa-py",
    -1,
    PhotosMethods
};

PyMODINIT_FUNC PyInit__photos(void) {
    return PyModule_Create(&photosmodule);
}

void registerPhotosModule(void) {
    PyImport_AppendInittab("_photos", PyInit__photos);
}
