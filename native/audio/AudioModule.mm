/*
 * AudioModule.mm — AVAudioEngine-based audio module for cocoa-py
 *
 * Provides sound effect playback (multiple simultaneous channels),
 * music playback with loop/fade, per-channel volume/pan/pitch,
 * global master volume, built-in effects (reverb, EQ), and
 * resource management via opaque handles.
 *
 * Registered as built-in module "_audio" via PyImport_AppendInittab.
 */

#import <Python.h>
#import <AVFoundation/AVFoundation.h>
#import <AudioToolbox/AudioToolbox.h>
#import <QuartzCore/CABase.h>
#import <mutex>
#import <atomic>
#import <unordered_map>
#import <vector>
#import <string>
#import <memory>
#include <algorithm>
#include <cmath>
#include <limits>
#include <utility>
#include <thread>
#include <array>

#import "AudioModule.h"
#include "AudioBuffers.h"

#if COCOA_PY_UIKIT || defined(COCOA_PY_AUDIO_IOS_TESTS)
#define COCOA_PY_AUDIO_SESSION 1
#else
#define COCOA_PY_AUDIO_SESSION 0
#import <CoreAudio/CoreAudio.h>
#endif

/* ─────────────────────────────────────────────────────────────────────── */
/* Handle management                                                      */
/* ─────────────────────────────────────────────────────────────────────── */

static std::atomic<long long> gNextHandle{1};
static std::mutex gMutex;

