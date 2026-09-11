#pragma once

// Included after the channel helpers. All queue access is protected by gMutex;
// producers store only native state, and Python objects are built when polled.
enum class AudioEventKind { PlaybackEnded, Interruption, RouteChanged, ConfigurationChanged, EngineError };
struct AudioEvent {
    AudioEventKind kind = AudioEventKind::PlaybackEnded;
    double timestamp = 0;
    long long channel = 0;
    const char *detail = "";
    __strong NSString *error = nil;
    unsigned reason = 0;
    bool flag = false;
};
static constexpr size_t kAudioEventCapacity = 256;
static std::array<AudioEvent, kAudioEventCapacity> gAudioEvents;
static size_t gAudioEventHead = 0, gAudioEventCount = 0;
static unsigned long long gAudioEventsDropped = 0, gAudioEpoch = 0;

static void clearAudioEvents() {
    for (auto &event : gAudioEvents) event = AudioEvent{};
    gAudioEventHead = gAudioEventCount = 0;
    gAudioEventsDropped = 0;
}

static void pushAudioEvent(AudioEventKind kind, long long channel = 0, const char *detail = "",
                           NSString *error = nil, unsigned reason = 0, bool flag = false) {
    if (gAudioEventCount == kAudioEventCapacity) {
        gAudioEventHead = (gAudioEventHead + 1) % kAudioEventCapacity;
        --gAudioEventCount;
        ++gAudioEventsDropped;
    }
    auto &event = gAudioEvents[(gAudioEventHead + gAudioEventCount++) % kAudioEventCapacity];
    event = AudioEvent{kind, CACurrentMediaTime(), channel, detail, error, reason, flag};
}

static const char *channelState(ChannelRecord *ch) {
    if (!ch) return "stopped";
    if (ch->error) return "failed";
    if (ch->completed) return ch->stopped ? "stopped" : "finished";
    if (ch->paused) return "paused";
    if (!channelReady(ch)) return "loading";
    if (gInterrupted || ch->resumePending || !gEngine.isRunning) return "interrupted";
    // Completion is acknowledged by the generation-checked callback, so a brief
    // gap before that callback cannot make wait() return prematurely.
    return "playing";
}

static PyObject *audio_channel_status(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    auto ch = channelRecord(handle);
    return Py_BuildValue("{s:s,s:z}", "state", channelState(ch),
                         "error", ch && ch->error ? ch->error.UTF8String : nullptr);
}

static PyObject *audioEventObject(const AudioEvent &event) {
    switch (event.kind) {
        case AudioEventKind::PlaybackEnded:
            return Py_BuildValue("{s:s,s:d,s:L,s:s,s:z}",
                "type", "playback_ended", "timestamp", event.timestamp, "channel", event.channel,
                "reason", event.detail, "error", event.error ? event.error.UTF8String : nullptr);
        case AudioEventKind::Interruption:
            return Py_BuildValue("{s:s,s:d,s:s,s:O}", "type", "interruption", "timestamp", event.timestamp,
                "phase", event.detail, "should_resume", event.flag ? Py_True : Py_False);
        case AudioEventKind::RouteChanged:
            return Py_BuildValue("{s:s,s:d,s:I}", "type", "route_changed", "timestamp", event.timestamp,
                "reason", event.reason);
        case AudioEventKind::ConfigurationChanged:
            return Py_BuildValue("{s:s,s:d,s:O}", "type", "configuration_changed", "timestamp", event.timestamp,
                "engine_running", event.flag ? Py_True : Py_False);
        case AudioEventKind::EngineError:
            return Py_BuildValue("{s:s,s:d,s:z}", "type", "engine_error", "timestamp", event.timestamp,
                "error", event.error ? event.error.UTF8String : nullptr);
    }
    PyErr_SetString(PyExc_SystemError, "Unknown audio event.");
    return nullptr;
}

