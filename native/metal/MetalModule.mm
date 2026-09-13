#include <Python.h>

#import <Metal/Metal.h>
#import <QuartzCore/CAMetalLayer.h>
#import <QuartzCore/CADisplayLink.h>
#include "../common/CocoaPlatform.h"
#import <ImageIO/ImageIO.h>
#if COCOA_PY_UIKIT
#import <UIKit/UIKit.h>
using CocoaFont = UIFont;
using CocoaColor = UIColor;
#define CocoaFontWeightRegular UIFontWeightRegular
#else
#import <AppKit/AppKit.h>
using CocoaFont = NSFont;
using CocoaColor = NSColor;
#define CocoaFontWeightRegular NSFontWeightRegular
#endif

#include <algorithm>
#include <atomic>
#include <cmath>
#include <mach/mach.h>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

#include "MetalModule.h"

extern "C" void *objc_autoreleasePoolPush(void);
extern "C" void objc_autoreleasePoolPop(void *);

static id<MTLDevice> gDevice = nil;
static id<MTLCommandQueue> gCommandQueue = nil;
static std::mutex gStateMutex;
static std::atomic<long long> gNextHandle{1};

// Call with the GIL held and without gStateMutex. The block must only use
// native values; Python argument conversion and result creation stay outside.
static void runOnMainSync(dispatch_block_t block) {
    if ([NSThread isMainThread]) {
        block();
        return;
    }

    // A queued Python UI callback may need the GIL before our block can run.
    Py_BEGIN_ALLOW_THREADS
    dispatch_sync(dispatch_get_main_queue(), block);
    Py_END_ALLOW_THREADS
}

static bool ensureMetalContext() {
    if (gDevice && gCommandQueue) {
        return true;
    }
    gDevice = MTLCreateSystemDefaultDevice();
    if (!gDevice) {
        return false;
    }
    gCommandQueue = [gDevice newCommandQueue];
    return gCommandQueue != nil;
}

static MTLClearColor clearColorFromPyObject(PyObject *value, MTLClearColor fallback) {
    if (!PySequence_Check(value) || PySequence_Size(value) != 4) {
        return fallback;
    }

    double components[4] = {fallback.red, fallback.green, fallback.blue, fallback.alpha};
    for (Py_ssize_t index = 0; index < 4; index++) {
        PyObject *item = PySequence_GetItem(value, index);
        if (!item) {
            PyErr_Clear();
            return fallback;
        }
        components[index] = PyFloat_AsDouble(item);
        Py_DECREF(item);
        if (PyErr_Occurred() != nullptr) {
            PyErr_Clear();
            return fallback;
        }
    }
    return MTLClearColorMake(components[0], components[1], components[2], components[3]);
}

static MTLPrimitiveType primitiveTypeFromName(const std::string &name) {
    if (name == "triangle") return MTLPrimitiveTypeTriangle;
    if (name == "triangle_strip") return MTLPrimitiveTypeTriangleStrip;
    if (name == "line") return MTLPrimitiveTypeLine;
    if (name == "line_strip") return MTLPrimitiveTypeLineStrip;
    if (name == "point") return MTLPrimitiveTypePoint;
    return MTLPrimitiveTypeTriangleStrip;
}

static MTLPixelFormat pixelFormatFromName(const char *name) {
    if (!name || strcmp(name, "bgra8") == 0) return MTLPixelFormatBGRA8Unorm;
    if (strcmp(name, "rgba8") == 0) return MTLPixelFormatRGBA8Unorm;
    if (strcmp(name, "rgba16f") == 0) return MTLPixelFormatRGBA16Float;
    if (strcmp(name, "r8") == 0) return MTLPixelFormatR8Unorm;
    if (strcmp(name, "r16f") == 0) return MTLPixelFormatR16Float;
    if (strcmp(name, "r32f") == 0) return MTLPixelFormatR32Float;
    if (strcmp(name, "rg16f") == 0) return MTLPixelFormatRG16Float;
    if (strcmp(name, "rg32f") == 0) return MTLPixelFormatRG32Float;
    return MTLPixelFormatBGRA8Unorm;
}

static NSUInteger bytesPerPixelForFormat(MTLPixelFormat fmt) {
    switch (fmt) {
        case MTLPixelFormatR8Unorm: return 1;
        case MTLPixelFormatR16Float: return 2;
        case MTLPixelFormatR32Float: case MTLPixelFormatRG16Float:
        case MTLPixelFormatBGRA8Unorm: case MTLPixelFormatRGBA8Unorm: return 4;
        case MTLPixelFormatRG32Float: case MTLPixelFormatRGBA16Float: return 8;
        case MTLPixelFormatStencil8: return 1;
        case MTLPixelFormatDepth32Float: return 4;
        case MTLPixelFormatDepth32Float_Stencil8: return 5;
        default: return 4;
    }
}

static NSString *textureAllocationError(NSUInteger width, NSUInteger height, NSUInteger bytesPerPixel) {
    constexpr NSUInteger maxDimension = 16384;
    constexpr NSUInteger maxBytes = 256 * 1024 * 1024;
    if (!width || !height || width > maxDimension || height > maxDimension) {
        return @"Texture dimensions must be positive and at most 16384 per axis.";
    }
    if (!bytesPerPixel || width > maxBytes / bytesPerPixel ||
        height > maxBytes / bytesPerPixel / width) {
        return @"A texture allocation must not exceed 256 MiB.";
    }
    return nil;
}

static MTLPixelFormat depthFormatFromName(const char *name) {
    if (!name) return MTLPixelFormatInvalid;
    if (strcmp(name, "depth32f") == 0) return MTLPixelFormatDepth32Float;
    if (strcmp(name, "depth32f_stencil8") == 0) return MTLPixelFormatDepth32Float_Stencil8;
    return MTLPixelFormatInvalid;
}

static MTLCompareFunction compareFuncFromName(const char *name) {
    if (!name || strcmp(name, "less") == 0) return MTLCompareFunctionLess;
    if (strcmp(name, "less_equal") == 0) return MTLCompareFunctionLessEqual;
    if (strcmp(name, "greater") == 0) return MTLCompareFunctionGreater;
    if (strcmp(name, "greater_equal") == 0) return MTLCompareFunctionGreaterEqual;
    if (strcmp(name, "equal") == 0) return MTLCompareFunctionEqual;
    if (strcmp(name, "not_equal") == 0) return MTLCompareFunctionNotEqual;
    if (strcmp(name, "always") == 0) return MTLCompareFunctionAlways;
    if (strcmp(name, "never") == 0) return MTLCompareFunctionNever;
    return MTLCompareFunctionLess;
}

static MTLIndexType indexTypeFromName(const char *name) {
    if (name && strcmp(name, "uint32") == 0) return MTLIndexTypeUInt32;
    return MTLIndexTypeUInt16;
}

struct TouchEvent {
    int phase;           // 0=began 1=moved 2=ended 3=cancelled
    long long touchId;
    double x, y;
    double prevX, prevY;
    double timestamp;
    unsigned long long epoch = 0;
};

struct ScrollEvent {
    double x, y, dx, dy, timestamp;
    bool precise, momentum;
    unsigned long long epoch = 0;
};

struct SceneKeyEvent {
    unsigned code = 0, modifiers = 0;
    std::string key;
    double timestamp = 0;
    bool down = false, repeat = false, cancelled = false;
};

struct ScenePlatformEvent {
    int kind = 0; // 0=key, 1=state, 2=input reset
    SceneKeyEvent key;
    bool active = true, foreground = true, focused = true;
    unsigned long long epoch = 0;
    double timestamp = 0;
    std::string reason;
    unsigned long long sequence = 0;
};

@class CocoaPySceneEventObserver;

#if COCOA_PY_UIKIT
#include "MetalUIKitViews.h"
#else
#include "MetalMacViews.h"
#endif

struct LibraryRecord {
    long long handle;
    __strong id<MTLLibrary> library;
};

struct PipelineRecord {
    long long handle;
    __strong id<MTLRenderPipelineState> pipeline;
};

struct BufferRecord {
    long long handle;
    __strong id<MTLBuffer> buffer;
};

struct TextureRecord {
    long long handle;
    __strong id<MTLTexture> texture;
    NSUInteger width;
    NSUInteger height;
    MTLPixelFormat format;
};

struct ComputePipelineRecord {
    long long handle;
    __strong id<MTLComputePipelineState> pipeline;
};

struct WindowRecord {
    long long handle;
#if COCOA_PY_UIKIT
    __strong UIWindow *window;
#else
    __strong NSWindow *window;
#endif
    __strong CocoaPyMetalViewController *controller;
    __strong CAMetalLayer *layer;
    __strong id<CAMetalDrawable> drawable;
    __strong id<MTLCommandBuffer> commandBuffer;
    __strong id<MTLRenderCommandEncoder> encoder;
    __strong id<MTLTexture> targetTexture;
    __strong id<MTLComputeCommandEncoder> computeEncoder;
    __strong id<MTLCommandBuffer> computeCommandBuffer;
    /* Blit encoder */
    __strong id<MTLBlitCommandEncoder> blitEncoder;
    __strong id<MTLCommandBuffer> blitCommandBuffer;
    BOOL blitActive;
    /* Depth/Stencil auto-managed textures */
    __strong id<MTLTexture> depthTexture;
    NSUInteger depthWidth, depthHeight, depthSampleCount;
    MTLPixelFormat depthFormat;
    __strong id<MTLTexture> stencilTexture;
    NSUInteger stencilWidth, stencilHeight, stencilSampleCount;
    /* MSAA auto-managed texture */
    __strong id<MTLTexture> msaaTexture;
    NSUInteger msaaWidth, msaaHeight, msaaSampleCount;
    MTLPixelFormat msaaFormat;
    /* Display */
    __strong CADisplayLink *displayLink;
    dispatch_semaphore_t vsyncSemaphore;
    dispatch_semaphore_t frameSemaphore;
    unsigned long long submittedFrames;
    unsigned long long completedFrames;
    BOOL frameSlotAcquired;
    void *frameAutoreleasePool;
    BOOL frameActive;
    BOOL computeActive;
    __strong id<MTLCommandBuffer> offscreenCB;
    std::vector<TouchEvent> touchQueue;
    std::vector<ScrollEvent> scrollQueue;
    std::unordered_map<void *, long long> touchIdMap;
    long long nextTouchId = 1;
    bool active = true, foreground = true, focused = true;
    bool keyboardEvents = false;
    long long activeTextInput = 0;
    unsigned long long inputEpoch = 0;
    unsigned long long platformSequence = 0;
    std::vector<ScenePlatformEvent> platformQueue;
    std::unordered_map<unsigned, SceneKeyEvent> pressedKeys;
    __strong CocoaPySceneEventObserver *eventObserver;
    __strong id<MTLCommandBuffer> lastSubmittedCB;
};

static std::unordered_map<long long, LibraryRecord> gLibraries;
static std::unordered_map<long long, PipelineRecord> gPipelines;
static std::unordered_map<long long, ComputePipelineRecord> gComputePipelines;
static std::unordered_map<long long, BufferRecord> gBuffers;
static std::unordered_map<long long, TextureRecord> gTextures;
static std::unordered_map<long long, WindowRecord> gWindows;

#include "MetalEvents.h"

#if COCOA_PY_UIKIT
#include "MetalUIKitInput.h"
@implementation CocoaPyMetalSurfaceView (Touch)
- (void)_enqueueTouches:(NSSet<UITouch *> *)touches phase:(int)phase {
    std::lock_guard<std::mutex> lock(gStateMutex);
    auto it = gWindows.find(self.windowHandle);
    if (it == gWindows.end()) return;
    auto &wr = it->second;
    if (!wr.active || !wr.foreground || !wr.focused) return;
    for (UITouch *touch in touches) {
        void *key = (__bridge void *)touch;
        long long tid;
        if (phase == 0) {
            // began: assign new unique ID
            tid = wr.nextTouchId++;
            wr.touchIdMap[key] = tid;
        } else {
            auto mi = wr.touchIdMap.find(key);
            if (mi == wr.touchIdMap.end()) continue;
            tid = mi->second;
            if (phase >= 2) {
                // ended or cancelled: remove mapping
                wr.touchIdMap.erase(key);
            }
        }
        CGPoint loc = [touch locationInView:self];
        CGPoint prev = [touch previousLocationInView:self];
        if (wr.touchQueue.size() >= 4096) {
            metalResetInput(wr, "overflow");
            break;
        }
        wr.touchQueue.push_back({
            phase, tid,
            loc.x, loc.y,
            prev.x, prev.y,
            touch.timestamp, wr.inputEpoch
        });
    }
}
- (void)touchesBegan:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event {
    [self _enqueueTouches:touches phase:0];
}
- (void)touchesMoved:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event {
    [self _enqueueTouches:touches phase:1];
}
- (void)touchesEnded:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event {
    [self _enqueueTouches:touches phase:2];
}
- (void)touchesCancelled:(NSSet<UITouch *> *)touches withEvent:(UIEvent *)event {
    [self _enqueueTouches:touches phase:3];
}
@end

#else
#include "MetalMacInput.h"
#endif

static long long nextHandle() {
    // Main-queue work and Python resource creation can now run concurrently.
    return gNextHandle.fetch_add(1, std::memory_order_relaxed);
}

#if COCOA_PY_UIKIT
static UIWindowScene *activeScene() {
    UIWindowScene *foregroundInactive = nil;
    UIWindowScene *fallback = nil;
    for (UIScene *scene in UIApplication.sharedApplication.connectedScenes) {
        if (![scene isKindOfClass:[UIWindowScene class]]) {
            continue;
        }
        UIWindowScene *windowScene = (UIWindowScene *)scene;
        if (scene.activationState == UISceneActivationStateForegroundActive) {
            return windowScene;
        }
        if (scene.activationState == UISceneActivationStateForegroundInactive && foregroundInactive == nil) {
            foregroundInactive = windowScene;
        }
        if (fallback == nil) {
            fallback = windowScene;
        }
    }
    return foregroundInactive ?: fallback;
}

