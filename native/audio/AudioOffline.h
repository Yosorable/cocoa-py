#pragma once

struct OfflineEffects {
    bool reverb = false, delay = false, distortion = false, eq = false;
    double reverbMix = 0, delayTime = 0, feedback = 0, delayMix = 0, distortionMix = 0;
    double low = 0, mid = 0, high = 0;
    int reverbPreset = 0, distortionPreset = 0;
};

static bool inAudioRange(double value, double low, double high) {
    return std::isfinite(value) && value >= low && value <= high;
}

static bool parseOfflineEffects(PyObject *value, OfflineEffects &effects) {
    if (!PyTuple_Check(value) || PyTuple_GET_SIZE(value) != 4) {
        PyErr_SetString(PyExc_ValueError, "Invalid audio effect specification."); return false;
    }
    PyObject *r = PyTuple_GET_ITEM(value, 0), *d = PyTuple_GET_ITEM(value, 1);
    PyObject *dist = PyTuple_GET_ITEM(value, 2), *eq = PyTuple_GET_ITEM(value, 3);
    effects.reverb = r != Py_None; effects.delay = d != Py_None;
    effects.distortion = dist != Py_None; effects.eq = eq != Py_None;
    if (effects.reverb && !PyArg_ParseTuple(r, "di", &effects.reverbMix, &effects.reverbPreset)) return false;
    if (effects.delay && !PyArg_ParseTuple(d, "ddd", &effects.delayTime, &effects.feedback, &effects.delayMix)) return false;
    if (effects.distortion && !PyArg_ParseTuple(dist, "id", &effects.distortionPreset, &effects.distortionMix)) return false;
    if (effects.eq && !PyArg_ParseTuple(eq, "ddd", &effects.low, &effects.mid, &effects.high)) return false;
    if (!inAudioRange(effects.reverbMix, 0, 100) || effects.reverbPreset < 0 || effects.reverbPreset > 12
        || !inAudioRange(effects.delayTime, 0, 2) || !inAudioRange(effects.feedback, -100, 100)
        || !inAudioRange(effects.delayMix, 0, 100) || !inAudioRange(effects.distortionMix, 0, 100)
        || effects.distortionPreset < 0 || effects.distortionPreset > 21
        || !inAudioRange(effects.low, -96, 24) || !inAudioRange(effects.mid, -96, 24) || !inAudioRange(effects.high, -96, 24)) {
        PyErr_SetString(PyExc_ValueError, "Audio effect parameter is outside its supported range."); return false;
    }
    return true;
}

static AVAudioNode *connectOfflineEffects(AVAudioEngine *engine, AVAudioNode *previous,
                                          AVAudioFormat *format, const OfflineEffects &spec) {
    auto append = [&](AVAudioNode *node) {
        [engine attachNode:node]; [engine connect:previous to:node format:format]; previous = node;
    };
    if (spec.reverb) {
        AVAudioUnitReverb *node = [AVAudioUnitReverb new];
        [node loadFactoryPreset:(AVAudioUnitReverbPreset)spec.reverbPreset]; node.wetDryMix = spec.reverbMix; append(node);
    }
    if (spec.delay) {
        AVAudioUnitDelay *node = [AVAudioUnitDelay new];
        node.delayTime = spec.delayTime; node.feedback = spec.feedback; node.wetDryMix = spec.delayMix; append(node);
    }
    if (spec.distortion) {
        AVAudioUnitDistortion *node = [AVAudioUnitDistortion new];
        [node loadFactoryPreset:(AVAudioUnitDistortionPreset)spec.distortionPreset]; node.wetDryMix = spec.distortionMix; append(node);
    }
    if (spec.eq) {
        AVAudioUnitEQ *node = [[AVAudioUnitEQ alloc] initWithNumberOfBands:3];
        double frequencies[] = {100, 1000, std::min(8000.0, format.sampleRate * 0.4)};
        double gains[] = {spec.low, spec.mid, spec.high};
        for (unsigned i = 0; i < 3; ++i) {
            node.bands[i].filterType = AVAudioUnitEQFilterTypeParametric;
            node.bands[i].frequency = frequencies[i]; node.bands[i].bandwidth = 1;
            node.bands[i].gain = gains[i]; node.bands[i].bypass = NO;
        }
        append(node);
    }
    return previous;
}