static long long nextHandle(void) {
    return gNextHandle.fetch_add(1);
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Records                                                                 */
/* ─────────────────────────────────────────────────────────────────────── */

struct SoundRecord {
    long long handle;
    __strong AVAudioPCMBuffer *buffer;
    __strong AVAudioFormat *format;
    double duration;            // seconds
    int channels;               // 1 or 2
    double sampleRate;          // Hz
    AVAudioFramePosition frames; // 64-bit metadata for long files
    __strong NSURL *streamURL = nil;
    std::shared_ptr<AudioFileAccess> fileAccess;
    BOOL loaded;                // YES only when decoded audio is usable
    __strong NSString *error = nil;
    BOOL unloadRequested = NO;
    std::vector<long long> pendingPlays;
};

struct ChannelRecord {
    long long handle;
    long long soundHandle;
    __strong AVAudioPlayerNode *player;
    __strong AVAudioUnitTimePitch *timePitch;
    __strong AVAudioMixerNode *effectMixer;
    BOOL isMusic;
    BOOL looping;
    BOOL paused;
    BOOL resumePending = NO;    // play requested while a system interruption prevents starting
    BOOL completed;
    BOOL stopped = NO;          // terminal cancellation, distinct from natural completion
    BOOL ownerReleased = NO;    // Python Channel was collected; playback may continue
    __strong NSString *error = nil;
    double duration = 0;
    double positionOffset = 0;  // source position at the start of this schedule
    double pausedPosition = 0;
    float volume;
    float pan;
    float pitch;  // playback rate
    float semitones = 0;
    /* fade state */
    float fadeStart;        // volume when fade began
    float fadeTarget;
    double fadeDuration;    // total fade time in seconds
    double fadeElapsed;     // time elapsed since fade began
    BOOL fadeActive;        // YES while fading
    __strong AVAudioPCMBuffer *soundBuffer;  // retained until playback finishes
    std::shared_ptr<AudioFileAccess> fileAccess;
    __strong AVAudioFile *streamFile = nil; // destroyed before its access token
    AVAudioFramePosition nextStreamFrame = 0;
    unsigned queuedSegments = 0;
    int generation;     // incremented on chain rebuild; stale completion handlers check this
    /* per-channel effects (nil = not active) */
    __strong AVAudioUnitReverb *chReverb;
    __strong AVAudioUnitDelay *chDelay;
    __strong AVAudioUnitDistortion *chDistortion;
    __strong AVAudioUnitEQ *chEQ;
};

/* ─────────────────────────────────────────────────────────────────────── */
/* Global state                                                            */
/* ─────────────────────────────────────────────────────────────────────── */

static std::unordered_map<long long, SoundRecord> gSounds;
static std::unordered_map<long long, ChannelRecord> gChannels;
static __strong AVAudioEngine *gEngine = nil;
static __strong AVAudioMixerNode *gMixer = nil;
static dispatch_queue_t gLoadQueue = nullptr;  // serial queue for async decode
static __strong AVAudioUnitReverb *gReverb = nil;
static __strong AVAudioUnitEQ *gEQ = nil;
static __strong AVAudioUnitDelay *gDelay = nil;
static __strong AVAudioUnitDistortion *gDistortion = nil;
/* Global effects are always connected; bypass=YES means inactive */
static float gMasterVolume = 1.0f;
static BOOL gEngineReady = NO;

/* Notification observers */
static __strong id gInterruptionObserver = nil;
static __strong id gRouteChangeObserver = nil;
static __strong id gConfigChangeObserver = nil;
static BOOL gInterrupted = NO;

/* Fade timer */
static dispatch_source_t gFadeTimer = nullptr;
static BOOL gFadeTimerRunning = NO;

/* Recorder state */
struct RecorderRecord {
    long long handle;
    std::atomic<bool> recording{false};
    double sampleRate;
    int channels;
    std::vector<float> samples;
    std::mutex recMutex;  // separate from gMutex to avoid priority inversion with audio thread
    std::unique_ptr<AudioRing> ring;
    std::unique_ptr<AudioFileWriter> writer;
    std::atomic<unsigned> callbacks{0};
    std::atomic<OSStatus> ioError{noErr};
    std::atomic<uint64_t> frames{0}, droppedFrames{0};
};
static std::unordered_map<long long, std::shared_ptr<RecorderRecord>> gRecorders;
static long long gRecorderStartHandle = 0;

struct OutputState {
    std::shared_ptr<AudioRing> ring;
    std::atomic<bool> paused{false};
    std::atomic<uint64_t> underruns{0};
};
struct OutputRecord {
    std::shared_ptr<OutputState> state;
    __strong AVAudioSourceNode *node = nil;
    __strong AVAudioMixerNode *mixer = nil;
    double sampleRate;
};
static std::unordered_map<long long, std::shared_ptr<OutputRecord>> gOutputs;
static void closeOutput(const std::shared_ptr<OutputRecord> &rec);

struct RecorderStartGuard {
    long long handle;
    ~RecorderStartGuard() {
        std::lock_guard<std::mutex> lock(gMutex);
        if (gRecorderStartHandle == handle) gRecorderStartHandle = 0;
    }
};

/* ─────────────────────────────────────────────────────────────────────── */
/* Engine lifecycle                                                        */
/* ─────────────────────────────────────────────────────────────────────── */

static void setupNotifications(void);

static BOOL rescheduleFromPosition(ChannelRecord *ch, double posSec);

static void ensureLoadQueue() {
    if (!gLoadQueue) gLoadQueue = dispatch_queue_create("org.cocoa-py.audio.load", DISPATCH_QUEUE_SERIAL);
}

static BOOL ensureEngine(void) {
    if (gEngineReady) return YES;

    @autoreleasepool {
        /* Configure audio session */
        NSError *err = nil;
#if COCOA_PY_AUDIO_SESSION
        AVAudioSession *session = [AVAudioSession sharedInstance];
        [session setCategory:AVAudioSessionCategoryPlayback
                 withOptions:AVAudioSessionCategoryOptionMixWithOthers
                       error:&err];
        [session setActive:YES error:&err];
#endif

        gEngine = [[AVAudioEngine alloc] init];
        gMixer = [gEngine mainMixerNode];

        /* Create global effects — always wired, controlled via bypass.
           Chain: Mixer → EQ → Delay → Distortion → Reverb → Output.
           Only 4 nodes; bypass approach avoids engine stop/restart. */
        gEQ = [[AVAudioUnitEQ alloc] initWithNumberOfBands:3];
        AVAudioUnitEQFilterParameters *low = gEQ.bands[0];
        low.filterType = AVAudioUnitEQFilterTypeParametric;
        low.frequency = 100; low.bandwidth = 1.0; low.gain = 0; low.bypass = NO;
        AVAudioUnitEQFilterParameters *mid = gEQ.bands[1];
        mid.filterType = AVAudioUnitEQFilterTypeParametric;
        mid.frequency = 1000; mid.bandwidth = 1.0; mid.gain = 0; mid.bypass = NO;
        AVAudioUnitEQFilterParameters *hi = gEQ.bands[2];
        hi.filterType = AVAudioUnitEQFilterTypeParametric;
        hi.frequency = 8000; hi.bandwidth = 1.0; hi.gain = 0; hi.bypass = NO;
        gEQ.bypass = YES;

        gDelay = [[AVAudioUnitDelay alloc] init];
        gDelay.bypass = YES;

        gDistortion = [[AVAudioUnitDistortion alloc] init];
        gDistortion.bypass = YES;

        gReverb = [[AVAudioUnitReverb alloc] init];
        gReverb.bypass = YES;

        /* Start engine first so outputNode has a valid hardware format */
        if (![gEngine startAndReturnError:&err]) {
            NSLog(@"[audio] engine start failed: %@", err);
            return NO;
        }

        /* Wire global effects: Mixer → EQ → Delay → Distortion → Reverb → Output */
        [gEngine stop];
        [gEngine attachNode:gEQ];
        [gEngine attachNode:gDelay];
        [gEngine attachNode:gDistortion];
        [gEngine attachNode:gReverb];
        AVAudioFormat *outFmt = [gMixer outputFormatForBus:0];
        [gEngine connect:gMixer      to:gEQ         format:outFmt];
        [gEngine connect:gEQ         to:gDelay      format:outFmt];
        [gEngine connect:gDelay      to:gDistortion format:outFmt];
        [gEngine connect:gDistortion to:gReverb     format:outFmt];
        [gEngine connect:gReverb     to:[gEngine outputNode] format:outFmt];
        if (![gEngine startAndReturnError:&err]) {
            NSLog(@"[audio] engine restart after effects wiring failed: %@", err);
            return NO;
        }
        gEngineReady = YES;
        setupNotifications();

        ensureLoadQueue();
    }
    return YES;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Helpers                                                                 */
/* ─────────────────────────────────────────────────────────────────────── */

static SoundRecord *soundRecord(long long h) {
    auto it = gSounds.find(h);
    return it == gSounds.end() ? nullptr : &it->second;
}

static ChannelRecord *channelRecord(long long h) {
    auto it = gChannels.find(h);
    return it == gChannels.end() ? nullptr : &it->second;
}

static NSString *resolveAudioPath(const char *path) {
    NSString *p = [NSString stringWithUTF8String:path];
    if ([p hasPrefix:@"/"]) return p;
    /* Relative path: resolve against current working directory */
    NSString *cwd = [[NSFileManager defaultManager] currentDirectoryPath];
    return [cwd stringByAppendingPathComponent:p];
}

static void disconnectChannel(ChannelRecord *ch);
static void completeChannel(long long handle, BOOL stopped = NO);

static BOOL raiseAudioError(NSString *error) {
    if (!error) return NO;
    // Only call at Python entry points, with the GIL held.
    PyErr_SetString(PyExc_RuntimeError, error.UTF8String ?: "Audio operation failed.");
    return YES;
}

static BOOL validPCMFormat(double rate, int channels, bool boundedRate = true) {
    if (!std::isfinite(rate) || rate <= 0 || (boundedRate && (rate < 8000 || rate > 192000)) || channels < 1 || channels > 2) {
        PyErr_SetString(PyExc_ValueError, boundedRate
            ? "sample_rate must be 8000...192000 and channels must be 1 or 2."
            : "sample_rate must be finite and positive, and channels must be 1 or 2.");
        return NO;
    }
    return YES;
}

static AVAudioFormat *channelFormat(ChannelRecord *ch) {
    return ch->streamFile ? ch->streamFile.processingFormat : ch->soundBuffer.format;
}

static BOOL channelReady(ChannelRecord *ch) { return ch->soundBuffer || ch->streamFile; }

static double channelPosition(ChannelRecord *ch) {
    if (!ch || ch->completed) return 0;
    if (ch->paused || ch->resumePending) return ch->pausedPosition;
    double pos = ch->positionOffset;
    AVAudioTime *nodeTime = (channelReady(ch) && ch->player.engine) ? ch->player.lastRenderTime : nil;
    if (nodeTime && nodeTime.isSampleTimeValid) {
        AVAudioTime *time = [ch->player playerTimeForNodeTime:nodeTime];
        double sr = channelFormat(ch).sampleRate;
        if (time && time.isSampleTimeValid && sr > 0)
            pos += (double)time.sampleTime / sr;
    }
    if (ch->looping && ch->duration > 0) pos = fmod(pos, ch->duration);
    return std::clamp(pos, 0.0, ch->duration);
}

static void pauseChannel(ChannelRecord *ch) {
    if (!ch || ch->completed) return;
    if (ch->paused) { ch->resumePending = NO; return; }
    ch->pausedPosition = channelPosition(ch);
    if (channelReady(ch)) [ch->player pause];
    ch->paused = YES;
    ch->resumePending = NO;
}

static NSString *restartAudioEngine() {
    if (!gEngine) return @"Audio engine is closed.";
    if (gEngine.isRunning) return nil;
    @try {
        NSError *error = nil;
#if COCOA_PY_AUDIO_SESSION
        if (![[AVAudioSession sharedInstance] setActive:YES error:&error])
            return error.localizedDescription ?: @"Cannot activate the audio session.";
#endif
        if (![gEngine startAndReturnError:&error])
            return error.localizedDescription ?: @"Cannot resume the audio engine.";
    } @catch (NSException *exception) {
        return exception.reason ?: @"Cannot resume the audio engine.";
    }
    return nil;
}

static NSString *startChannelPlayback(ChannelRecord *ch) {
    if (gInterrupted) {
        ch->resumePending = YES;
        return nil;
    }
    @try {
        NSString *error = restartAudioEngine();
        if (error) return error;
        if (channelReady(ch)) [ch->player play];
        ch->resumePending = NO;
    } @catch (NSException *exception) {
        return exception.reason ?: @"Cannot resume audio playback.";
    }
    return nil;
}

static NSString *resumeChannel(ChannelRecord *ch) {
    if (!ch || ch->completed) return nil;
    if (!ch->paused && !ch->resumePending && (gInterrupted || gEngine.isRunning)) return nil;
    NSString *error = startChannelPlayback(ch);
    if (error) {
        ch->error = error;
        completeChannel(ch->handle);
        return error;
    }
    ch->paused = NO;
    return nil;
}

// Called under gMutex after system recovery; a failed channel may erase itself.
static void resumePendingChannels() {
    for (auto it = gChannels.begin(); it != gChannels.end();) {
        auto current = it++;
        if (current->second.resumePending && !current->second.paused)
            resumeChannel(&current->second);
    }
}

#include "AudioEvents.h"

/* Reschedule a channel's buffer from the given position (seconds).
   Assumes player is stopped and ch->generation is already incremented.
   Sets up generation-checked completion handler for non-looping channels. */
static dispatch_queue_t streamControlQueue() {
    static dispatch_queue_t queue = dispatch_queue_create("org.cocoa-py.audio.stream", DISPATCH_QUEUE_SERIAL);
    return queue;
}

// At most three 32768-frame file segments are queued. AVAudioPlayerNode performs
// incremental decoding; callbacks only enqueue control work, never touch Python.
static void fillStreamSegments(ChannelRecord *ch) {
    while (ch->queuedSegments < 3) {
        if (ch->nextStreamFrame >= ch->streamFile.length) {
            if (!ch->looping) break;
            ch->nextStreamFrame = 0;
        }
        auto count = (AVAudioFrameCount)std::min<AVAudioFramePosition>(32768, ch->streamFile.length - ch->nextStreamFrame);
        BOOL last = !ch->looping && ch->nextStreamFrame + count == ch->streamFile.length;
        long long handle = ch->handle;
        int generation = ch->generation;
        [ch->player scheduleSegment:ch->streamFile startingFrame:ch->nextStreamFrame frameCount:count atTime:nil
            completionCallbackType:last ? AVAudioPlayerNodeCompletionDataPlayedBack : AVAudioPlayerNodeCompletionDataConsumed
            completionHandler:^(AVAudioPlayerNodeCompletionCallbackType type) {
                dispatch_async(streamControlQueue(), ^{
                    std::lock_guard<std::mutex> lock(gMutex);
                    ChannelRecord *current = channelRecord(handle);
                    if (!current || current->generation != generation || current->completed) return;
                    if (last) { completeChannel(handle); return; }
                    --current->queuedSegments;
                    @try { fillStreamSegments(current); }
                    @catch (NSException *exception) {
                        current->error = exception.reason ?: @"File streaming failed.";
                        completeChannel(handle);
                    }
                });
            }];
        ch->nextStreamFrame += count;
        ++ch->queuedSegments;
    }
}

static BOOL rescheduleFromPosition(ChannelRecord *ch, double posSec) {
    if (ch->streamFile) {
        double sr = ch->streamFile.processingFormat.sampleRate;
        ch->nextStreamFrame = (AVAudioFramePosition)(std::clamp(posSec, 0.0, (ch->streamFile.length - 1) / sr) * sr);
        ch->positionOffset = ch->pausedPosition = ch->nextStreamFrame / sr;
        ch->queuedSegments = 0;
        fillStreamSegments(ch);
        return YES;
    }
    int capturedGen = ch->generation;
    AVAudioFormat *fmt = channelFormat(ch);
    double sr = fmt.sampleRate;
    AVAudioFrameCount totalFrames = ch->soundBuffer.frameLength;
    if (!ch->player || !totalFrames || sr <= 0 || !std::isfinite(posSec)) {
        ch->error = @"Cannot schedule empty or invalid audio.";
        return NO;
    }
    double lastPosition = (double)(totalFrames - 1) / sr;
    AVAudioFrameCount startFrame = (AVAudioFrameCount)(std::clamp(posSec, 0.0, lastPosition) * sr);
    ch->positionOffset = (double)startFrame / sr;
    ch->pausedPosition = ch->positionOffset;
    AVAudioPCMBuffer *buffer = ch->soundBuffer;
    if (startFrame > 0) {
        AVAudioFrameCount remain = totalFrames - startFrame;
        buffer = [[AVAudioPCMBuffer alloc] initWithPCMFormat:fmt frameCapacity:remain];
        if (!buffer) {
            ch->error = @"Cannot allocate audio playback buffer.";
            return NO;
        }
        buffer.frameLength = remain;
        for (AVAudioChannelCount c = 0; c < fmt.channelCount; c++) {
            memcpy(buffer.floatChannelData[c], ch->soundBuffer.floatChannelData[c] + startFrame,
                   remain * sizeof(float));
        }
    }
    if (ch->looping) {
        // Queue the full loop before the tail ends; no main-queue scheduling gap.
        if (startFrame > 0)
            [ch->player scheduleBuffer:buffer atTime:nil options:0 completionHandler:nil];
        [ch->player scheduleBuffer:ch->soundBuffer atTime:nil
                           options:AVAudioPlayerNodeBufferLoops completionHandler:nil];
    } else {
        long long capturedH = ch->handle;
        [ch->player scheduleBuffer:buffer atTime:nil options:0
             completionCallbackType:AVAudioPlayerNodeCompletionDataPlayedBack
                 completionHandler:^(AVAudioPlayerNodeCompletionCallbackType type) {
            dispatch_async(dispatch_get_main_queue(), ^{
                std::lock_guard<std::mutex> lock(gMutex);
                auto it = gChannels.find(capturedH);
                if (it != gChannels.end() && it->second.generation == capturedGen)
                    completeChannel(capturedH);
            });
        }];
    }
    return YES;
}

/* Connect a channel's node chain dynamically.
   Only Player + TimePitch are always present; effect nodes are wired
   only when non-nil (created lazily on first use).
   Chain: Player → TimePitch → [active effects...] → Mixer */
static void connectChannel(ChannelRecord *ch) {
    AVAudioFormat *fmt = channelFormat(ch);
    [gEngine attachNode:ch->player];
    [gEngine attachNode:ch->timePitch];
    [gEngine connect:ch->player to:ch->timePitch format:fmt];

    AVAudioNode *prev = ch->timePitch;
    AVAudioNode *effects[] = {ch->chReverb, ch->chDelay, ch->chDistortion, ch->chEQ};
    AVAudioFormat *effectFormat = fmt;
    if (fmt.channelCount != 2 && (ch->chReverb || ch->chDelay || ch->chDistortion || ch->chEQ)) {
        // Effect units use a stereo graph; a mixer converts mono/multichannel sources.
        if (!ch->effectMixer) ch->effectMixer = [[AVAudioMixerNode alloc] init];
        [gEngine attachNode:ch->effectMixer];
        [gEngine connect:prev to:ch->effectMixer format:fmt];
        prev = ch->effectMixer;
        effectFormat = [[AVAudioFormat alloc] initStandardFormatWithSampleRate:fmt.sampleRate channels:2];
    }
    for (AVAudioNode *fx : effects) {
        if (fx) {
            [gEngine attachNode:fx];
            [gEngine connect:prev to:fx format:effectFormat];
            prev = fx;
        }
    }
    [gEngine connect:prev to:gMixer format:effectFormat];
}

/* Disconnect and detach all of a channel's nodes */
static void disconnectChannel(ChannelRecord *ch) {
    ch->generation++;
    @try {
        if (ch->player) [ch->player stop];
        /* Detach removes all connections automatically */
        AVAudioNode *nodes[] = {ch->player, ch->timePitch, ch->effectMixer,
                                ch->chReverb, ch->chDelay,
                                ch->chDistortion, ch->chEQ};
        for (AVAudioNode *n : nodes) {
            if (n && n.engine) [gEngine detachNode:n];
        }
    } @catch (NSException *e) { }
    ch->chReverb = nil;
    ch->chDelay = nil;
    ch->chDistortion = nil;
    ch->chEQ = nil;
    ch->effectMixer = nil;
}

// Keep only query/error state while a Python Channel is still alive.
// A collected Channel relinquishes ownership without stopping active playback.
static void completeChannel(long long handle, BOOL stopped) {
    auto it = gChannels.find(handle);
    if (it == gChannels.end()) return;
    ChannelRecord *ch = &it->second;
    if (ch->completed) return;
    ch->stopped = stopped;
    pushAudioEvent(AudioEventKind::PlaybackEnded, handle,
                   ch->error ? "failed" : stopped ? "stopped" : "finished", ch->error);
    disconnectChannel(ch);
    ch->player = nil;
    ch->timePitch = nil;
    ch->soundBuffer = nil;
    ch->streamFile = nil;
    ch->fileAccess.reset();
    ch->queuedSegments = 0;
    ch->completed = YES;
    ch->paused = NO;
    ch->resumePending = NO;
    ch->fadeActive = NO;
    if (ch->ownerReleased) gChannels.erase(it);
}

/* Rebuild a channel's audio chain after adding an effect node.
   Preserves playback position and play/pause state.
   Increments generation to invalidate stale completion handlers. */
static BOOL rebuildChannelChain(ChannelRecord *ch) {
    if (!ch->player || !channelReady(ch)) return YES;

    @autoreleasepool {
        @try {
            double posSec = channelPosition(ch);

            /* Stop/detach; stale completion handlers are dispatched to the main queue. */
            [ch->player stop];
            AVAudioNode *nodes[] = {ch->player, ch->timePitch, ch->effectMixer,
                                    ch->chReverb, ch->chDelay, ch->chDistortion, ch->chEQ};
            for (AVAudioNode *n : nodes) {
                if (n && n.engine) [gEngine detachNode:n];
            }
            ch->generation++;

            connectChannel(ch);
            ch->player.volume = ch->volume * gMasterVolume;
            ch->player.pan = ch->pan;
            if (!rescheduleFromPosition(ch, posSec)) {
                raiseAudioError(ch->error);
                completeChannel(ch->handle);
                return NO;
            }
            if (!ch->paused && (ch->error = startChannelPlayback(ch))) {
                raiseAudioError(ch->error);
                completeChannel(ch->handle);
                return NO;
            }
        } @catch (NSException *exception) {
            ch->error = exception.reason ?: @"Cannot rebuild the audio effect chain.";
            raiseAudioError(ch->error);
            completeChannel(ch->handle);
            return NO;
        }
    }
    return YES;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Python functions                                                        */
/* ─────────────────────────────────────────────────────────────────────── */

/* Start playback for all pending play requests queued while sound was decoding.
   Must be called with gMutex held. */
static void fulfillPendingPlays(SoundRecord *snd) {
    if (snd->pendingPlays.empty()) return;
    auto plays = std::exchange(snd->pendingPlays, {});
    for (long long handle : plays) {
        auto it = gChannels.find(handle);
        if (it == gChannels.end()) continue;
        ChannelRecord *crec = &it->second;
        if (crec->completed) continue;
        if (snd->error) {
            crec->error = snd->error;
            completeChannel(handle);
            continue;
        }
        crec->soundBuffer = snd->buffer;
        crec->duration = snd->duration;
        @try {
            connectChannel(crec);
            crec->player.volume = crec->volume * gMasterVolume;
            crec->player.pan = crec->pan;
            crec->timePitch.rate = crec->pitch;
            crec->timePitch.pitch = crec->semitones * 100;
            if (!rescheduleFromPosition(crec, crec->positionOffset)) {
                completeChannel(handle);
                continue;
            }
            if (!crec->paused && (crec->error = startChannelPlayback(crec))) completeChannel(handle);
        } @catch (NSException *exception) {
            crec->error = exception.reason ?: @"Cannot start audio playback.";
            completeChannel(handle);
        }
    }
}

static void finishSoundLoading(long long handle, AVAudioPCMBuffer *buffer, NSString *error) {
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gSounds.find(handle);
    if (it == gSounds.end()) return; // unloaded or closed while decoding
    SoundRecord *snd = &it->second;
    if (!error && (!buffer || !buffer.frameLength)) error = @"Audio decoding produced no samples.";
    snd->error = error;
    snd->loaded = (error == nil);
    if (snd->loaded) {
        snd->buffer = buffer;
        snd->frames = buffer.frameLength;
        snd->duration = (double)buffer.frameLength / buffer.format.sampleRate;
    }
    fulfillPendingPlays(snd);
    if (snd->unloadRequested) gSounds.erase(it);
}

// ── load(path) → handle ──

static PyObject *audio_load(PyObject *self, PyObject *args) {
    (void)self;
    const char *path = nullptr;
    if (!PyArg_ParseTuple(args, "s", &path)) return nullptr;

    ensureLoadQueue();

    @autoreleasepool {
        NSString *resolved = resolveAudioPath(path);
        NSURL *url = [NSURL fileURLWithPath:resolved];
        auto access = std::make_shared<AudioFileAccess>(url);
        NSError *err = nil;
        AVAudioFile *file = [[AVAudioFile alloc] initForReading:url error:&err];
        if (!file) {
            PyErr_Format(PyExc_FileNotFoundError, "Cannot load audio: %s (%s)",
                         path, err.localizedDescription.UTF8String);
            return nullptr;
        }

        /* Extract metadata synchronously (cheap) */
        AVAudioFormat *fmt = file.processingFormat;
        if (file.length <= 0 || file.length > std::numeric_limits<AVAudioFrameCount>::max()) {
            PyErr_SetString(PyExc_ValueError, "Audio frame count is empty or too large.");
            return nullptr;
        }
        AVAudioFrameCount frames = (AVAudioFrameCount)file.length;
        double sr = fmt.sampleRate;
        int ch = (int)fmt.channelCount;
        double dur = (sr > 0) ? (double)frames / sr : 0.0;

        long long h = nextHandle();
        {
            std::lock_guard<std::mutex> lock(gMutex);
            SoundRecord rec;
            rec.handle = h;
            rec.buffer = nil;
            rec.format = fmt;
            rec.duration = dur;
            rec.channels = ch;
            rec.sampleRate = sr;
            rec.frames = frames;
            rec.loaded = NO;
            gSounds.emplace(h, std::move(rec));
        }

        /* Decode PCM in background */
        dispatch_async(gLoadQueue, ^{
            (void)access; // retain external access through the actual asynchronous read
            @autoreleasepool {
                @try {
                    AVAudioPCMBuffer *buf = [[AVAudioPCMBuffer alloc]
                        initWithPCMFormat:fmt frameCapacity:frames];
                    NSError *readErr = nil;
                    BOOL ok = buf && [file readIntoBuffer:buf error:&readErr];
                    finishSoundLoading(h, buf, ok ? nil : (readErr.localizedDescription ?: @"Audio decoding failed."));
                } @catch (NSException *exception) {
                    finishSoundLoading(h, nil, exception.reason ?: @"Audio decoding failed.");
                }
            }
        });

        return PyLong_FromLongLong(h);
    }
}

static PyObject *audio_load_stream(PyObject *, PyObject *args) {
    const char *path;
    if (!PyArg_ParseTuple(args, "s", &path)) return nullptr;
    @autoreleasepool {
        NSURL *url = [NSURL fileURLWithPath:resolveAudioPath(path)];
        auto access = std::make_shared<AudioFileAccess>(url);
        NSError *error = nil;
        AVAudioFile *file = [[AVAudioFile alloc] initForReading:url commonFormat:AVAudioPCMFormatFloat32
                                                   interleaved:NO error:&error];
        if (!file) {
            PyErr_Format(PyExc_OSError, "Cannot open audio stream: %s", error.localizedDescription.UTF8String);
            return nullptr;
        }
        if (file.length <= 0 || !validPCMFormat(file.processingFormat.sampleRate, (int)file.processingFormat.channelCount)) {
            if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "Audio file is empty.");
            return nullptr;
        }
        SoundRecord rec{};
        rec.handle = nextHandle(); rec.format = file.processingFormat;
        rec.frames = file.length; rec.sampleRate = rec.format.sampleRate;
        rec.channels = (int)rec.format.channelCount; rec.duration = rec.frames / rec.sampleRate;
        rec.loaded = YES; rec.streamURL = url; rec.fileAccess = access;
        PyObject *result = PyLong_FromLongLong(rec.handle);
        if (!result) return nullptr;
        std::lock_guard<std::mutex> lock(gMutex);
        gSounds.emplace(rec.handle, std::move(rec));
        return result;
    }
}

// ── unload(handle) ──

static PyObject *audio_unload(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gSounds.find(h);
    if (it != gSounds.end()) {
        if (!it->second.loaded && !it->second.error && !it->second.pendingPlays.empty())
            it->second.unloadRequested = YES;
        else
            gSounds.erase(it);
    }
    Py_RETURN_NONE;
}

// ── load_bytes(data) → handle — load from in-memory bytes (WAV/MP3/AAC/etc.) ──

struct MemAudioState {
    __strong NSData *data;
    AudioFileID audioFile = nullptr;
    ExtAudioFileRef extFile = nullptr;

    ~MemAudioState() {
        if (extFile) ExtAudioFileDispose(extFile);
        if (audioFile) AudioFileClose(audioFile);
    }
};

static OSStatus mem_read_proc(void *clientData, SInt64 pos, UInt32 reqCount,
                              void *buf, UInt32 *actualCount) {
    MemAudioState *s = (MemAudioState *)clientData;
    *actualCount = 0;
    if (pos < 0) return kAudioFilePositionError;
    if ((uint64_t)pos >= s->data.length) return noErr;
    NSUInteger avail = s->data.length - (NSUInteger)pos;
    UInt32 n = (UInt32)std::min<NSUInteger>(reqCount, avail);
    memcpy(buf, (const uint8_t *)s->data.bytes + pos, n);
    *actualCount = n;
    return noErr;
}

static SInt64 mem_getsize_proc(void *clientData) {
    return (SInt64)((MemAudioState *)clientData)->data.length;
}

static PyObject *audio_load_bytes(PyObject *self, PyObject *args) {
    (void)self;
    Py_buffer buf;
    if (!PyArg_ParseTuple(args, "y*", &buf)) return nullptr;

    ensureLoadQueue();

    /* Copy bytes so the Python buffer can be released immediately */
    auto source = std::make_shared<MemAudioState>();
    source->data = [NSData dataWithBytes:buf.buf length:buf.len];
    PyBuffer_Release(&buf);

    @autoreleasepool {
        /* Parse header synchronously (cheap) to get metadata */
        OSStatus st = AudioFileOpenWithCallbacks(
            source.get(), mem_read_proc, NULL, mem_getsize_proc, NULL,
            0 /* auto-detect type */, &source->audioFile);
        if (st != noErr) {
            PyErr_Format(PyExc_RuntimeError,
                         "Cannot parse audio data (OSStatus %d)", (int)st);
            return nullptr;
        }

        st = ExtAudioFileWrapAudioFileID(source->audioFile, false, &source->extFile);
        if (st != noErr) {
            PyErr_Format(PyExc_RuntimeError,
                         "ExtAudioFile wrap failed (OSStatus %d)", (int)st);
            return nullptr;
        }

        AudioStreamBasicDescription srcFmt = {0};
        UInt32 propSz = sizeof(srcFmt);
        st = ExtAudioFileGetProperty(source->extFile, kExtAudioFileProperty_FileDataFormat,
                                     &propSz, &srcFmt);
        if (st != noErr || !std::isfinite(srcFmt.mSampleRate) || srcFmt.mSampleRate <= 0
            || srcFmt.mChannelsPerFrame == 0) {
            PyErr_SetString(PyExc_ValueError, "Invalid audio format.");
            return nullptr;
        }

        AVAudioFormat *avFmt = [[AVAudioFormat alloc]
            initStandardFormatWithSampleRate:srcFmt.mSampleRate
                                   channels:srcFmt.mChannelsPerFrame];
        if (!avFmt) {
            PyErr_SetString(PyExc_ValueError, "Unsupported audio format.");
            return nullptr;
        }
        const AudioStreamBasicDescription *outFmt = avFmt.streamDescription;
        st = ExtAudioFileSetProperty(source->extFile, kExtAudioFileProperty_ClientDataFormat,
                                     sizeof(AudioStreamBasicDescription), outFmt);
        if (st != noErr) {
            PyErr_Format(PyExc_RuntimeError,
                         "Set client format failed (OSStatus %d)", (int)st);
            return nullptr;
        }

        SInt64 totalFrames = 0;
        propSz = sizeof(totalFrames);
        st = ExtAudioFileGetProperty(source->extFile, kExtAudioFileProperty_FileLengthFrames,
                                     &propSz, &totalFrames);
        if (st != noErr || totalFrames <= 0
            || totalFrames > std::numeric_limits<UInt32>::max() / sizeof(float)) {
            PyErr_SetString(PyExc_ValueError, "Audio frame count is empty or too large.");
            return nullptr;
        }

        /* Register sound with metadata; decode in background */
        double sr = srcFmt.mSampleRate;
        int nch = (int)srcFmt.mChannelsPerFrame;
        double dur = (sr > 0) ? (double)totalFrames / sr : 0.0;
        long long h = nextHandle();

        {
            std::lock_guard<std::mutex> lock(gMutex);
            SoundRecord rec;
            rec.handle = h;
            rec.buffer = nil;
            rec.format = avFmt;
            rec.duration = dur;
            rec.channels = nch;
            rec.sampleRate = sr;
            rec.frames = (AVAudioFrameCount)totalFrames;
            rec.loaded = NO;
            gSounds.emplace(h, std::move(rec));
        }

        /* Capture the stable callback context and its input/handles until decoding ends. */
        AVAudioFrameCount frameCap = (AVAudioFrameCount)totalFrames;
        dispatch_async(gLoadQueue, ^{
            @autoreleasepool {
                @try {
                    AVAudioPCMBuffer *pcmBuf = [[AVAudioPCMBuffer alloc]
                        initWithPCMFormat:avFmt frameCapacity:frameCap];
                    if (!pcmBuf) {
                        finishSoundLoading(h, nil, @"Cannot allocate audio decode buffer.");
                        return;
                    }
                    // ExtAudioFileRead needs writable byte sizes as well as capacity.
                    pcmBuf.frameLength = frameCap;
                    UInt32 framesToRead = frameCap;
                    OSStatus readSt = ExtAudioFileRead(source->extFile, &framesToRead,
                                                      pcmBuf.mutableAudioBufferList);
                    pcmBuf.frameLength = framesToRead;
                    NSString *error = readSt == noErr ? nil :
                        [NSString stringWithFormat:@"Audio decoding failed (OSStatus %d).", (int)readSt];
                    finishSoundLoading(h, pcmBuf, error);
                } @catch (NSException *exception) {
                    finishSoundLoading(h, nil, exception.reason ?: @"Audio decoding failed.");
                }
            }
        });

        return PyLong_FromLongLong(h);
    }
}

// ── load_pcm(data, channels=1, sample_rate=44100) → handle — raw float32 PCM ──

static PyObject *audio_load_pcm(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    Py_buffer buf;
    int channels = 1;
    double sampleRate = 44100.0;

    static char s_data[] = "data";
    static char s_ch[] = "channels";
    static char s_sr[] = "sample_rate";
    static char *kwlist[] = {s_data, s_ch, s_sr, nullptr};

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "y*|id", kwlist,
                                     &buf, &channels, &sampleRate))
        return nullptr;

    if (!validPCMFormat(sampleRate, channels, false)) {
        PyBuffer_Release(&buf);
        return nullptr;
    }

    if (buf.len % (channels * sizeof(float)) || buf.len / (channels * sizeof(float)) > UINT32_MAX / sizeof(float)) {
        PyBuffer_Release(&buf);
        PyErr_SetString(PyExc_ValueError, "PCM must contain complete float32 frames and fit an audio buffer.");
        return nullptr;
    }

    @autoreleasepool {
        AVAudioFrameCount frames = (AVAudioFrameCount)(buf.len / (sizeof(float) * channels));
        if (frames == 0) {
            PyBuffer_Release(&buf);
            PyErr_SetString(PyExc_ValueError, "Empty PCM data.");
            return nullptr;
        }

        AVAudioFormat *fmt = [[AVAudioFormat alloc]
            initStandardFormatWithSampleRate:sampleRate
                                   channels:(AVAudioChannelCount)channels];
        AVAudioPCMBuffer *pcmBuf = [[AVAudioPCMBuffer alloc]
            initWithPCMFormat:fmt frameCapacity:frames];
        if (!pcmBuf) { PyBuffer_Release(&buf); PyErr_NoMemory(); return nullptr; }
        pcmBuf.frameLength = frames;

        const float *src = (const float *)buf.buf;
        if (channels == 1) {
            memcpy(pcmBuf.floatChannelData[0], src, frames * sizeof(float));
        } else {
            /* Deinterleave [L,R,L,R,...] → separate channels */
            float *left = pcmBuf.floatChannelData[0];
            float *right = pcmBuf.floatChannelData[1];
            for (AVAudioFrameCount i = 0; i < frames; i++) {
                left[i]  = src[i * 2];
                right[i] = src[i * 2 + 1];
            }
        }

        PyBuffer_Release(&buf);

        long long h = nextHandle();
        double dur = (sampleRate > 0) ? (double)frames / sampleRate : 0.0;

        std::lock_guard<std::mutex> lock(gMutex);
        SoundRecord rec;
        rec.handle = h; rec.buffer = pcmBuf; rec.format = fmt;
        rec.duration = dur; rec.channels = channels;
        rec.sampleRate = sampleRate; rec.frames = frames; rec.loaded = YES;
        gSounds.emplace(h, std::move(rec));
        return PyLong_FromLongLong(h);
    }
}