#endif

static WindowRecord *windowRecord(long long handle) {
    auto it = gWindows.find(handle);
    return it == gWindows.end() ? nullptr : &it->second;
}

static LibraryRecord *libraryRecord(long long handle) {
    auto it = gLibraries.find(handle);
    return it == gLibraries.end() ? nullptr : &it->second;
}

static PipelineRecord *pipelineRecord(long long handle) {
    auto it = gPipelines.find(handle);
    return it == gPipelines.end() ? nullptr : &it->second;
}

static BufferRecord *bufferRecord(long long handle) {
    auto it = gBuffers.find(handle);
    return it == gBuffers.end() ? nullptr : &it->second;
}

static TextureRecord *textureRecord(long long handle) {
    auto it = gTextures.find(handle);
    return it == gTextures.end() ? nullptr : &it->second;
}

#include "MetalTextInput.h"
#include "MetalTextInputBridge.h"
#include "MetalTextInputSnapshot.h"

#if COCOA_PY_UIKIT
#include "MetalUIKitWindows.h"
#else
#include "MetalMacWindows.h"
#endif

static PyObject *metal_create_library(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    const char *source = nullptr;
    const char *path = nullptr;
    static char kw_source[] = "source";
    static char kw_path[] = "path";
    static char *kwlist[] = {kw_source, kw_path, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|ss", kwlist, &source, &path)) {
        return nullptr;
    }
    if (!source && !path) {
        PyErr_SetString(PyExc_ValueError, "Either source or path must be provided");
        return nullptr;
    }
    if (!ensureMetalContext()) {
        PyErr_SetString(PyExc_RuntimeError, "Metal device is unavailable on this device.");
        return nullptr;
    }

    id<MTLLibrary> library = nil;
    if (path) {
        NSString *nsPath = @(path);
        if ([nsPath hasSuffix:@".metallib"]) {
            /* Pre-compiled metallib — instant load, no runtime compilation */
            NSError *error = nil;
            NSURL *url = [NSURL fileURLWithPath:nsPath];
            library = [gDevice newLibraryWithURL:url error:&error];
            if (!library) {
                PyErr_Format(PyExc_RuntimeError, "Cannot load metallib: %s",
                             error.localizedDescription.UTF8String);
                return nullptr;
            }
        } else {
            /* Source file — compile at runtime */
            NSError *readError = nil;
            NSString *nsSource = [NSString stringWithContentsOfFile:nsPath
                                                          encoding:NSUTF8StringEncoding
                                                             error:&readError];
            if (!nsSource) {
                PyErr_Format(PyExc_FileNotFoundError, "Cannot read shader file: %s",
                             readError.localizedDescription.UTF8String);
                return nullptr;
            }
            NSError *error = nil;
            library = [gDevice newLibraryWithSource:nsSource options:nil error:&error];
            if (!library) {
                PyErr_SetString(PyExc_RuntimeError, error.localizedDescription.UTF8String);
                return nullptr;
            }
        }
    } else if (source) {
        if (strcmp(source, "__default__") == 0) {
            /* Load the app's default Metal library (built by Xcode) */
            library = [gDevice newDefaultLibrary];
            if (!library) {
                PyErr_SetString(PyExc_RuntimeError, "No default Metal library found in app bundle.");
                return nullptr;
            }
        } else {
            NSError *error = nil;
            library = [gDevice newLibraryWithSource:@(source) options:nil error:&error];
            if (!library) {
                PyErr_SetString(PyExc_RuntimeError, error.localizedDescription.UTF8String);
                return nullptr;
            }
        }
    } else {
        PyErr_SetString(PyExc_ValueError, "Either source or path must be provided");
        return nullptr;
    }

    long long handle = nextHandle();
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        gLibraries.emplace(handle, LibraryRecord{handle, library});
    }
    return PyLong_FromLongLong(handle);
}

static PyObject *metal_destroy_library(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) {
        return nullptr;
    }
    std::lock_guard<std::mutex> lock(gStateMutex);
    gLibraries.erase(handle);
    Py_RETURN_NONE;
}

static PyObject *metal_create_render_pipeline(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long libraryHandle = 0;
    const char *vertex = nullptr;
    const char *fragment = nullptr;
    int blending = 1;
    int premultiplied = 0;
    const char *format = "bgra8";
    const char *depthFmt = nullptr;
    int sampleCount = 1;
    const char *stencilFmt = nullptr;
    int colorWrite = 1;
    static char libraryKeyword[] = "library";
    static char vertexKeyword[] = "vertex";
    static char fragmentKeyword[] = "fragment";
    static char blendingKeyword[] = "blending";
    static char premultipliedKeyword[] = "premultiplied";
    static char formatKeyword[] = "format";
    static char depthFormatKeyword[] = "depth_format";
    static char sampleCountKeyword[] = "sample_count";
    static char stencilFormatKeyword[] = "stencil_format";
    static char colorWriteKeyword[] = "color_write";
    static char *kwlist[] = {libraryKeyword, vertexKeyword, fragmentKeyword,
                             blendingKeyword, premultipliedKeyword,
                             formatKeyword, depthFormatKeyword, sampleCountKeyword,
                             stencilFormatKeyword, colorWriteKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "Lss|ppszIzp", kwlist,
            &libraryHandle, &vertex, &fragment, &blending, &premultiplied,
            &format, &depthFmt, &sampleCount, &stencilFmt, &colorWrite)) {
        return nullptr;
    }

    if (!ensureMetalContext()) {
        PyErr_SetString(PyExc_RuntimeError, "Metal device is unavailable on this device.");
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(gStateMutex);
    LibraryRecord *library = libraryRecord(libraryHandle);
    if (!library) {
        PyErr_SetString(PyExc_KeyError, "_metal library handle not found.");
        return nullptr;
    }

    id<MTLFunction> vertexFunction = [library->library newFunctionWithName:[NSString stringWithUTF8String:vertex]];
    id<MTLFunction> fragmentFunction = [library->library newFunctionWithName:[NSString stringWithUTF8String:fragment]];
    if (!vertexFunction || !fragmentFunction) {
        PyErr_SetString(PyExc_RuntimeError, "Metal entry point not found in library.");
        return nullptr;
    }

    MTLRenderPipelineDescriptor *descriptor = [MTLRenderPipelineDescriptor new];
    descriptor.vertexFunction = vertexFunction;
    descriptor.fragmentFunction = fragmentFunction;
    descriptor.colorAttachments[0].pixelFormat = pixelFormatFromName(format);
    descriptor.rasterSampleCount = (NSUInteger)(sampleCount > 0 ? sampleCount : 1);
    descriptor.colorAttachments[0].blendingEnabled = blending != 0;
    descriptor.colorAttachments[0].rgbBlendOperation = MTLBlendOperationAdd;
    descriptor.colorAttachments[0].alphaBlendOperation = MTLBlendOperationAdd;
    if (premultiplied) {
        descriptor.colorAttachments[0].sourceRGBBlendFactor = MTLBlendFactorOne;
        descriptor.colorAttachments[0].sourceAlphaBlendFactor = MTLBlendFactorOne;
    } else {
        descriptor.colorAttachments[0].sourceRGBBlendFactor = MTLBlendFactorSourceAlpha;
        descriptor.colorAttachments[0].sourceAlphaBlendFactor = MTLBlendFactorSourceAlpha;
    }
    descriptor.colorAttachments[0].destinationRGBBlendFactor = MTLBlendFactorOneMinusSourceAlpha;
    descriptor.colorAttachments[0].destinationAlphaBlendFactor = MTLBlendFactorOneMinusSourceAlpha;

    /* Color write mask */
    if (!colorWrite) {
        descriptor.colorAttachments[0].writeMask = MTLColorWriteMaskNone;
    }

    /* Depth/Stencil */
    MTLPixelFormat dFmt = depthFormatFromName(depthFmt);
    if (dFmt != MTLPixelFormatInvalid) {
        descriptor.depthAttachmentPixelFormat = dFmt;
        if (dFmt == MTLPixelFormatDepth32Float_Stencil8) {
            descriptor.stencilAttachmentPixelFormat = dFmt;
        }
    }
    if (stencilFmt && strcmp(stencilFmt, "stencil8") == 0) {
        descriptor.stencilAttachmentPixelFormat = MTLPixelFormatStencil8;
    }

    NSError *error = nil;
    id<MTLRenderPipelineState> pipeline = [gDevice newRenderPipelineStateWithDescriptor:descriptor error:&error];
    if (!pipeline) {
        PyErr_SetString(PyExc_RuntimeError, error.localizedDescription.UTF8String);
        return nullptr;
    }

    long long handle = nextHandle();
    gPipelines.emplace(handle, PipelineRecord{handle, pipeline});
    return PyLong_FromLongLong(handle);
}

static PyObject *metal_destroy_render_pipeline(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) {
        return nullptr;
    }
    std::lock_guard<std::mutex> lock(gStateMutex);
    gPipelines.erase(handle);
    Py_RETURN_NONE;
}

static PyObject *metal_create_buffer(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long length = 0;
    static char lengthKeyword[] = "length";
    static char *kwlist[] = {lengthKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "L", kwlist, &length)) {
        return nullptr;
    }

    if (!ensureMetalContext()) {
        PyErr_SetString(PyExc_RuntimeError, "Metal device is unavailable on this device.");
        return nullptr;
    }
    if (length <= 0) {
        PyErr_SetString(PyExc_ValueError, "Buffer length must be positive.");
        return nullptr;
    }

    id<MTLBuffer> buffer = [gDevice newBufferWithLength:(NSUInteger)length options:MTLResourceStorageModeShared];
    if (!buffer) {
        PyErr_SetString(PyExc_RuntimeError, "Failed to allocate Metal buffer.");
        return nullptr;
    }

    long long handle = nextHandle();
    std::lock_guard<std::mutex> lock(gStateMutex);
    gBuffers.emplace(handle, BufferRecord{handle, buffer});
    return PyLong_FromLongLong(handle);
}

static PyObject *metal_destroy_buffer(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) {
        return nullptr;
    }
    std::lock_guard<std::mutex> lock(gStateMutex);
    gBuffers.erase(handle);
    Py_RETURN_NONE;
}

#include "MetalTextLayout.h"

static PyObject *metal_create_text_texture(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    const char *text = nullptr;
    double fontSize = 0.0;
    const char *fontName = nullptr;
    static char textKeyword[] = "text";
    static char fontSizeKeyword[] = "font_size";
    static char fontNameKeyword[] = "font_name";
    static char *kwlist[] = {textKeyword, fontSizeKeyword, fontNameKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "sd|z", kwlist, &text, &fontSize, &fontName)) {
        return nullptr;
    }

    if (!ensureMetalContext()) {
        PyErr_SetString(PyExc_RuntimeError, "Metal device is unavailable on this device.");
        return nullptr;
    }

    __block id<MTLTexture> texture = nil;
    __block NSUInteger width = 0;
    __block NSUInteger height = 0;
    NSString *nsText;
    CocoaFont *font;
    @autoreleasepool {
        nsText = [NSString stringWithUTF8String:text];
        NSString *nsFontName = fontName ? [NSString stringWithUTF8String:fontName] : nil;
        font = nsFontName.length > 0 ? [CocoaFont fontWithName:nsFontName size:(CGFloat)fontSize] : nil;
        if (!font) {
            font = [CocoaFont systemFontOfSize:(CGFloat)fontSize weight:CocoaFontWeightRegular];
        }
    }

    runOnMainSync(^{
        @autoreleasepool {
            NSDictionary *attrs = @{
                NSFontAttributeName: font,
                NSForegroundColorAttributeName: [CocoaColor whiteColor],
            };

            CGSize measured = [nsText sizeWithAttributes:attrs];
            CGFloat padding = MAX(4.0, ceil(font.pointSize * 0.18));
            width = (NSUInteger)MAX(1.0, ceil(measured.width + padding * 2.0));
            height = (NSUInteger)MAX(1.0, ceil(measured.height + padding * 2.0));

            CGColorSpaceRef colorSpace = CGColorSpaceCreateDeviceRGB();
            const size_t bytesPerRow = width * 4;
            void *raw = calloc(height, bytesPerRow);
            CGContextRef bitmap = CGBitmapContextCreate(
                raw,
                width,
                height,
                8,
                bytesPerRow,
                colorSpace,
                kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Little
            );
            CGColorSpaceRelease(colorSpace);
            if (!bitmap) {
                free(raw);
                return;
            }

            CGContextTranslateCTM(bitmap, 0, height);
            CGContextScaleCTM(bitmap, 1, -1);
#if COCOA_PY_UIKIT
            UIGraphicsPushContext(bitmap);
#else
            [NSGraphicsContext saveGraphicsState];
            NSGraphicsContext.currentContext = [NSGraphicsContext graphicsContextWithCGContext:bitmap flipped:YES];
#endif
            [nsText drawAtPoint:CGPointMake(padding, padding) withAttributes:attrs];
#if COCOA_PY_UIKIT
            UIGraphicsPopContext();
#else
            [NSGraphicsContext restoreGraphicsState];
#endif

            MTLTextureDescriptor *descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:width height:height mipmapped:NO];
            descriptor.usage = MTLTextureUsageShaderRead;
            texture = [gDevice newTextureWithDescriptor:descriptor];
            if (texture) {
                [texture replaceRegion:MTLRegionMake2D(0, 0, width, height) mipmapLevel:0 withBytes:raw bytesPerRow:bytesPerRow];
            }

            CGContextRelease(bitmap);
            free(raw);
        }
    });

    if (!texture) {
        PyErr_SetString(PyExc_RuntimeError, "Failed to rasterize text into a Metal texture.");
        return nullptr;
    }

    long long handle = nextHandle();
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        gTextures.emplace(handle, TextureRecord{handle, texture, width, height, MTLPixelFormatBGRA8Unorm});
    }

    PyObject *dict = PyDict_New();
    PyObject *handleValue = PyLong_FromLongLong(handle);
    PyObject *sizeValue = Py_BuildValue("(II)", (unsigned int)width, (unsigned int)height);
    PyDict_SetItemString(dict, "handle", handleValue);
    PyDict_SetItemString(dict, "size", sizeValue);
    Py_DECREF(handleValue);
    Py_DECREF(sizeValue);
    return dict;
}

