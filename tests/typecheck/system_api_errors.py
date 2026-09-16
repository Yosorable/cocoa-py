"""Each marked line must be rejected by installed-package type checking."""

import clipboard
import device
import location
import motion
import share


def invalid_calls() -> None:
    location.watch(capacity="4")  # expected-error
    location.heading(orientation="up")  # expected-error
    motion.watch("unknown")  # expected-error
    share.present(attachments={"report.txt": "unencoded text"})  # expected-error
    clipboard.write_bytes("unencoded text", type="public.data")  # expected-error
    device.info().platform = "macos"  # expected-error
