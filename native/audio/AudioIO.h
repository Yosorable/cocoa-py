#pragma once

static PyObject *audio_sound_pcm(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    auto sound = soundRecord(handle);
    if (!sound || !sound->buffer) { raiseAudioError(@"Sound is not decoded, is closed, or is a file Stream."); return nullptr; }
    if (raiseAudioError(sound->error)) return nullptr;
    auto buffer = sound->buffer;
    auto channels = buffer.format.channelCount;
    PyObject *data = PyBytes_FromStringAndSize(nullptr, (Py_ssize_t)buffer.frameLength * channels * sizeof(float));
    if (!data) return nullptr;
    float *out = (float *)PyBytes_AS_STRING(data);
    for (unsigned i = 0; i < buffer.frameLength; ++i)
        for (unsigned c = 0; c < channels; ++c) out[i * channels + c] = buffer.floatChannelData[c][i];
    return data;
}

static bool audioStatusError(OSStatus status, const char *operation) {
    if (!status) return false;
    PyErr_Format(PyExc_OSError, "%s (OSStatus %d).", operation, (int)status);
    return true;
}

static PyObject *audio_set_semitones(PyObject *, PyObject *args) {
    long long handle; float value;
    if (!PyArg_ParseTuple(args, "Lf", &handle, &value)) return nullptr;
    if (!std::isfinite(value) || value < -24 || value > 24) {
        PyErr_SetString(PyExc_ValueError, "semitones must be -24...24."); return nullptr;
    }
    std::lock_guard<std::mutex> lock(gMutex);
    auto ch = channelRecord(handle);
    if (ch && !ch->completed) { ch->semitones = value; ch->timePitch.pitch = value * 100; }
    Py_RETURN_NONE;
}

static PyObject *audio_input_info(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gRecorders.find(handle);
    if (it == gRecorders.end()) { PyErr_SetString(PyExc_RuntimeError, "Audio input is closed."); return nullptr; }
    auto rec = it->second;
    if (audioStatusError(rec->ioError, "Audio input failed; call stop() or close()")) return nullptr;
    return Py_BuildValue("{s:K,s:K,s:K,s:K,s:O}", "frames", rec->frames.load(),
        "available_frames", rec->ring ? rec->ring->available() : 0,
        "capacity", rec->ring ? rec->ring->capacity : 0,
        "dropped_frames", rec->droppedFrames.load(), "active", rec->recording ? Py_True : Py_False);
}

static PyObject *audio_input_read(PyObject *, PyObject *args) {
    long long handle; unsigned frames;
    if (!PyArg_ParseTuple(args, "LI", &handle, &frames)) return nullptr;
    if (!frames || frames > 65536) { PyErr_SetString(PyExc_ValueError, "frames must be 1...65536."); return nullptr; }
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gRecorders.find(handle);
    if (it == gRecorders.end() || !it->second->ring) {
        PyErr_SetString(PyExc_RuntimeError, "PCM input stream is closed."); return nullptr;
    }
    auto rec = it->second;
    if (audioStatusError(rec->ioError, "Audio input failed")) return nullptr;
    auto count = std::min<uint64_t>(frames, rec->ring->available());
    PyObject *data = PyBytes_FromStringAndSize(nullptr, count * rec->channels * sizeof(float));
    if (data) rec->ring->read((float *)PyBytes_AS_STRING(data), nullptr, count);
    return data;
}

static void closeOutput(const std::shared_ptr<OutputRecord> &rec) {
    rec->state->paused = true;
    @try {
        if (rec->node.engine) [rec->node.engine detachNode:rec->node];
        if (rec->mixer.engine) [rec->mixer.engine detachNode:rec->mixer];
    } @catch (NSException *exception) { }
    rec->node = nil; rec->mixer = nil;
}