/* ─── Glyph Atlas ─────────────────────────────────────────────────────────── */

static PyObject *metal_create_glyph_atlas(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    const char *fontName = nullptr;
    double fontSize = 0.0;
    const char *chars = nullptr;
    double extraPadding = -1.0;  /* -1 = use default */
    int sdfMode = 0;             /* 1 = generate SDF from bitmap */
    double sdfSpread = 8.0;
    static char fnKey[] = "font_name";
    static char fsKey[] = "font_size";
    static char chKey[] = "chars";
    static char padKey[] = "padding";
    static char sdfKey[] = "sdf";
    static char sprKey[] = "sdf_spread";
    static char *kwlist[] = {fnKey, fsKey, chKey, padKey, sdfKey, sprKey, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "zds|dpd", kwlist,
            &fontName, &fontSize, &chars, &extraPadding, &sdfMode, &sdfSpread)) {
        return nullptr;
    }
    if (!std::isfinite(fontSize) || fontSize <= 0 || fontSize > 16384 ||
        !std::isfinite(extraPadding) || extraPadding > 16384 ||
        !std::isfinite(sdfSpread) || sdfSpread < 0 || sdfSpread > 16384) {
        PyErr_SetString(PyExc_ValueError, "Glyph raster dimensions must be finite and within texture limits.");
        return nullptr;
    }
    if (!ensureMetalContext()) {
        PyErr_SetString(PyExc_RuntimeError, "Metal device unavailable");
        return nullptr;
    }

    /* Decode UTF-8 chars string into array of (code_point, NSString) pairs.
       Handles emoji (U+1F600+), composed sequences (flags, skin tones), and CJK. */
    NSString *nsChars = [NSString stringWithUTF8String:chars];
    NSMutableArray<NSString *> *charStrings = [NSMutableArray array];
    NSMutableArray<NSNumber *> *codePoints = [NSMutableArray array];
    [nsChars enumerateSubstringsInRange:NSMakeRange(0, nsChars.length)
                               options:NSStringEnumerationByComposedCharacterSequences
                            usingBlock:^(NSString *sub, NSRange r, NSRange er, BOOL *stop) {
        if (sub.length > 0) {
            /* Extract full 32-bit code point (handles surrogate pairs) */
            uint32_t cp = [sub characterAtIndex:0];
            if (sub.length >= 2 && CFStringIsSurrogateHighCharacter((unichar)cp)) {
                unichar hi = (unichar)cp;
                unichar lo = [sub characterAtIndex:1];
                cp = CFStringGetLongCharacterForSurrogatePair(hi, lo);
            }
            [codePoints addObject:@(cp)];
            [charStrings addObject:sub];
        }
    }];

    struct PackedGlyph {
        uint32_t code;
        int charIdx;  /* index into charStrings for rendering */
        double px, py, gw, gh, advance, bx, by;
    };

    __block id<MTLTexture> texture = nil;
    __block NSUInteger atlasW = 0, atlasH = 0;
    __block std::vector<PackedGlyph> packed;
    __block NSString *allocationError = nil;

    NSString *nsFontName = fontName ? [NSString stringWithUTF8String:fontName] : nil;
    runOnMainSync(^{
        @autoreleasepool {
            /* Create font */
            CocoaFont *font = nsFontName.length > 0 ? [CocoaFont fontWithName:nsFontName size:(CGFloat)fontSize] : nil;
            if (!font) {
                font = [CocoaFont systemFontOfSize:(CGFloat)fontSize weight:CocoaFontWeightRegular];
            }

            NSDictionary *attrs = @{
                NSFontAttributeName: font,
                NSForegroundColorAttributeName: [CocoaColor whiteColor],
            };

            CGFloat padding = (extraPadding >= 0) ? (CGFloat)extraPadding : MAX(2.0, ceil(font.pointSize * 0.1));

            /* Measure each glyph */
            packed.reserve(codePoints.count);
            for (NSUInteger i = 0; i < codePoints.count; i++) {
                uint32_t code = (uint32_t)[codePoints[i] unsignedIntValue];
                NSString *ch = charStrings[i];
                CGSize measured = [ch sizeWithAttributes:attrs];
                double gw = ceil(measured.width);
                double gh = ceil(measured.height);
                if (gw < 1) gw = 1;
                if (gh < 1) gh = 1;
                PackedGlyph pg;
                pg.code = code;
                pg.charIdx = (int)i;
                pg.gw = gw;
                pg.gh = gh;
                pg.advance = gw;
                pg.bx = 0;
                pg.by = 0;
                pg.px = 0;
                pg.py = 0;
                packed.push_back(pg);
            }

            /* Shelf-packing: pack glyphs into rows */
            double maxW = 1024;
            for (const auto &pg : packed) maxW = std::max(maxW, pg.gw + padding * 3);
            if (!std::isfinite(maxW) || maxW > 16384) {
                allocationError = @"Glyph atlas width exceeds the texture limit.";
                return;
            }
            double curX = padding, curY = padding, rowH = 0;
            for (auto &pg : packed) {
                double slotW = pg.gw + padding * 2;
                double slotH = pg.gh + padding * 2;
                if (curX + slotW > maxW) {
                    curX = padding;
                    curY += rowH + padding;
                    rowH = 0;
                }
                pg.px = curX;
                pg.py = curY;
                curX += slotW;
                if (slotH > rowH) rowH = slotH;
            }

            double measuredHeight = MAX(64, ceil(curY + rowH + padding));
            if (!std::isfinite(measuredHeight) || measuredHeight > 16384) {
                allocationError = @"Glyph atlas height exceeds the texture limit.";
                return;
            }
            atlasW = (NSUInteger)ceil(maxW);
            atlasH = (NSUInteger)measuredHeight;
            NSUInteger nextPow2 = 64;
            while (nextPow2 < atlasH) nextPow2 *= 2;
            atlasH = nextPow2;
            allocationError = textureAllocationError(atlasW, atlasH, 4);
            if (allocationError) return;

            /* Create bitmap and rasterize each glyph */
            size_t bytesPerRow = atlasW * 4;
            void *raw = calloc(atlasH, bytesPerRow);
            CGColorSpaceRef colorSpace = CGColorSpaceCreateDeviceRGB();
            CGContextRef ctx = CGBitmapContextCreate(
                raw, atlasW, atlasH, 8, bytesPerRow, colorSpace,
                kCGImageAlphaPremultipliedFirst | kCGBitmapByteOrder32Little
            );
            CGColorSpaceRelease(colorSpace);

            if (ctx) {
                CGContextTranslateCTM(ctx, 0, atlasH);
                CGContextScaleCTM(ctx, 1.0, -1.0);

#if COCOA_PY_UIKIT
                UIGraphicsPushContext(ctx);
#else
                [NSGraphicsContext saveGraphicsState];
                NSGraphicsContext.currentContext = [NSGraphicsContext graphicsContextWithCGContext:ctx flipped:YES];
#endif
                for (auto &pg : packed) {
                    NSString *ch = charStrings[pg.charIdx];
                    [ch drawAtPoint:CGPointMake(pg.px, pg.py) withAttributes:attrs];
                }
#if COCOA_PY_UIKIT
                UIGraphicsPopContext();
#else
                [NSGraphicsContext restoreGraphicsState];
#endif
                CGContextRelease(ctx);
            }

            /* ── SDF generation (CPU Felzenszwalb EDT, inline, <1ms for small atlas) ── */
            uint8_t *uploadData = (uint8_t *)raw;
            if (sdfMode && raw) {
                int w = (int)atlasW, h = (int)atlasH;
                float spread = (float)sdfSpread;

                /* Build distance grids from alpha.
                   dist_in[i]  = 0 at inside pixels (alpha > 127)  → EDT gives distance to nearest inside
                   dist_out[i] = 0 at outside pixels (alpha <= 127) → EDT gives distance to nearest outside */
                float INF = 1e10f;
                float *dist_in  = (float *)malloc(sizeof(float) * w * h);
                float *dist_out = (float *)malloc(sizeof(float) * w * h);
                for (int i = 0; i < w * h; i++) {
                    /* BGRA layout: [0]=B [1]=G [2]=R [3]=A */
                    uint8_t a = ((uint8_t *)raw)[i * 4 + 3];
                    dist_in[i]  = (a > 127) ? 0.0f : INF;
                    dist_out[i] = (a > 127) ? INF : 0.0f;
                }

                /* 1D squared-distance transform (Felzenszwalb & Huttenlocher) */
                auto edt1d = [](float *f, int n) {
                    int *v = (int *)malloc(sizeof(int) * n);
                    float *z = (float *)malloc(sizeof(float) * (n + 1));
                    float *d = (float *)malloc(sizeof(float) * n);
                    v[0] = 0; z[0] = -1e20f; z[1] = 1e20f;
                    int k = 0;
                    for (int q = 1; q < n; q++) {
                        float s;
                        do {
                            s = ((f[q] + (float)(q*q)) - (f[v[k]] + (float)(v[k]*v[k]))) / (float)(2*q - 2*v[k]);
                            if (s > z[k]) break;
                            k--;
                        } while (k >= 0);
                        k++;
                        v[k] = q; z[k] = s; z[k+1] = 1e20f;
                    }
                    k = 0;
                    for (int q = 0; q < n; q++) {
                        while (z[k+1] < (float)q) k++;
                        d[q] = (float)((q - v[k]) * (q - v[k])) + f[v[k]];
                    }
                    memcpy(f, d, n * sizeof(float));
                    free(v); free(z); free(d);
                };

                /* 2D EDT = rows then columns, then sqrt */
                auto edt2d = [&](float *dist) {
                    float *col = (float *)malloc(sizeof(float) * std::max(w, h));
                    for (int y = 0; y < h; y++) edt1d(dist + y * w, w);
                    for (int x = 0; x < w; x++) {
                        for (int y = 0; y < h; y++) col[y] = dist[y * w + x];
                        edt1d(col, h);
                        for (int y = 0; y < h; y++) dist[y * w + x] = col[y];
                    }
                    for (int i = 0; i < w * h; i++) dist[i] = sqrtf(dist[i]);
                    free(col);
                };

                edt2d(dist_in);
                edt2d(dist_out);

                /* Encode signed distance into BGRA8: val > 0.5 = inside */
                for (int i = 0; i < w * h; i++) {
                    float sd = dist_out[i] - dist_in[i];
                    float val = fmaxf(0.0f, fminf(1.0f, sd / (2.0f * spread) + 0.5f));
                    uint8_t v = (uint8_t)(val * 255.0f + 0.5f);
                    ((uint8_t *)raw)[i * 4 + 0] = v;
                    ((uint8_t *)raw)[i * 4 + 1] = v;
                    ((uint8_t *)raw)[i * 4 + 2] = v;
                    ((uint8_t *)raw)[i * 4 + 3] = 255;
                }
                free(dist_in);
                free(dist_out);
            }

            /* Create Metal texture */
            MTLTextureDescriptor *desc = [MTLTextureDescriptor
                texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                             width:atlasW height:atlasH mipmapped:NO];
            desc.usage = MTLTextureUsageShaderRead;
            texture = [gDevice newTextureWithDescriptor:desc];
            if (texture) {
                [texture replaceRegion:MTLRegionMake2D(0, 0, atlasW, atlasH)
                          mipmapLevel:0 withBytes:uploadData bytesPerRow:bytesPerRow];
            }
            free(raw);
        }
    });

    /* Build Python glyph dict — on calling thread where GIL is held */
    PyObject *glyphDict = PyDict_New();
    for (auto &pg : packed) {
        PyObject *key = PyLong_FromUnsignedLong(pg.code);
        PyObject *val = Py_BuildValue("(ddddddd)",
            pg.px, pg.py, pg.gw, pg.gh, pg.advance, pg.bx, pg.by);
        PyDict_SetItem(glyphDict, key, val);
        Py_DECREF(key);
        Py_DECREF(val);
    }

    if (!texture) {
        Py_XDECREF(glyphDict);
        if (allocationError) {
            PyErr_SetString(PyExc_ValueError, allocationError.UTF8String);
            return nullptr;
        }
        PyErr_SetString(PyExc_RuntimeError, "Failed to create glyph atlas texture");
        return nullptr;
    }

    long long handle = nextHandle();
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        gTextures.emplace(handle, TextureRecord{handle, texture, atlasW, atlasH, MTLPixelFormatBGRA8Unorm});
    }

    PyObject *result = PyDict_New();
    PyObject *hv = PyLong_FromLongLong(handle);
    PyObject *sv = Py_BuildValue("(II)", (unsigned int)atlasW, (unsigned int)atlasH);
    PyDict_SetItemString(result, "handle", hv);
    PyDict_SetItemString(result, "size", sv);
    PyDict_SetItemString(result, "glyphs", glyphDict);
    Py_DECREF(hv);
    Py_DECREF(sv);
    Py_DECREF(glyphDict);
    return result;
}

