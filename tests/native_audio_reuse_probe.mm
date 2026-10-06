// Compile the shipped audio implementation into an isolated rendering probe.
// No microphone, device output, game state, or production App is used.
#include "AudioModule.mm"
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <filesystem>

static constexpr unsigned kProbeBlock = 128;
static constexpr double kProbeOutputRate = 48000;
static AVAudioPCMBuffer *probeOutput;
static bool probeLastReused;

static void check(BOOL value, NSString *message) {
    if (!value) throw std::runtime_error(message.UTF8String ?: "audio probe failed");
}

static void beginProbeEngine() {
    gEngine = [AVAudioEngine new];
    gMixer = gEngine.mainMixerNode;
    auto format = [[AVAudioFormat alloc] initStandardFormatWithSampleRate:kProbeOutputRate channels:2];
    [gEngine connect:gMixer to:gEngine.outputNode format:format];
    NSError *error = nil;
    check([gEngine enableManualRenderingMode:AVAudioEngineManualRenderingModeOffline
                                     format:format maximumFrameCount:kProbeBlock error:&error], error.localizedDescription);
    check([gEngine startAndReturnError:&error], error.localizedDescription);
    probeOutput = [[AVAudioPCMBuffer alloc] initWithPCMFormat:format frameCapacity:kProbeBlock];
}

static void endProbeEngine() {
    for (auto &entry : gChannels) {
        if (!entry.second.completed) disconnectChannel(&entry.second);
    }
    clearIdleVoices();
    gChannels.clear();
    [gEngine stop];
    gMixer = nil;
    gEngine = nil;
    gEngineReady = NO;
    probeOutput = nil;
}

static long long playProbeSound(double sr, unsigned channels, double frequency, double seconds, float rate) {
    const auto frames = (AVAudioFrameCount)llround(sr * seconds);
    auto format = [[AVAudioFormat alloc] initStandardFormatWithSampleRate:sr channels:channels];
    auto buffer = [[AVAudioPCMBuffer alloc] initWithPCMFormat:format frameCapacity:frames];
    buffer.frameLength = frames;
    for (unsigned c = 0; c < channels; ++c) {
        for (unsigned i = 0; i < frames; ++i) {
            // Fade edges so a discontinuous test asset cannot masquerade as a
            // playback-node reuse artifact. Stopped cases deliberately cut it.
            double envelope = std::min({1.0, i / (sr * .005), (frames - 1 - i) / (sr * .005)});
            buffer.floatChannelData[c][i] = frequency == 0 ? 0 : .25 * envelope * std::sin(2 * M_PI * frequency * i / sr);
        }
    }
    long long handle = nextHandle();
    ChannelRecord &channel = gChannels[handle];
    channel.handle = handle;
    channel.soundHandle = 0;
    channel.soundBuffer = buffer;
    channel.duration = seconds;
    channel.volume = 1;
    channel.pan = 0;
    channel.pitch = rate;
    channel.semitones = 12 * std::log2(rate);
    channel.generation = 1;
    connectChannel(&channel);
    channel.player.volume = 1;
    channel.player.pan = 0;
    check(rescheduleFromPosition(&channel, 0), channel.error);
    check(startChannelPlayback(&channel) == nil, channel.error);
    return handle;
}

static void renderBlock(std::vector<float> *samples = nullptr) {
    NSError *error = nil;
    auto result = [gEngine renderOffline:kProbeBlock toBuffer:probeOutput error:&error];
    check(result == AVAudioEngineManualRenderingStatusSuccess, error.localizedDescription);
    check(probeOutput.frameLength == kProbeBlock, @"partial output block");
    if (samples) {
        for (unsigned i = 0; i < probeOutput.frameLength; ++i)
            samples->push_back(probeOutput.floatChannelData[0][i]);
    }
    // Service the implementation's genuine generation-checked completion
    // handler after each deterministic output block.
    CFRunLoopRunInMode(kCFRunLoopDefaultMode, .0005, false);
}

static std::vector<float> transition(double sr, unsigned channels, float firstRate, float nextRate,
                                     bool stopEarly, bool fresh, double nextFrequency) {
    beginProbeEngine();
    auto first = playProbeSound(sr, channels, 997, .15, firstRate);
    unsigned firstBlocks = 0;
    if (stopEarly) {
        for (unsigned i = 0; i < 15; ++i) renderBlock();
        completeChannel(first, YES);
    } else {
        while (!gChannels[first].completed && firstBlocks++ < 1500) renderBlock();
        check(gChannels[first].completed, @"natural completion was not delivered");
    }
    const auto reusedBefore = gVoicesReused;
    if (fresh) clearIdleVoices();
    auto next = playProbeSound(sr, channels, nextFrequency, .15, nextRate);
    probeLastReused = gVoicesReused - reusedBefore == 1;
    check(!fresh || !probeLastReused, @"fresh comparison unexpectedly reused a voice");
    std::vector<float> output;
    for (unsigned i = 0; i < 300; ++i) renderBlock(&output);
    check(gChannels[next].completed, @"second sound did not finish");
    endProbeEngine();
    return output;
}