static PyObject *audio_output_open(PyObject *, PyObject *args) {
    double rate, seconds; int channels, paused;
    if (!PyArg_ParseTuple(args, "didp", &rate, &channels, &seconds, &paused)) return nullptr;
    if (!validPCMFormat(rate, channels)) return nullptr;
    if (!std::isfinite(seconds) || seconds < 0.02 || seconds > 10) {
        PyErr_SetString(PyExc_ValueError, "buffer_duration must be 0.02...10 seconds."); return nullptr;
    }
    auto rec = std::make_shared<OutputRecord>();
    auto state = std::make_shared<OutputState>();
    state->ring = std::make_shared<AudioRing>((uint64_t)ceil(rate * seconds), channels);
    state->paused = paused;
    rec->state = state; rec->sampleRate = rate;
    @autoreleasepool {
        std::lock_guard<std::mutex> lock(gMutex);
        if (!ensureEngine()) { raiseAudioError(@"Audio engine unavailable."); return nullptr; }
        @try {
            AVAudioFormat *format = [[AVAudioFormat alloc] initStandardFormatWithSampleRate:rate channels:channels];
            rec->node = [[AVAudioSourceNode alloc] initWithFormat:format renderBlock:
                ^OSStatus(BOOL *silence, const AudioTimeStamp *, AVAudioFrameCount count, AudioBufferList *buffers) {
                    for (unsigned c = 0; c < buffers->mNumberBuffers; ++c)
                        memset(buffers->mBuffers[c].mData, 0, buffers->mBuffers[c].mDataByteSize);
                    uint64_t read = state->paused ? 0 : state->ring->read(nullptr, buffers, count);
                    if (!state->paused && read < count) state->underruns.fetch_add(count - read, std::memory_order_relaxed);
                    *silence = read == 0;
                    return noErr;
                }];
            rec->mixer = [AVAudioMixerNode new];
            [gEngine attachNode:rec->node]; [gEngine attachNode:rec->mixer];
            [gEngine connect:rec->node to:rec->mixer format:format];
            [gEngine connect:rec->mixer to:gMixer format:format];
            rec->mixer.outputVolume = gMasterVolume;
            if (!paused && !gInterrupted) {
                NSString *error = restartAudioEngine();
                if (error) { closeOutput(rec); raiseAudioError(error); return nullptr; }
            }
        } @catch (NSException *exception) {
            closeOutput(rec); raiseAudioError(exception.reason); return nullptr;
        }
        long long handle = nextHandle();
        PyObject *result = PyLong_FromLongLong(handle);
        if (!result) { closeOutput(rec); return nullptr; }
        gOutputs.emplace(handle, rec);
        return result;
    }
}