static PyObject *metal_create_render_texture(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long width = 0;
    long long height = 0;
    const char *format = "bgra8";
    static char widthKeyword[] = "width";
    static char heightKeyword[] = "height";
    static char formatKeyword[] = "format";
    static char *kwlist[] = {widthKeyword, heightKeyword, formatKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LL|s", kwlist, &width, &height, &format)) {
        return nullptr;
    }

    if (!ensureMetalContext()) {
        PyErr_SetString(PyExc_RuntimeError, "Metal device is unavailable on this device.");
        return nullptr;
    }
    if (width <= 0 || height <= 0) {
        PyErr_SetString(PyExc_ValueError, "Render texture dimensions must be positive.");
        return nullptr;
    }

    MTLPixelFormat fmt = pixelFormatFromName(format);
    NSString *allocationError = textureAllocationError((NSUInteger)width, (NSUInteger)height,
                                                      bytesPerPixelForFormat(fmt));
    if (allocationError) {
        PyErr_SetString(PyExc_ValueError, allocationError.UTF8String);
        return nullptr;
    }
    id<MTLTexture> texture;
    @autoreleasepool {
        MTLTextureDescriptor *descriptor = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:fmt
                                                                                               width:(NSUInteger)width
                                                                                              height:(NSUInteger)height
                                                                                           mipmapped:NO];
        descriptor.usage = MTLTextureUsageShaderRead | MTLTextureUsageRenderTarget | MTLTextureUsageShaderWrite;
        descriptor.storageMode = MTLStorageModePrivate;
        texture = [gDevice newTextureWithDescriptor:descriptor];
    }
    if (!texture) {
        PyErr_SetString(PyExc_RuntimeError, "Failed to allocate Metal render texture.");
        return nullptr;
    }

    long long handle = nextHandle();
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        gTextures.emplace(handle, TextureRecord{handle, texture, (NSUInteger)width, (NSUInteger)height, fmt});
    }

    PyObject *dict = PyDict_New();
    PyObject *handleValue = PyLong_FromLongLong(handle);
    PyObject *sizeValue = Py_BuildValue("(II)", (unsigned int)width, (unsigned int)height);
    PyDict_SetItemString(dict, "handle", handleValue);
    PyDict_SetItemString(dict, "size", sizeValue);
    Py_DECREF(handleValue);
    Py_DECREF(sizeValue);
    return dict;
}

static PyObject *metal_destroy_texture(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) {
        return nullptr;
    }
    std::lock_guard<std::mutex> lock(gStateMutex);
    gTextures.erase(handle);
    Py_RETURN_NONE;
}

static PyObject *metal_write_buffer(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long handle = 0;
    PyObject *dataObject = nullptr;
    long long offset = 0;
    static char handleKeyword[] = "handle";
    static char dataKeyword[] = "data";
    static char offsetKeyword[] = "offset";
    static char *kwlist[] = {handleKeyword, dataKeyword, offsetKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LO|L", kwlist, &handle, &dataObject, &offset)) {
        return nullptr;
    }

    Py_buffer view;
    if (PyObject_GetBuffer(dataObject, &view, PyBUF_SIMPLE) != 0) {
        return nullptr;
    }

    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        BufferRecord *buffer = bufferRecord(handle);
        if (!buffer) {
            PyBuffer_Release(&view);
            PyErr_SetString(PyExc_KeyError, "_metal buffer handle not found.");
            return nullptr;
        }
        if (offset < 0 || offset + view.len > (Py_ssize_t)buffer->buffer.length) {
            PyBuffer_Release(&view);
            PyErr_SetString(PyExc_ValueError, "Buffer write exceeds Metal buffer length.");
            return nullptr;
        }
        memcpy((char *)buffer->buffer.contents + offset, view.buf, (size_t)view.len);
    }

    PyBuffer_Release(&view);
    Py_RETURN_NONE;
}

static PyObject *metal_begin_frame(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0;
    PyObject *clearValue = Py_None;
    long long targetTextureHandle = 0;
    int depth = 0;
    int sampleCount = 1;
    int stencil = 0;
    static char windowKeyword[] = "window";
    static char clearKeyword[] = "clear_color";
    static char targetKeyword[] = "target_texture";
    static char depthKeyword[] = "depth";
    static char sampleCountKeyword[] = "sample_count";
    static char stencilKeyword[] = "stencil";
    static char *kwlist[] = {windowKeyword, clearKeyword, targetKeyword,
                             depthKeyword, sampleCountKeyword, stencilKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "L|OLpIp", kwlist,
            &windowHandle, &clearValue, &targetTextureHandle, &depth, &sampleCount, &stencil)) {
        return nullptr;
    }
    if (sampleCount < 1) sampleCount = 1;
    if (!metalRequireGPU(windowHandle)) return nullptr;

    MTLClearColor clearColor = clearColorFromPyObject(
        clearValue,
        MTLClearColorMake(0.03, 0.04, 0.06, 1.0)
    );

    dispatch_semaphore_t frameSemaphore = nullptr;
    BOOL acquiredFrameSlot = NO;
    BOOL frameSlotAlreadyAcquired = NO;
    if (targetTextureHandle == 0) {
        {
            std::lock_guard<std::mutex> lock(gStateMutex);
            WindowRecord *window = windowRecord(windowHandle);
            if (window) {
                frameSemaphore = window->frameSemaphore;
                frameSlotAlreadyAcquired = window->frameSlotAcquired;
            }
        }
        if (frameSemaphore && !frameSlotAlreadyAcquired) {
            Py_BEGIN_ALLOW_THREADS
            dispatch_semaphore_wait(frameSemaphore, DISPATCH_TIME_FOREVER);
            Py_END_ALLOW_THREADS
            acquiredFrameSlot = YES;
        }
    }

    __block NSString *errorMessage = nil;
    runOnMainSync(^{
        @autoreleasepool {
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *window = windowRecord(windowHandle);
        if (!window) {
            errorMessage = @"_metal window handle not found.";
            return;
        }
        if (window->frameActive) {
            errorMessage = @"A Metal frame is already active for this window.";
            return;
        }
        if (!metalGPUAllowed(*window)) { errorMessage = metalSuspendedMessage; return; }

        window->drawable = nil;
        window->targetTexture = nil;
        if (targetTextureHandle == 0 && acquiredFrameSlot) {
            window->frameSlotAcquired = YES;
        }

        if (targetTextureHandle != 0) {
            TextureRecord *targetTexture = textureRecord(targetTextureHandle);
            if (!targetTexture) {
                errorMessage = @"_metal target texture handle not found.";
                return;
            }
            window->targetTexture = targetTexture->texture;
            if (!window->offscreenCB) {
                window->offscreenCB = [gCommandQueue commandBuffer];
            }
            window->commandBuffer = window->offscreenCB;
        } else {
            if (window->offscreenCB) {
                metalCommit(*window, window->offscreenCB);
                window->offscreenCB = nil;
            }
#if COCOA_PY_UIKIT
            CGFloat scale = window->window.screen.scale;
#else
            CGFloat scale = window->window.backingScaleFactor;
#endif
            CGSize bounds = window->controller.surfaceView.bounds.size;
            window->layer.contentsScale = scale;
            window->layer.frame = window->controller.surfaceView.bounds;
            window->layer.drawableSize = CGSizeMake(bounds.width * scale, bounds.height * scale);
            window->drawable = [window->layer nextDrawable];
            if (!window->drawable) {
                errorMessage = @"Failed to acquire a CAMetalDrawable.";
                return;
            }
        }

        if (!window->commandBuffer) {
            window->commandBuffer = [gCommandQueue commandBuffer];
            if (!window->commandBuffer) {
                errorMessage = @"Failed to create a Metal command buffer.";
                window->drawable = nil;
                window->targetTexture = nil;
                return;
            }
        }

        id<MTLTexture> colorTarget = window->targetTexture ? window->targetTexture : window->drawable.texture;
        NSUInteger tw = colorTarget.width, th = colorTarget.height;
        MTLPixelFormat colorFmt = colorTarget.pixelFormat;

        MTLRenderPassDescriptor *descriptor = [MTLRenderPassDescriptor renderPassDescriptor];

        /* MSAA: auto-create/reuse multisample texture */
        if (sampleCount > 1) {
            if (!window->msaaTexture || tw != window->msaaWidth || th != window->msaaHeight ||
                colorFmt != window->msaaFormat || (NSUInteger)sampleCount != window->msaaSampleCount) {
                MTLTextureDescriptor *msaaDesc = [MTLTextureDescriptor new];
                msaaDesc.textureType = MTLTextureType2DMultisample;
                msaaDesc.pixelFormat = colorFmt;
                msaaDesc.width = tw; msaaDesc.height = th;
                msaaDesc.sampleCount = (NSUInteger)sampleCount;
                msaaDesc.storageMode = MTLStorageModePrivate;
                msaaDesc.usage = MTLTextureUsageRenderTarget;
                window->msaaTexture = [gDevice newTextureWithDescriptor:msaaDesc];
                window->msaaWidth = tw; window->msaaHeight = th;
                window->msaaFormat = colorFmt; window->msaaSampleCount = (NSUInteger)sampleCount;
            }
            descriptor.colorAttachments[0].texture = window->msaaTexture;
            descriptor.colorAttachments[0].resolveTexture = colorTarget;
            descriptor.colorAttachments[0].storeAction = MTLStoreActionMultisampleResolve;
        } else {
            descriptor.colorAttachments[0].texture = colorTarget;
            descriptor.colorAttachments[0].storeAction = MTLStoreActionStore;
        }
        descriptor.colorAttachments[0].loadAction = MTLLoadActionClear;
        descriptor.colorAttachments[0].clearColor = clearColor;

        /* Depth: auto-create/reuse depth texture */
        if (depth) {
            MTLPixelFormat dFmt = MTLPixelFormatDepth32Float;
            NSUInteger dsc = (sampleCount > 1) ? (NSUInteger)sampleCount : 1;
            if (!window->depthTexture || tw != window->depthWidth || th != window->depthHeight ||
                dFmt != window->depthFormat || dsc != window->depthSampleCount) {
                MTLTextureDescriptor *dDesc = [MTLTextureDescriptor new];
                dDesc.pixelFormat = dFmt;
                dDesc.width = tw; dDesc.height = th;
                dDesc.storageMode = MTLStorageModePrivate;
                dDesc.usage = MTLTextureUsageRenderTarget;
                if (dsc > 1) {
                    dDesc.textureType = MTLTextureType2DMultisample;
                    dDesc.sampleCount = dsc;
                } else {
                    dDesc.textureType = MTLTextureType2D;
                }
                window->depthTexture = [gDevice newTextureWithDescriptor:dDesc];
                window->depthWidth = tw; window->depthHeight = th;
                window->depthFormat = dFmt; window->depthSampleCount = dsc;
            }
            descriptor.depthAttachment.texture = window->depthTexture;
            descriptor.depthAttachment.loadAction = MTLLoadActionClear;
            descriptor.depthAttachment.storeAction = MTLStoreActionDontCare;
            descriptor.depthAttachment.clearDepth = 1.0;
        }

        if (stencil) {
            NSUInteger ssc = (sampleCount > 1) ? (NSUInteger)sampleCount : 1;
            if (!window->stencilTexture || tw != window->stencilWidth || th != window->stencilHeight || ssc != window->stencilSampleCount) {
                MTLTextureDescriptor *sDesc = [MTLTextureDescriptor new];
                sDesc.pixelFormat = MTLPixelFormatStencil8;
                sDesc.width = tw; sDesc.height = th;
                sDesc.storageMode = MTLStorageModePrivate;
                sDesc.usage = MTLTextureUsageRenderTarget;
                if (ssc > 1) {
                    sDesc.textureType = MTLTextureType2DMultisample;
                    sDesc.sampleCount = ssc;
                } else {
                    sDesc.textureType = MTLTextureType2D;
                }
                window->stencilTexture = [gDevice newTextureWithDescriptor:sDesc];
                window->stencilWidth = tw; window->stencilHeight = th; window->stencilSampleCount = ssc;
            }
            descriptor.stencilAttachment.texture = window->stencilTexture;
            descriptor.stencilAttachment.loadAction = MTLLoadActionClear;
            descriptor.stencilAttachment.storeAction = MTLStoreActionDontCare;
            descriptor.stencilAttachment.clearStencil = 0;
        }

        window->encoder = [window->commandBuffer renderCommandEncoderWithDescriptor:descriptor];
        if (!window->encoder) {
            errorMessage = @"Failed to create a Metal render encoder.";
            window->commandBuffer = nil;
            window->drawable = nil;
            window->targetTexture = nil;
            return;
        }
        window->frameActive = YES;
        }
    });

    if (errorMessage) {
        if (targetTextureHandle == 0 && (acquiredFrameSlot || frameSlotAlreadyAcquired)) {
            dispatch_semaphore_t releaseSemaphore = frameSemaphore;
            BOOL shouldReleaseFrameSlot = NO;
            {
                std::lock_guard<std::mutex> lock(gStateMutex);
                WindowRecord *window = windowRecord(windowHandle);
                if (window && window->frameSlotAcquired && !window->frameActive) {
                    releaseSemaphore = window->frameSemaphore;
                    window->frameSlotAcquired = NO;
                    shouldReleaseFrameSlot = YES;
                } else if (acquiredFrameSlot && frameSemaphore) {
                    shouldReleaseFrameSlot = YES;
                }
            }
            if (shouldReleaseFrameSlot && releaseSemaphore) {
                dispatch_semaphore_signal(releaseSemaphore);
            }
        }
        metalSetWindowError(errorMessage);
        return nullptr;
    }

    void *framePool = objc_autoreleasePoolPush();
    BOOL keepFramePool = NO;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *window = windowRecord(windowHandle);
        if (window && window->frameActive && window->frameAutoreleasePool == nullptr) {
            window->frameAutoreleasePool = framePool;
            keepFramePool = YES;
        }
    }
    if (!keepFramePool && framePool) {
        objc_autoreleasePoolPop(framePool);
    }
    Py_RETURN_NONE;
}

