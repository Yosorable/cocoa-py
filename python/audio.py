"""audio — AVAudioEngine-based audio for Apple platforms.

Independent top-level module, no scene dependency.

Quick start::

    import audio

    # Sound effects
    sfx = audio.Sound("laser.wav")
    ch = sfx.play()              # returns Channel
    ch.volume = 0.5
    print(sfx.duration, sfx.channels, sfx.sample_rate)

    # Music (loop, fade)
    music = audio.Stream("bgm.mp3")
    bg = music.play(loop=True, music=True)
    bg.fade_out(2.0)

    # Playback position
    print(bg.position, bg.duration)
    bg.seek(10.0)

    # Wait for playback to finish
    import time
    while ch.playing:
        time.sleep(0.05)

    # Per-channel effects
    ch.reverb(mix=40, preset=audio.REVERB_LARGE_HALL)
    ch.delay(time=0.3, feedback=50, mix=30)
    ch.distortion(preset=audio.DISTORTION_DRUMS_BIT_BRUSH, mix=50)
    ch.eq(low=3, mid=0, high=-2)

    # Global control
    audio.master_volume = 0.8
    audio.pause_all()
    audio.resume_all()

    # Global effects
    audio.reverb(mix=40, preset=audio.REVERB_LARGE_HALL)
    audio.delay(time=0.3, feedback=50, mix=30)
    audio.distortion(preset=audio.DISTORTION_DRUMS_BIT_BRUSH, mix=50)
    audio.eq(low=3, mid=0, high=-2)

    # Recording
    rec = audio.Recorder()
    rec.start()
    data = rec.stop()
    snd = audio.Sound.from_pcm(data, channels=rec.channels,
                               sample_rate=rec.sample_rate)

    # Cleanup
    audio.close()
"""

from _cocoa import _audio
import math as _math
import operator as _operator
import os as _os
import time as _time

# ── Reverb presets (re-export from C module) ──

REVERB_SMALL_ROOM = _audio.REVERB_SMALL_ROOM
REVERB_MEDIUM_ROOM = _audio.REVERB_MEDIUM_ROOM
REVERB_LARGE_ROOM = _audio.REVERB_LARGE_ROOM
REVERB_MEDIUM_HALL = _audio.REVERB_MEDIUM_HALL
REVERB_LARGE_HALL = _audio.REVERB_LARGE_HALL
REVERB_PLATE = _audio.REVERB_PLATE
REVERB_MEDIUM_CHAMBER = _audio.REVERB_MEDIUM_CHAMBER
REVERB_LARGE_CHAMBER = _audio.REVERB_LARGE_CHAMBER
REVERB_CATHEDRAL = _audio.REVERB_CATHEDRAL
REVERB_LARGE_ROOM2 = _audio.REVERB_LARGE_ROOM2
REVERB_MEDIUM_HALL2 = _audio.REVERB_MEDIUM_HALL2
REVERB_MEDIUM_HALL3 = _audio.REVERB_MEDIUM_HALL3
REVERB_LARGE_HALL2 = _audio.REVERB_LARGE_HALL2

# ── Distortion presets ──

DISTORTION_DRUMS_BIT_BRUSH = _audio.DISTORTION_DRUMS_BIT_BRUSH
DISTORTION_DRUMS_BUFFER_BEATS = _audio.DISTORTION_DRUMS_BUFFER_BEATS
DISTORTION_DRUMS_LOFI = _audio.DISTORTION_DRUMS_LOFI
DISTORTION_MULTI_BROKEN_SPEAKER = _audio.DISTORTION_MULTI_BROKEN_SPEAKER
DISTORTION_MULTI_CELLPHONE_CONCERT = _audio.DISTORTION_MULTI_CELLPHONE_CONCERT
DISTORTION_MULTI_DECIMATED1 = _audio.DISTORTION_MULTI_DECIMATED1
DISTORTION_MULTI_DECIMATED2 = _audio.DISTORTION_MULTI_DECIMATED2
DISTORTION_MULTI_DECIMATED3 = _audio.DISTORTION_MULTI_DECIMATED3
DISTORTION_MULTI_DECIMATED4 = _audio.DISTORTION_MULTI_DECIMATED4
DISTORTION_MULTI_DISTORTED_FUNK = _audio.DISTORTION_MULTI_DISTORTED_FUNK
DISTORTION_MULTI_DISTORTED_CUBED = _audio.DISTORTION_MULTI_DISTORTED_CUBED
DISTORTION_MULTI_DISTORTED_SQUARED = _audio.DISTORTION_MULTI_DISTORTED_SQUARED
DISTORTION_MULTI_ECHO1 = _audio.DISTORTION_MULTI_ECHO1
DISTORTION_MULTI_ECHO2 = _audio.DISTORTION_MULTI_ECHO2
DISTORTION_MULTI_ECHO_TIGHT1 = _audio.DISTORTION_MULTI_ECHO_TIGHT1
DISTORTION_MULTI_ECHO_TIGHT2 = _audio.DISTORTION_MULTI_ECHO_TIGHT2
DISTORTION_MULTI_EVERYTHING_IS_BROKEN = _audio.DISTORTION_MULTI_EVERYTHING_IS_BROKEN
DISTORTION_SPEECH_ALIEN_CHATTER = _audio.DISTORTION_SPEECH_ALIEN_CHATTER
DISTORTION_SPEECH_COSMIC_INTERFERENCE = _audio.DISTORTION_SPEECH_COSMIC_INTERFERENCE
DISTORTION_SPEECH_GOLDEN_PI = _audio.DISTORTION_SPEECH_GOLDEN_PI
DISTORTION_SPEECH_RADIO_TOWER = _audio.DISTORTION_SPEECH_RADIO_TOWER
DISTORTION_SPEECH_WAVES = _audio.DISTORTION_SPEECH_WAVES

