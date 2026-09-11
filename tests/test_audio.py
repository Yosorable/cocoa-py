"""Public audio regression tests using the actual native offline renderer."""

from array import array
from pathlib import Path
import math
import tempfile
import time
import unittest
import wave

import audio


class AudioTests(unittest.TestCase):
    def tearDown(self):
        audio.close()

    def test_device_query_does_not_activate_audio(self):
        audio.close()
        info = audio.get_device_info()
        self.assertFalse(info["engine_running"])
        for key in ("sample_rate", "input_latency", "output_latency", "io_buffer_duration"):
            self.assertTrue(math.isfinite(info[key]))
            self.assertGreaterEqual(info[key], 0)
        self.assertIsInstance(info["inputs"], list)
        self.assertIsInstance(info["outputs"], list)

    def test_interleaved_pcm_round_trip(self):
        data = array("f", [0.1, -0.2, 0.3, -0.4] * 100)
        sound = audio.Sound.from_pcm(data.tobytes(), channels=2, sample_rate=16000)
        try:
            self.assertEqual(sound.frames, 200)
            self.assertEqual(sound.to_pcm(), data.tobytes())
        finally:
            sound.close()

    def test_offline_downsampling_and_rate_do_not_run_out_of_source_frames(self):
        sound = audio.tone(320, duration=4, volume=0.4, sample_rate=48000)
        self.addCleanup(sound.close)
        for rate in (1.0, 2.0, 4.0):
            with self.subTest(rate=rate):
                mixed = audio.mix(audio.Track(sound, rate=rate), sample_rate=8000, channels=1)
                try:
                    self.assertAlmostEqual(mixed.duration, 4 / rate, places=3)
                    samples = array("f", mixed.to_pcm())
                    for offset in range(1024, len(samples) - 1024, 512):
                        window = samples[offset:offset + 512]
                        rms = math.sqrt(sum(value * value for value in window) / len(window))
                        self.assertGreater(rms, 0.05, f"Unexpected silence at frame {offset}")
                finally:
                    mixed.close()
        self.assertFalse(audio.get_device_info()["engine_running"])

    def test_three_export_formats_and_overwrite_protection(self):
        sound = audio.tone(440, duration=0.4, volume=0.3, sample_rate=22050)
        self.addCleanup(sound.close)
        with tempfile.TemporaryDirectory(prefix="cocoa_py_audio_") as directory:
            for extension in ("wav", "caf", "m4a"):
                path = Path(directory) / f"mixed.{extension}"
                result = audio.export(sound, path, sample_rate=22050, channels=1)
                self.assertEqual(Path(result), path)
                self.assertGreater(path.stat().st_size, 100)
                with self.assertRaises(OSError):
                    audio.export(sound, path, sample_rate=22050, channels=1)
            with wave.open(str(Path(directory) / "mixed.wav")) as recorded:
                self.assertEqual(recorded.getframerate(), 22050)
                self.assertEqual(recorded.getnchannels(), 1)
                self.assertEqual(recorded.getnframes(), 8820)

    def test_memory_wav_decodes_without_playback(self):
        from io import BytesIO
        payload = BytesIO()
        with wave.open(payload, "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            output.writeframes(array("h", [1000, -1000] * 800).tobytes())
        sound = audio.Sound(payload.getvalue())
        try:
            deadline = time.monotonic() + 5
            while not sound.loaded and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(sound.loaded)
            self.assertEqual(sound.frames, 1600)
            self.assertTrue(any(array("f", sound.to_pcm())))
            self.assertFalse(audio.get_device_info()["engine_running"])
        finally:
            sound.close()

    def test_invalid_pcm_is_rejected(self):
        for data, channels in ((b"", 1), (b"x", 1), (b"\0" * 4, 2)):
            with self.subTest(data=data, channels=channels), self.assertRaises(ValueError):
                audio.Sound.from_pcm(data, channels=channels)


if __name__ == "__main__":
    unittest.main()