static PyObject *metal_set_pipeline(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0;
    long long pipelineHandle = 0;
    static char windowKeyword[] = "window";
    static char pipelineKeyword[] = "pipeline";
    static char *kwlist[] = {windowKeyword, pipelineKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LL", kwlist, &windowHandle, &pipelineHandle)) {
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *window = windowRecord(windowHandle);
    PipelineRecord *pipeline = pipelineRecord(pipelineHandle);
    if (!window || !pipeline) {
        PyErr_SetString(PyExc_KeyError, "_metal window or pipeline handle not found.");
        return nullptr;
    }
    if (!window->frameActive || !window->encoder) {
        PyErr_SetString(PyExc_RuntimeError, "begin_frame() must be called before set_pipeline().");
        return nullptr;
    }
    [window->encoder setRenderPipelineState:pipeline->pipeline];
    Py_RETURN_NONE;
}

static PyObject *metal_set_vertex_buffer(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0;
    long long bufferHandle = 0;
    long long index = 0;
    long long offset = 0;
    static char windowKeyword[] = "window";
    static char bufferKeyword[] = "buffer";
    static char indexKeyword[] = "index";
    static char offsetKeyword[] = "offset";
    static char *kwlist[] = {windowKeyword, bufferKeyword, indexKeyword, offsetKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LLL|L", kwlist, &windowHandle, &bufferHandle, &index, &offset)) {
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *window = windowRecord(windowHandle);
    BufferRecord *buffer = bufferRecord(bufferHandle);
    if (!window || !buffer) {
        PyErr_SetString(PyExc_KeyError, "_metal window or buffer handle not found.");
        return nullptr;
    }
    if (!window->frameActive || !window->encoder) {
        PyErr_SetString(PyExc_RuntimeError, "begin_frame() must be called before set_vertex_buffer().");
        return nullptr;
    }
    [window->encoder setVertexBuffer:buffer->buffer offset:(NSUInteger)offset atIndex:(NSUInteger)index];
    Py_RETURN_NONE;
}

static PyObject *metal_set_fragment_buffer(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0;
    long long bufferHandle = 0;
    long long index = 0;
    long long offset = 0;
    static char windowKeyword[] = "window";
    static char bufferKeyword[] = "buffer";
    static char indexKeyword[] = "index";
    static char offsetKeyword[] = "offset";
    static char *kwlist[] = {windowKeyword, bufferKeyword, indexKeyword, offsetKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LLL|L", kwlist, &windowHandle, &bufferHandle, &index, &offset)) {
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *window = windowRecord(windowHandle);
    BufferRecord *buffer = bufferRecord(bufferHandle);
    if (!window || !buffer) {
        PyErr_SetString(PyExc_KeyError, "_metal window or buffer handle not found.");
        return nullptr;
    }
    if (!window->frameActive || !window->encoder) {
        PyErr_SetString(PyExc_RuntimeError, "begin_frame() must be called before set_fragment_buffer().");
        return nullptr;
    }
    [window->encoder setFragmentBuffer:buffer->buffer offset:(NSUInteger)offset atIndex:(NSUInteger)index];
    Py_RETURN_NONE;
}

static PyObject *metal_set_fragment_texture(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0;
    long long textureHandle = 0;
    long long index = 0;
    static char windowKeyword[] = "window";
    static char textureKeyword[] = "texture";
    static char indexKeyword[] = "index";
    static char *kwlist[] = {windowKeyword, textureKeyword, indexKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LLL", kwlist, &windowHandle, &textureHandle, &index)) {
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *window = windowRecord(windowHandle);
    TextureRecord *texture = textureRecord(textureHandle);
    if (!window || !texture) {
        PyErr_SetString(PyExc_KeyError, "_metal window or texture handle not found.");
        return nullptr;
    }
    if (!window->frameActive || !window->encoder) {
        PyErr_SetString(PyExc_RuntimeError, "begin_frame() must be called before set_fragment_texture().");
        return nullptr;
    }
    [window->encoder setFragmentTexture:texture->texture atIndex:(NSUInteger)index];
    Py_RETURN_NONE;
}

static PyObject *metal_draw(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0;
    const char *primitive = "triangle_strip";
    long long vertexStart = 0;
    long long vertexCount = 0;
    static char windowKeyword[] = "window";
    static char primitiveKeyword[] = "primitive";
    static char vertexStartKeyword[] = "vertex_start";
    static char vertexCountKeyword[] = "vertex_count";
    static char *kwlist[] = {windowKeyword, primitiveKeyword, vertexStartKeyword, vertexCountKeyword, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LsLL", kwlist, &windowHandle, &primitive, &vertexStart, &vertexCount)) {
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *window = windowRecord(windowHandle);
    if (!window) {
        PyErr_SetString(PyExc_KeyError, "_metal window handle not found.");
        return nullptr;
    }
    if (!window->frameActive || !window->encoder) {
        PyErr_SetString(PyExc_RuntimeError, "begin_frame() must be called before draw().");
        return nullptr;
    }
    [window->encoder drawPrimitives:primitiveTypeFromName(primitive)
                        vertexStart:(NSUInteger)vertexStart
                        vertexCount:(NSUInteger)vertexCount];
    Py_RETURN_NONE;
}

static PyObject *metal_end_frame(PyObject *self, PyObject *args) {
    (void)self;
    long long windowHandle = 0;
    if (!PyArg_ParseTuple(args, "L", &windowHandle)) {
        return nullptr;
    }

    __block NSString *errorMessage = nil;
    runOnMainSync(^{
        @autoreleasepool {
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *window = windowRecord(windowHandle);
        if (!window) {
            errorMessage = @"_metal window handle not found.";
            return;
        }
        if (!window->frameActive || !window->encoder || !window->commandBuffer || (!window->drawable && !window->targetTexture)) {
            if (window->frameSlotAcquired && window->frameSemaphore) {
                dispatch_semaphore_signal(window->frameSemaphore);
                window->frameSlotAcquired = NO;
            }
            errorMessage = @"begin_frame() must be called before end_frame().";
            return;
        }
        [window->encoder endEncoding];
        window->encoder = nil;
        if (!metalGPUAllowed(*window)) {
            window->commandBuffer = nil;
            window->offscreenCB = nil;
            window->targetTexture = nil;
            window->drawable = nil;
            if (window->frameSlotAcquired && window->frameSemaphore) dispatch_semaphore_signal(window->frameSemaphore);
            window->frameSlotAcquired = NO;
            errorMessage = metalSuspendedMessage;
        } else if (window->targetTexture) {
            window->commandBuffer = nil;
            window->targetTexture = nil;
        } else {
            id<MTLCommandBuffer> commandBuffer = window->commandBuffer;
            if (window->frameSlotAcquired && window->frameSemaphore) {
                dispatch_semaphore_t frameSemaphore = window->frameSemaphore;
                long long completedWindowHandle = windowHandle;
                window->submittedFrames++;
                [commandBuffer addCompletedHandler:^(id<MTLCommandBuffer> buffer) {
                    (void)buffer;
                    dispatch_semaphore_signal(frameSemaphore);
                    std::lock_guard<std::mutex> completionLock(gStateMutex);
                    WindowRecord *completedWindow = windowRecord(completedWindowHandle);
                    if (completedWindow) completedWindow->completedFrames++;
                }];
                window->frameSlotAcquired = NO;
            }
            if (window->drawable) {
                [commandBuffer presentDrawable:window->drawable];
            }
            metalCommit(*window, commandBuffer);
            window->commandBuffer = nil;
            window->drawable = nil;
        }
        window->frameActive = NO;
        }
    });

    void *framePool = nullptr;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *window = windowRecord(windowHandle);
        if (window) {
            framePool = window->frameAutoreleasePool;
            window->frameAutoreleasePool = nullptr;
        }
    }
    if (framePool) {
        objc_autoreleasePoolPop(framePool);
    }

    if (errorMessage) {
        metalSetWindowError(errorMessage);
        return nullptr;
    }
    Py_RETURN_NONE;
}

// ── draw_indexed ──

static PyObject *metal_draw_indexed(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0;
    const char *primitive = "triangle";
    long long indexBufferHandle = 0;
    long long indexCount = 0;
    const char *indexType = "uint16";
    long long offset = 0;
    static char wk[] = "window"; static char pk[] = "primitive";
    static char ibk[] = "index_buffer"; static char ick[] = "index_count";
    static char itk[] = "index_type"; static char ok[] = "offset";
    static char *kwlist[] = {wk, pk, ibk, ick, itk, ok, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LsLL|sL", kwlist,
            &windowHandle, &primitive, &indexBufferHandle, &indexCount, &indexType, &offset)) {
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *window = windowRecord(windowHandle);
    BufferRecord *ibuf = bufferRecord(indexBufferHandle);
    if (!window || !ibuf) {
        PyErr_SetString(PyExc_KeyError, "_metal window or index buffer handle not found.");
        return nullptr;
    }
    if (!window->frameActive || !window->encoder) {
        PyErr_SetString(PyExc_RuntimeError, "begin_frame() must be called before draw_indexed().");
        return nullptr;
    }
    [window->encoder drawIndexedPrimitives:primitiveTypeFromName(primitive)
                                indexCount:(NSUInteger)indexCount
                                 indexType:indexTypeFromName(indexType)
                               indexBuffer:ibuf->buffer
                         indexBufferOffset:(NSUInteger)offset];
    Py_RETURN_NONE;
}

// ── draw_instanced ──

static PyObject *metal_draw_instanced(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0;
    const char *primitive = "triangle";
    long long vertexStart = 0, vertexCount = 0, instanceCount = 1;
    static char wk[] = "window"; static char pk[] = "primitive";
    static char vsk[] = "vertex_start"; static char vck[] = "vertex_count";
    static char ick[] = "instance_count";
    static char *kwlist[] = {wk, pk, vsk, vck, ick, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LsLLL", kwlist,
            &windowHandle, &primitive, &vertexStart, &vertexCount, &instanceCount)) {
        return nullptr;
    }
    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *window = windowRecord(windowHandle);
    if (!window) { PyErr_SetString(PyExc_KeyError, "_metal window handle not found."); return nullptr; }
    if (!window->frameActive || !window->encoder) {
        PyErr_SetString(PyExc_RuntimeError, "begin_frame() must be called first."); return nullptr;
    }
    [window->encoder drawPrimitives:primitiveTypeFromName(primitive)
                        vertexStart:(NSUInteger)vertexStart
                        vertexCount:(NSUInteger)vertexCount
                      instanceCount:(NSUInteger)instanceCount];
    Py_RETURN_NONE;
}

// ── draw_indexed_instanced ──

static PyObject *metal_draw_indexed_instanced(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0;
    const char *primitive = "triangle";
    long long indexBufferHandle = 0, indexCount = 0, instanceCount = 1;
    const char *indexType = "uint16";
    long long offset = 0;
    static char wk[] = "window"; static char pk[] = "primitive";
    static char ibk[] = "index_buffer"; static char ick[] = "index_count";
    static char itk[] = "index_type"; static char ok[] = "offset";
    static char inck[] = "instance_count";
    static char *kwlist[] = {wk, pk, ibk, ick, itk, ok, inck, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LsLL|sLL", kwlist,
            &windowHandle, &primitive, &indexBufferHandle, &indexCount,
            &indexType, &offset, &instanceCount)) {
        return nullptr;
    }
    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *window = windowRecord(windowHandle);
    BufferRecord *ibuf = bufferRecord(indexBufferHandle);
    if (!window || !ibuf) {
        PyErr_SetString(PyExc_KeyError, "_metal window or index buffer handle not found."); return nullptr;
    }
    if (!window->frameActive || !window->encoder) {
        PyErr_SetString(PyExc_RuntimeError, "begin_frame() must be called first."); return nullptr;
    }
    [window->encoder drawIndexedPrimitives:primitiveTypeFromName(primitive)
                                indexCount:(NSUInteger)indexCount
                                 indexType:indexTypeFromName(indexType)
                               indexBuffer:ibuf->buffer
                         indexBufferOffset:(NSUInteger)offset
                             instanceCount:(NSUInteger)instanceCount];
    Py_RETURN_NONE;
}

// ── draw_indirect ──

static PyObject *metal_draw_indirect(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0;
    const char *primitive = "triangle";
    long long indirectBufferHandle = 0, offset = 0;
    static char wk[] = "window"; static char pk[] = "primitive";
    static char ibk[] = "indirect_buffer"; static char ok[] = "offset";
    static char *kwlist[] = {wk, pk, ibk, ok, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LsL|L", kwlist,
            &windowHandle, &primitive, &indirectBufferHandle, &offset)) {
        return nullptr;
    }
    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *window = windowRecord(windowHandle);
    BufferRecord *ibuf = bufferRecord(indirectBufferHandle);
    if (!window || !ibuf) {
        PyErr_SetString(PyExc_KeyError, "_metal window or indirect buffer handle not found."); return nullptr;
    }
    if (!window->frameActive || !window->encoder) {
        PyErr_SetString(PyExc_RuntimeError, "begin_frame() must be called first."); return nullptr;
    }
    [window->encoder drawPrimitives:primitiveTypeFromName(primitive)
                     indirectBuffer:ibuf->buffer
               indirectBufferOffset:(NSUInteger)offset];
    Py_RETURN_NONE;
}

// ── draw_indexed_indirect ──

static PyObject *metal_draw_indexed_indirect(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0;
    const char *primitive = "triangle";
    long long indexBufferHandle = 0, indirectBufferHandle = 0;
    const char *indexType = "uint16";
    long long indirectOffset = 0;
    static char wk[] = "window"; static char pk[] = "primitive";
    static char ibk[] = "index_buffer"; static char itk[] = "index_type";
    static char idk[] = "indirect_buffer"; static char ok[] = "indirect_offset";
    static char *kwlist[] = {wk, pk, ibk, itk, idk, ok, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LsLsL|L", kwlist,
            &windowHandle, &primitive, &indexBufferHandle, &indexType,
            &indirectBufferHandle, &indirectOffset)) {
        return nullptr;
    }
    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *window = windowRecord(windowHandle);
    BufferRecord *ibuf = bufferRecord(indexBufferHandle);
    BufferRecord *indir = bufferRecord(indirectBufferHandle);
    if (!window || !ibuf || !indir) {
        PyErr_SetString(PyExc_KeyError, "_metal handle not found."); return nullptr;
    }
    if (!window->frameActive || !window->encoder) {
        PyErr_SetString(PyExc_RuntimeError, "begin_frame() must be called first."); return nullptr;
    }
    [window->encoder drawIndexedPrimitives:primitiveTypeFromName(primitive)
                                 indexType:indexTypeFromName(indexType)
                               indexBuffer:ibuf->buffer
                         indexBufferOffset:0
                            indirectBuffer:indir->buffer
                      indirectBufferOffset:(NSUInteger)indirectOffset];
    Py_RETURN_NONE;
}