# ── Channel ──

class Channel:
    """A playing audio channel. Returned by Sound.play()."""
    __slots__ = ("_handle", "_sound")

    def __init__(self, handle, sound=None):
        self._handle = handle
        self._sound = sound  # prevent Sound from being GC'd while channel is alive

    @property
    def handle(self):
        return self._handle

    def stop(self):
        """Stop playback and release the channel."""
        if self._handle is not None:
            _audio.stop(self._handle)
            self._handle = None

    def pause(self):
        """Pause playback."""
        if self._handle is not None:
            _audio.pause(self._handle)

    def resume(self):
        """Resume paused playback."""
        if self._handle is not None:
            _audio.resume(self._handle)

    @property
    def playing(self):
        """True while playing; raises RuntimeError if queued playback failed."""
        if self._handle is None:
            return False
        return _audio.channel_playing(self._handle)

    def _status(self):
        if self._handle is None:
            return {"state": "stopped", "error": None}
        return _audio.channel_status(self._handle)

    @property
    def state(self):
        """Lifecycle: loading, playing, paused, interrupted, finished, stopped, or failed."""
        return self._status()["state"]

    @property
    def paused(self):
        """Whether playback is explicitly paused, including while loading."""
        return self.state == "paused"

    @property
    def finished(self):
        """True only after natural completion; explicit stop and fade-out are distinct."""
        return self.state == "finished"

    @property
    def error(self):
        """Playback failure text, or None; inspecting state/error does not raise it."""
        return self._status()["error"]

    def wait(self, timeout=None):
        """Wait for natural completion or stop, returning False on timeout.

        Loading, pause and interruption do not count as completion. Playback
        failures raise RuntimeError. Call from the script thread; Stop can
        interrupt the short waits. This does not resume or stop the channel.
        """
        deadline = _deadline(timeout)
        while True:
            status = self._status()
            if status["state"] == "failed":
                raise RuntimeError(status["error"] or "Audio playback failed.")
            if status["state"] in ("finished", "stopped"):
                return True
            if _expired(deadline):
                return False
            _time.sleep(0.01)

    @property
    def volume(self):
        info = self._info()
        return info["volume"] if info else 0.0

    @volume.setter
    def volume(self, v):
        if self._handle is not None:
            _audio.set_volume(self._handle, float(v))

    @property
    def pan(self):
        info = self._info()
        return info["pan"] if info else 0.0

    @pan.setter
    def pan(self, v):
        if self._handle is not None:
            _audio.set_pan(self._handle, float(v))

    @property
    def pitch(self):
        info = self._info()
        return info["pitch"] if info else 1.0

    @pitch.setter
    def pitch(self, v):
        if self._handle is not None:
            _audio.set_pitch(self._handle, float(v))

    rate = property(lambda self: self.pitch, lambda self, value: setattr(self, "pitch", value),
                    doc="Playback speed (0.25...4); pitch is its legacy alias.")

    @property
    def semitones(self):
        """Pitch shift in semitones (-24...24), independent of playback rate."""
        info = self._info()
        return info["semitones"] if info else 0.0

    @semitones.setter
    def semitones(self, value):
        if self._handle is not None:
            _audio.set_semitones(self._handle, float(value))

    @property
    def position(self):
        """Current playback position in seconds (read-only)."""
        if self._handle is None:
            return 0.0
        return _audio.channel_position(self._handle)

    @property
    def duration(self):
        """Total duration of the sound in seconds (read-only)."""
        if self._handle is None:
            return 0.0
        return _audio.channel_duration(self._handle)

    def seek(self, time):
        """Jump to a position in seconds."""
        if self._handle is not None:
            _audio.channel_seek(self._handle, float(time))

    def fade(self, target, duration=1.0):
        """Fade volume to *target* over *duration* seconds."""
        if self._handle is not None:
            _audio.fade(self._handle, float(target), float(duration))

    def fade_in(self, duration=1.0):
        """Fade volume from 0 to current volume."""
        if self._handle is not None:
            info = self._info()
            target = info["volume"] if info else 1.0
            _audio.set_volume(self._handle, 0.0)
            _audio.fade(self._handle, target, float(duration))

    def fade_out(self, duration=1.0):
        """Fade volume to 0 (auto-stops when done)."""
        if self._handle is not None:
            _audio.fade(self._handle, 0.0, float(duration))

    # ── Per-channel effects ──

    def reverb(self, mix=40.0, preset=REVERB_LARGE_HALL):
        """Enable per-channel reverb."""
        if self._handle is not None:
            _audio.channel_reverb(self._handle, mix=float(mix), preset=int(preset))

    def reverb_off(self):
        """Disable per-channel reverb."""
        if self._handle is not None:
            _audio.channel_reverb_off(self._handle)

    def delay(self, time=0.3, feedback=50.0, mix=30.0):
        """Enable per-channel delay."""
        if self._handle is not None:
            _audio.channel_delay(self._handle, time=float(time),
                                 feedback=float(feedback), mix=float(mix))

    def delay_off(self):
        """Disable per-channel delay."""
        if self._handle is not None:
            _audio.channel_delay_off(self._handle)

    def distortion(self, preset=DISTORTION_DRUMS_BIT_BRUSH, mix=50.0):
        """Enable per-channel distortion."""
        if self._handle is not None:
            _audio.channel_distortion(self._handle, preset=int(preset),
                                      mix=float(mix))

    def distortion_off(self):
        """Disable per-channel distortion."""
        if self._handle is not None:
            _audio.channel_distortion_off(self._handle)

    def eq(self, low=0.0, mid=0.0, high=0.0):
        """Enable per-channel 3-band EQ (gains in dB)."""
        if self._handle is not None:
            _audio.channel_eq(self._handle, low=float(low),
                              mid=float(mid), high=float(high))

    def eq_off(self):
        """Disable per-channel EQ."""
        if self._handle is not None:
            _audio.channel_eq_off(self._handle)

    def _info(self):
        if self._handle is None:
            return None
        return _audio.channel_info(self._handle)

    def __repr__(self):
        return f"Channel({self._handle})"

    def __del__(self):
        try:
            if self._handle is not None:
                _audio.release_channel(self._handle)
        except Exception:
            pass