// ── generate_tone(frequency, duration, volume=0.5, sample_rate=44100) → handle ──

static PyObject *audio_generate_tone(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    double freq = 440.0, duration = 0.2, volume = 0.5, sampleRate = 44100.0;

    static char s_f[] = "frequency";
    static char s_d[] = "duration";
    static char s_v[] = "volume";
    static char s_sr[] = "sample_rate";
    static char *kwlist[] = {s_f, s_d, s_v, s_sr, nullptr};

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "dd|dd", kwlist,
                                     &freq, &duration, &volume, &sampleRate))
        return nullptr;

    if (!validPCMFormat(sampleRate, 1, false)) return nullptr;
    if (!std::isfinite(freq) || !std::isfinite(duration)
        || duration < 0 || sampleRate * duration > UINT32_MAX / sizeof(float)
        || !std::isfinite(volume) || volume < 0 || volume > 1) {
        PyErr_SetString(PyExc_ValueError, "Invalid tone frequency, duration, or volume.");
        return nullptr;
    }

    @autoreleasepool {
        AVAudioFrameCount frames = (AVAudioFrameCount)(sampleRate * duration);
        if (frames < 1) frames = 1;

        AVAudioFormat *fmt = [[AVAudioFormat alloc]
            initStandardFormatWithSampleRate:sampleRate channels:1];
        AVAudioPCMBuffer *buf = [[AVAudioPCMBuffer alloc]
            initWithPCMFormat:fmt frameCapacity:frames];
        if (!buf) { PyErr_NoMemory(); return nullptr; }
        buf.frameLength = frames;

        float *samples = buf.floatChannelData[0];
        float vol = (float)volume;
        double phase_inc = 2.0 * M_PI * freq / sampleRate;
        int fade = (int)(sampleRate * 0.005);  /* 5ms fade */
        if (fade > (int)frames / 2) fade = (int)frames / 2;

        for (AVAudioFrameCount i = 0; i < frames; i++) {
            float env = 1.0f;
            if (fade > 0) {
                if ((int)i < fade)
                    env = (float)i / fade;
                else if ((int)i >= (int)frames - fade)
                    env = (float)((int)frames - 1 - (int)i) / fade;
            }
            samples[i] = vol * env * sinf((float)(phase_inc * i));
        }

        long long h = nextHandle();
        double dur = (sampleRate > 0) ? (double)frames / sampleRate : 0.0;

        std::lock_guard<std::mutex> lock(gMutex);
        { SoundRecord rec; rec.handle = h; rec.buffer = buf; rec.format = fmt;
          rec.duration = dur; rec.channels = 1; rec.sampleRate = sampleRate;
          rec.frames = frames; rec.loaded = YES;
          gSounds.emplace(h, std::move(rec)); }
        return PyLong_FromLongLong(h);
    }
}

