#pragma once

struct AudioMacDeviceInfo {
    AudioDeviceID device = kAudioObjectUnknown;
    __strong NSString *name = @"";
    __strong NSString *type = @"unknown";
    double sampleRate = 0;
    UInt32 channels = 0, latencyFrames = 0, bufferFrames = 0;
};

template<class T>
static bool audioMacProperty(AudioObjectID object, AudioObjectPropertySelector selector,
                             AudioObjectPropertyScope scope, T &value) {
    AudioObjectPropertyAddress address{selector, scope, kAudioObjectPropertyElementMain};
    UInt32 size = sizeof(value);
    return AudioObjectGetPropertyData(object, &address, 0, nullptr, &size, &value) == noErr;
}

static AudioMacDeviceInfo audioMacDevice(bool input) {
    AudioMacDeviceInfo info;
    audioMacProperty(kAudioObjectSystemObject,
        input ? kAudioHardwarePropertyDefaultInputDevice : kAudioHardwarePropertyDefaultOutputDevice,
        kAudioObjectPropertyScopeGlobal, info.device);
    if (info.device == kAudioObjectUnknown) return info;
    auto scope = input ? kAudioObjectPropertyScopeInput : kAudioObjectPropertyScopeOutput;
    CFStringRef name = nullptr;
    if (audioMacProperty(info.device, kAudioObjectPropertyName, kAudioObjectPropertyScopeGlobal, name) && name)
        info.name = CFBridgingRelease(name);
    audioMacProperty(info.device, kAudioDevicePropertyNominalSampleRate,
        kAudioObjectPropertyScopeGlobal, info.sampleRate);
    audioMacProperty(info.device, kAudioDevicePropertyLatency, scope, info.latencyFrames);
    UInt32 safety = 0;
    audioMacProperty(info.device, kAudioDevicePropertySafetyOffset, scope, safety);
    info.latencyFrames += safety;
    audioMacProperty(info.device, kAudioDevicePropertyBufferFrameSize,
        kAudioObjectPropertyScopeGlobal, info.bufferFrames);
    UInt32 transport = 0;
    audioMacProperty(info.device, kAudioDevicePropertyTransportType, kAudioObjectPropertyScopeGlobal, transport);
    switch (transport) {
        case kAudioDeviceTransportTypeBuiltIn: info.type = @"BuiltIn"; break;
        case kAudioDeviceTransportTypeUSB: info.type = @"USB"; break;
        case kAudioDeviceTransportTypeBluetooth:
        case kAudioDeviceTransportTypeBluetoothLE: info.type = @"Bluetooth"; break;
        case kAudioDeviceTransportTypeAirPlay: info.type = @"AirPlay"; break;
        case kAudioDeviceTransportTypeHDMI: info.type = @"HDMI"; break;
        case kAudioDeviceTransportTypeVirtual: info.type = @"Virtual"; break;
        case kAudioDeviceTransportTypeAggregate: info.type = @"Aggregate"; break;
        default: break;
    }
    AudioObjectPropertyAddress address{kAudioDevicePropertyStreamConfiguration, scope, kAudioObjectPropertyElementMain};
    UInt32 size = 0;
    if (AudioObjectGetPropertyDataSize(info.device, &address, 0, nullptr, &size) == noErr && size >= sizeof(AudioBufferList)) {
        std::vector<unsigned char> storage(size);
        auto buffers = reinterpret_cast<AudioBufferList *>(storage.data());
        if (AudioObjectGetPropertyData(info.device, &address, 0, nullptr, &size, buffers) == noErr)
            for (UInt32 i = 0; i < buffers->mNumberBuffers; ++i) info.channels += buffers->mBuffers[i].mNumberChannels;
    }
    return info;
}

static PyObject *audioMacPorts(const AudioMacDeviceInfo &info) {
    if (info.device == kAudioObjectUnknown) return PyList_New(0);
    return Py_BuildValue("[{s:s,s:s,s:I}]", "name", info.name.UTF8String,
                         "type", info.type.UTF8String, "channels", info.channels);
}

static PyObject *audio_get_device_info(PyObject *, PyObject *) {
    @autoreleasepool {
        std::lock_guard<std::mutex> lock(gMutex);
        @try {
            auto input = audioMacDevice(true), output = audioMacDevice(false);
            auto permission = AVAudioApplication.sharedInstance.recordPermission;
            const char *permissionName = permission == AVAudioApplicationRecordPermissionGranted ? "granted"
                : permission == AVAudioApplicationRecordPermissionDenied ? "denied" : "not_determined";
            PyObject *inputs = audioMacPorts(input);
            if (!inputs) return nullptr;
            PyObject *outputs = audioMacPorts(output);
            if (!outputs) { Py_DECREF(inputs); return nullptr; }
            return Py_BuildValue("{s:d,s:I,s:I,s:d,s:d,s:d,s:O,s:O,s:O,s:s,s:N,s:N}",
                "sample_rate", output.sampleRate,
                "input_channels", input.channels, "output_channels", output.channels,
                "input_latency", input.sampleRate > 0 ? input.latencyFrames / input.sampleRate : 0.0,
                "output_latency", output.sampleRate > 0 ? output.latencyFrames / output.sampleRate : 0.0,
                "io_buffer_duration", output.sampleRate > 0 ? output.bufferFrames / output.sampleRate : 0.0,
                "input_available", input.channels ? Py_True : Py_False,
                "engine_running", gEngine.isRunning ? Py_True : Py_False,
                "interrupted", gInterrupted ? Py_True : Py_False,
                "microphone_permission", permissionName, "inputs", inputs, "outputs", outputs);
        } @catch (NSException *exception) {
            raiseAudioError(exception.reason ?: @"Cannot query audio device information.");
            return nullptr;
        }
    }
}