struct OfflineTrack {
    std::shared_ptr<AudioFileAccess> access;
    __strong AVAudioFile *file = nil; // released before external access
    __strong AVAudioPCMBuffer *source = nil;
    __strong AVAudioPlayerNode *player = nil;
    __strong AVAudioFormat *format = nil;
    __strong AVAudioFormat *playbackFormat = nil;
    double start = 0, rate = 1;
    float leftGain = 1, rightGain = 1;
    AVAudioFramePosition offset = 0, length = 0, cursor = 0, scheduled = 0, limit = 0;
    bool loop = false;
};

struct OfflineRender {
    __strong AVAudioEngine *engine = nil;
    __strong AVAudioPCMBuffer *buffer = nil;
    std::vector<OfflineTrack> tracks;
    double sampleRate = 0;
    unsigned channels = 0;
    AVAudioFramePosition frames = 0, cursor = 0;
    std::mutex mutex;
    __strong NSString *error = nil;
    ~OfflineRender() { [engine stop]; }

    bool fill() {
        const auto nextCursor = std::min<AVAudioFramePosition>(cursor + 4096, frames);
        for (auto &track : tracks) {
            double sourceRate = track.format.sampleRate;
            // Supply the entire next output block, plus source-frame headroom for
            // conversion/TimePitch. No more buffers can be queued during renderOffline().
            auto wanted = (AVAudioFramePosition)std::ceil(std::max(0.0, nextCursor / sampleRate - track.start)
                                                         * sourceRate * track.rate) + 12288;
            while (track.scheduled < wanted && track.scheduled < track.limit) {
                if (track.cursor == track.length) {
                    if (!track.loop) break;
                    track.cursor = 0;
                }
                auto count = (AVAudioFrameCount)std::min({(AVAudioFramePosition)4096, track.length - track.cursor, track.limit - track.scheduled});
                AVAudioPCMBuffer *chunk = [[AVAudioPCMBuffer alloc] initWithPCMFormat:track.format frameCapacity:count];
                if (!chunk) { error = @"Cannot allocate offline audio buffer."; return false; }
                if (track.file) {
                    track.file.framePosition = track.offset + track.cursor;
                    NSError *readError = nil;
                    if (![track.file readIntoBuffer:chunk frameCount:count error:&readError] || chunk.frameLength != count) {
                        error = readError.localizedDescription ?: @"Unexpected end of audio file during export."; return false;
                    }
                } else {
                    chunk.frameLength = count;
                    for (unsigned c = 0; c < track.format.channelCount; ++c)
                        memcpy(chunk.floatChannelData[c], track.source.floatChannelData[c] + track.offset + track.cursor, count * sizeof(float));
                }
                AVAudioPCMBuffer *stereo = [[AVAudioPCMBuffer alloc] initWithPCMFormat:track.playbackFormat frameCapacity:count];
                if (!stereo) { error = @"Cannot allocate offline stereo buffer."; return false; }
                stereo.frameLength = count;
                const float *left = chunk.floatChannelData[0];
                const float *right = chunk.floatChannelData[track.format.channelCount == 1 ? 0 : 1];
                for (unsigned i = 0; i < count; ++i) {
                    stereo.floatChannelData[0][i] = left[i] * track.leftGain;
                    stereo.floatChannelData[1][i] = right[i] * track.rightGain;
                }
                AVAudioTime *time = track.scheduled == 0 ? [AVAudioTime timeWithSampleTime:
                    (AVAudioFramePosition)llround(track.start * sourceRate * track.rate) atRate:sourceRate] : nil;
                [track.player scheduleBuffer:stereo atTime:time options:0 completionHandler:nil];
                track.cursor += count; track.scheduled += count;
            }
        }
        return true;
    }
};

static std::unordered_map<long long, std::shared_ptr<OfflineRender>> gRenders;