// ── generate_noise(duration, volume=0.3, sample_rate=44100) → handle ──

static PyObject *audio_generate_noise(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    double duration = 0.2, volume = 0.3, sampleRate = 44100.0;

    static char s_d[] = "duration";
    static char s_v[] = "volume";
    static char s_sr[] = "sample_rate";
    static char *kwlist[] = {s_d, s_v, s_sr, nullptr};

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "d|dd", kwlist,
                                     &duration, &volume, &sampleRate))
        return nullptr;

    if (!validPCMFormat(sampleRate, 1, false)) return nullptr;
    if (!std::isfinite(duration) || duration < 0 || sampleRate * duration > UINT32_MAX / sizeof(float)
        || !std::isfinite(volume) || volume < 0 || volume > 1) {
        PyErr_SetString(PyExc_ValueError, "Invalid noise duration or volume.");
        return nullptr;
    }

    @autoreleasepool {
        AVAudioFrameCount frames = (AVAudioFrameCount)(sampleRate * duration);
        if (frames < 1) frames = 1;

        AVAudioFormat *fmt = [[AVAudioFormat alloc]
            initStandardFormatWithSampleRate:sampleRate channels:1];
        AVAudioPCMBuffer *buf = [[AVAudioPCMBuffer alloc]
            initWithPCMFormat:fmt frameCapacity:frames];
        if (!buf) { PyErr_NoMemory(); return nullptr; }
        buf.frameLength = frames;

        float *samples = buf.floatChannelData[0];
        float vol = (float)volume;
        int fade = (int)(sampleRate * 0.005);
        if (fade > (int)frames / 2) fade = (int)frames / 2;

        for (AVAudioFrameCount i = 0; i < frames; i++) {
            float env = 1.0f;
            if (fade > 0) {
                if ((int)i < fade)
                    env = (float)i / fade;
                else if ((int)i >= (int)frames - fade)
                    env = (float)((int)frames - 1 - (int)i) / fade;
            }
            /* arc4random_uniform gives uniform [0, 2^32) */
            float r = (float)arc4random() / (float)UINT32_MAX * 2.0f - 1.0f;
            samples[i] = vol * env * r;
        }

        long long h = nextHandle();
        double dur = (sampleRate > 0) ? (double)frames / sampleRate : 0.0;

        std::lock_guard<std::mutex> lock(gMutex);
        { SoundRecord rec; rec.handle = h; rec.buffer = buf; rec.format = fmt;
          rec.duration = dur; rec.channels = 1; rec.sampleRate = sampleRate;
          rec.frames = frames; rec.loaded = YES;
          gSounds.emplace(h, std::move(rec)); }
        return PyLong_FromLongLong(h);
    }
}

// ── play(sound_handle, *, loop=False, volume=1.0, pan=0.0, pitch=1.0, music=False) → channel_handle ──

static PyObject *audio_play(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long soundH = 0;
    int loop = 0, music = 0;
    float volume = 1.0f, pan = 0.0f, pitch = 1.0f, semitones = 0;

    static char s_sound[] = "sound";
    static char s_loop[] = "loop";
    static char s_volume[] = "volume";
    static char s_pan[] = "pan";
    static char s_pitch[] = "pitch";
    static char s_music[] = "music";
    static char s_semitones[] = "semitones";
    static char *kwlist[] = {s_sound, s_loop, s_volume, s_pan, s_pitch, s_music, s_semitones, nullptr};

    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "L|pfffpf", kwlist,
                                     &soundH, &loop, &volume, &pan, &pitch, &music, &semitones))
        return nullptr;
    if (!std::isfinite(volume) || !std::isfinite(pan) || !std::isfinite(pitch) || !std::isfinite(semitones)
        || volume < 0 || volume > 1 || pan < -1 || pan > 1 || pitch < 0.25 || pitch > 4
        || semitones < -24 || semitones > 24) {
        PyErr_SetString(PyExc_ValueError, "Invalid volume, pan, rate, or semitones."); return nullptr;
    }

    if (!ensureEngine()) {
        PyErr_SetString(PyExc_RuntimeError, "Audio engine unavailable.");
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(gMutex);
    SoundRecord *snd = soundRecord(soundH);
    if (!snd) {
        PyErr_SetString(PyExc_ValueError, "Invalid sound handle.");
        return nullptr;
    }
    if (raiseAudioError(snd->error)) return nullptr;

    @autoreleasepool {
        AVAudioPlayerNode *player = [[AVAudioPlayerNode alloc] init];
        AVAudioUnitTimePitch *tp = [[AVAudioUnitTimePitch alloc] init];
        tp.rate = pitch;
        tp.pitch = semitones * 100;

        long long ch = nextHandle();
        ChannelRecord rec;
        rec.handle = ch;
        rec.soundHandle = soundH;
        rec.player = player;
        rec.timePitch = tp;
        rec.isMusic = music ? YES : NO;
        rec.looping = loop ? YES : NO;
        rec.paused = NO;
        rec.completed = NO;
        rec.volume = volume;
        rec.pan = pan;
        rec.pitch = pitch;
        rec.semitones = semitones;
        rec.fadeStart = volume;
        rec.fadeTarget = volume;
        rec.fadeDuration = 0;
        rec.fadeElapsed = 0;
        rec.fadeActive = NO;
        rec.soundBuffer = snd->buffer;  /* nil if not yet loaded */
        rec.generation = 0;
        rec.duration = snd->duration;
        if (snd->streamURL) {
            NSError *error = nil;
            rec.fileAccess = snd->fileAccess;
            rec.streamFile = [[AVAudioFile alloc] initForReading:snd->streamURL commonFormat:AVAudioPCMFormatFloat32
                                                   interleaved:NO error:&error];
            if (!rec.streamFile) {
                raiseAudioError(error.localizedDescription ?: @"Cannot reopen audio stream."); return nullptr;
            }
            // Re-read metadata in case the on-disk file changed after opening Stream.
            if (rec.streamFile.length <= 0) {
                PyErr_SetString(PyExc_ValueError, "Audio stream is empty."); return nullptr;
            }
            rec.duration = rec.streamFile.length / rec.streamFile.processingFormat.sampleRate;
        }

        gChannels.emplace(ch, std::move(rec));
        auto resultHandle = [ch]() -> PyObject * {
            PyObject *result = PyLong_FromLongLong(ch);
            if (!result) {
                completeChannel(ch);
                gChannels.erase(ch);
            }
            return result;
        };

        if (!snd->loaded) {
            /* Sound still decoding — queue play request for later */
            snd->pendingPlays.push_back(ch);
            return resultHandle();
        }

        /* Sound is ready — start playback immediately */
        ChannelRecord *crec = &gChannels[ch];
        @try {
            connectChannel(crec);
            player.volume = volume * gMasterVolume;
            player.pan = pan;
            if (rescheduleFromPosition(crec, 0)) crec->error = startChannelPlayback(crec);
        } @catch (NSException *exception) {
            crec->error = exception.reason ?: @"Cannot start audio playback.";
        }
        if (raiseAudioError(crec->error)) {
            completeChannel(ch);
            gChannels.erase(ch);
            return nullptr;
        }
        return resultHandle();
    }
}

// ── stop(channel_handle) ──

static PyObject *audio_stop(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gChannels.find(h);
    if (it != gChannels.end()) {
        if (!it->second.completed) pushAudioEvent(AudioEventKind::PlaybackEnded, h, "stopped");
        @autoreleasepool {
            disconnectChannel(&it->second);
        }
        gChannels.erase(it);
    }
    Py_RETURN_NONE;
}

static PyObject *audio_release_channel(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gChannels.find(h);
    if (it != gChannels.end()) {
        it->second.ownerReleased = YES;
        if (it->second.completed) gChannels.erase(it);
    }
    Py_RETURN_NONE;
}

// ── pause(channel_handle) ──

static PyObject *audio_pause(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (ch && raiseAudioError(ch->error)) return nullptr;
    pauseChannel(ch);
    Py_RETURN_NONE;
}

// ── resume(channel_handle) ──

static PyObject *audio_resume(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (ch && raiseAudioError(ch->error)) return nullptr;
    if (raiseAudioError(resumeChannel(ch))) return nullptr;
    Py_RETURN_NONE;
}

// ── set_volume(channel, volume) ──