# ── Sound ──

class Sound:
    """A loaded audio resource. Call .play() to create a channel.

    Audio decoding happens asynchronously in a background thread.
    The constructor returns immediately; queued playback starts automatically
    when decoding completes. Use ``loaded`` to check status; decoding failures
    raise RuntimeError when queried or played.

    Parameters
    ----------
    source : str, bytes, or bytearray
        File path (str) or in-memory audio data (bytes).
        Bytes can be any supported format (WAV, MP3, AAC, etc.).
    """
    __slots__ = ("_handle", "path")

    def __init__(self, source):
        if isinstance(source, (bytes, bytearray, memoryview)):
            self.path = "<bytes>"
            self._handle = _audio.load_bytes(bytes(source))
        else:
            from pathlib import Path as _P
            p = _P(source)
            if not p.is_absolute():
                p = _P.cwd() / p
            self.path = str(p)
            self._handle = _audio.load(self.path)

    @classmethod
    def from_pcm(cls, data, *, channels=1, sample_rate=44100):
        """Create a Sound from raw float32 PCM samples.

        Parameters
        ----------
        data : bytes
            Float32 samples. Mono: [s0,s1,...]. Stereo interleaved: [L0,R0,L1,R1,...].
        channels : int
            1 (mono) or 2 (stereo).
        sample_rate : int
            Sample rate in Hz.
        """
        obj = object.__new__(cls)
        obj.path = "<pcm>"
        obj._handle = _audio.load_pcm(bytes(data), channels=channels,
                                       sample_rate=sample_rate)
        return obj

    @property
    def handle(self):
        return self._handle

    @property
    def loaded(self):
        """True when ready to play; raises RuntimeError if decoding failed."""
        if self._handle is None:
            return False
        return _audio.sound_loaded(self._handle)

    def _sound_info(self):
        if self._handle is None:
            return None
        return _audio.sound_info(self._handle)

    @property
    def duration(self):
        """Duration in seconds."""
        info = self._sound_info()
        return info["duration"] if info else 0.0

    @property
    def channels(self):
        """Number of audio channels (1=mono, 2=stereo)."""
        info = self._sound_info()
        return info["channels"] if info else 0

    @property
    def sample_rate(self):
        """Sample rate in Hz."""
        info = self._sound_info()
        return info["sample_rate"] if info else 0

    @property
    def frames(self):
        """Total frame count."""
        info = self._sound_info()
        return info["frames"] if info else 0

    def play(self, *, loop=False, volume=1.0, pan=0.0, pitch=1.0, music=False,
             rate=None, semitones=0.0):
        """Play the sound. Returns a Channel immediately.

        If the sound is still decoding (``loaded`` is False), playback
        starts automatically when decoding completes.

        Parameters
        ----------
        loop : bool
            Loop continuously.
        volume : float
            Channel volume (0.0 - 1.0).
        pan : float
            Stereo pan (-1.0 left, 0.0 center, 1.0 right).
        pitch : float
            Legacy name for playback rate (0.25 - 4.0, 1.0 = normal).
        rate : float, optional
            Playback rate, preserving pitch. Specify rate or legacy pitch.
        semitones : float
            Independent pitch shift (-24...24), 12 = one octave up.
        music : bool
            Compatibility flag; uses the same playback and global controls.

        Returns
        -------
        Channel
        """
        if rate is not None:
            if pitch != 1.0:
                raise ValueError("Specify rate or legacy pitch, not both.")
            pitch = rate
        h = _audio.play(self._handle, loop=loop, volume=volume,
                        pan=pan, pitch=pitch, music=music, semitones=semitones)
        return Channel(h, sound=self)

    def export(self, path, **options):
        """Render this sound to WAV (16-bit), CAF (float32), or M4A (AAC)."""
        return export([self], path, **options)

    def to_pcm(self):
        """Copy decoded samples as interleaved float32 bytes; Stream is excluded."""
        return _audio.sound_pcm(self._handle)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def unload(self):
        """Release the loaded audio data."""
        if self._handle is not None:
            _audio.unload(self._handle)
            self._handle = None

    def close(self):
        """Alias for unload()."""
        self.unload()

    def __repr__(self):
        return f"Sound({self.path!r})"

    def __del__(self):
        try:
            self.unload()
        except Exception:
            pass


