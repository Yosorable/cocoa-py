"""Check native voice reuse against freshly created nodes at the PCM output."""

from array import array
import json
import math
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import wave


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Requires Apple's macOS audio engine and compiler")
class AudioReuseSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory(prefix="cocoa_audio_reuse_")
        cls.addClassCleanup(directory.cleanup)
        cls.directory = Path(directory.name)
        cls.executable = cls.directory / "probe"
        cls.rendered_cases = {}
        command = ["xcrun", "clang++", "-std=c++17", "-fobjc-arc", "-O1",
                   "-I", str(ROOT / "native/audio"), "-I", sysconfig.get_path("include")]
        framework = sysconfig.get_config_var("PYTHONFRAMEWORK")
        if framework:
            command += ["-F", sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX"), "-framework", framework]
        else:
            library_dir = sysconfig.get_config_var("LIBDIR")
            command += [str(Path(library_dir) / sysconfig.get_config_var("LDLIBRARY")),
                        "-Wl,-rpath," + library_dir]
            command += shlex.split(sysconfig.get_config_var("LIBS") or "")
            command += shlex.split(sysconfig.get_config_var("SYSLIBS") or "")
        for name in ("AVFoundation", "AudioToolbox", "CoreAudio", "QuartzCore", "AppKit"):
            command += ["-framework", name]
        command += [str(ROOT / "tests/native_audio_reuse_probe.mm"), "-o", str(cls.executable)]
        compiled = subprocess.run(command, capture_output=True, text=True, timeout=90)
        if compiled.returncode:
            raise AssertionError(compiled.stdout + compiled.stderr)

    def render_cases(self, mode):
        if mode not in self.rendered_cases:
            directory = self.directory / mode
            directory.mkdir()
            if mode == "pending":
                samples = array("h", (int(8191 * math.sin(2 * math.pi * 1733 * i / 22050))
                                      for i in range(13230)))
                with wave.open(str(directory / "pending.wav"), "wb") as output:
                    output.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
                    output.writeframes(samples.tobytes())
            rendered = subprocess.run([str(self.executable), str(directory), mode],
                                      capture_output=True, text=True, timeout=60)
            self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)
            name = "cases" if mode.isdecimal() else mode
            cases = [json.loads(line) for line in (directory / f"{name}.jsonl").read_text().splitlines()]
            self.rendered_cases[mode] = directory, cases
        return self.rendered_cases[mode]

    @staticmethod
    def read_pcm(path):
        pcm = array("f")
        pcm.frombytes(path.read_bytes())
        return pcm

    def test_stopped_and_finished_voices_do_not_leak_into_following_silence(self):
        directory, cases = self.render_cases("16")
        self.assertEqual(len(cases), 16)
        for case in cases:
            with self.subTest(**case):
                pcm = self.read_pcm(directory / case["file"])
                self.assertEqual(len(pcm), 38400)
                peak = max(abs(value) for value in pcm)
                if case["frequency"] == 0:
                    self.assertLessEqual(peak, 1e-6, "Previous sound leaked into a silent buffer")
                else:
                    self.assertGreater(peak, .1, "Audible control buffer did not reach the output")
                    self.assertLess(peak, .26, "Playback produced an unexpected output peak")
                self.assertEqual(case["reused"], not case["fresh"])

    def test_seek_during_loading_preserves_the_requested_position(self):
        directory, cases = self.render_cases("pending")
        self.assertEqual(len(cases), 6)
        for case in cases:
            with self.subTest(**case):
                self.assertAlmostEqual(case["pending_offset"], .2)
                self.assertAlmostEqual(case["paused_position"], .2, delta=1 / 22050)
                pcm = self.read_pcm(directory / f"{case['prefix']}-resumed.f32")
                audible = [i for i, value in enumerate(pcm) if abs(value) > .001]
                self.assertGreater(len(audible), 1000)
                duration = (audible[-1] - audible[0] + 1) / 48000
                self.assertAlmostEqual(duration, (.6 - .2) / case["rate"], delta=.015)

    def test_pause_during_loading_keeps_a_reused_player_silent_until_resume(self):
        directory, cases = self.render_cases("pending")
        self.assertEqual(len(cases), 6)
        for case in cases:
            with self.subTest(**case):
                self.assertFalse(case["playing_while_paused"])
                self.assertFalse(case["finished_while_paused"])
                self.assertEqual(case["reused"], case["warm"])
                paused = self.read_pcm(directory / f"{case['prefix']}-paused.f32")
                resumed = self.read_pcm(directory / f"{case['prefix']}-resumed.f32")
                self.assertLessEqual(max(map(abs, paused)), 1e-6)
                self.assertGreater(max(map(abs, resumed)), .1)
                self.assertTrue(case["completed"])

    def test_recording_restoration_preserves_rate_pitch_and_cached_reuse(self):
        directory, cases = self.render_cases("restored")
        self.assertEqual(len(cases), 6)
        for case in cases:
            with self.subTest(**case):
                self.assertEqual(case["reused"], 1)
                if case["paused"]:
                    pcm = self.read_pcm(directory / f"{case['prefix']}-paused.f32")
                    self.assertLessEqual(max(map(abs, pcm)), 1e-6)
                for stage in ("restored", "reused"):
                    pcm = self.read_pcm(directory / f"{case['prefix']}-{stage}.f32")
                    audible = [i for i, value in enumerate(pcm) if abs(value) > .001]
                    self.assertGreater(len(audible), 1000)
                    # Interpolate zero crossings away from the fades to measure
                    # audible pitch independently of processor properties.
                    crossings = [i - pcm[i] / (pcm[i + 1] - pcm[i])
                                 for i in range(audible[0] + 480, audible[-1] - 480)
                                 if pcm[i] <= 0 < pcm[i + 1]]
                    self.assertGreater(len(crossings), 10)
                    frequency = 48000 * (len(crossings) - 1) / (crossings[-1] - crossings[0])
                    self.assertAlmostEqual(frequency, 997 * case["rate"], delta=2)
                    duration = (audible[-1] - audible[0] + 1) / 48000
                    source_duration = .15 if stage == "reused" else .4 - case["position"]
                    self.assertAlmostEqual(duration, source_duration / case["rate"], delta=.015)


if __name__ == "__main__":
    unittest.main()