static PyObject *audio_set_volume(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    float vol = 1.0f;
    if (!PyArg_ParseTuple(args, "Lf", &h, &vol)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (ch && !ch->completed) {
        ch->volume = vol;
        if (ch->player) ch->player.volume = vol * gMasterVolume;
    }
    Py_RETURN_NONE;
}

// ── set_pan(channel, pan) — -1.0 left, 0.0 center, 1.0 right ──

static PyObject *audio_set_pan(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    float pan = 0.0f;
    if (!PyArg_ParseTuple(args, "Lf", &h, &pan)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (ch && !ch->completed) {
        ch->pan = pan;
        if (ch->player) ch->player.pan = pan;
    }
    Py_RETURN_NONE;
}

// ── set_pitch(channel, rate) — 0.25 to 4.0 ──

static PyObject *audio_set_pitch(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    float rate = 1.0f;
    if (!PyArg_ParseTuple(args, "Lf", &h, &rate)) return nullptr;
    if (!std::isfinite(rate) || rate < 0.25 || rate > 4) {
        PyErr_SetString(PyExc_ValueError, "rate must be 0.25...4."); return nullptr;
    }

    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (ch && !ch->completed) {
        ch->pitch = rate;
        if (ch->timePitch) ch->timePitch.rate = rate;
    }
    Py_RETURN_NONE;
}

// ── Fade timer (DispatchSourceTimer, native-driven) ──

static void fadeTimerTick(void) {
    static double lastTime = 0;
    double now = CACurrentMediaTime();
    double dt = (lastTime > 0) ? (now - lastTime) : (1.0 / 60.0);
    lastTime = now;

    std::lock_guard<std::mutex> lock(gMutex);
    BOOL anyActive = NO;
    std::vector<long long> toRemove;
    for (auto &pair : gChannels) {
        ChannelRecord &ch = pair.second;
        if (ch.completed || !ch.fadeActive) continue;
        anyActive = YES;
        ch.fadeElapsed += dt;
        double t = ch.fadeElapsed / ch.fadeDuration;
        if (t >= 1.0) {
            ch.volume = ch.fadeTarget;
            ch.fadeActive = NO;
        } else {
            ch.volume = ch.fadeStart + (ch.fadeTarget - ch.fadeStart) * (float)t;
        }
        if (ch.player) ch.player.volume = ch.volume * gMasterVolume;
        /* Auto-stop if faded to zero */
        if (!ch.fadeActive && ch.volume < 0.001f) {
            toRemove.push_back(pair.first);
        }
    }
    for (long long h : toRemove) {
        completeChannel(h, YES);
    }
    if (!anyActive && gFadeTimer && gFadeTimerRunning) {
        dispatch_suspend(gFadeTimer);
        gFadeTimerRunning = NO;
        lastTime = 0;
    }
}

static void ensureFadeTimer(void) {
    if (!gFadeTimer) {
        dispatch_queue_t q = dispatch_get_global_queue(QOS_CLASS_USER_INTERACTIVE, 0);
        gFadeTimer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, q);
        dispatch_source_set_timer(gFadeTimer,
                                  dispatch_time(DISPATCH_TIME_NOW, 0),
                                  (uint64_t)(16 * NSEC_PER_MSEC),
                                  (uint64_t)(2 * NSEC_PER_MSEC));
        dispatch_source_set_event_handler(gFadeTimer, ^{ fadeTimerTick(); });
        dispatch_resume(gFadeTimer);
        gFadeTimerRunning = YES;
    } else if (!gFadeTimerRunning) {
        dispatch_resume(gFadeTimer);
        gFadeTimerRunning = YES;
    }
}

static void stopFadeTimer(void) {
    if (gFadeTimer) {
        if (!gFadeTimerRunning) dispatch_resume(gFadeTimer);
        dispatch_source_cancel(gFadeTimer);
        gFadeTimer = nullptr;
        gFadeTimerRunning = NO;
    }
}

// ── fade(channel, target_volume, duration) ──

static PyObject *audio_fade(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    float target = 0.0f, duration = 1.0f;
    if (!PyArg_ParseTuple(args, "Lff", &h, &target, &duration)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (ch && !ch->completed) {
        if (duration < 0.001f) duration = 0.001f;
        ch->fadeStart = ch->volume;
        ch->fadeTarget = target;
        ch->fadeDuration = (double)duration;
        ch->fadeElapsed = 0.0;
        ch->fadeActive = YES;
        ensureFadeTimer();
    }
    Py_RETURN_NONE;
}


// ── master_volume getter/setter ──

static PyObject *audio_get_master_volume(PyObject *self, PyObject *args) {
    (void)self; (void)args;
    std::lock_guard<std::mutex> lock(gMutex);
    return PyFloat_FromDouble(gMasterVolume);
}

static PyObject *audio_set_master_volume(PyObject *self, PyObject *args) {
    (void)self;
    float vol = 1.0f;
    if (!PyArg_ParseTuple(args, "f", &vol)) return nullptr;
    if (!std::isfinite(vol) || vol < 0 || vol > 1) {
        PyErr_SetString(PyExc_ValueError, "master_volume must be 0...1."); return nullptr;
    }

    std::lock_guard<std::mutex> lock(gMutex);
    gMasterVolume = vol;
    for (auto &pair : gChannels) {
        if (!pair.second.completed && pair.second.player)
            pair.second.player.volume = pair.second.volume * gMasterVolume;
    }
    for (auto &pair : gOutputs) pair.second->mixer.outputVolume = gMasterVolume;
    Py_RETURN_NONE;
}

// ── pause_all / resume_all ──

static PyObject *audio_pause_all(PyObject *self, PyObject *args) {
    (void)self; (void)args;

    std::lock_guard<std::mutex> lock(gMutex);
    for (auto &pair : gChannels) {
        pauseChannel(&pair.second);
    }
    for (auto &pair : gOutputs) pair.second->state->paused = true;
    Py_RETURN_NONE;
}

static PyObject *audio_resume_all(PyObject *self, PyObject *args) {
    (void)self; (void)args;

    std::lock_guard<std::mutex> lock(gMutex);
    if ((!gChannels.empty() || !gOutputs.empty()) && !gInterrupted) {
        if (raiseAudioError(restartAudioEngine())) return nullptr;
    }
    for (auto it = gChannels.begin(); it != gChannels.end();) {
        auto current = it++;
        if (raiseAudioError(resumeChannel(&current->second))) return nullptr;
    }
    for (auto &pair : gOutputs) pair.second->state->paused = false;
    Py_RETURN_NONE;
}

// ── stop_all() ──

static PyObject *audio_stop_all(PyObject *self, PyObject *args) {
    (void)self; (void)args;

    std::lock_guard<std::mutex> lock(gMutex);
    @autoreleasepool {
        for (auto &pair : gChannels) {
            if (!pair.second.completed) pushAudioEvent(AudioEventKind::PlaybackEnded, pair.first, "stopped");
            disconnectChannel(&pair.second);
        }
    }
    gChannels.clear();
    for (auto &pair : gOutputs) closeOutput(pair.second);
    gOutputs.clear();
    Py_RETURN_NONE;
}

// ── Global effects: just toggle bypass + set params (always wired) ──

static PyObject *audio_reverb_enable(PyObject *self, PyObject *args) {
    (void)self;
    float mix = 30.0f;
    int preset = (int)AVAudioUnitReverbPresetMediumHall;
    if (!PyArg_ParseTuple(args, "|fi", &mix, &preset)) return nullptr;
    if (!ensureEngine()) { PyErr_SetString(PyExc_RuntimeError, "Audio engine unavailable."); return nullptr; }
    gReverb.wetDryMix = mix;
    [gReverb loadFactoryPreset:(AVAudioUnitReverbPreset)preset];
    gReverb.bypass = NO;
    Py_RETURN_NONE;
}

static PyObject *audio_reverb_disable(PyObject *self, PyObject *args) {
    (void)self; (void)args;
    if (gReverb) gReverb.bypass = YES;
    Py_RETURN_NONE;
}

static PyObject *audio_eq_set(PyObject *self, PyObject *args) {
    (void)self;
    int band = 0; float gain = 0.0f;
    if (!PyArg_ParseTuple(args, "if", &band, &gain)) return nullptr;
    if (!ensureEngine()) { PyErr_SetString(PyExc_RuntimeError, "Audio engine unavailable."); return nullptr; }
    if (band < 0 || band > 2) { PyErr_SetString(PyExc_ValueError, "band must be 0-2."); return nullptr; }
    gEQ.bands[band].gain = gain;
    gEQ.bypass = NO;
    Py_RETURN_NONE;
}

static PyObject *audio_eq_disable(PyObject *self, PyObject *args) {
    (void)self; (void)args;
    if (gEQ) gEQ.bypass = YES;
    Py_RETURN_NONE;
}

static PyObject *audio_delay_enable(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    float time = 0.3f, feedback = 50.0f, mix = 30.0f;
    static char s_t[] = "time"; static char s_fb[] = "feedback"; static char s_m[] = "mix";
    static char *kwlist[] = {s_t, s_fb, s_m, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|fff", kwlist, &time, &feedback, &mix)) return nullptr;
    if (!ensureEngine()) { PyErr_SetString(PyExc_RuntimeError, "Audio engine unavailable."); return nullptr; }
    gDelay.delayTime = time;
    gDelay.feedback = feedback;
    gDelay.wetDryMix = mix;
    gDelay.bypass = NO;
    Py_RETURN_NONE;
}

static PyObject *audio_delay_disable(PyObject *self, PyObject *args) {
    (void)self; (void)args;
    if (gDelay) gDelay.bypass = YES;
    Py_RETURN_NONE;
}

static PyObject *audio_distortion_enable(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    int preset = (int)AVAudioUnitDistortionPresetDrumsBitBrush; float mix = 50.0f;
    static char s_p[] = "preset"; static char s_m[] = "mix";
    static char *kwlist[] = {s_p, s_m, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|if", kwlist, &preset, &mix)) return nullptr;
    if (!ensureEngine()) { PyErr_SetString(PyExc_RuntimeError, "Audio engine unavailable."); return nullptr; }
    [gDistortion loadFactoryPreset:(AVAudioUnitDistortionPreset)preset];
    gDistortion.wetDryMix = mix;
    gDistortion.bypass = NO;
    Py_RETURN_NONE;
}

static PyObject *audio_distortion_disable(PyObject *self, PyObject *args) {
    (void)self; (void)args;
    if (gDistortion) gDistortion.bypass = YES;
    Py_RETURN_NONE;
}

// ── sound_info(handle) → dict ──

static PyObject *audio_sound_info(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    SoundRecord *snd = soundRecord(h);
    if (!snd) Py_RETURN_NONE;
    if (raiseAudioError(snd->error)) return nullptr;
    return Py_BuildValue("{s:d,s:i,s:d,s:L}",
                         "duration", snd->duration,
                         "channels", snd->channels,
                         "sample_rate", snd->sampleRate,
                         "frames", (long long)snd->frames);
}

// ── sound_loaded(handle) → bool ──

static PyObject *audio_sound_loaded(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    SoundRecord *snd = soundRecord(h);
    if (!snd) Py_RETURN_FALSE;
    if (raiseAudioError(snd->error)) return nullptr;
    return PyBool_FromLong(snd->loaded);
}

// ── channel_position(handle) → double ──

static PyObject *audio_channel_position(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (ch && raiseAudioError(ch->error)) return nullptr;
    return PyFloat_FromDouble(channelPosition(ch));
}

// ── channel_duration(handle) → double ──

static PyObject *audio_channel_duration(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (ch && raiseAudioError(ch->error)) return nullptr;
    return PyFloat_FromDouble(ch ? ch->duration : 0.0);
}

// ── channel_seek(handle, time) ──

static PyObject *audio_channel_seek(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    double time = 0.0;
    if (!PyArg_ParseTuple(args, "Ld", &h, &time)) return nullptr;
    if (!std::isfinite(time)) {
        PyErr_SetString(PyExc_ValueError, "Playback position must be finite.");
        return nullptr;
    }

    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (ch && raiseAudioError(ch->error)) return nullptr;
    if (!ch || !ch->player || ch->completed) Py_RETURN_NONE;
    if (!channelReady(ch)) {
        ch->positionOffset = std::clamp(time, 0.0, ch->duration);
        ch->pausedPosition = ch->positionOffset;
        Py_RETURN_NONE;
    }

    @try {
        [ch->player stop];
        ch->generation++;
        if (!rescheduleFromPosition(ch, time)) {
            raiseAudioError(ch->error);
            completeChannel(h);
            return nullptr;
        }
        if (!ch->paused && (ch->error = startChannelPlayback(ch))) {
            raiseAudioError(ch->error);
            completeChannel(h);
            return nullptr;
        }
    } @catch (NSException *exception) {
        ch->error = exception.reason ?: @"Cannot seek audio playback.";
        raiseAudioError(ch->error);
        completeChannel(h);
        return nullptr;
    }
    Py_RETURN_NONE;
}

// ── Per-channel effects ──

static PyObject *audio_channel_reverb(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long h = 0;
    float mix = 40.0f;
    int preset = (int)AVAudioUnitReverbPresetLargeHall;
    static char s_h[] = "handle"; static char s_m[] = "mix"; static char s_p[] = "preset";
    static char *kwlist[] = {s_h, s_m, s_p, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "L|fi", kwlist, &h, &mix, &preset))
        return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (!ch || ch->completed) Py_RETURN_NONE;
    BOOL needsRebuild = (ch->chReverb == nil);
    if (needsRebuild) ch->chReverb = [[AVAudioUnitReverb alloc] init];
    ch->chReverb.wetDryMix = mix;
    [ch->chReverb loadFactoryPreset:(AVAudioUnitReverbPreset)preset];
    ch->chReverb.bypass = NO;
    if (needsRebuild && !rebuildChannelChain(ch)) return nullptr;
    Py_RETURN_NONE;
}

static PyObject *audio_channel_reverb_off(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (!ch || !ch->chReverb) Py_RETURN_NONE;
    ch->chReverb.bypass = YES;
    Py_RETURN_NONE;
}

static PyObject *audio_channel_delay(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long h = 0;
    float time = 0.3f, feedback = 50.0f, mix = 30.0f;
    static char s_h[] = "handle"; static char s_t[] = "time";
    static char s_fb[] = "feedback"; static char s_m[] = "mix";
    static char *kwlist[] = {s_h, s_t, s_fb, s_m, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "L|fff", kwlist, &h, &time, &feedback, &mix))
        return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (!ch || ch->completed) Py_RETURN_NONE;
    BOOL needsRebuild = (ch->chDelay == nil);
    if (needsRebuild) ch->chDelay = [[AVAudioUnitDelay alloc] init];
    ch->chDelay.delayTime = time;
    ch->chDelay.feedback = feedback;
    ch->chDelay.wetDryMix = mix;
    ch->chDelay.bypass = NO;
    if (needsRebuild && !rebuildChannelChain(ch)) return nullptr;
    Py_RETURN_NONE;
}

static PyObject *audio_channel_delay_off(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (!ch || !ch->chDelay) Py_RETURN_NONE;
    ch->chDelay.bypass = YES;
    Py_RETURN_NONE;
}

static PyObject *audio_channel_distortion(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long h = 0;
    int preset = (int)AVAudioUnitDistortionPresetDrumsBitBrush;
    float mix = 50.0f;
    static char s_h[] = "handle"; static char s_p[] = "preset"; static char s_m[] = "mix";
    static char *kwlist[] = {s_h, s_p, s_m, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "L|if", kwlist, &h, &preset, &mix))
        return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (!ch || ch->completed) Py_RETURN_NONE;
    BOOL needsRebuild = (ch->chDistortion == nil);
    if (needsRebuild) ch->chDistortion = [[AVAudioUnitDistortion alloc] init];
    [ch->chDistortion loadFactoryPreset:(AVAudioUnitDistortionPreset)preset];
    ch->chDistortion.wetDryMix = mix;
    ch->chDistortion.bypass = NO;
    if (needsRebuild && !rebuildChannelChain(ch)) return nullptr;
    Py_RETURN_NONE;
}

static PyObject *audio_channel_distortion_off(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (!ch || !ch->chDistortion) Py_RETURN_NONE;
    ch->chDistortion.bypass = YES;
    Py_RETURN_NONE;
}

static PyObject *audio_channel_eq(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    long long h = 0;
    float low = 0.0f, mid = 0.0f, high = 0.0f;
    static char s_h[] = "handle"; static char s_l[] = "low";
    static char s_m[] = "mid"; static char s_hi[] = "high";
    static char *kwlist[] = {s_h, s_l, s_m, s_hi, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "L|fff", kwlist, &h, &low, &mid, &high))
        return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (!ch || ch->completed) Py_RETURN_NONE;
    BOOL needsRebuild = (ch->chEQ == nil);
    if (needsRebuild) {
        ch->chEQ = [[AVAudioUnitEQ alloc] initWithNumberOfBands:3];
        ch->chEQ.bands[0].filterType = AVAudioUnitEQFilterTypeParametric;
        ch->chEQ.bands[0].frequency = 100;  ch->chEQ.bands[0].bandwidth = 1.0; ch->chEQ.bands[0].bypass = NO;
        ch->chEQ.bands[1].filterType = AVAudioUnitEQFilterTypeParametric;
        ch->chEQ.bands[1].frequency = 1000; ch->chEQ.bands[1].bandwidth = 1.0; ch->chEQ.bands[1].bypass = NO;
        ch->chEQ.bands[2].filterType = AVAudioUnitEQFilterTypeParametric;
        ch->chEQ.bands[2].frequency = 8000; ch->chEQ.bands[2].bandwidth = 1.0; ch->chEQ.bands[2].bypass = NO;
    }
    ch->chEQ.bands[0].gain = low;
    ch->chEQ.bands[1].gain = mid;
    ch->chEQ.bands[2].gain = high;
    ch->chEQ.bypass = NO;
    if (needsRebuild && !rebuildChannelChain(ch)) return nullptr;
    Py_RETURN_NONE;
}