# ── Recorder ──

class Stream(Sound):
    """A local file decoded incrementally during playback.

    Supports the same Channel controls and effects as Sound. Each play opens
    an independent file reader. No whole-file PCM allocation is made.
    Keep the source file available until all playback channels have stopped.
    """
    __slots__ = ()

    def __init__(self, path):
        self._handle = None
        self.path = _os.path.abspath(_os.fsdecode(_os.fspath(path)))
        self._handle = _audio.load_stream(self.path)

class Recorder:
    """Audio recorder using device microphone.

    Example::

        rec = audio.Recorder()
        rec.start()
        # ... record ...
        data = rec.stop()                    # raw float32 PCM bytes
        snd = audio.Sound.from_pcm(data, channels=rec.channels,
                                   sample_rate=rec.sample_rate)
        snd.play()

        # Or save to file:
        rec.start()
        # ...
        rec.stop(path="recording.wav")       # saves WAV file
    """
    __slots__ = ("_handle", "_sample_rate", "_channels", "_pending_data", "_file_path", "_frames")

    def __init__(self):
        self._handle = None
        self._sample_rate = 44100
        self._channels = 1
        self._pending_data = None
        self._file_path = None
        self._frames = 0

    def start(self, *, sample_rate=44100, channels=1, path=None, overwrite=False):
        """Start recording from microphone.

        Note: actual sample_rate and channels may differ from requested
        values due to hardware constraints. The actual values are stored
        in self.sample_rate and self.channels after start().

        path selects bounded, direct-to-file recording (.wav, .caf, .m4a).
        Existing files require overwrite=True. Call stop() without a path to
        flush/finalize; it returns None. With no path, PCM accumulates in memory
        and the existing stop()/stop(path=...) behavior is preserved.
        """
        if self._handle is not None:
            raise RuntimeError("Already recording.")
        if self._pending_data is not None:
            raise RuntimeError("Retrieve or save the previous recording with stop() before starting again.")
        file_path = _os.path.abspath(_os.fsdecode(_os.fspath(path))) if path is not None else None
        info = _audio.recorder_start(sample_rate=sample_rate, channels=channels,
                                      path=file_path, overwrite=overwrite)
        self._handle = info["handle"]
        self._sample_rate = int(info["sample_rate"])
        self._channels = info["channels"]
        self._file_path = file_path
        self._frames = 0

    def stop(self, path=None):
        """Stop recording and return PCM data.

        Parameters
        ----------
        path : str, optional
            If provided, save as WAV file and return None.
            Otherwise, return raw float32 PCM bytes.

        Returns
        -------
        bytes or None

        If saving fails, the PCM data is retained. Call stop() to retrieve
        it, or stop(path=...) to retry saving. An empty recording can also
        be saved as a valid WAV file.
        """
        if self._file_path is not None:
            if path is not None:
                raise ValueError("File recording already has a destination; use stop() without a path.")
            if self._handle is not None:
                try:
                    final_frames = _audio.recorder_stop(self._handle)
                    if isinstance(final_frames, int):
                        self._frames = final_frames
                    return None
                finally:
                    self._handle = None
            return None
        if self._handle is not None:
            data = _audio.recorder_stop(self._handle)
            self._pending_data = data if data is not None else b""
            self._frames = len(self._pending_data) // (self.channels * 4)
            self._handle = None
        if self._pending_data is None:
            return b""
        data = self._pending_data
        if path is not None:
            self._save_wav(path, data)
            self._pending_data = None
            return None
        self._pending_data = None
        return data

    @property
    def frames(self):
        """Frames captured so far (or at the end of file recording)."""
        if self._handle is not None:
            self._frames = _audio.input_info(self._handle)["frames"]
        return self._frames

    @property
    def duration(self):
        return self.frames / self.sample_rate

    def close(self):
        """Stop, finalizing file recordings and discarding in-memory recordings."""
        self.stop()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def sample_rate(self):
        """Actual recording sample rate (set after start())."""
        return self._sample_rate

    @property
    def channels(self):
        """Actual recording channel count (set after start())."""
        return self._channels

    @property
    def is_recording(self):
        """True if currently recording."""
        if self._handle is None:
            return False
        return _audio.recorder_is_recording(self._handle)

    def _save_wav(self, path, pcm_data):
        """Save raw float32 PCM as WAV file."""
        from contextlib import ExitStack
        import struct
        import array
        floats = array.array('f', pcm_data)
        # Convert float32 to int16
        int16_data = bytearray(len(floats) * 2)
        for i, s in enumerate(floats):
            clamped = max(-1.0, min(1.0, s))
            struct.pack_into('<h', int16_data, i * 2, round(clamped * 32767))

        sr = self._sample_rate
        ch = self._channels
        bits = 16
        byte_rate = sr * ch * bits // 8
        block_align = ch * bits // 8
        data_size = len(int16_data)

        path = _os.path.abspath(_os.fsdecode(_os.fspath(path)))
        with ExitStack() as access:
            guard = _audio.file_access_open(path)
            access.callback(_audio.file_access_close, guard)
            # Keep access through writes and close/flush, including failed saves.
            f = access.enter_context(open(path, 'wb'))
            f.write(b'RIFF')
            f.write(struct.pack('<I', 36 + data_size))
            f.write(b'WAVE')
            f.write(b'fmt ')
            f.write(struct.pack('<I', 16))
            f.write(struct.pack('<H', 1))        # PCM format
            f.write(struct.pack('<H', ch))
            f.write(struct.pack('<I', sr))
            f.write(struct.pack('<I', byte_rate))
            f.write(struct.pack('<H', block_align))
            f.write(struct.pack('<H', bits))
            f.write(b'data')
            f.write(struct.pack('<I', data_size))
            f.write(int16_data)

    def __del__(self):
        try:
            if self._handle is not None:
                _audio.recorder_stop(self._handle)
        except Exception:
            pass