// ── set_depth_stencil ──

static std::unordered_map<uint64_t, id<MTLDepthStencilState>> gDepthStencilCache;

static MTLStencilOperation stencilOpFromName(const char *name) {
    if (strcmp(name, "keep") == 0) return MTLStencilOperationKeep;
    if (strcmp(name, "zero") == 0) return MTLStencilOperationZero;
    if (strcmp(name, "replace") == 0) return MTLStencilOperationReplace;
    if (strcmp(name, "incr") == 0) return MTLStencilOperationIncrementClamp;
    if (strcmp(name, "decr") == 0) return MTLStencilOperationDecrementClamp;
    if (strcmp(name, "incr_wrap") == 0) return MTLStencilOperationIncrementWrap;
    if (strcmp(name, "decr_wrap") == 0) return MTLStencilOperationDecrementWrap;
    if (strcmp(name, "invert") == 0) return MTLStencilOperationInvert;
    return MTLStencilOperationKeep;
}

static PyObject *metal_set_depth_stencil(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0;
    const char *compare = "always";
    int writeEnabled = 0;
    const char *stencilCompare = "always";
    const char *stencilPass = "keep";
    const char *stencilFail = "keep";
    int stencilRef = 0;
    const char *stencilBackPass = nullptr;
    static char wk[] = "window"; static char ck[] = "compare"; static char wek[] = "write_enabled";
    static char sck[] = "stencil_compare"; static char spk[] = "stencil_pass";
    static char sfk[] = "stencil_fail"; static char srk[] = "stencil_ref";
    static char sbpk[] = "stencil_back_pass";
    static char *kwlist[] = {wk, ck, wek, sck, spk, sfk, srk, sbpk, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "L|spsssiz", kwlist,
            &windowHandle, &compare, &writeEnabled,
            &stencilCompare, &stencilPass, &stencilFail, &stencilRef, &stencilBackPass)) {
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *window = windowRecord(windowHandle);
    if (!window || !window->frameActive || !window->encoder) {
        PyErr_SetString(PyExc_RuntimeError, "begin_frame() must be called before set_depth_stencil().");
        return nullptr;
    }

    MTLCompareFunction cf = compareFuncFromName(compare);
    MTLCompareFunction scf = compareFuncFromName(stencilCompare);
    MTLStencilOperation sop = stencilOpFromName(stencilPass);
    MTLStencilOperation sfop = stencilOpFromName(stencilFail);
    MTLStencilOperation bsop = stencilBackPass ? stencilOpFromName(stencilBackPass) : sop;
    uint64_t cacheKey = ((uint64_t)cf << 32) | ((uint64_t)bsop << 24) | ((uint64_t)scf << 16)
        | ((uint64_t)sop << 8) | ((uint64_t)sfop << 4) | (writeEnabled ? 1 : 0);
    auto it = gDepthStencilCache.find(cacheKey);
    id<MTLDepthStencilState> state;
    if (it != gDepthStencilCache.end()) {
        state = it->second;
    } else {
        @autoreleasepool {
            MTLDepthStencilDescriptor *desc = [MTLDepthStencilDescriptor new];
            desc.depthCompareFunction = cf;
            desc.depthWriteEnabled = writeEnabled ? YES : NO;
            MTLStencilDescriptor *sd = [MTLStencilDescriptor new];
            sd.stencilCompareFunction = scf;
            sd.stencilFailureOperation = sfop;
            sd.depthFailureOperation = MTLStencilOperationKeep;
            sd.depthStencilPassOperation = sop;
            sd.readMask = 0xFF;
            sd.writeMask = 0xFF;
            desc.frontFaceStencil = sd;
            MTLStencilDescriptor *back = [MTLStencilDescriptor new];
            back.stencilCompareFunction = scf;
            back.stencilFailureOperation = sfop;
            back.depthFailureOperation = MTLStencilOperationKeep;
            back.depthStencilPassOperation = bsop;
            back.readMask = 0xFF;
            back.writeMask = 0xFF;
            desc.backFaceStencil = back;
            state = [gDevice newDepthStencilStateWithDescriptor:desc];
            gDepthStencilCache[cacheKey] = state;
        }
    }
    [window->encoder setDepthStencilState:state];
    [window->encoder setStencilReferenceValue:(uint32_t)stencilRef];

    Py_RETURN_NONE;
}

// ── blit encoder ──

static PyObject *metal_begin_blit(PyObject *self, PyObject *args) {
    (void)self;
    long long windowHandle = 0;
    if (!PyArg_ParseTuple(args, "L", &windowHandle)) return nullptr;

    __block NSString *errorMessage = nil;
    runOnMainSync(^{
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *wr = windowRecord(windowHandle);
        if (!wr) { errorMessage = @"_metal window handle not found."; return; }
        if (!metalGPUAllowed(*wr)) { errorMessage = metalSuspendedMessage; return; }
        if (wr->blitActive) { errorMessage = @"A blit pass is already active."; return; }
        if (wr->frameActive) { errorMessage = @"End the render pass before starting a blit pass."; return; }
        // Offscreen passes are batched until presentation or an explicit transfer.
        // Submit them first so readback observes the completed rendering.
        if (wr->offscreenCB) {
            metalCommit(*wr, wr->offscreenCB);
            wr->offscreenCB = nil;
        }
        wr->blitCommandBuffer = [gCommandQueue commandBuffer];
        wr->blitEncoder = [wr->blitCommandBuffer blitCommandEncoder];
        wr->blitActive = YES;
    });
    if (errorMessage) { metalSetWindowError(errorMessage); return nullptr; }
    Py_RETURN_NONE;
}

static PyObject *metal_copy_texture_to_buffer(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0, textureHandle = 0, bufferHandle = 0;
    long long bytesPerRow = 0;
    static char wk[] = "window"; static char tk[] = "texture";
    static char bk[] = "buffer"; static char brk[] = "bytes_per_row";
    static char *kwlist[] = {wk, tk, bk, brk, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LLL|L", kwlist,
            &windowHandle, &textureHandle, &bufferHandle, &bytesPerRow)) {
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *wr = windowRecord(windowHandle);
    TextureRecord *tr = textureRecord(textureHandle);
    BufferRecord *br = bufferRecord(bufferHandle);
    if (!wr || !tr || !br) {
        PyErr_SetString(PyExc_KeyError, "_metal handle not found."); return nullptr;
    }
    if (!wr->blitActive || !wr->blitEncoder) {
        PyErr_SetString(PyExc_RuntimeError, "begin_blit() must be called first."); return nullptr;
    }

    NSUInteger bpr = (bytesPerRow > 0) ? (NSUInteger)bytesPerRow
                     : tr->width * bytesPerPixelForFormat(tr->format);
    if (bytesPerRow < 0 || bpr < tr->width * bytesPerPixelForFormat(tr->format) ||
        tr->height > br->buffer.length / bpr) {
        PyErr_SetString(PyExc_ValueError, "The destination buffer or row stride is too small.");
        return nullptr;
    }
    [wr->blitEncoder copyFromTexture:tr->texture
                         sourceSlice:0 sourceLevel:0
                        sourceOrigin:MTLOriginMake(0, 0, 0)
                          sourceSize:MTLSizeMake(tr->width, tr->height, 1)
                            toBuffer:br->buffer
                   destinationOffset:0
              destinationBytesPerRow:bpr
            destinationBytesPerImage:bpr * tr->height];
    Py_RETURN_NONE;
}

static PyObject *metal_generate_mipmaps(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long windowHandle = 0, textureHandle = 0;
    static char wk[] = "window"; static char tk[] = "texture";
    static char *kwlist[] = {wk, tk, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LL", kwlist, &windowHandle, &textureHandle)) {
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *wr = windowRecord(windowHandle);
    TextureRecord *tr = textureRecord(textureHandle);
    if (!wr || !tr) { PyErr_SetString(PyExc_KeyError, "_metal handle not found."); return nullptr; }
    if (!wr->blitActive || !wr->blitEncoder) {
        PyErr_SetString(PyExc_RuntimeError, "begin_blit() must be called first."); return nullptr;
    }
    [wr->blitEncoder generateMipmapsForTexture:tr->texture];
    Py_RETURN_NONE;
}

static PyObject *metal_end_blit(PyObject *self, PyObject *args) {
    (void)self;
    long long windowHandle = 0;
    if (!PyArg_ParseTuple(args, "L", &windowHandle)) return nullptr;

    __block NSString *errorMessage = nil;
    runOnMainSync(^{
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *wr = windowRecord(windowHandle);
        if (!wr) { errorMessage = @"_metal window handle not found."; return; }
        if (!wr->blitActive) { errorMessage = @"No blit pass active."; return; }
        [wr->blitEncoder endEncoding];
        if (metalGPUAllowed(*wr)) {
            metalCommit(*wr, wr->blitCommandBuffer);
            [wr->blitCommandBuffer waitUntilCompleted];
        } else errorMessage = metalSuspendedMessage;
        wr->blitEncoder = nil;
        wr->blitCommandBuffer = nil;
        wr->blitActive = NO;
    });
    if (errorMessage) { metalSetWindowError(errorMessage); return nullptr; }
    Py_RETURN_NONE;
}

// ── read_buffer ──

static PyObject *metal_read_buffer(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long handle = 0;
    Py_ssize_t offset = 0, length = -1;
    static char hKw[] = "handle"; static char oKw[] = "offset"; static char lKw[] = "length";
    static char *kwlist[] = {hKw, oKw, lKw, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "L|nn", kwlist, &handle, &offset, &length)) {
        return nullptr;
    }
    std::lock_guard<std::mutex> lock(gStateMutex);
    BufferRecord *br = bufferRecord(handle);
    if (!br) { PyErr_SetString(PyExc_KeyError, "Buffer handle not found."); return nullptr; }
    NSUInteger bufLen = br->buffer.length;
    if (length < 0) length = (Py_ssize_t)bufLen - offset;
    if (offset < 0 || (NSUInteger)(offset + length) > bufLen) {
        PyErr_SetString(PyExc_ValueError, "read_buffer: offset/length out of range."); return nullptr;
    }
    return PyBytes_FromStringAndSize((const char *)br->buffer.contents + offset, length);
}

// ── create_image_texture ──

static PyObject *metal_create_image_texture(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    const char *path = nullptr;
    static char pKw[] = "path";
    static char *kwlist[] = {pKw, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "s", kwlist, &path)) return nullptr;
    if (!ensureMetalContext()) { PyErr_SetString(PyExc_RuntimeError, "Metal unavailable."); return nullptr; }

    __block long long handle = 0;
    __block NSUInteger texW = 0, texH = 0;
    __block NSString *errorMessage = nil;

    NSString *nsPath = [NSString stringWithUTF8String:path];
    runOnMainSync(^{
        @autoreleasepool {
            NSURL *url = [NSURL fileURLWithPath:nsPath];
            CocoaPyFileAccess access(url);
            CGImageSourceRef source = CGImageSourceCreateWithURL((__bridge CFURLRef)url, nullptr);
            if (!source) { errorMessage = @"Failed to load image file."; return; }
            CGImageRef cgImage = CGImageSourceCreateImageAtIndex(source, 0, nullptr);
            CFRelease(source);
            if (!cgImage) { errorMessage = @"Failed to decode image file."; return; }
            struct ImageGuard { CGImageRef value; ~ImageGuard() { CGImageRelease(value); } } guard{cgImage};
            texW = CGImageGetWidth(cgImage);
            texH = CGImageGetHeight(cgImage);
            MTLTextureDescriptor *desc = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                                                                           width:texW height:texH mipmapped:NO];
            desc.usage = MTLTextureUsageShaderRead;
            id<MTLTexture> texture = [gDevice newTextureWithDescriptor:desc];
            if (!texture) { errorMessage = @"Failed to create texture."; return; }

            uint8_t *pixels = (uint8_t *)calloc(texW * texH * 4, 1);
            CGColorSpaceRef cs = CGColorSpaceCreateDeviceRGB();
            CGContextRef ctx = CGBitmapContextCreate(pixels, texW, texH, 8, texW * 4, cs,
                kCGBitmapByteOrder32Little | kCGImageAlphaPremultipliedFirst);
            CGContextDrawImage(ctx, CGRectMake(0, 0, texW, texH), cgImage);
            CGContextRelease(ctx);
            CGColorSpaceRelease(cs);

            [texture replaceRegion:MTLRegionMake2D(0, 0, texW, texH) mipmapLevel:0
                         withBytes:pixels bytesPerRow:texW * 4];
            free(pixels);

            handle = nextHandle();
            std::lock_guard<std::mutex> lock(gStateMutex);
            gTextures.emplace(handle, TextureRecord{handle, texture, texW, texH, MTLPixelFormatBGRA8Unorm});
        }
    });

    if (errorMessage) { PyErr_SetString(PyExc_RuntimeError, errorMessage.UTF8String); return nullptr; }
    return Py_BuildValue("{s:L,s:(II)}", "handle", handle, "size", (unsigned int)texW, (unsigned int)texH);
}

// ── compute pipeline ──

static PyObject *metal_create_compute_pipeline(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long libraryHandle = 0;
    const char *function = nullptr;
    static char lKw[] = "library"; static char fKw[] = "function";
    static char *kwlist[] = {lKw, fKw, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "Ls", kwlist, &libraryHandle, &function)) return nullptr;
    if (!ensureMetalContext()) { PyErr_SetString(PyExc_RuntimeError, "Metal unavailable."); return nullptr; }

    std::lock_guard<std::mutex> lock(gStateMutex);
    LibraryRecord *lib = libraryRecord(libraryHandle);
    if (!lib) { PyErr_SetString(PyExc_KeyError, "Library handle not found."); return nullptr; }
    id<MTLFunction> fn = [lib->library newFunctionWithName:[NSString stringWithUTF8String:function]];
    if (!fn) { PyErr_SetString(PyExc_RuntimeError, "Compute function not found in library."); return nullptr; }
    NSError *error = nil;
    id<MTLComputePipelineState> pipeline = [gDevice newComputePipelineStateWithFunction:fn error:&error];
    if (!pipeline) { PyErr_SetString(PyExc_RuntimeError, error.localizedDescription.UTF8String); return nullptr; }
    long long handle = nextHandle();
    gComputePipelines.emplace(handle, ComputePipelineRecord{handle, pipeline});
    return PyLong_FromLongLong(handle);
}