static PyObject *audio_channel_eq_off(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (!ch || !ch->chEQ) Py_RETURN_NONE;
    ch->chEQ.bypass = YES;
    Py_RETURN_NONE;
}

// ── Recording ──

static PyObject *audio_recorder_start(PyObject *self, PyObject *args, PyObject *kwargs) {
    (void)self;
    double sampleRate = 44100.0;
    int channels = 1;
    const char *path = nullptr;
    int stream = 0, overwrite = 0;
    double bufferDuration = 0.5;
    static char s_sr[] = "sample_rate"; static char s_ch[] = "channels";
    static char s_path[] = "path", s_stream[] = "stream", s_buffer[] = "buffer_duration", s_overwrite[] = "overwrite";
    static char *kwlist[] = {s_sr, s_ch, s_path, s_stream, s_buffer, s_overwrite, nullptr};
    if (!PyArg_ParseTupleAndKeywords(args, kwargs, "|dizpdp", kwlist, &sampleRate, &channels,
                                    &path, &stream, &bufferDuration, &overwrite))
        return nullptr;
    if (!validPCMFormat(sampleRate, channels, false)) return nullptr;
    if ((path && stream) || !std::isfinite(bufferDuration) || bufferDuration < 0.02 || bufferDuration > 10) {
        PyErr_SetString(PyExc_ValueError, "Choose file or stream recording; buffer_duration must be 0.02...10 seconds.");
        return nullptr;
    }

    if (!ensureEngine()) {
        PyErr_SetString(PyExc_RuntimeError, "Audio engine unavailable.");
        return nullptr;
    }

    /* Reserve the input bus before releasing the GIL for permission. */
    long long startHandle = nextHandle();
    {
        std::lock_guard<std::mutex> lock(gMutex);
        if (gRecorderStartHandle || !gRecorders.empty()) {
            PyErr_SetString(PyExc_RuntimeError, "A recording is already in progress.");
            return nullptr;
        }
        gRecorderStartHandle = startHandle;
    }
    RecorderStartGuard startGuard{startHandle};

    @autoreleasepool {
        NSError *err = nil;
#if COCOA_PY_AUDIO_SESSION
        AVAudioSession *session = [AVAudioSession sharedInstance];
#endif

        /* Request microphone permission synchronously via semaphore.
           On first call iOS shows the permission dialog; subsequent
           calls return immediately.
           iOS 17+: use AVAudioApplication API. */
        __block BOOL permitted = NO;
        {
            AVAudioApplicationRecordPermission perm =
                [AVAudioApplication sharedInstance].recordPermission;
            if (perm == AVAudioApplicationRecordPermissionGranted) {
                permitted = YES;
            } else if (perm == AVAudioApplicationRecordPermissionDenied) {
                PyErr_SetString(PyExc_PermissionError,
                    "Microphone permission denied. Enable in Settings > Privacy > Microphone.");
                return nullptr;
            } else {
                /* Undetermined — ask now (blocks via semaphore) */
#if COCOA_PY_UIKIT
                if ([NSThread isMainThread]) {
                    PyErr_SetString(PyExc_RuntimeError,
                        "Start the first recording from the Python execution thread, not a UI callback.");
                    return nullptr;
                }
#endif
#if !defined(COCOA_PY_AUDIO_IOS_TESTS)
                if (![[NSBundle mainBundle] objectForInfoDictionaryKey:@"NSMicrophoneUsageDescription"]) {
                    PyErr_SetString(PyExc_RuntimeError,
                        "The host must provide NSMicrophoneUsageDescription before requesting microphone access.");
                    return nullptr;
                }
#endif
                dispatch_semaphore_t sem = dispatch_semaphore_create(0);
                dispatch_async(dispatch_get_main_queue(), ^{
                    [AVAudioApplication requestRecordPermissionWithCompletionHandler:^(BOOL granted) {
                        permitted = granted;
                        dispatch_semaphore_signal(sem);
                    }];
                });
                bool permissionFinished = false;
                Py_BEGIN_ALLOW_THREADS
                permissionFinished = CocoaPyWaitSemaphore(sem, 120.0);
                Py_END_ALLOW_THREADS
                if (!permissionFinished) {
                    PyErr_SetString(PyExc_TimeoutError, "Microphone permission request timed out.");
                    return nullptr;
                }
                if (!permitted) {
                    PyErr_SetString(PyExc_PermissionError,
                        "Microphone permission denied by user.");
                    return nullptr;
                }
            }
        }

        std::lock_guard<std::mutex> lock(gMutex);
        if (gRecorderStartHandle != startHandle || !gEngine || !gEngineReady) {
            PyErr_SetString(PyExc_RuntimeError, "Recording was cancelled while waiting for microphone permission.");
            return nullptr;
        }
        if (gInterrupted) {
            PyErr_SetString(PyExc_RuntimeError, "Cannot start recording during an audio interruption.");
            return nullptr;
        }

        AVAudioEngine *engine = gEngine;
        BOOL wasRunning = engine.isRunning;
#if COCOA_PY_AUDIO_SESSION
        AVAudioSessionCategory oldCategory = session.category;
        AVAudioSessionMode oldMode = session.mode;
        AVAudioSessionCategoryOptions oldOptions = session.categoryOptions;
#endif
        AVAudioInputNode *inputNode = nil;
        BOOL tapInstalled = NO;
        NSString *failure = nil;
        auto rec = std::make_shared<RecorderRecord>();
        rec->handle = startHandle;
        std::vector<long long> suspendedChannels;
        auto suspendPlayback = [&] {
            suspendedChannels.reserve(gChannels.size());
            for (auto &entry : gChannels) {
                auto &ch = entry.second;
                if (ch.completed || !channelReady(&ch)) continue;
                double position = channelPosition(&ch);
                suspendedChannels.push_back(ch.handle);
                // Engine stop can discard queued audio without clearing isPlaying.
                // Invalidate callbacks before stopping any player, and freeze its
                // source position until the replacement schedule starts.
                ++ch.generation;
                ch.positionOffset = ch.pausedPosition = position;
                ch.resumePending = !ch.paused;
                [ch.player stop];
            }
        };
        auto restorePlayback = [&]() -> NSString * {
            NSString *error = nil;
            for (long long handle : suspendedChannels) {
                auto ch = channelRecord(handle);
                if (!ch || ch->completed) continue;
                @try {
                    // Reusing a player after engine stop can lose played-back
                    // callbacks even after buffers are rescheduled. Replace just
                    // the player, retaining TimePitch and the channel's effects.
                    if (ch->player.engine) [engine detachNode:ch->player];
                    ch->player = [AVAudioPlayerNode new];
                    [engine attachNode:ch->player];
                    [engine connect:ch->player to:ch->timePitch format:channelFormat(ch)];
                    ch->player.volume = ch->volume * gMasterVolume;
                    ch->player.pan = ch->pan;
                    if (rescheduleFromPosition(ch, ch->pausedPosition)
                        && !ch->paused && engine.isRunning)
                        ch->error = startChannelPlayback(ch);
                } @catch (NSException *exception) {
                    ch->error = exception.reason ?: @"Cannot restore playback after recording setup.";
                }
                if (ch->error) {
                    if (!error) error = ch->error;
                    completeChannel(handle);
                }
            }
            suspendedChannels.clear();
            return error;
        };
        auto rollback = [&] {
            rec->recording = false;
            if (tapInstalled) {
                @try { [inputNode removeTapOnBus:0]; }
                @catch (NSException *exception) { }
            }
#if COCOA_PY_AUDIO_SESSION
            [session setCategory:oldCategory mode:oldMode options:oldOptions error:nil];
#endif
            if (wasRunning && !engine.isRunning) {
                NSString *error = restartAudioEngine();
                if (error) pushAudioEvent(AudioEventKind::EngineError, 0, "", error);
            }
            // Even when restart fails, leave replacement buffers queued and
            // resumePending set so a later explicit/system resume can recover.
            restorePlayback();
        };

        @try {
            suspendPlayback();
            if (wasRunning) [engine stop];
#if COCOA_PY_AUDIO_SESSION
            if (![session setCategory:AVAudioSessionCategoryPlayAndRecord
                          withOptions:(AVAudioSessionCategoryOptionMixWithOthers |
                                       AVAudioSessionCategoryOptionDefaultToSpeaker)
                                error:&err] || ![session setActive:YES error:&err]) {
                failure = err.localizedDescription ?: @"Cannot configure the microphone audio session.";
            }
#endif
            if (!failure) {
                inputNode = engine.inputNode;
                AVAudioFormat *recordFmt = [inputNode outputFormatForBus:0];
                rec->sampleRate = recordFmt.sampleRate;
                rec->channels = (int)recordFmt.channelCount;
                if (!std::isfinite(rec->sampleRate) || rec->sampleRate <= 0
                    || rec->channels < 1 || rec->channels > 2
                    || recordFmt.commonFormat != AVAudioPCMFormatFloat32 || recordFmt.isInterleaved) {
                    failure = @"Microphone is unavailable or does not provide mono/stereo float32 audio.";
                } else {
                    if (stream) {
                        rec->ring = std::make_unique<AudioRing>((uint64_t)ceil(rec->sampleRate * bufferDuration), rec->channels);
                    } else if (path) {
                        rec->writer = std::make_unique<AudioFileWriter>();
                        NSURL *url = [NSURL fileURLWithPath:resolveAudioPath(path)];
                        OSStatus status = rec->writer->open(url, recordFmt, overwrite, true);
                        if (status) {
                            rollback();
                            PyErr_Format(PyExc_OSError, "Cannot create recording file (OSStatus %d).", (int)status);
                            return nullptr;
                        }
                    }
                    [inputNode installTapOnBus:0 bufferSize:4096 format:recordFmt
                                        block:^(AVAudioPCMBuffer *buffer, AVAudioTime *when) {
                        // The map owns rec until after the tap is removed. The callback
                        // retains only native state and never enters the interpreter.
                        auto &strong = rec;
                        strong->callbacks.fetch_add(1, std::memory_order_acq_rel);
                        struct Done { RecorderRecord *rec; ~Done() { rec->callbacks.fetch_sub(1, std::memory_order_release); } } done{strong.get()};
                        if (!strong->recording) return;
                        AVAudioFrameCount frames = buffer.frameLength;
                        AVAudioChannelCount chCount = buffer.format.channelCount;
                        if (!buffer.floatChannelData || chCount != strong->channels || buffer.format.sampleRate != strong->sampleRate) {
                            strong->ioError = kAudioFileUnsupportedDataFormatError;
                            strong->recording = false;
                            return;
                        }
                        if (strong->writer) {
                            OSStatus status = strong->writer->write(frames, buffer.audioBufferList, true);
                            if (status) { strong->ioError = status; strong->recording = false; }
                            else strong->frames.fetch_add(frames, std::memory_order_relaxed);
                            return;
                        }
                        if (strong->ring) {
                            uint64_t written = strong->ring->write(nullptr, buffer.floatChannelData, frames);
                            strong->droppedFrames.fetch_add(frames - written, std::memory_order_relaxed);
                            strong->frames.fetch_add(frames, std::memory_order_relaxed);
                            return;
                        }
                        std::lock_guard<std::mutex> lk(strong->recMutex);
                        if (!strong->recording) return;
                        if (chCount == 1) {
                            const float *src = buffer.floatChannelData[0];
                            strong->samples.insert(strong->samples.end(), src, src + frames);
                        } else {
                            const float *left = buffer.floatChannelData[0];
                            const float *right = buffer.floatChannelData[1];
                            for (AVAudioFrameCount i = 0; i < frames; i++) {
                                strong->samples.push_back(left[i]);
                                strong->samples.push_back(right[i]);
                            }
                        }
                        strong->frames.fetch_add(frames, std::memory_order_relaxed);
                    }];
                    tapInstalled = YES;
                    if (![engine startAndReturnError:&err])
                        failure = err.localizedDescription ?: @"Cannot start the microphone audio engine.";
                }
            }
        } @catch (NSException *exception) {
            failure = exception.reason ?: @"Cannot start recording.";
        }
        if (!failure) failure = restorePlayback();
        if (failure) {
            rollback();
            raiseAudioError(failure);
            return nullptr;
        }
        PyObject *result = Py_BuildValue("{s:L,s:d,s:i}", "handle", startHandle,
                                        "sample_rate", rec->sampleRate, "channels", rec->channels);
        if (!result) {
            rollback();
            return nullptr;
        }
        gRecorders.emplace(startHandle, rec);
        rec->recording = true;
        return result;
    }
}