def _deadline(timeout):
    if timeout is None:
        return None
    timeout = float(timeout)
    if not _math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be a finite nonnegative number or None.")
    return _time.monotonic() + timeout


def _expired(deadline):
    return deadline is not None and _time.monotonic() >= deadline


class InputStream:
    """Bounded microphone input, read by Python without real-time callbacks.

    Use as a context manager or call start()/close(). read() returns native
    float32 interleaved bytes in the actual hardware format. If Python falls
    behind, new incoming frames are dropped and counted by dropped_frames.
    Only one Recorder or InputStream can use the microphone at a time.
    """
    def __init__(self, *, sample_rate=44100, channels=1, buffer_duration=0.5):
        self._handle = None
        self._sample_rate = sample_rate
        self._channels = channels
        self.buffer_duration = buffer_duration

    @property
    def sample_rate(self):
        return self._sample_rate

    @property
    def channels(self):
        return self._channels

    def start(self):
        if self._handle is not None:
            raise RuntimeError("Input stream is already open.")
        info = _audio.recorder_start(sample_rate=self.sample_rate, channels=self.channels,
                                    stream=True, buffer_duration=self.buffer_duration)
        self._handle = info["handle"]
        self._sample_rate = info["sample_rate"]
        self._channels = info["channels"]
        return self

    def _info(self):
        if self._handle is None:
            raise RuntimeError("Input stream is closed.")
        return _audio.input_info(self._handle)

    @property
    def available_frames(self):
        return self._info()["available_frames"]

    @property
    def dropped_frames(self):
        return self._info()["dropped_frames"]

    @property
    def capacity(self):
        return self._info()["capacity"]

    @property
    def active(self):
        return self._handle is not None and _audio.recorder_is_recording(self._handle)

    def read(self, frames, *, block=True, timeout=None):
        """Read up to frames (1...65536); timeout/nonblocking reads may be short."""
        frames = _operator.index(frames)
        if not 1 <= frames <= 65536:
            raise ValueError("frames must be 1...65536.")
        deadline = _deadline(timeout)
        chunks, remaining = [], frames
        while remaining:
            if self._handle is None:
                raise RuntimeError("Input stream is closed.")
            chunk = _audio.input_read(self._handle, remaining)
            if chunk:
                chunks.append(chunk)
            remaining -= len(chunk) // (self.channels * 4)
            if not block or not remaining or _expired(deadline):
                break
            if not chunk:
                _time.sleep(0.005)
        return b"".join(chunks)

    def close(self):
        if self._handle is not None:
            try:
                _audio.recorder_stop(self._handle)
            finally:
                self._handle = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class OutputStream:
    """Bounded float32 PCM output for synthesis or decoded audio blocks.

    write() provides backpressure and returns the number of accepted frames.
    A nonblocking write or timeout may accept only a prefix: retry the suffix.
    Underruns produce silence. Use a context manager or call close() explicitly.
    """
    def __init__(self, *, sample_rate=44100, channels=1, buffer_duration=0.5, start=True):
        self._handle = None
        self._handle = _audio.output_open(float(sample_rate), _operator.index(channels),
                                          float(buffer_duration), not start)
        self._sample_rate = float(sample_rate)
        self._channels = _operator.index(channels)

    @property
    def sample_rate(self):
        return self._sample_rate

    @property
    def channels(self):
        return self._channels

    def _info(self):
        if self._handle is None:
            raise RuntimeError("Output stream is closed.")
        return _audio.output_info(self._handle)

    @property
    def buffered_frames(self):
        return self._info()["buffered_frames"]

    @property
    def capacity(self):
        return self._info()["capacity"]

    @property
    def available_frames(self):
        info = self._info()
        return info["capacity"] - info["buffered_frames"]

    @property
    def underrun_frames(self):
        return self._info()["underrun_frames"]

    def write(self, data, *, block=True, timeout=None):
        """Accept contiguous float32 PCM bytes or a buffer such as a NumPy array."""
        deadline = _deadline(timeout)
        data = memoryview(data).cast("B")
        frame_bytes = self.channels * 4
        if len(data) % frame_bytes:
            raise ValueError("PCM must contain complete float32 frames.")
        written = 0
        while written * frame_bytes < len(data):
            if self._handle is None:
                raise RuntimeError("Output stream is closed.")
            offset = written * frame_bytes
            count = _audio.output_write(self._handle, data[offset:offset + 65536 * frame_bytes])
            written += count
            if not block or _expired(deadline):
                break
            if not count:
                _time.sleep(0.005)
        return written

    def pause(self):
        if self._handle is not None:
            _audio.output_pause(self._handle, True)

    def resume(self):
        if self._handle is not None:
            _audio.output_pause(self._handle, False)

    def drain(self, *, timeout=None):
        """Wait until the engine consumes queued PCM; hardware latency can remain."""
        deadline = _deadline(timeout)
        while self.buffered_frames:
            if _expired(deadline):
                return False
            _time.sleep(0.005)
        return True

    def close(self):
        if self._handle is not None:
            try:
                _audio.output_close(self._handle)
            finally:
                self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class Track:
    """An offline mix input: source, timeline start, source offset/duration and effects.

    duration trims the source before rate is applied. loop=True repeats that
    region and requires an explicit total duration for mix()/export().
    Effects are dictionaries, e.g. {"reverb": {"mix": 25}, "eq": {"low": 3}}.
    """
    def __init__(self, source, *, start=0.0, offset=0.0, duration=None, volume=1.0,
                 pan=0.0, rate=1.0, semitones=0.0, loop=False, effects=None):
        self.source = source
        self.start, self.offset, self.duration = start, offset, duration
        self.volume, self.pan = volume, pan
        self.rate, self.semitones, self.loop = rate, semitones, loop
        self.effects = effects

    def _spec(self):
        source = self.source.handle if isinstance(self.source, Sound) else _os.fsdecode(_os.fspath(self.source))
        return (source, float(self.start), float(self.offset),
                -1.0 if self.duration is None else float(self.duration),
                float(self.volume), float(self.pan), float(self.rate), float(self.semitones),
                bool(self.loop), _effect_spec(self.effects))