static PyObject *audio_render_open(PyObject *, PyObject *args) {
    PyObject *specs, *effectSpec; double sampleRate, duration, tail; int channels;
    if (!PyArg_ParseTuple(args, "OdiddO", &specs, &sampleRate, &channels, &duration, &tail, &effectSpec)) return nullptr;
    if (!validPCMFormat(sampleRate, channels)) return nullptr;
    if (!PyList_Check(specs) || PyList_GET_SIZE(specs) < 1 || PyList_GET_SIZE(specs) > 64
        || !(duration == -1 || inAudioRange(duration, 0.00001, 86400)) || !inAudioRange(tail, 0, 60)) {
        PyErr_SetString(PyExc_ValueError, "Provide 1...64 tracks, duration up to 24 hours, and tail 0...60 seconds."); return nullptr;
    }
    OfflineEffects masterEffects;
    if (!parseOfflineEffects(effectSpec, masterEffects)) return nullptr;
    auto render = std::make_shared<OfflineRender>();
    render->sampleRate = sampleRate; render->channels = channels;
    @autoreleasepool {
        std::lock_guard<std::mutex> lock(gMutex);
        @try {
            render->engine = [AVAudioEngine new];
            AVAudioEngine *engine = render->engine;
            AVAudioFormat *stereo = [[AVAudioFormat alloc] initStandardFormatWithSampleRate:sampleRate channels:2];
            // Render stereo and explicitly average for mono output. The system's
            // equal-power downmix otherwise boosts duplicated mono inputs by 3 dB.
            AVAudioFormat *output = stereo;
            double end = 0;
            for (Py_ssize_t i = 0; i < PyList_GET_SIZE(specs); ++i) {
                PyObject *source, *effectsObject;
                double start, offset, length, volume, pan, rate, semitones; int loop;
                if (!PyArg_ParseTuple(PyList_GET_ITEM(specs, i), "OdddddddpO", &source, &start, &offset,
                    &length, &volume, &pan, &rate, &semitones, &loop, &effectsObject)) return nullptr;
                if (!inAudioRange(start, 0, 86400) || !inAudioRange(offset, 0, 1e12)
                    || !(length == -1 || inAudioRange(length, 0.00001, 86400))
                    || !inAudioRange(volume, 0, 1) || !inAudioRange(pan, -1, 1)
                    || !inAudioRange(rate, 0.25, 4) || !inAudioRange(semitones, -24, 24) || (loop && duration < 0)) {
                    PyErr_SetString(PyExc_ValueError, "Invalid track timing or playback parameter; looping requires render duration."); return nullptr;
                }
                OfflineEffects effects;
                if (!parseOfflineEffects(effectsObject, effects)) return nullptr;
                OfflineTrack track;
                NSURL *url = nil;
                if (PyUnicode_Check(source)) {
                    const char *path = PyUnicode_AsUTF8(source); if (!path) return nullptr;
                    url = [NSURL fileURLWithPath:resolveAudioPath(path)];
                    track.access = std::make_shared<AudioFileAccess>(url);
                } else {
                    long long handle = PyLong_AsLongLong(source); if (PyErr_Occurred()) return nullptr;
                    SoundRecord *sound = soundRecord(handle);
                    if (!sound) { PyErr_SetString(PyExc_ValueError, "Sound is closed."); return nullptr; }
                    if (raiseAudioError(sound->error)) return nullptr;
                    if (!sound->loaded) { raiseAudioError(@"Wait for Sound.loaded before rendering."); return nullptr; }
                    track.source = sound->buffer; url = sound->streamURL; track.access = sound->fileAccess;
                }
                if (url) {
                    NSError *error = nil;
                    track.file = [[AVAudioFile alloc] initForReading:url commonFormat:AVAudioPCMFormatFloat32 interleaved:NO error:&error];
                    if (!track.file) { raiseAudioError(error.localizedDescription); return nullptr; }
                }
                track.format = track.file ? track.file.processingFormat : track.source.format;
                auto total = track.file ? track.file.length : track.source.frameLength;
                if (!total || !validPCMFormat(track.format.sampleRate, (int)track.format.channelCount)) {
                    if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "Audio source is empty."); return nullptr;
                }
                if (offset >= total / track.format.sampleRate) {
                    PyErr_SetString(PyExc_ValueError, "Track offset is outside the source."); return nullptr;
                }
                track.offset = (AVAudioFramePosition)llround(offset * track.format.sampleRate);
                track.length = total - track.offset;
                if (length >= 0) track.length = std::min(track.length, (AVAudioFramePosition)llround(length * track.format.sampleRate));
                if (track.length <= 0) { PyErr_SetString(PyExc_ValueError, "Track contains no frames."); return nullptr; }
                track.start = start; track.rate = rate; track.loop = loop;
                track.leftGain = volume * (pan > 0 ? 1 - pan : 1);
                track.rightGain = volume * (pan < 0 ? 1 + pan : 1);
                track.playbackFormat = [[AVAudioFormat alloc] initStandardFormatWithSampleRate:track.format.sampleRate channels:2];
                track.limit = duration < 0 ? track.length : (AVAudioFramePosition)llround(std::max(0.0, duration - start) * track.format.sampleRate * rate);
                if (!loop) track.limit = std::min(track.limit, track.length);
                end = std::max(end, start + track.length / track.format.sampleRate / rate);
                track.player = [AVAudioPlayerNode new]; [engine attachNode:track.player];
                AVAudioNode *previous = track.player;
                if (rate != 1 || semitones != 0) {
                    AVAudioUnitTimePitch *node = [AVAudioUnitTimePitch new]; node.rate = rate; node.pitch = semitones * 100;
                    [engine attachNode:node]; [engine connect:previous to:node format:track.playbackFormat]; previous = node;
                }
                AVAudioMixerNode *mixer = [AVAudioMixerNode new]; [engine attachNode:mixer];
                [engine connect:previous to:mixer format:track.playbackFormat];
                previous = connectOfflineEffects(engine, mixer, stereo, effects);
                [engine connect:previous to:engine.mainMixerNode format:stereo];
                render->tracks.push_back(std::move(track));
            }
            double seconds = (duration < 0 ? end : duration) + tail;
            if (!inAudioRange(seconds, 0.00001, 86460)) { PyErr_SetString(PyExc_ValueError, "Render duration is too large."); return nullptr; }
            render->frames = std::max<AVAudioFramePosition>(1, llround(seconds * sampleRate));
            AVAudioNode *last = connectOfflineEffects(engine, engine.mainMixerNode, stereo, masterEffects);
            AVAudioMixerNode *outputMixer = [AVAudioMixerNode new]; [engine attachNode:outputMixer];
            [engine connect:last to:outputMixer format:stereo];
            [engine connect:outputMixer to:engine.outputNode format:output];
            NSError *error = nil;
            if (![engine enableManualRenderingMode:AVAudioEngineManualRenderingModeOffline format:output maximumFrameCount:4096 error:&error]
                || ![engine startAndReturnError:&error]) { raiseAudioError(error.localizedDescription); return nullptr; }
            render->buffer = [[AVAudioPCMBuffer alloc] initWithPCMFormat:output frameCapacity:4096];
            if (!render->buffer || !render->fill()) { raiseAudioError(render->error ?: @"Cannot allocate render buffer."); return nullptr; }
            for (auto &track : render->tracks) [track.player play];
        } @catch (NSException *exception) { raiseAudioError(exception.reason); return nullptr; }
        long long handle = nextHandle();
        PyObject *result = PyLong_FromLongLong(handle);
        if (!result) return nullptr;
        gRenders.emplace(handle, render);
        return result;
    }
}