static PyObject *audio_recorder_stop(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gRecorders.find(h);
    if (it == gRecorders.end()) return PyBytes_FromStringAndSize(nullptr, 0);
    std::shared_ptr<RecorderRecord> rec = it->second;

    rec->recording = NO;

    @autoreleasepool {
        @try { [[gEngine inputNode] removeTapOnBus:0]; }
        @catch (NSException *e) { }
        while (rec->callbacks.load(std::memory_order_acquire)) std::this_thread::yield();

        /* Restore playback-only without another shared-engine restart.
           recorder_start preserves and rebuilds player schedules around
           the stop/start required for configuring microphone input. */
        NSError *err = nil;
#if COCOA_PY_AUDIO_SESSION
        [[AVAudioSession sharedInstance] setCategory:AVAudioSessionCategoryPlayback
                                         withOptions:AVAudioSessionCategoryOptionMixWithOthers
                                               error:&err];
#endif
    }

    if (rec->writer) {
        OSStatus status = rec->writer->close(); // flush and finalize WAV/CAF/AAC before reporting success
        if (rec->ioError) status = rec->ioError;
        gRecorders.erase(it);
        if (status) {
            PyErr_Format(PyExc_OSError, "Recording file write failed (OSStatus %d); the file may be partial.", (int)status);
            return nullptr;
        }
        return PyLong_FromUnsignedLongLong(rec->frames.load());
    }
    if (rec->ring) {
        gRecorders.erase(it);
        Py_RETURN_NONE;
    }

    std::lock_guard<std::mutex> samplesLock(rec->recMutex);
    PyObject *result = PyBytes_FromStringAndSize((const char *)rec->samples.data(),
                                                rec->samples.size() * sizeof(float));
    if (result) gRecorders.erase(it);
    return result;
}

static PyObject *audio_recorder_is_recording(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gRecorders.find(h);
    if (it == gRecorders.end()) Py_RETURN_FALSE;
    if (it->second->ioError) {
        PyErr_Format(PyExc_OSError, "Audio input failed (OSStatus %d); call stop() or close().", (int)it->second->ioError.load());
        return nullptr;
    }
    return PyBool_FromLong(it->second->recording);
}

// ── channel_info(handle) → dict ──

static PyObject *audio_channel_info(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (!ch) Py_RETURN_NONE;
    if (raiseAudioError(ch->error)) return nullptr;

    BOOL playing = ch->completed ? NO : (ch->player ? ch->player.isPlaying : NO);
    return Py_BuildValue("{s:f,s:f,s:f,s:f,s:N,s:N,s:N}",
                         "volume", ch->volume,
                         "pan", ch->pan,
                         "pitch", ch->pitch,
                         "semitones", ch->semitones,
                         "playing", PyBool_FromLong(playing),
                         "paused", PyBool_FromLong(ch->paused),
                         "looping", PyBool_FromLong(ch->looping));
}

// ── channel_playing(handle) → bool ──

static PyObject *audio_channel_playing(PyObject *self, PyObject *args) {
    (void)self;
    long long h = 0;
    if (!PyArg_ParseTuple(args, "L", &h)) return nullptr;

    std::lock_guard<std::mutex> lock(gMutex);
    ChannelRecord *ch = channelRecord(h);
    if (ch && raiseAudioError(ch->error)) return nullptr;
    if (!ch || ch->completed) Py_RETURN_FALSE;
    return PyBool_FromLong(ch->player ? ch->player.isPlaying : NO);
}

#include "AudioIO.h"
#include "AudioOffline.h"

// ── close() — tear down engine ──