def _effect_spec(effects):
    effects = {} if effects is None else dict(effects)
    definitions = {
        "reverb": (("mix", 40.0, float), ("preset", REVERB_LARGE_HALL, int)),
        "delay": (("time", 0.3, float), ("feedback", 50.0, float), ("mix", 30.0, float)),
        "distortion": (("preset", DISTORTION_DRUMS_BIT_BRUSH, int), ("mix", 50.0, float)),
        "eq": (("low", 0.0, float), ("mid", 0.0, float), ("high", 0.0, float)),
    }
    unknown = effects.keys() - definitions.keys()
    if unknown:
        raise ValueError(f"Unknown effects: {sorted(unknown)}")
    result = []
    for name, fields in definitions.items():
        if name not in effects:
            result.append(None)
            continue
        values = dict(effects[name])
        unknown = values.keys() - {field[0] for field in fields}
        if unknown:
            raise ValueError(f"Unknown {name} parameters: {sorted(unknown)}")
        result.append(tuple(convert(values.get(key, default)) for key, default, convert in fields))
    return tuple(result)


def _tracks(tracks):
    if isinstance(tracks, (Track, Sound, str, _os.PathLike)):
        tracks = [tracks]
    result = []
    for track in tracks:
        if len(result) == 64:
            raise ValueError("At most 64 tracks are supported.")
        result.append(track if isinstance(track, Track) else Track(track))
    if not result:
        raise ValueError("Provide at least one audio track.")
    return result