static PyObject *metal_destroy_compute_pipeline(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::lock_guard<std::mutex> lock(gStateMutex);
    gComputePipelines.erase(handle);
    Py_RETURN_NONE;
}

static PyObject *metal_begin_compute(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;

    __block NSString *errorMessage = nil;
    runOnMainSync(^{
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *wr = windowRecord(handle);
        if (!wr) { errorMessage = @"Invalid window handle."; return; }
        if (!metalGPUAllowed(*wr)) { errorMessage = metalSuspendedMessage; return; }
        if (wr->computeActive) { errorMessage = @"Compute pass already active."; return; }
        wr->computeCommandBuffer = [gCommandQueue commandBuffer];
        wr->computeEncoder = [wr->computeCommandBuffer computeCommandEncoder];
        wr->computeActive = YES;
    });
    if (errorMessage) { metalSetWindowError(errorMessage); return nullptr; }
    Py_RETURN_NONE;
}

static PyObject *metal_set_compute_pipeline(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long winHandle = 0, pipeHandle = 0;
    static char wKw[] = "window"; static char pKw[] = "pipeline";
    static char *kwlist[] = {wKw, pKw, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LL", kwlist, &winHandle, &pipeHandle)) return nullptr;
    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *wr = windowRecord(winHandle);
    if (!wr || !wr->computeActive) { PyErr_SetString(PyExc_RuntimeError, "No active compute pass."); return nullptr; }
    auto it = gComputePipelines.find(pipeHandle);
    if (it == gComputePipelines.end()) { PyErr_SetString(PyExc_KeyError, "Compute pipeline not found."); return nullptr; }
    [wr->computeEncoder setComputePipelineState:it->second.pipeline];
    Py_RETURN_NONE;
}

static PyObject *metal_set_compute_buffer(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long winHandle = 0, bufHandle = 0;
    int index = 0; Py_ssize_t offset = 0;
    static char wKw[] = "window"; static char bKw[] = "buffer"; static char iKw[] = "index"; static char oKw[] = "offset";
    static char *kwlist[] = {wKw, bKw, iKw, oKw, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LLi|n", kwlist, &winHandle, &bufHandle, &index, &offset)) return nullptr;
    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *wr = windowRecord(winHandle);
    BufferRecord *br = bufferRecord(bufHandle);
    if (!wr || !wr->computeActive) { PyErr_SetString(PyExc_RuntimeError, "No active compute pass."); return nullptr; }
    if (!br) { PyErr_SetString(PyExc_KeyError, "Buffer not found."); return nullptr; }
    [wr->computeEncoder setBuffer:br->buffer offset:offset atIndex:index];
    Py_RETURN_NONE;
}

static PyObject *metal_set_compute_texture(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long winHandle = 0, texHandle = 0;
    int index = 0;
    static char wKw[] = "window"; static char tKw[] = "texture"; static char iKw[] = "index";
    static char *kwlist[] = {wKw, tKw, iKw, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "LLi", kwlist, &winHandle, &texHandle, &index)) return nullptr;
    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *wr = windowRecord(winHandle);
    TextureRecord *tr = textureRecord(texHandle);
    if (!wr || !wr->computeActive) { PyErr_SetString(PyExc_RuntimeError, "No active compute pass."); return nullptr; }
    if (!tr) { PyErr_SetString(PyExc_KeyError, "Texture not found."); return nullptr; }
    [wr->computeEncoder setTexture:tr->texture atIndex:index];
    Py_RETURN_NONE;
}

static PyObject *metal_dispatch_compute(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long winHandle = 0;
    int gx = 1, gy = 1, gz = 1, tx = 1, ty = 1, tz = 1;
    static char wKw[] = "window";
    static char gxK[] = "grid_x"; static char gyK[] = "grid_y"; static char gzK[] = "grid_z";
    static char txK[] = "tg_x"; static char tyK[] = "tg_y"; static char tzK[] = "tg_z";
    static char *kwlist[] = {wKw, gxK, gyK, gzK, txK, tyK, tzK, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "L|iiiiii", kwlist, &winHandle, &gx, &gy, &gz, &tx, &ty, &tz)) return nullptr;
    std::lock_guard<std::mutex> lock(gStateMutex);
    WindowRecord *wr = windowRecord(winHandle);
    if (!wr || !wr->computeActive) { PyErr_SetString(PyExc_RuntimeError, "No active compute pass."); return nullptr; }
    [wr->computeEncoder dispatchThreadgroups:MTLSizeMake(gx, gy, gz) threadsPerThreadgroup:MTLSizeMake(tx, ty, tz)];
    /* Insert texture barrier so subsequent dispatches see writes from this one */
    [wr->computeEncoder memoryBarrierWithScope:MTLBarrierScopeTextures];
    Py_RETURN_NONE;
}

static PyObject *metal_end_compute(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    __block NSString *errorMessage = nil;
    runOnMainSync(^{
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *wr = windowRecord(handle);
        if (!wr) { errorMessage = @"Invalid window handle."; return; }
        if (!wr->computeActive) { errorMessage = @"No active compute pass."; return; }
        [wr->computeEncoder endEncoding];
        if (metalGPUAllowed(*wr)) {
            metalCommit(*wr, wr->computeCommandBuffer);
            [wr->computeCommandBuffer waitUntilCompleted];
        } else errorMessage = metalSuspendedMessage;
        wr->computeEncoder = nil;
        wr->computeCommandBuffer = nil;
        wr->computeActive = NO;
    });
    if (errorMessage) { metalSetWindowError(errorMessage); return nullptr; }
    Py_RETURN_NONE;
}

// ── vsync ──

static PyObject *metal_set_target_fps(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    int fps = 60;
    if (!PyArg_ParseTuple(args, "Li", &handle, &fps)) return nullptr;
    __block int appliedFps = fps < 1 ? 1 : fps;
    runOnMainSync(^{
        @autoreleasepool {
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *wr = windowRecord(handle);
        if (!wr || !wr->displayLink) return;
        NSInteger maxFps = wr->window.screen.maximumFramesPerSecond;
        if (maxFps <= 0) maxFps = 60;
        if (appliedFps > maxFps) appliedFps = (int)maxFps;
        wr->displayLink.preferredFrameRateRange = CAFrameRateRangeMake(appliedFps, appliedFps, appliedFps);
        }
    });
    return PyLong_FromLong(appliedFps);
}

static PyObject *metal_acquire_frame_slot(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    if (!metalRequireGPU(handle)) return nullptr;

    dispatch_semaphore_t frameSemaphore = nullptr;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *wr = windowRecord(handle);
        if (!wr) {
            PyErr_SetString(PyExc_ValueError, "Invalid window handle.");
            return nullptr;
        }
        if (wr->frameSlotAcquired) {
            Py_RETURN_NONE;
        }
        frameSemaphore = wr->frameSemaphore;
    }

    if (frameSemaphore) {
        Py_BEGIN_ALLOW_THREADS
        dispatch_semaphore_wait(frameSemaphore, DISPATCH_TIME_FOREVER);
        Py_END_ALLOW_THREADS
    }

    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *wr = windowRecord(handle);
        if (!wr) {
            if (frameSemaphore) {
                dispatch_semaphore_signal(frameSemaphore);
            }
            PyErr_SetString(PyExc_ValueError, "Invalid window handle.");
            return nullptr;
        }
        if (wr->frameSlotAcquired) {
            if (frameSemaphore) {
                dispatch_semaphore_signal(frameSemaphore);
            }
        } else {
            if (!metalGPUAllowed(*wr)) {
                if (frameSemaphore) dispatch_semaphore_signal(frameSemaphore);
                PyErr_SetString(gWindowSuspendedError, metalSuspendedMessage.UTF8String);
                return nullptr;
            }
            wr->frameSlotAcquired = YES;
        }
    }

    Py_RETURN_NONE;
}

static PyObject *metal_release_frame_slot(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;

    dispatch_semaphore_t frameSemaphore = nullptr;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *wr = windowRecord(handle);
        if (!wr) {
            PyErr_SetString(PyExc_ValueError, "Invalid window handle.");
            return nullptr;
        }
        if (wr->frameSlotAcquired && !wr->frameActive) {
            frameSemaphore = wr->frameSemaphore;
            wr->frameSlotAcquired = NO;
        }
    }
    if (frameSemaphore) {
        dispatch_semaphore_signal(frameSemaphore);
    }

    Py_RETURN_NONE;
}

static PyObject *metal_vsync(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;

    dispatch_semaphore_t sem = nullptr;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        WindowRecord *wr = windowRecord(handle);
        if (!wr) { PyErr_SetString(PyExc_ValueError, "Invalid window handle."); return nullptr; }
        sem = wr->vsyncSemaphore;
    }
    if (!sem) { Py_RETURN_NONE; }

    Py_BEGIN_ALLOW_THREADS
    if (dispatch_semaphore_wait(sem, DISPATCH_TIME_NOW) == 0) {
        while (dispatch_semaphore_wait(sem, DISPATCH_TIME_NOW) == 0) {}
    } else {
#if COCOA_PY_UIKIT
        CocoaPyWaitSemaphore(sem, 0.25);
#else
        CocoaPyWaitSemaphore(sem, 0.25);
        metalMacPumpEvents();
#endif
    }
    Py_END_ALLOW_THREADS

    Py_RETURN_NONE;
}

// ── touches ──

static PyObject *metal_consume_scrolls(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::vector<ScrollEvent> events;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto window = gWindows.find(handle);
        if (window == gWindows.end()) {
            PyErr_SetString(PyExc_KeyError, "Window handle not found."); return nullptr;
        }
        events.swap(window->second.scrollQueue);
    }
    PyObject *result = PyList_New(events.size());
    if (!result) return nullptr;
    for (size_t i = 0; i < events.size(); ++i) {
        const auto &event = events[i];
        PyObject *item = Py_BuildValue("{s:d,s:d,s:d,s:d,s:d,s:O,s:O,s:K}",
            "x", event.x, "y", event.y, "dx", event.dx, "dy", event.dy,
            "timestamp", event.timestamp, "precise", event.precise ? Py_True : Py_False,
            "momentum", event.momentum ? Py_True : Py_False, "epoch", event.epoch);
        if (!item) { Py_DECREF(result); return nullptr; }
        PyList_SET_ITEM(result, i, item);
    }
    return result;
}

static PyObject *metal_consume_touches(PyObject *self, PyObject *args) {
    (void)self;
    long long handle = 0;
    if (!PyArg_ParseTuple(args, "L", &handle)) {
        return nullptr;
    }

    std::vector<TouchEvent> events;
    {
        std::lock_guard<std::mutex> lock(gStateMutex);
        auto it = gWindows.find(handle);
        if (it == gWindows.end()) {
            PyErr_SetString(PyExc_ValueError, "Invalid window handle.");
            return nullptr;
        }
        events.swap(it->second.touchQueue);
    }

    PyObject *list = PyList_New(events.size());
    if (!list) return nullptr;

    for (size_t i = 0; i < events.size(); i++) {
        const auto &e = events[i];
        PyObject *dict = Py_BuildValue("{s:i,s:L,s:d,s:d,s:d,s:d,s:d,s:K}",
            "phase", e.phase,
            "id", e.touchId,
            "x", e.x,
            "y", e.y,
            "prev_x", e.prevX,
            "prev_y", e.prevY,
            "timestamp", e.timestamp, "epoch", e.epoch
        );
        if (!dict) { Py_DECREF(list); return nullptr; }
        PyList_SET_ITEM(list, i, dict);
    }
    return list;
}

static unsigned long long estimatedTextureBytes(id<MTLTexture> texture) {
    if (!texture) return 0;
    NSUInteger samples = texture.sampleCount > 0 ? texture.sampleCount : 1;
    return (unsigned long long)texture.width
        * (unsigned long long)texture.height
        * (unsigned long long)samples
        * (unsigned long long)bytesPerPixelForFormat(texture.pixelFormat);
}

static void dictSetUnsigned(PyObject *dict, const char *key, unsigned long long value) {
    PyObject *pyValue = PyLong_FromUnsignedLongLong(value);
    if (!pyValue) return;
    PyDict_SetItemString(dict, key, pyValue);
    Py_DECREF(pyValue);
}

static void currentProcessMemoryBytes(unsigned long long *residentBytes, unsigned long long *footprintBytes) {
    if (residentBytes) *residentBytes = 0;
    if (footprintBytes) *footprintBytes = 0;

    mach_task_basic_info_data_t basicInfo;
    mach_msg_type_number_t basicCount = MACH_TASK_BASIC_INFO_COUNT;
    if (task_info(mach_task_self(), MACH_TASK_BASIC_INFO,
                  (task_info_t)&basicInfo, &basicCount) == KERN_SUCCESS) {
        if (residentBytes) *residentBytes = (unsigned long long)basicInfo.resident_size;
    }

    task_vm_info_data_t vmInfo;
    mach_msg_type_number_t vmCount = TASK_VM_INFO_COUNT;
    if (task_info(mach_task_self(), TASK_VM_INFO,
                  (task_info_t)&vmInfo, &vmCount) == KERN_SUCCESS) {
        if (footprintBytes) *footprintBytes = (unsigned long long)vmInfo.phys_footprint;
    }
}