static PyObject *audio_render_read(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::shared_ptr<OfflineRender> render;
    {
        std::lock_guard<std::mutex> lock(gMutex);
        auto it = gRenders.find(handle);
        if (it == gRenders.end()) { raiseAudioError(@"Offline renderer is closed."); return nullptr; }
        render = it->second;
    }
    std::unique_lock<std::mutex> lock(render->mutex, std::try_to_lock);
    if (!lock.owns_lock()) { raiseAudioError(@"Offline renderer is already reading."); return nullptr; }
    if (raiseAudioError(render->error)) return nullptr;
    if (render->cursor == render->frames) return PyBytes_FromStringAndSize(nullptr, 0);
    auto count = (AVAudioFrameCount)std::min<AVAudioFramePosition>(4096, render->frames - render->cursor);
    PyObject *result = PyBytes_FromStringAndSize(nullptr, count * render->channels * sizeof(float));
    if (!result) return nullptr;
    float *out = (float *)PyBytes_AS_STRING(result);
    // Return to Python after every block so Stop and generator finally blocks work.
    Py_BEGIN_ALLOW_THREADS
    @autoreleasepool {
        @try {
            if (render->fill()) {
                NSError *error = nil;
                unsigned attempts = 0;
                AVAudioEngineManualRenderingStatus status;
                do {
                    status = [render->engine renderOffline:count toBuffer:render->buffer error:&error];
                } while (status == AVAudioEngineManualRenderingStatusCannotDoInCurrentContext && ++attempts < 32);
                if (status != AVAudioEngineManualRenderingStatusSuccess || render->buffer.frameLength != count)
                    render->error = error.localizedDescription ?: @"Offline rendering did not produce the requested frames.";
                else {
                    for (unsigned i = 0; i < count; ++i)
                        for (unsigned c = 0; c < render->channels; ++c)
                            out[i * render->channels + c] = render->channels == 1
                                ? (render->buffer.floatChannelData[0][i] + render->buffer.floatChannelData[1][i]) * 0.5f
                                : render->buffer.floatChannelData[c][i];
                    render->cursor += count;
                }
            }
        } @catch (NSException *exception) { render->error = exception.reason; }
    }
    Py_END_ALLOW_THREADS
    if (raiseAudioError(render->error)) { Py_DECREF(result); return nullptr; }
    return result;
}

static PyObject *audio_render_close(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    gRenders.erase(handle);
    Py_RETURN_NONE;
}