def _render_blocks(tracks, sample_rate, channels, duration, tail, effects):
    handle = _audio.render_open([track._spec() for track in tracks], float(sample_rate),
                               _operator.index(channels), -1.0 if duration is None else float(duration),
                               float(tail), _effect_spec(effects))
    try:
        while block := _audio.render_read(handle):
            yield block
    finally:
        _audio.render_close(handle)


def mix(tracks, *, sample_rate=44100, channels=2, duration=None, tail=0.0, effects=None):
    """Render tracks/effects into a new Sound, without playing through the device.

    The result is kept in memory. Use export() for long mixes. tail adds silence
    after the source ends so delay/reverb can decay; it defaults to zero.
    """
    data = b"".join(_render_blocks(_tracks(tracks), sample_rate, channels, duration, tail, effects))
    return Sound.from_pcm(data, sample_rate=sample_rate, channels=channels)


def export(tracks, path, *, sample_rate=44100, channels=2, duration=None, tail=0.0,
           effects=None, overwrite=False):
    """Render to WAV (16-bit), CAF (float32), or M4A (AAC), using bounded blocks.

    Existing files require overwrite=True. On write failure or Stop, the file is
    closed but may contain only the completed prefix. Returns the absolute path.
    """
    from contextlib import ExitStack, closing
    from itertools import chain
    tracks = _tracks(tracks)
    path = _os.path.abspath(_os.fsdecode(_os.fspath(path)))
    sources = [track.source.path if isinstance(track.source, Sound) else _os.fsdecode(_os.fspath(track.source))
               for track in tracks]
    with ExitStack() as access:
        # Both sides need access before realpath/stat, including when the source
        # is local or belongs to a different external root than the destination.
        for location in [path, *sources]:
            guard = _audio.file_access_open(location)
            access.callback(_audio.file_access_close, guard)
        for source in sources:
            if _os.path.realpath(source) == _os.path.realpath(path):
                raise ValueError("Export destination must differ from every source file.")
        with closing(_render_blocks(tracks, sample_rate, channels, duration, tail, effects)) as blocks:
            first = next(blocks)  # validate sources/effects before creating the destination
            # Reject hard-link aliases before overwrite=True can truncate an input.
            for source in sources:
                try:
                    same_file = _os.path.samefile(source, path)
                except FileNotFoundError:
                    same_file = False
                if same_file:
                    raise ValueError("Export destination must differ from every source file.")
            handle = _audio.writer_open(path, float(sample_rate), _operator.index(channels), bool(overwrite))
            try:
                for block in chain((first,), blocks):
                    _audio.writer_write(handle, block)
            except BaseException:
                try:
                    _audio.writer_close(handle)
                except Exception:
                    pass
                raise
            else:
                _audio.writer_close(handle)
    return path


# ── Convenience functions (module-level) ──

def load(source):
    """Load a sound, return a Sound object. Accepts file path or bytes."""
    return Sound(source)


