#pragma once

#include "../common/CocoaPlatform.h"

using AudioFileAccess = CocoaPyFileAccess;

// One producer and one consumer. Python callers are serialized by gMutex;
// the audio callback never takes a lock, allocates, or calls Python.
struct AudioRing {
    const uint64_t capacity;
    const unsigned channels;
    std::vector<float> samples;
    std::atomic<uint64_t> readIndex{0}, writeIndex{0};
    AudioRing(uint64_t frames, unsigned ch) : capacity(frames), channels(ch), samples(frames * ch) { }
    uint64_t available() const {
        auto r = readIndex.load(std::memory_order_acquire);
        auto w = writeIndex.load(std::memory_order_acquire);
        return std::min(w - r, capacity);
    }
    uint64_t write(const float *interleaved, const float *const *planar, uint64_t frames) {
        auto w = writeIndex.load(std::memory_order_relaxed);
        auto r = readIndex.load(std::memory_order_acquire);
        frames = std::min(frames, capacity - (w - r));
        for (uint64_t i = 0; i < frames; ++i)
            for (unsigned c = 0; c < channels; ++c)
                samples[((w + i) % capacity) * channels + c] =
                    planar ? planar[c][i] : interleaved[i * channels + c];
        writeIndex.store(w + frames, std::memory_order_release);
        return frames;
    }
    uint64_t read(float *interleaved, AudioBufferList *planar, uint64_t frames) {
        auto r = readIndex.load(std::memory_order_relaxed);
        auto w = writeIndex.load(std::memory_order_acquire);
        frames = std::min(frames, w - r);
        for (uint64_t i = 0; i < frames; ++i)
            for (unsigned c = 0; c < channels; ++c) {
                float value = samples[((r + i) % capacity) * channels + c];
                if (planar) ((float *)planar->mBuffers[c].mData)[i] = value;
                else interleaved[i * channels + c] = value;
            }
        readIndex.store(r + frames, std::memory_order_release);
        return frames;
    }
};

struct AudioFileWriter {
    std::shared_ptr<AudioFileAccess> access;
    ExtAudioFileRef file = nullptr;
    bool wav = false;
    unsigned channels = 0;
    uint64_t frames = 0;
    OSStatus close() {
        auto value = std::exchange(file, nullptr);
        OSStatus status = value ? ExtAudioFileDispose(value) : noErr;
        access.reset();
        return status;
    }
    ~AudioFileWriter() { close(); }
    OSStatus open(NSURL *url, AVAudioFormat *client, bool overwrite, bool async) {
        access = std::make_shared<AudioFileAccess>(url);
        channels = client.channelCount;
        NSString *ext = url.pathExtension.lowercaseString;
        AudioFileTypeID type;
        AudioStreamBasicDescription format = {};
        format.mSampleRate = client.sampleRate;
        format.mChannelsPerFrame = channels;
        if ([ext isEqualToString:@"m4a"]) {
            type = kAudioFileM4AType;
            format.mFormatID = kAudioFormatMPEG4AAC;
            UInt32 size = sizeof(format);
            OSStatus status = AudioFormatGetProperty(kAudioFormatProperty_FormatInfo, 0, nullptr, &size, &format);
            if (status) return status;
        } else {
            wav = [ext isEqualToString:@"wav"];
            if (!wav && ![ext isEqualToString:@"caf"]) return kAudioFileUnsupportedFileTypeError;
            type = wav ? kAudioFileWAVEType : kAudioFileCAFType;
            format.mFormatID = kAudioFormatLinearPCM;
            format.mFormatFlags = kAudioFormatFlagIsPacked | (wav ? kAudioFormatFlagIsSignedInteger : kAudioFormatFlagIsFloat);
            format.mBitsPerChannel = wav ? 16 : 32;
            format.mBytesPerFrame = channels * format.mBitsPerChannel / 8;
            format.mFramesPerPacket = 1;
            format.mBytesPerPacket = format.mBytesPerFrame;
        }
        OSStatus status = ExtAudioFileCreateWithURL((__bridge CFURLRef)url, type, &format, nullptr,
                                                    overwrite ? kAudioFileFlags_EraseFile : 0, &file);
        if (!status) status = ExtAudioFileSetProperty(file, kExtAudioFileProperty_ClientDataFormat,
                                                    sizeof(AudioStreamBasicDescription), client.streamDescription);
        // Prewarm Apple's bounded, lock-free async write queue off the render thread.
        if (!status && async) status = ExtAudioFileWriteAsync(file, 0, nullptr);
        return status;
    }
    OSStatus write(UInt32 count, const AudioBufferList *data, bool async) {
        if (wav && (frames + count) * channels * 2 > UINT32_MAX - 4096)
            return kAudioFileDoesNotAllow64BitDataSizeError;
        OSStatus status = async ? ExtAudioFileWriteAsync(file, count, data) : ExtAudioFileWrite(file, count, data);
        if (!status) frames += count;
        return status;
    }
};