static void writeSamples(const std::string &directory, const std::string &name,
                         const std::vector<float> &samples) {
    std::ofstream file(directory + "/" + name, std::ios::binary);
    file.write((const char *)samples.data(), samples.size() * sizeof(float));
    if (!file) throw std::runtime_error("cannot write rendered samples");
}

static PyObject *checkedResult(PyObject *value) {
    if (!value) {
        PyErr_Print();
        throw std::runtime_error("native Python audio call failed");
    }
    return value;
}

static void pendingPlaybackCases(const std::string &directory) {
    Py_Initialize();
    std::ofstream metadata(directory + "/pending.jsonl");
    unsigned index = 0;
    for (bool warm : {false, true}) {
        for (float rate : {.5f, 1.f, 2.f}) {
            beginProbeEngine();
            gEngineReady = YES;
            if (warm) {
                auto first = playProbeSound(22050, 1, 997, .15, .5f);
                for (unsigned i = 0; !gChannels[first].completed && i < 1500; ++i) renderBlock();
                check(gChannels[first].completed, @"cache warm-up did not finish");
            }
            const auto reusedBefore = gVoicesReused;
            // Exercise real asynchronous file decoding while keeping the public
            // loading-state controls deterministic, without slowing file I/O.
            ensureLoadQueue();
            dispatch_suspend(gLoadQueue);
            PyObject *args = Py_BuildValue("(s)", (directory + "/pending.wav").c_str());
            PyObject *value = checkedResult(audio_load(nullptr, args));
            Py_DECREF(args);
            const auto sound = PyLong_AsLongLong(value);
            Py_DECREF(value);
            args = Py_BuildValue("(L)", sound);
            PyObject *kwargs = Py_BuildValue("{s:d,s:d}", "pitch", (double)rate,
                                            "semitones", 12 * std::log2((double)rate));
            value = checkedResult(audio_play(nullptr, args, kwargs));
            Py_DECREF(args);
            Py_DECREF(kwargs);
            const auto handle = PyLong_AsLongLong(value);
            Py_DECREF(value);
            check(!gSounds[sound].loaded && !gChannels[handle].player,
                  @"playback was not queued during loading");
            args = Py_BuildValue("(Ld)", handle, .2);
            Py_DECREF(checkedResult(audio_channel_seek(nullptr, args)));
            Py_DECREF(args);
            const auto pendingOffset = gChannels[handle].positionOffset;
            args = Py_BuildValue("(L)", handle);
            Py_DECREF(checkedResult(audio_pause(nullptr, args)));
            Py_DECREF(args);
            dispatch_resume(gLoadQueue);
            dispatch_sync(gLoadQueue, ^{});
            auto &channel = gChannels[handle];
            check(!channel.error && channelReady(&channel), channel.error);
            const bool playingWhilePaused = channel.player.isPlaying;
            const auto pausedPosition = channelPosition(&channel);
            std::vector<float> pausedOutput;
            for (unsigned i = 0; i < 60; ++i) renderBlock(&pausedOutput);
            const bool finishedWhilePaused = channel.completed;
            args = Py_BuildValue("(L)", handle);
            Py_DECREF(checkedResult(audio_resume(nullptr, args)));
            Py_DECREF(args);
            std::vector<float> resumedOutput;
            for (unsigned i = 0; i < 650; ++i) renderBlock(&resumedOutput);
            const std::string prefix = "pending-" + std::to_string(index++);
            writeSamples(directory, prefix + "-paused.f32", pausedOutput);
            writeSamples(directory, prefix + "-resumed.f32", resumedOutput);
            metadata << "{\"prefix\":\"" << prefix << "\",\"warm\":" << warm
                << ",\"rate\":" << rate << ",\"pending_offset\":" << pendingOffset
                << ",\"paused_position\":" << pausedPosition
                << ",\"playing_while_paused\":" << playingWhilePaused
                << ",\"finished_while_paused\":" << finishedWhilePaused
                << ",\"completed\":" << (bool)channel.completed
                << ",\"reused\":" << (gVoicesReused - reusedBefore) << "}\n";
            endProbeEngine();
            gSounds.clear();
        }
    }
    Py_Finalize();
}

