"""Exercise optional file-access hooks across actual dynamic library boundaries."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HOST = r'''
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
static atomic_int active, starts;
void *CocoaPyBeginFileAccess(const char *path) {
    atomic_fetch_add(&active, 1);
    atomic_fetch_add(&starts, 1);
    return strdup(path);
}
#ifndef INCOMPLETE_HOST
void CocoaPyEndFileAccess(void *token) {
    free(token);
    atomic_fetch_sub(&active, 1);
}
#endif
int host_active(void) { return atomic_load(&active); }
int host_starts(void) { return atomic_load(&starts); }
'''

PROBE = r'''
import ctypes
import os
from pathlib import Path
import sys
import tempfile
import time

host = ctypes.CDLL(sys.argv[1], mode=os.RTLD_GLOBAL)
import audio
import device

with tempfile.TemporaryDirectory() as temporary:
    path = Path(temporary) / "audio.wav"
    with audio.tone(440, duration=0.2) as tone:
        audio.export(tone, path, sample_rate=8000, channels=1)
    with audio.Sound(path) as sound:
        deadline = time.monotonic() + 5
        while not sound.loaded and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sound.loaded and sound.frames == 1600
    device.storage(temporary)
    audio.close()
    deadline = time.monotonic() + 5
    while host.host_active() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert host.host_active() == 0, "An asynchronous file token was leaked"
    if sys.argv[2] == "complete":
        assert host.host_starts() >= 4, "Independent native extensions did not find the host"
    else:
        assert host.host_starts() == 0, "An incomplete pair of hooks must never acquire a token"
'''


@unittest.skipUnless(sys.platform == "darwin", "Builds a small macOS host fixture.")
class HostHookTests(unittest.TestCase):
    def test_complete_and_incomplete_optional_hosts(self):
        with tempfile.TemporaryDirectory(prefix="cocoa_py_host_tests_") as temporary:
            source = Path(temporary) / "host.c"
            source.write_text(HOST)
            for complete in (True, False):
                with self.subTest(complete=complete):
                    binary = Path(temporary) / ("complete.dylib" if complete else "incomplete.dylib")
                    subprocess.run([
                        "xcrun", "clang", "-dynamiclib", "-std=c17", str(source), "-o", str(binary),
                        *([] if complete else ["-DINCOMPLETE_HOST=1"]),
                    ], check=True, capture_output=True)
                    result = subprocess.run([
                        sys.executable, "-c", PROBE, str(binary), "complete" if complete else "incomplete",
                    ], capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