static PyObject *audio_output_write(PyObject *, PyObject *args) {
    long long handle; Py_buffer data;
    if (!PyArg_ParseTuple(args, "Ly*", &handle, &data)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gOutputs.find(handle);
    if (it == gOutputs.end()) {
        PyBuffer_Release(&data); PyErr_SetString(PyExc_RuntimeError, "PCM output stream is closed."); return nullptr;
    }
    auto ring = it->second->state->ring;
    if (data.len % (ring->channels * sizeof(float))) {
        PyBuffer_Release(&data); PyErr_SetString(PyExc_ValueError, "PCM must contain complete float32 frames."); return nullptr;
    }
    const float *samples = (const float *)data.buf;
    for (Py_ssize_t i = 0; i < data.len / sizeof(float); ++i) {
        if (!std::isfinite(samples[i])) {
            PyBuffer_Release(&data); PyErr_SetString(PyExc_ValueError, "PCM samples must be finite."); return nullptr;
        }
    }
    uint64_t count = ring->write(samples, nullptr, data.len / (ring->channels * sizeof(float)));
    PyBuffer_Release(&data);
    return PyLong_FromUnsignedLongLong(count);
}

static PyObject *audio_output_info(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gOutputs.find(handle);
    if (it == gOutputs.end()) { PyErr_SetString(PyExc_RuntimeError, "PCM output stream is closed."); return nullptr; }
    auto rec = it->second;
    return Py_BuildValue("{s:K,s:K,s:K,s:O}", "buffered_frames", rec->state->ring->available(),
        "capacity", rec->state->ring->capacity, "underrun_frames", rec->state->underruns.load(),
        "paused", rec->state->paused ? Py_True : Py_False);
}

static PyObject *audio_output_pause(PyObject *, PyObject *args) {
    long long handle; int paused;
    if (!PyArg_ParseTuple(args, "Lp", &handle, &paused)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gOutputs.find(handle);
    if (it != gOutputs.end()) {
        if (!paused && !gInterrupted && raiseAudioError(restartAudioEngine())) return nullptr;
        it->second->state->paused = paused;
    }
    Py_RETURN_NONE;
}

static PyObject *audio_output_close(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gOutputs.find(handle);
    if (it != gOutputs.end()) { closeOutput(it->second); gOutputs.erase(it); }
    Py_RETURN_NONE;
}

static std::unordered_map<long long, std::shared_ptr<AudioFileWriter>> gWriters;

// A capsule owns access independently of the engine and of any open file.
// Python holds it across path checks, rendering and writer finalization.
static constexpr const char *kAudioFileAccessCapsule = "cocoa-py.audio.file_access";

static void destroyAudioFileAccess(PyObject *capsule) {
    delete static_cast<AudioFileAccess *>(PyCapsule_GetPointer(capsule, kAudioFileAccessCapsule));
}

static PyObject *audio_file_access_open(PyObject *, PyObject *args) {
    const char *path;
    if (!PyArg_ParseTuple(args, "s", &path)) return nullptr;
    @autoreleasepool {
        @try {
            auto access = std::make_unique<AudioFileAccess>([NSURL fileURLWithPath:resolveAudioPath(path)]);
            PyObject *capsule = PyCapsule_New(access.get(), kAudioFileAccessCapsule, destroyAudioFileAccess);
            if (capsule) access.release();
            return capsule;
        } @catch (NSException *exception) { raiseAudioError(exception.reason); return nullptr; }
    }
}

static PyObject *audio_file_access_close(PyObject *, PyObject *capsule) {
    auto access = static_cast<AudioFileAccess *>(PyCapsule_GetPointer(capsule, kAudioFileAccessCapsule));
    if (!access) return nullptr;
    access->close();
    Py_RETURN_NONE;
}

static PyObject *audio_writer_open(PyObject *, PyObject *args) {
    const char *path; double rate; int channels, overwrite;
    if (!PyArg_ParseTuple(args, "sdip", &path, &rate, &channels, &overwrite)) return nullptr;
    if (!validPCMFormat(rate, channels)) return nullptr;
    @autoreleasepool {
        auto writer = std::make_shared<AudioFileWriter>();
        AVAudioFormat *format = [[AVAudioFormat alloc] initWithCommonFormat:AVAudioPCMFormatFloat32
                                                       sampleRate:rate channels:channels interleaved:YES];
        OSStatus status = writer->open([NSURL fileURLWithPath:resolveAudioPath(path)], format, overwrite, false);
        if (audioStatusError(status, "Cannot create audio file")) return nullptr;
        auto handle = nextHandle();
        PyObject *result = PyLong_FromLongLong(handle);
        if (!result) return nullptr;
        std::lock_guard<std::mutex> lock(gMutex);
        gWriters.emplace(handle, writer);
        return result;
    }
}

static PyObject *audio_writer_write(PyObject *, PyObject *args) {
    long long handle; Py_buffer data;
    if (!PyArg_ParseTuple(args, "Ly*", &handle, &data)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gWriters.find(handle);
    if (it == gWriters.end()) { PyBuffer_Release(&data); raiseAudioError(@"Audio file is closed."); return nullptr; }
    auto writer = it->second;
    if (data.len % (writer->channels * sizeof(float)) || data.len > 65536 * writer->channels * sizeof(float)) {
        PyBuffer_Release(&data); PyErr_SetString(PyExc_ValueError, "Write at most 65536 complete float32 frames at a time."); return nullptr;
    }
    AudioBufferList list = {1, {{writer->channels, (UInt32)data.len, data.buf}}};
    OSStatus status = writer->write((UInt32)(data.len / (writer->channels * sizeof(float))), &list, false);
    PyBuffer_Release(&data);
    if (audioStatusError(status, "Audio file write failed")) return nullptr;
    Py_RETURN_NONE;
}

static PyObject *audio_writer_close(PyObject *, PyObject *args) {
    long long handle;
    if (!PyArg_ParseTuple(args, "L", &handle)) return nullptr;
    std::lock_guard<std::mutex> lock(gMutex);
    auto it = gWriters.find(handle);
    if (it != gWriters.end()) {
        auto writer = it->second; gWriters.erase(it);
        if (audioStatusError(writer->close(), "Cannot finalize audio file")) return nullptr;
    }
    Py_RETURN_NONE;
}