def play(source, *, loop=False, volume=1.0, pan=0.0, pitch=1.0, music=False,
         rate=None, semitones=0.0):
    """One-shot: load and play a sound. Returns (Sound, Channel)."""
    snd = Sound(source)
    ch = snd.play(loop=loop, volume=volume, pan=pan, pitch=pitch, music=music,
                  rate=rate, semitones=semitones)
    return snd, ch


def tone(frequency=440, duration=0.2, *, volume=0.5, sample_rate=44100):
    """Generate a sine tone and return a Sound.

    Parameters
    ----------
    frequency : float
        Frequency in Hz.
    duration : float
        Duration in seconds.
    volume : float
        Amplitude (0.0 - 1.0).
    sample_rate : int
        Sample rate in Hz.

    Returns
    -------
    Sound
    """
    obj = object.__new__(Sound)
    obj.path = "<tone>"
    obj._handle = _audio.generate_tone(float(frequency), float(duration),
                                        float(volume), float(sample_rate))
    return obj


def noise(duration=0.2, *, volume=0.3, sample_rate=44100):
    """Generate white noise and return a Sound.

    Parameters
    ----------
    duration : float
        Duration in seconds.
    volume : float
        Amplitude (0.0 - 1.0).
    sample_rate : int
        Sample rate in Hz.

    Returns
    -------
    Sound
    """
    obj = object.__new__(Sound)
    obj.path = "<noise>"
    obj._handle = _audio.generate_noise(float(duration), float(volume),
                                         float(sample_rate))
    return obj


# ── Master volume ──

def get_master_volume():
    """Get the master volume (0.0 - 1.0)."""
    return _audio.get_master_volume()


def set_master_volume(v):
    """Set the master volume (0.0 - 1.0)."""
    _audio.set_master_volume(float(v))


# Make master_volume accessible as audio.master_volume = 0.5
import sys as _sys

class _AudioModule(_sys.modules[__name__].__class__):
    @property
    def master_volume(self):
        return _audio.get_master_volume()

    @master_volume.setter
    def master_volume(self, v):
        _audio.set_master_volume(float(v))

_sys.modules[__name__].__class__ = _AudioModule


# ── Global control ──

def pause_all():
    """Pause all active channels."""
    _audio.pause_all()

def resume_all():
    """Resume all paused channels."""
    _audio.resume_all()

def stop_all():
    """Stop all channels."""
    _audio.stop_all()

def close():
    """Shut down the audio engine and release all resources."""
    _audio.close()


def get_events(max_events=64):
    """Consume up to 1...256 queued playback/system events without waiting.

    The process-wide queue keeps the latest 256 native events. An overflow
    event reports discarded older entries. close() clears pending events.
    No Python callback runs on an audio thread.
    """
    return _audio.get_events(_operator.index(max_events))


def get_device_info():
    """Return current routes, format, latency and microphone permission as a dict.

    Does not activate the audio session, start an engine, or ask permission.
    Durations are seconds; idle sessions may report zero format/latency values.
    """
    return _audio.get_device_info()


# ── Global effects ──

def reverb(mix=30.0, preset=REVERB_MEDIUM_HALL):
    """Enable global reverb effect.

    Parameters
    ----------
    mix : float
        Wet/dry mix (0-100).
    preset : int
        Reverb preset constant (REVERB_*).
    """
    _audio.reverb_enable(float(mix), int(preset))

def reverb_off():
    """Disable global reverb."""
    _audio.reverb_disable()

def eq(low=0.0, mid=0.0, high=0.0):
    """Set global 3-band EQ gains in dB.

    Parameters
    ----------
    low : float
        100 Hz band gain (-96 to +24 dB).
    mid : float
        1 kHz band gain.
    high : float
        8 kHz band gain.
    """
    _audio.eq_set(0, float(low))
    _audio.eq_set(1, float(mid))
    _audio.eq_set(2, float(high))

def eq_off():
    """Disable global EQ."""
    _audio.eq_disable()

def delay(time=0.3, feedback=50.0, mix=30.0):
    """Enable global delay effect.

    Parameters
    ----------
    time : float
        Delay time in seconds (0-2).
    feedback : float
        Feedback percentage (-100 to 100).
    mix : float
        Wet/dry mix (0-100).
    """
    _audio.delay_enable(time=float(time), feedback=float(feedback),
                        mix=float(mix))

def delay_off():
    """Disable global delay."""
    _audio.delay_disable()

def distortion(preset=DISTORTION_DRUMS_BIT_BRUSH, mix=50.0):
    """Enable global distortion effect.

    Parameters
    ----------
    preset : int
        Distortion preset constant (DISTORTION_*).
    mix : float
        Wet/dry mix (0-100).
    """
    _audio.distortion_enable(preset=int(preset), mix=float(mix))

def distortion_off():
    """Disable global distortion."""
    _audio.distortion_disable()