static void restoredPlaybackCases(const std::string &directory) {
    beginProbeEngine();
    auto idle = playProbeSound(22050, 1, 330, .04, 1);
    for (unsigned i = 0; !gChannels[idle].completed && i < 1500; ++i) renderBlock();
    check(gChannels[idle].completed && gIdleVoices.size() == 1,
          @"recording cache warm-up did not finish");
    const auto cached = gIdleVoices.front();
    std::vector<long long> inactive;
    suspendChannelsForEngineStop(inactive);
    check(inactive.empty() && gIdleVoices.empty() && !cached.player.engine
          && !cached.varispeed.engine && !cached.timePitch.engine,
          @"recording suspension left a cached engine timeline behind");
    endProbeEngine();
    std::ofstream metadata(directory + "/restored.jsonl");
    unsigned index = 0;
    for (bool paused : {false, true}) {
        for (float rate : {.5f, 1.f, 2.f}) {
            beginProbeEngine();
            const auto handle = playProbeSound(22050, 1, 997, .4, rate);
            for (unsigned i = 0; i < 15; ++i) renderBlock();
            auto &channel = gChannels[handle];
            if (paused) pauseChannel(&channel);
            const auto position = channelPosition(&channel);
            // Match recorder setup's suspension and engine restart, then invoke
            // the same restoration function. No microphone permission is used.
            std::vector<long long> suspended;
            suspendChannelsForEngineStop(suspended);
            check(suspended.size() == 1 && suspended[0] == handle && gIdleVoices.empty(),
                  @"recording suspension left a cached engine timeline behind");
            [gEngine stop];
            check(restartAudioEngine() == nil, @"manual engine restart failed");
            restoreChannelAfterEngineStop(&channel);
            check(!channel.error, channel.error);
            std::vector<float> pausedOutput;
            if (paused) {
                for (unsigned i = 0; i < 60; ++i) renderBlock(&pausedOutput);
                check(resumeChannel(&channel) == nil, @"restored channel resume failed");
            }
            std::vector<float> restoredOutput;
            for (unsigned i = 0; i < 650; ++i) renderBlock(&restoredOutput);
            check(channel.completed,
                  [NSString stringWithFormat:@"restored channel did not finish (rate=%g, paused=%d, position=%g, current=%g, latency=%g)",
                   rate, paused, position, channelPosition(&channel), channel.player.outputPresentationLatency]);
            const auto reusedBefore = gVoicesReused;
            auto next = playProbeSound(22050, 1, 997, .15, rate);
            std::vector<float> reusedOutput;
            for (unsigned i = 0; i < 650; ++i) renderBlock(&reusedOutput);
            check(gChannels[next].completed,
                  [NSString stringWithFormat:@"post-restoration reuse did not finish (rate=%g, paused=%d)", rate, paused]);
            const std::string prefix = "restored-" + std::to_string(index++);
            writeSamples(directory, prefix + "-paused.f32", pausedOutput);
            writeSamples(directory, prefix + "-restored.f32", restoredOutput);
            writeSamples(directory, prefix + "-reused.f32", reusedOutput);
            metadata << "{\"prefix\":\"" << prefix << "\",\"paused\":" << paused
                << ",\"rate\":" << rate << ",\"position\":" << position
                << ",\"reused\":" << (gVoicesReused - reusedBefore) << "}\n";
            endProbeEngine();
        }
    }
}

int main(int argc, char **argv) {
    if (argc < 2 || argc > 3) return 2;
    std::filesystem::create_directories(argv[1]);
    try {
        @autoreleasepool {
            if (argc == 3 && std::string(argv[2]) == "pending") {
                pendingPlaybackCases(argv[1]);
                return 0;
            }
            if (argc == 3 && std::string(argv[2]) == "restored") {
                restoredPlaybackCases(argv[1]);
                return 0;
            }
            const unsigned limit = argc == 3 ? (unsigned)std::stoul(argv[2]) : 768;
            std::ofstream metadata(std::string(argv[1]) + "/cases.jsonl");
            unsigned caseIndex = 0;
            for (double sr : {22050., 44100., 48000.}) {
                for (unsigned channels : {1U, 2U}) {
                    for (float firstRate : {.5f, .7937005f, 1.f, 2.f}) {
                        for (float nextRate : {.5f, .7937005f, 1.f, 2.f}) {
                            for (bool stopped : {false, true}) {
                                for (double frequency : {0., 1733.}) {
                                    for (bool fresh : {false, true}) {
                                        auto samples = transition(sr, channels, firstRate, nextRate, stopped, fresh, frequency);
                                        std::string name = std::to_string(caseIndex++) + ".f32";
                                        writeSamples(argv[1], name, samples);
                                        double peak = 0;
                                        for (float sample : samples) peak = std::max(peak, std::abs((double)sample));
                                        metadata << "{\"file\":\"" << name << "\",\"sample_rate\":" << sr
                                            << ",\"channels\":" << channels << ",\"old_rate\":" << firstRate
                                            << ",\"new_rate\":" << nextRate << ",\"stopped\":" << stopped
                                            << ",\"fresh\":" << fresh << ",\"reused\":" << probeLastReused << ",\"frequency\":" << frequency
                                            << ",\"peak\":" << peak << "}\n";
                                        metadata.flush();
                                        std::cout << caseIndex << " " << peak << std::endl;
                                        if (caseIndex >= limit) return 0;
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    } catch (const std::exception &error) {
        std::cerr << error.what() << std::endl;
        return 1;
    }
    return 0;
}