static PyObject *audio_close(PyObject *self, PyObject *args) {
    (void)self; (void)args;

    std::lock_guard<std::mutex> lock(gMutex);
    OSStatus fileError = noErr;
    @autoreleasepool {
        ++gAudioEpoch;
        clearAudioEvents();
        gRecorderStartHandle = 0;
        stopFadeTimer();

        /* Stop any active recorders */
        for (auto &pair : gRecorders) {
            pair.second->recording = NO;
        }
        @try { if (!gRecorders.empty()) [[gEngine inputNode] removeTapOnBus:0]; }
        @catch (NSException *e) { }
        for (auto &pair : gRecorders) {
            while (pair.second->callbacks.load(std::memory_order_acquire)) std::this_thread::yield();
            if (pair.second->writer) {
                OSStatus status = pair.second->writer->close();
                if (pair.second->ioError) status = pair.second->ioError;
                if (!fileError) fileError = status;
            }
        }
        gRecorders.clear();
        for (auto &pair : gOutputs) closeOutput(pair.second);
        gOutputs.clear();
        for (auto &pair : gWriters) {
            OSStatus status = pair.second->close();
            if (!fileError) fileError = status;
        }
        gWriters.clear();
        gRenders.clear();

        for (auto &pair : gChannels) {
            disconnectChannel(&pair.second);
        }
        gChannels.clear();
        gSounds.clear();

        /* Remove notification observers */
        NSNotificationCenter *nc = [NSNotificationCenter defaultCenter];
        if (gInterruptionObserver) { [nc removeObserver:gInterruptionObserver]; gInterruptionObserver = nil; }
        if (gRouteChangeObserver)  { [nc removeObserver:gRouteChangeObserver];  gRouteChangeObserver = nil; }
        if (gConfigChangeObserver) { [nc removeObserver:gConfigChangeObserver]; gConfigChangeObserver = nil; }
        gInterrupted = NO;

        if (gEngine) {
            [gEngine stop];
        }
        gReverb = nil;
        gEQ = nil;
        gDelay = nil;
        gDistortion = nil;
        gEngine = nil;
        gMixer = nil;
        gLoadQueue = nullptr;
        gEngineReady = NO;
    }
    if (audioStatusError(fileError, "Audio file finalization failed; resources were closed")) return nullptr;
    Py_RETURN_NONE;
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Audio session notifications                                             */
/* ─────────────────────────────────────────────────────────────────────── */

static void setupNotifications(void) {
    NSNotificationCenter *nc = [NSNotificationCenter defaultCenter];
    const auto epoch = ++gAudioEpoch;

#if COCOA_PY_AUDIO_SESSION
    /* 1. Audio session interruption (phone call, Siri, etc.) */
    gInterruptionObserver = [nc addObserverForName:AVAudioSessionInterruptionNotification
                                            object:[AVAudioSession sharedInstance]
                                             queue:nil
                                        usingBlock:^(NSNotification *note) {
        NSDictionary *info = note.userInfo;
        AVAudioSessionInterruptionType type =
            (AVAudioSessionInterruptionType)[info[AVAudioSessionInterruptionTypeKey] unsignedIntegerValue];

        if (type == AVAudioSessionInterruptionTypeBegan) {
            dispatch_async(dispatch_get_main_queue(), ^{
                std::lock_guard<std::mutex> lock(gMutex);
                if (gAudioEpoch != epoch || !gEngine) return;
                gInterrupted = YES;
                pushAudioEvent(AudioEventKind::Interruption, 0, "began");
            });
        } else if (type == AVAudioSessionInterruptionTypeEnded) {
            AVAudioSessionInterruptionOptions opts =
                (AVAudioSessionInterruptionOptions)[info[AVAudioSessionInterruptionOptionKey] unsignedIntegerValue];
            dispatch_async(dispatch_get_main_queue(), ^{
                std::lock_guard<std::mutex> lock(gMutex);
                if (gAudioEpoch != epoch || !gEngine) return;
                gInterrupted = NO;
                bool shouldResume = (opts & AVAudioSessionInterruptionOptionShouldResume) != 0;
                pushAudioEvent(AudioEventKind::Interruption, 0, "ended", nil, 0, shouldResume);
                if (shouldResume) {
                    NSString *error = restartAudioEngine();
                    if (error) pushAudioEvent(AudioEventKind::EngineError, 0, "", error);
                    else resumePendingChannels();
                } else {
                    for (auto &pair : gChannels) pauseChannel(&pair.second);
                    for (auto &pair : gOutputs) pair.second->state->paused = true;
                }
            });
        }
    }];

    /* 2. Route change (headphones unplugged → pause to avoid speaker blast) */
    gRouteChangeObserver = [nc addObserverForName:AVAudioSessionRouteChangeNotification
                                           object:[AVAudioSession sharedInstance]
                                            queue:nil
                                       usingBlock:^(NSNotification *note) {
        NSDictionary *info = note.userInfo;
        AVAudioSessionRouteChangeReason reason =
            (AVAudioSessionRouteChangeReason)[info[AVAudioSessionRouteChangeReasonKey] unsignedIntegerValue];

        dispatch_async(dispatch_get_main_queue(), ^{
            std::lock_guard<std::mutex> lock(gMutex);
            if (gAudioEpoch != epoch || !gEngine) return;
            pushAudioEvent(AudioEventKind::RouteChanged, 0, "", nil, (unsigned)reason);
            if (reason == AVAudioSessionRouteChangeReasonOldDeviceUnavailable) {
                for (auto &pair : gChannels) {
                    pauseChannel(&pair.second);
                }
                for (auto &pair : gOutputs) pair.second->state->paused = true;
            }
        });
    }];

    /* 3. Engine configuration change (sample rate / device change) */
#endif
    gConfigChangeObserver = [nc addObserverForName:AVAudioEngineConfigurationChangeNotification
                                            object:gEngine
                                             queue:nil
                                        usingBlock:^(NSNotification *note) {
#if COCOA_PY_AUDIO_SESSION
        dispatch_queue_t queue = dispatch_get_main_queue();
#else
        // A command-line Python program may occupy the main thread without
        // pumping AppKit. Engine recovery must still respond to device changes.
        static dispatch_queue_t queue = dispatch_queue_create("org.cocoa-py.audio.events", DISPATCH_QUEUE_SERIAL);
#endif
        dispatch_async(queue, ^{
            std::lock_guard<std::mutex> lock(gMutex);
            if (gAudioEpoch != epoch || !gEngine) return;
            if (!gInterrupted) {
                NSString *error = restartAudioEngine();
                if (error) pushAudioEvent(AudioEventKind::EngineError, 0, "", error);
                else resumePendingChannels();
            }
            pushAudioEvent(AudioEventKind::ConfigurationChanged, 0, "", nil, 0, gEngine.isRunning);
        });
    }];
}

/* ─────────────────────────────────────────────────────────────────────── */
/* Module definition                                                       */
/* ─────────────────────────────────────────────────────────────────────── */

static PyMethodDef audioMethods[] = {
    {"channel_status", audio_channel_status, METH_VARARGS, "Channel lifecycle state and optional error."},
    {"get_events", audio_get_events, METH_VARARGS, "Consume bounded native audio events without waiting."},
    {"get_device_info", audio_get_device_info, METH_NOARGS, "Query routes, latency and permission without activating audio."},
    {"sound_pcm", audio_sound_pcm, METH_VARARGS, "Copy decoded Sound samples as interleaved float32 PCM."},
    {"load_stream", audio_load_stream, METH_VARARGS, "Open an incrementally decoded local audio file."},
    {"set_semitones", audio_set_semitones, METH_VARARGS, "Set pitch shift in semitones."},
    {"input_info", audio_input_info, METH_VARARGS, "Input buffer and recording counters."},
    {"input_read", audio_input_read, METH_VARARGS, "Read available float32 input frames without blocking."},
    {"output_open", audio_output_open, METH_VARARGS, "Create bounded PCM output."},
    {"output_write", audio_output_write, METH_VARARGS, "Write available PCM output frames without blocking."},
    {"output_info", audio_output_info, METH_VARARGS, "Output buffer counters."},
    {"output_pause", audio_output_pause, METH_VARARGS, "Pause or resume PCM output."},
    {"output_close", audio_output_close, METH_VARARGS, "Close PCM output."},
    {"file_access_open", audio_file_access_open, METH_VARARGS, "Hold file access across export path checks and I/O."},
    {"file_access_close", audio_file_access_close, METH_O, "Release an export file-access guard."},
    {"writer_open", audio_writer_open, METH_VARARGS, "Create WAV, CAF or M4A output."},
    {"writer_write", audio_writer_write, METH_VARARGS, "Write float32 PCM to file."},
    {"writer_close", audio_writer_close, METH_VARARGS, "Flush and close audio file."},
    {"render_open", audio_render_open, METH_VARARGS, "Create independent offline mixer."},
    {"render_read", audio_render_read, METH_VARARGS, "Render the next PCM block."},
    {"render_close", audio_render_close, METH_VARARGS, "Close offline mixer."},
    /* resource management */
    {"load",             (PyCFunction)audio_load,             METH_VARARGS, "Load an audio file, return sound handle."},
    {"load_bytes",       (PyCFunction)audio_load_bytes,       METH_VARARGS, "Load from in-memory bytes (WAV/MP3/AAC/etc.), return sound handle."},
    {"load_pcm",         (PyCFunction)audio_load_pcm,         METH_VARARGS | METH_KEYWORDS, "Load raw float32 PCM data, return sound handle."},
    {"generate_tone",    (PyCFunction)audio_generate_tone,    METH_VARARGS | METH_KEYWORDS, "Generate sine tone, return sound handle."},
    {"generate_noise",   (PyCFunction)audio_generate_noise,   METH_VARARGS | METH_KEYWORDS, "Generate white noise, return sound handle."},
    {"unload",           (PyCFunction)audio_unload,           METH_VARARGS, "Unload a sound by handle."},
    {"sound_info",       (PyCFunction)audio_sound_info,       METH_VARARGS, "Get sound info dict."},
    {"sound_loaded",     (PyCFunction)audio_sound_loaded,     METH_VARARGS, "Check if sound is decoded and ready."},
    {"close",            (PyCFunction)audio_close,            METH_NOARGS,  "Shut down the audio engine and release all resources."},

    /* playback */
    {"play",             (PyCFunction)audio_play,             METH_VARARGS | METH_KEYWORDS, "Play a sound, return channel handle."},
    {"stop",             (PyCFunction)audio_stop,             METH_VARARGS, "Stop a channel."},
    {"release_channel",  (PyCFunction)audio_release_channel,  METH_VARARGS, "Release the Python owner without stopping playback."},
    {"pause",            (PyCFunction)audio_pause,            METH_VARARGS, "Pause a channel."},
    {"resume",           (PyCFunction)audio_resume,           METH_VARARGS, "Resume a paused channel."},

    /* per-channel control */
    {"set_volume",       (PyCFunction)audio_set_volume,       METH_VARARGS, "Set channel volume (0.0-1.0)."},
    {"set_pan",          (PyCFunction)audio_set_pan,          METH_VARARGS, "Set channel pan (-1.0 left to 1.0 right)."},
    {"set_pitch",        (PyCFunction)audio_set_pitch,        METH_VARARGS, "Set channel playback rate (0.25-4.0)."},
    {"fade",             (PyCFunction)audio_fade,             METH_VARARGS, "Fade channel volume to target over duration."},

    /* channel query */
    {"channel_info",     (PyCFunction)audio_channel_info,     METH_VARARGS, "Get channel info dict."},
    {"channel_playing",  (PyCFunction)audio_channel_playing,  METH_VARARGS, "Check if channel is playing."},
    {"channel_position", (PyCFunction)audio_channel_position, METH_VARARGS, "Get channel playback position in seconds."},
    {"channel_duration", (PyCFunction)audio_channel_duration, METH_VARARGS, "Get channel total duration in seconds."},
    {"channel_seek",     (PyCFunction)audio_channel_seek,     METH_VARARGS, "Seek channel to position in seconds."},

    /* global control */
    {"get_master_volume",(PyCFunction)audio_get_master_volume, METH_NOARGS,  "Get master volume."},
    {"set_master_volume",(PyCFunction)audio_set_master_volume, METH_VARARGS, "Set master volume (0.0-1.0)."},
    {"pause_all",        (PyCFunction)audio_pause_all,        METH_NOARGS,  "Pause all channels."},
    {"resume_all",       (PyCFunction)audio_resume_all,       METH_NOARGS,  "Resume all channels."},
    {"stop_all",         (PyCFunction)audio_stop_all,         METH_NOARGS,  "Stop all channels."},

    /* global effects */
    {"reverb_enable",       (PyCFunction)audio_reverb_enable,       METH_VARARGS, "Enable reverb (wetDryMix, preset)."},
    {"reverb_disable",      (PyCFunction)audio_reverb_disable,      METH_NOARGS,  "Disable reverb."},
    {"eq_set",              (PyCFunction)audio_eq_set,              METH_VARARGS, "Set EQ band gain (band 0-2, gain in dB)."},
    {"eq_disable",          (PyCFunction)audio_eq_disable,          METH_NOARGS,  "Disable EQ."},
    {"delay_enable",        (PyCFunction)audio_delay_enable,        METH_VARARGS | METH_KEYWORDS, "Enable delay (time, feedback, mix)."},
    {"delay_disable",       (PyCFunction)audio_delay_disable,       METH_NOARGS,  "Disable delay."},
    {"distortion_enable",   (PyCFunction)audio_distortion_enable,   METH_VARARGS | METH_KEYWORDS, "Enable distortion (preset, mix)."},
    {"distortion_disable",  (PyCFunction)audio_distortion_disable,  METH_NOARGS,  "Disable distortion."},

    /* per-channel effects */
    {"channel_reverb",         (PyCFunction)audio_channel_reverb,         METH_VARARGS | METH_KEYWORDS, "Enable per-channel reverb."},
    {"channel_reverb_off",     (PyCFunction)audio_channel_reverb_off,     METH_VARARGS, "Disable per-channel reverb."},
    {"channel_delay",          (PyCFunction)audio_channel_delay,          METH_VARARGS | METH_KEYWORDS, "Enable per-channel delay."},
    {"channel_delay_off",      (PyCFunction)audio_channel_delay_off,      METH_VARARGS, "Disable per-channel delay."},
    {"channel_distortion",     (PyCFunction)audio_channel_distortion,     METH_VARARGS | METH_KEYWORDS, "Enable per-channel distortion."},
    {"channel_distortion_off", (PyCFunction)audio_channel_distortion_off, METH_VARARGS, "Disable per-channel distortion."},
    {"channel_eq",             (PyCFunction)audio_channel_eq,             METH_VARARGS | METH_KEYWORDS, "Enable per-channel 3-band EQ."},
    {"channel_eq_off",         (PyCFunction)audio_channel_eq_off,         METH_VARARGS, "Disable per-channel EQ."},

    /* recording */
    {"recorder_start",         (PyCFunction)audio_recorder_start,         METH_VARARGS | METH_KEYWORDS, "Start recording from microphone."},
    {"recorder_stop",          (PyCFunction)audio_recorder_stop,          METH_VARARGS, "Stop recording, return PCM bytes."},
    {"recorder_is_recording",  (PyCFunction)audio_recorder_is_recording,  METH_VARARGS, "Check if recorder is active."},

    {nullptr, nullptr, 0, nullptr},
};

static struct PyModuleDef audioModule = {
    PyModuleDef_HEAD_INIT,
    "_audio",
    "cocoa-py audio engine (AVAudioEngine).",
    -1,
    audioMethods,
};

PyMODINIT_FUNC PyInit__audio(void) {
    PyObject *m = PyModule_Create(&audioModule);
    if (!m) return nullptr;

    /* Export reverb preset constants */
    PyModule_AddIntConstant(m, "REVERB_SMALL_ROOM",    (int)AVAudioUnitReverbPresetSmallRoom);
    PyModule_AddIntConstant(m, "REVERB_MEDIUM_ROOM",   (int)AVAudioUnitReverbPresetMediumRoom);
    PyModule_AddIntConstant(m, "REVERB_LARGE_ROOM",    (int)AVAudioUnitReverbPresetLargeRoom);
    PyModule_AddIntConstant(m, "REVERB_MEDIUM_HALL",   (int)AVAudioUnitReverbPresetMediumHall);
    PyModule_AddIntConstant(m, "REVERB_LARGE_HALL",    (int)AVAudioUnitReverbPresetLargeHall);
    PyModule_AddIntConstant(m, "REVERB_PLATE",         (int)AVAudioUnitReverbPresetPlate);
    PyModule_AddIntConstant(m, "REVERB_MEDIUM_CHAMBER",(int)AVAudioUnitReverbPresetMediumChamber);
    PyModule_AddIntConstant(m, "REVERB_LARGE_CHAMBER", (int)AVAudioUnitReverbPresetLargeChamber);
    PyModule_AddIntConstant(m, "REVERB_CATHEDRAL",     (int)AVAudioUnitReverbPresetCathedral);
    PyModule_AddIntConstant(m, "REVERB_LARGE_ROOM2",   (int)AVAudioUnitReverbPresetLargeRoom2);
    PyModule_AddIntConstant(m, "REVERB_MEDIUM_HALL2",  (int)AVAudioUnitReverbPresetMediumHall2);
    PyModule_AddIntConstant(m, "REVERB_MEDIUM_HALL3",  (int)AVAudioUnitReverbPresetMediumHall3);
    PyModule_AddIntConstant(m, "REVERB_LARGE_HALL2",   (int)AVAudioUnitReverbPresetLargeHall2);

    /* Export distortion preset constants */
    PyModule_AddIntConstant(m, "DISTORTION_DRUMS_BIT_BRUSH",            (int)AVAudioUnitDistortionPresetDrumsBitBrush);
    PyModule_AddIntConstant(m, "DISTORTION_DRUMS_BUFFER_BEATS",         (int)AVAudioUnitDistortionPresetDrumsBufferBeats);
    PyModule_AddIntConstant(m, "DISTORTION_DRUMS_LOFI",                 (int)AVAudioUnitDistortionPresetDrumsLoFi);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_BROKEN_SPEAKER",       (int)AVAudioUnitDistortionPresetMultiBrokenSpeaker);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_CELLPHONE_CONCERT",    (int)AVAudioUnitDistortionPresetMultiCellphoneConcert);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_DECIMATED1",           (int)AVAudioUnitDistortionPresetMultiDecimated1);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_DECIMATED2",           (int)AVAudioUnitDistortionPresetMultiDecimated2);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_DECIMATED3",           (int)AVAudioUnitDistortionPresetMultiDecimated3);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_DECIMATED4",           (int)AVAudioUnitDistortionPresetMultiDecimated4);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_DISTORTED_FUNK",       (int)AVAudioUnitDistortionPresetMultiDistortedFunk);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_DISTORTED_CUBED",      (int)AVAudioUnitDistortionPresetMultiDistortedCubed);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_DISTORTED_SQUARED",    (int)AVAudioUnitDistortionPresetMultiDistortedSquared);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_ECHO1",                (int)AVAudioUnitDistortionPresetMultiEcho1);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_ECHO2",                (int)AVAudioUnitDistortionPresetMultiEcho2);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_ECHO_TIGHT1",          (int)AVAudioUnitDistortionPresetMultiEchoTight1);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_ECHO_TIGHT2",          (int)AVAudioUnitDistortionPresetMultiEchoTight2);
    PyModule_AddIntConstant(m, "DISTORTION_MULTI_EVERYTHING_IS_BROKEN", (int)AVAudioUnitDistortionPresetMultiEverythingIsBroken);
    PyModule_AddIntConstant(m, "DISTORTION_SPEECH_ALIEN_CHATTER",       (int)AVAudioUnitDistortionPresetSpeechAlienChatter);
    PyModule_AddIntConstant(m, "DISTORTION_SPEECH_COSMIC_INTERFERENCE", (int)AVAudioUnitDistortionPresetSpeechCosmicInterference);
    PyModule_AddIntConstant(m, "DISTORTION_SPEECH_GOLDEN_PI",           (int)AVAudioUnitDistortionPresetSpeechGoldenPi);
    PyModule_AddIntConstant(m, "DISTORTION_SPEECH_RADIO_TOWER",         (int)AVAudioUnitDistortionPresetSpeechRadioTower);
    PyModule_AddIntConstant(m, "DISTORTION_SPEECH_WAVES",               (int)AVAudioUnitDistortionPresetSpeechWaves);

    return m;
}

void registerAudioModule(void) {
    PyImport_AppendInittab("_audio", PyInit__audio);
}