static PyObject *metal_resource_counts(PyObject *self, PyObject *args) {
    (void)self;
    (void)args;

    unsigned long long bufferBytes = 0;
    unsigned long long textureBytes = 0;
    unsigned long long windowTextureBytes = 0;
    unsigned long long activeFrames = 0;
    unsigned long long activeCompute = 0;
    unsigned long long activeBlits = 0;
    unsigned long long offscreenCommandBuffers = 0;
    unsigned long long frameAutoreleasePools = 0;
    unsigned long long submittedFrames = 0;
    unsigned long long completedFrames = 0;
    unsigned long long inFlightFrames = 0;
    unsigned long long residentBytes = 0;
    unsigned long long footprintBytes = 0;
    currentProcessMemoryBytes(&residentBytes, &footprintBytes);

    std::lock_guard<std::mutex> lock(gStateMutex);
    for (const auto &entry : gBuffers) {
        bufferBytes += (unsigned long long)entry.second.buffer.length;
    }
    for (const auto &entry : gTextures) {
        textureBytes += estimatedTextureBytes(entry.second.texture);
    }
    for (const auto &entry : gWindows) {
        const WindowRecord &wr = entry.second;
        if (wr.frameActive) activeFrames++;
        if (wr.computeActive) activeCompute++;
        if (wr.blitActive) activeBlits++;
        if (wr.offscreenCB) offscreenCommandBuffers++;
        if (wr.frameAutoreleasePool) frameAutoreleasePools++;
        submittedFrames += wr.submittedFrames;
        completedFrames += wr.completedFrames;
        if (wr.submittedFrames >= wr.completedFrames) {
            inFlightFrames += wr.submittedFrames - wr.completedFrames;
        }
        windowTextureBytes += estimatedTextureBytes(wr.msaaTexture);
        windowTextureBytes += estimatedTextureBytes(wr.depthTexture);
        windowTextureBytes += estimatedTextureBytes(wr.stencilTexture);
    }

    PyObject *dict = PyDict_New();
    if (!dict) return nullptr;
    dictSetUnsigned(dict, "libraries", (unsigned long long)gLibraries.size());
    dictSetUnsigned(dict, "pipelines", (unsigned long long)gPipelines.size());
    dictSetUnsigned(dict, "compute_pipelines", (unsigned long long)gComputePipelines.size());
    dictSetUnsigned(dict, "depth_stencil_states", (unsigned long long)gDepthStencilCache.size());
    dictSetUnsigned(dict, "buffers", (unsigned long long)gBuffers.size());
    dictSetUnsigned(dict, "buffer_bytes", bufferBytes);
    dictSetUnsigned(dict, "textures", (unsigned long long)gTextures.size());
    dictSetUnsigned(dict, "texture_bytes", textureBytes);
    dictSetUnsigned(dict, "windows", (unsigned long long)gWindows.size());
    dictSetUnsigned(dict, "window_event_observers", gSceneEventObservers.load(std::memory_order_relaxed));
    dictSetUnsigned(dict, "text_inputs", gTextInputCount.load(std::memory_order_relaxed));
    dictSetUnsigned(dict, "window_texture_bytes", windowTextureBytes);
    dictSetUnsigned(dict, "active_frames", activeFrames);
    dictSetUnsigned(dict, "active_compute_passes", activeCompute);
    dictSetUnsigned(dict, "active_blit_passes", activeBlits);
    dictSetUnsigned(dict, "offscreen_command_buffers", offscreenCommandBuffers);
    dictSetUnsigned(dict, "frame_autorelease_pools", frameAutoreleasePools);
    dictSetUnsigned(dict, "submitted_frames", submittedFrames);
    dictSetUnsigned(dict, "completed_frames", completedFrames);
    dictSetUnsigned(dict, "inflight_frames", inFlightFrames);
    dictSetUnsigned(dict, "resident_bytes", residentBytes);
    dictSetUnsigned(dict, "phys_footprint_bytes", footprintBytes);
    return dict;
}

#include "MetalImages.h"

static PyMethodDef metalMethods[] = {
    {"window_state", metal_window_state, METH_VARARGS, "Read window lifecycle state."},
    {"keyboard_events", metal_keyboard_events, METH_VARARGS, "Enable queued keyboard events."},
    {"reset_window_input", metal_reset_window_input, METH_VARARGS, "Cancel pending input for a scene switch."},
    {"discard_pending_draws", metal_discard_pending_draws, METH_VARARGS, "Discard unsubmitted offscreen draws between passes."},
    {"consume_platform_events", metal_consume_platform_events, METH_VARARGS, "Consume keyboard and lifecycle events."},
    {"text_input_normalize", metal_text_input_normalize, METH_VARARGS, "Normalize plain text and its grapheme limit."},
    {"text_input_create", metal_text_input_create, METH_VARARGS, "Create a native scene text editor."},
    {"text_input_update", metal_text_input_update, METH_VARARGS, "Update text editor options."},
    {"text_input_command", metal_text_input_command, METH_VARARGS, "Perform a text editing command."},
    {"text_input_state", metal_text_input_state, METH_VARARGS, "Read a text editor state."},
    {"text_input_frame", metal_text_input_frame, METH_VARARGS, "Place a text editor in its scene window."},
    {"text_input_anchor", metal_text_input_anchor, METH_VARARGS, "Position input-method UI for an application-drawn caret."},
    {"text_input_events", metal_text_input_events, METH_VARARGS, "Consume text editing events."},
    {"text_input_close", metal_text_input_close, METH_VARARGS, "Release a text editor."},
    {"text_input_snapshot", metal_text_input_snapshot, METH_VARARGS, "Rasterize a text editor without changing its editing state."},
    {"text_input_keyboard", metal_text_input_keyboard, METH_VARARGS, "Return the current software keyboard rectangle."},
    {"prepare_image_capture", metal_prepare_image_capture, METH_VARARGS, "Wait for preceding scene draws before image capture."},
    {"read_texture_image", (PyCFunction)metal_read_texture_image, METH_VARARGS | METH_KEYWORDS, "Read an RGBA image after completing queued rendering."},
    {"encode_png", metal_encode_png, METH_VARARGS, "Encode straight RGBA bytes as PNG."},
    {"create_window", (PyCFunction)metal_create_window, METH_VARARGS | METH_KEYWORDS, "Create a Metal-backed presentation window."},
    {"close_window", metal_close_window, METH_VARARGS, "Close a Metal presentation window."},
    {"window_metrics", metal_window_metrics, METH_VARARGS, "Return logical and pixel metrics for a window."},
    {"window_metrics_if_changed", metal_window_metrics_if_changed, METH_VARARGS, "Return updated window metrics when layout revision changed."},
    {"consume_actions", metal_consume_actions, METH_VARARGS, "Consume action button presses for a window."},
    {"consume_touches", metal_consume_touches, METH_VARARGS, "Consume touch events for a window."},
    {"consume_scrolls", metal_consume_scrolls, METH_VARARGS, "Consume scrolling deltas in viewport points."},
    {"set_title", (PyCFunction)metal_set_title, METH_VARARGS | METH_KEYWORDS, "Set the presentation window title."},
    {"set_action_label", (PyCFunction)metal_set_action_label, METH_VARARGS | METH_KEYWORDS, "Set or hide the auxiliary action button label."},
    {"create_library", (PyCFunction)metal_create_library, METH_VARARGS | METH_KEYWORDS, "Compile a Metal library from complete source."},
    {"destroy_library", metal_destroy_library, METH_VARARGS, "Destroy a compiled Metal library."},
    {"create_render_pipeline", (PyCFunction)metal_create_render_pipeline, METH_VARARGS | METH_KEYWORDS, "Create a Metal render pipeline."},
    {"destroy_render_pipeline", metal_destroy_render_pipeline, METH_VARARGS, "Destroy a Metal render pipeline."},
    {"create_buffer", (PyCFunction)metal_create_buffer, METH_VARARGS | METH_KEYWORDS, "Create a shared Metal buffer."},
    {"destroy_buffer", metal_destroy_buffer, METH_VARARGS, "Destroy a Metal buffer."},
    {"write_buffer", (PyCFunction)metal_write_buffer, METH_VARARGS | METH_KEYWORDS, "Write bytes into a Metal buffer."},
    {"read_buffer", (PyCFunction)metal_read_buffer, METH_VARARGS | METH_KEYWORDS, "Read bytes from a Metal buffer."},
    {"create_text_texture", (PyCFunction)metal_create_text_texture, METH_VARARGS | METH_KEYWORDS, "Rasterize text into a Metal texture."},
    {"text_layout", metal_text_layout, METH_VARARGS, "Measure or rasterize a shaped label paragraph."},
    {"create_glyph_atlas", (PyCFunction)metal_create_glyph_atlas, METH_VARARGS | METH_KEYWORDS, "Build a glyph atlas texture for a font."},
    {"create_render_texture", (PyCFunction)metal_create_render_texture, METH_VARARGS | METH_KEYWORDS, "Allocate a renderable Metal texture."},
    {"create_image_texture", (PyCFunction)metal_create_image_texture, METH_VARARGS | METH_KEYWORDS, "Load an image file into a Metal texture."},
    {"destroy_texture", metal_destroy_texture, METH_VARARGS, "Destroy a Metal texture."},
    {"begin_frame", (PyCFunction)metal_begin_frame, METH_VARARGS | METH_KEYWORDS, "Begin encoding a frame for a window."},
    {"set_pipeline", (PyCFunction)metal_set_pipeline, METH_VARARGS | METH_KEYWORDS, "Bind a render pipeline for the active frame."},
    {"set_vertex_buffer", (PyCFunction)metal_set_vertex_buffer, METH_VARARGS | METH_KEYWORDS, "Bind a vertex buffer for the active frame."},
    {"set_fragment_buffer", (PyCFunction)metal_set_fragment_buffer, METH_VARARGS | METH_KEYWORDS, "Bind a fragment buffer for the active frame."},
    {"set_fragment_texture", (PyCFunction)metal_set_fragment_texture, METH_VARARGS | METH_KEYWORDS, "Bind a fragment texture for the active frame."},
    {"draw", (PyCFunction)metal_draw, METH_VARARGS | METH_KEYWORDS, "Issue a draw call for the active frame."},
    {"draw_indexed", (PyCFunction)metal_draw_indexed, METH_VARARGS | METH_KEYWORDS, "Issue an indexed draw call."},
    {"draw_instanced", (PyCFunction)metal_draw_instanced, METH_VARARGS | METH_KEYWORDS, "Issue an instanced draw call."},
    {"draw_indexed_instanced", (PyCFunction)metal_draw_indexed_instanced, METH_VARARGS | METH_KEYWORDS, "Issue an indexed instanced draw call."},
    {"draw_indirect", (PyCFunction)metal_draw_indirect, METH_VARARGS | METH_KEYWORDS, "Issue an indirect draw call."},
    {"draw_indexed_indirect", (PyCFunction)metal_draw_indexed_indirect, METH_VARARGS | METH_KEYWORDS, "Issue an indexed indirect draw call."},
    {"set_depth_stencil", (PyCFunction)metal_set_depth_stencil, METH_VARARGS | METH_KEYWORDS, "Set depth/stencil state."},
    {"end_frame", metal_end_frame, METH_VARARGS, "End encoding and present the active frame."},
    {"begin_blit", metal_begin_blit, METH_VARARGS, "Begin a blit pass."},
    {"copy_texture_to_buffer", (PyCFunction)metal_copy_texture_to_buffer, METH_VARARGS | METH_KEYWORDS, "Copy texture to buffer."},
    {"generate_mipmaps", (PyCFunction)metal_generate_mipmaps, METH_VARARGS | METH_KEYWORDS, "Generate mipmaps for a texture."},
    {"end_blit", metal_end_blit, METH_VARARGS, "End blit pass and wait."},
    {"create_compute_pipeline", (PyCFunction)metal_create_compute_pipeline, METH_VARARGS | METH_KEYWORDS, "Create a compute pipeline."},
    {"destroy_compute_pipeline", metal_destroy_compute_pipeline, METH_VARARGS, "Destroy a compute pipeline."},
    {"begin_compute", metal_begin_compute, METH_VARARGS, "Begin a compute pass."},
    {"set_compute_pipeline", (PyCFunction)metal_set_compute_pipeline, METH_VARARGS | METH_KEYWORDS, "Set compute pipeline."},
    {"set_compute_buffer", (PyCFunction)metal_set_compute_buffer, METH_VARARGS | METH_KEYWORDS, "Set compute buffer."},
    {"set_compute_texture", (PyCFunction)metal_set_compute_texture, METH_VARARGS | METH_KEYWORDS, "Set compute texture."},
    {"dispatch_compute", (PyCFunction)metal_dispatch_compute, METH_VARARGS | METH_KEYWORDS, "Dispatch compute threads."},
    {"end_compute", metal_end_compute, METH_VARARGS, "End compute pass and wait."},
    {"set_target_fps", metal_set_target_fps, METH_VARARGS, "Set display link frame rate."},
    {"acquire_frame_slot", metal_acquire_frame_slot, METH_VARARGS, "Wait for an available onscreen frame slot."},
    {"release_frame_slot", metal_release_frame_slot, METH_VARARGS, "Release a previously acquired onscreen frame slot."},
    {"vsync", metal_vsync, METH_VARARGS, "Wait for next display refresh."},
    {"resource_counts", metal_resource_counts, METH_NOARGS, "Return internal Metal resource counts for diagnostics."},
    {nullptr, nullptr, 0, nullptr},
};

static struct PyModuleDef metalModule = {
    PyModuleDef_HEAD_INIT,
    "_cocoa._metal",
    "cocoa-py Metal runtime bridge.",
    -1,
    metalMethods,
};

PyMODINIT_FUNC PyInit__metal(void) {
    PyObject *module = PyModule_Create(&metalModule);
    if (!module) return nullptr;
    gWindowSuspendedError = PyErr_NewException("_cocoa._metal.WindowSuspendedError", PyExc_RuntimeError, nullptr);
    if (!gWindowSuspendedError || PyModule_AddObject(module, "WindowSuspendedError", gWindowSuspendedError) < 0) {
        Py_XDECREF(gWindowSuspendedError);
        Py_DECREF(module);
        return nullptr;
    }
    return module;
}

void registerMetalModule(void) {
    PyImport_AppendInittab("_cocoa._metal", PyInit__metal);
}