static PyObject *audio_get_events(PyObject *, PyObject *args) {
    int limit;
    if (!PyArg_ParseTuple(args, "i", &limit)) return nullptr;
    if (limit < 1 || limit > (int)kAudioEventCapacity) {
        PyErr_SetString(PyExc_ValueError, "max_events must be 1...256."); return nullptr;
    }
    std::lock_guard<std::mutex> lock(gMutex);
    size_t overflow = gAudioEventsDropped ? 1 : 0;
    size_t count = std::min(gAudioEventCount, (size_t)limit - overflow);
    PyObject *result = PyList_New(count + overflow);
    if (!result) return nullptr;
    if (overflow) {
        PyObject *event = Py_BuildValue("{s:s,s:d,s:K}", "type", "overflow", "timestamp", CACurrentMediaTime(),
                                        "dropped", gAudioEventsDropped);
        if (!event) { Py_DECREF(result); return nullptr; }
        PyList_SET_ITEM(result, 0, event);
    }
    for (size_t i = 0; i < count; ++i) {
        PyObject *event = audioEventObject(gAudioEvents[(gAudioEventHead + i) % kAudioEventCapacity]);
        if (!event) { Py_DECREF(result); return nullptr; }
        PyList_SET_ITEM(result, i + overflow, event);
    }
    // Consume only after every Python allocation succeeded.
    for (size_t i = 0; i < count; ++i) gAudioEvents[(gAudioEventHead + i) % kAudioEventCapacity] = AudioEvent{};
    gAudioEventHead = (gAudioEventHead + count) % kAudioEventCapacity;
    gAudioEventCount -= count;
    gAudioEventsDropped = 0;
    return result;
}

#if COCOA_PY_AUDIO_SESSION
static PyObject *audioPorts(NSArray<AVAudioSessionPortDescription *> *ports) {
    PyObject *result = PyList_New(ports.count);
    if (!result) return nullptr;
    NSUInteger index = 0;
    for (AVAudioSessionPortDescription *port in ports) {
        PyObject *value = Py_BuildValue("{s:s,s:s,s:n}", "name", port.portName.UTF8String ?: "",
            "type", port.portType.UTF8String ?: "", "channels", (Py_ssize_t)port.channels.count);
        if (!value) { Py_DECREF(result); return nullptr; }
        PyList_SET_ITEM(result, index++, value);
    }
    return result;
}

static PyObject *audio_get_device_info(PyObject *, PyObject *) {
    @autoreleasepool {
        std::lock_guard<std::mutex> lock(gMutex);
        @try {
            // Read-only: do not activate the session, create an engine, or ask permission.
            AVAudioSession *session = [AVAudioSession sharedInstance];
            auto permission = [AVAudioApplication sharedInstance].recordPermission;
            const char *permissionName = permission == AVAudioApplicationRecordPermissionGranted ? "granted"
                : permission == AVAudioApplicationRecordPermissionDenied ? "denied"
                : permission == AVAudioApplicationRecordPermissionUndetermined ? "not_determined" : "unknown";
            AVAudioSessionRouteDescription *route = session.currentRoute;
            PyObject *inputs = audioPorts(route.inputs);
            if (!inputs) return nullptr;
            PyObject *outputs = audioPorts(route.outputs);
            if (!outputs) { Py_DECREF(inputs); return nullptr; }
            return Py_BuildValue("{s:d,s:n,s:n,s:d,s:d,s:d,s:O,s:O,s:O,s:s,s:N,s:N}",
                "sample_rate", session.sampleRate,
                "input_channels", (Py_ssize_t)session.inputNumberOfChannels,
                "output_channels", (Py_ssize_t)session.outputNumberOfChannels,
                "input_latency", session.inputLatency, "output_latency", session.outputLatency,
                "io_buffer_duration", session.IOBufferDuration,
                "input_available", session.isInputAvailable ? Py_True : Py_False,
                "engine_running", gEngine.isRunning ? Py_True : Py_False,
                "interrupted", gInterrupted ? Py_True : Py_False,
                "microphone_permission", permissionName, "inputs", inputs, "outputs", outputs);
        } @catch (NSException *exception) {
            raiseAudioError(exception.reason ?: @"Cannot query audio device information.");
            return nullptr;
        }
    }
}
#else
#include "AudioMacDevices.h"
#endif
