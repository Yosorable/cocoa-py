"""Live playback ownership, reuse and controls with a running desktop loop."""

from array import array
import math
import os
from pathlib import Path
import tempfile
import time
import unittest
import wave

import audio
from _cocoa import _audio
from scene import Scene, run


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1", "Set COCOA_PY_UI_TESTS=1 for live playback tests.")
class AudioPlaybackTests(unittest.TestCase):
    def setUp(self):
        audio.close()

    def tearDown(self):
        audio.close()

    def drive(self, check, timeout=5):
        class Finished(Exception):
            pass

        class PlaybackScene(Scene):
            def setup(self):
                self.deadline = time.monotonic() + timeout

            def update(self, dt):
                if check():
                    raise Finished
                if time.monotonic() > self.deadline:
                    raise AssertionError(f"Playback did not finish within {timeout} seconds")

        with self.assertRaises(Finished):
            run(PlaybackScene, title="cocoa-py playback validation", background="#000000")

    def test_completed_handles_cannot_control_a_reused_voice(self):
        sound = audio.tone(440, duration=.2, volume=.01, sample_rate=22050)
        following = audio.tone(660, duration=.8, volume=.01, sample_rate=22050)
        self.addCleanup(sound.close)
        self.addCleanup(following.close)
        previous = sound.play()
        current = None
        count = 0

        def check():
            nonlocal current, count
            if current is None:
                if not previous.finished:
                    return False
                current = following.play(loop=True, rate=1.25, semitones=4, volume=.2, pan=.4)
                previous.volume, previous.pan, previous.rate, previous.semitones = .9, -.8, .5, -12
                previous.pause()
                previous.resume()
                previous.stop()
            count += 1
            if count < 4:
                return False
            self.assertTrue(current.playing)
            self.assertAlmostEqual(current.volume, .2)
            self.assertAlmostEqual(current.pan, .4)
            self.assertAlmostEqual(current.rate, 1.25)
            self.assertAlmostEqual(current.semitones, 4)
            self.assertLess(current.position, .15)
            self.assertGreater(_audio._voice_cache_info()["reused"], 0)
            current.stop()
            return True

        self.drive(check)

    def test_short_resampled_sounds_finish_and_reuse_a_running_voice(self):
        # Natural completion preserves a running player timeline. Repeated low
        # sample-rate pitch shifts also exercise the iOS resampler under ASan.
        sound = audio.tone(440, duration=.04, volume=.005, sample_rate=22050)
        self.addCleanup(sound.close)
        shifts = (0, -4, -4, 4, 0, -12, 12, 0) * 2
        before = _audio._voice_cache_info()
        channel = None
        count = 0

        def check():
            nonlocal channel, count
            if channel is not None:
                if not channel.finished:
                    return False
                self.assertEqual(channel.state, "finished")
            if count == len(shifts):
                after = _audio._voice_cache_info()
                self.assertEqual(after["created"] - before["created"], 1)
                self.assertEqual(after["reused"] - before["reused"], len(shifts) - 1)
                return True
            semitones = shifts[count]
            channel = sound.play(rate=2 ** (semitones / 12), semitones=semitones)
            count += 1
            return False

        self.drive(check, timeout=15)

    def test_reuse_across_formats_keeps_pause_seek_and_pitch_controls(self):
        first = audio.tone(220, duration=.3, volume=.01, sample_rate=22050)
        second = audio.Sound.from_pcm(array("f", [.001, -.001] * 13230).tobytes(), channels=2, sample_rate=44100)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        count = 0

        def check():
            nonlocal count
            sound = first if count % 2 else second
            channel = sound.play(loop=True, volume=.1)
            channel.pause()
            self.assertTrue(channel.paused)
            channel.rate, channel.semitones = 1.1, 2
            channel.seek(.02)
            channel.resume()
            self.assertTrue(channel.playing)
            channel.rate, channel.semitones = 1, 0
            channel.stop()
            count += 1
            if count < 12:
                return False
            info = _audio._voice_cache_info()
            self.assertGreater(info["reused"], 0)
            self.assertLessEqual(info["idle"], info["capacity"])
            audio.stop_all()
            self.assertEqual(_audio._voice_cache_info()["idle"], 0)
            return True

        self.drive(check)

    def test_effect_rebuild_and_shutdown_release_cached_voices(self):
        sound = audio.tone(330, duration=.3, volume=.005)
        self.addCleanup(sound.close)
        warm = sound.play(loop=True)
        warm.stop()
        channel = sound.play(loop=True, volume=.1)
        channel.delay(time=.02, feedback=10, mix=5)
        channel.reverb(mix=5)
        count = 0

        def check():
            nonlocal count
            self.assertTrue(channel.playing)
            count += 1
            if count < 4:
                return False
            channel.stop()
            voices = [sound.play(loop=True, volume=.01) for _ in range(18)]
            for voice in voices:
                voice.stop()
            info = _audio._voice_cache_info()
            self.assertGreater(info["idle"], 0)
            self.assertLessEqual(info["idle"], info["capacity"])
            audio.close()
            self.assertEqual(_audio._voice_cache_info()["idle"], 0)
            self.assertFalse(audio.get_device_info()["engine_running"])
            return True

        self.drive(check)

    def test_file_stream_keeps_an_independent_timeline_after_pcm_completion(self):
        directory = tempfile.TemporaryDirectory(prefix="cocoa_audio_stream_")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "stream.wav"
        with wave.open(str(path), "wb") as output:
            output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            output.writeframes(bytes(22050 * 2))
        sound = audio.tone(220, duration=.2, volume=.005, sample_rate=22050)
        stream = audio.Stream(str(path))
        self.addCleanup(sound.close)
        self.addCleanup(stream.close)
        previous = sound.play()
        current = None
        count = 0

        def check():
            nonlocal current, count
            if current is None:
                if not previous.finished:
                    return False
                reused = _audio._voice_cache_info()["reused"]
                current = stream.play()
                self.assertEqual(_audio._voice_cache_info()["reused"], reused)
            count += 1
            if count < 4:
                return False
            self.assertTrue(current.playing)
            self.assertLess(current.position, .15)
            current.stop()
            return True

        self.drive(check)

    def test_live_rate_pitch_changes_switch_processors_and_survive_reuse(self):
        sound = audio.tone(220, duration=.8, volume=.005)
        self.addCleanup(sound.close)
        semitones = 3.71
        channel = sound.play(loop=True, rate=2 ** (semitones / 12), semitones=semitones)
        count = 0

        def routes(resampling, stretching):
            info = _audio._voice_cache_info()
            self.assertEqual(info["resampling"], resampling)
            self.assertEqual(info["stretching"], stretching)

        def check():
            nonlocal count, channel
            count += 1
            if count == 2:
                routes(1, 0)
                channel.semitones = 0
                routes(0, 1)
            elif count == 4:
                channel.pause()
                channel.rate = .5
                channel.semitones = -12
                channel.seek(.2)
                channel.resume()
                routes(1, 0)
                self.assertTrue(channel.playing)
                self.assertAlmostEqual(channel.position, .2, delta=.06)
            elif count == 6:
                channel.delay(time=.02, feedback=0, mix=5)
                routes(1, 0)
            elif count == 8:
                channel.stop()
                routes(0, 0)
                warm = sound.play(loop=True, rate=2, semitones=12)
                warm.stop()
                reused = _audio._voice_cache_info()["reused"]
                channel = sound.play(loop=True, rate=1, semitones=0)
                self.assertGreater(_audio._voice_cache_info()["reused"], reused)
                routes(0, 0)
                self.assertTrue(channel.playing)
                channel.rate = 1.3
                channel.semitones = 12 * math.log2(1.3)
                routes(1, 0)
            elif count == 10:
                channel.stop()
                routes(0, 0)
                audio.stop_all()
                self.assertEqual(_audio._voice_cache_info()["idle"], 0)
                return True
            return False

        self.drive(check)


if __name__ == "__main__":
    unittest.main()
