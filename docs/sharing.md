# Sharing

`share` presents the operating system's sharing interface on iOS and macOS.
Importing it does not open a window, inspect user content, or import Pillow.
The user chooses the destination; available services depend on the supplied
content and installed applications.

## Content

```python
import share

result = share.present(text="Hello from Python", urls=["https://example.org/"])
print(result.completed, result.activity)
```

Both `open()` and `present()` accept these keyword-only content arguments:

| Argument | Accepted values | Behavior |
| --- | --- | --- |
| `text` | A string or `None` | One text item, including an empty string. |
| `files` | An iterable of local paths | Existing files, including documents, images, audio and video. |
| `urls` | An iterable of absolute non-file URL strings | Shares the URLs without downloading them. |
| `images` | An iterable of Pillow images or encoded image buffers | Decodes images for native image-sharing services. |
| `attachments` | A mapping of plain filenames to contiguous buffers | Preserves the exact bytes in library-managed temporary files. |

Content can be combined in one request. All inputs must be valid before the
sharing UI appears. Put individual images in a sequence, even for one image;
use `images=[image]` rather than `images=image`.

### Images

```python
from PIL import Image
import share

with Image.new("RGBA", (320, 180), (30, 100, 200, 180)) as image:
    result = share.present(images=[image])
```

Encoded PNG/JPEG bytes, `bytearray` and C-contiguous `memoryview` inputs do not
require Pillow. The OS decoder may accept additional image formats. Pillow
inputs use their current frame with EXIF orientation applied. The native image
representation does not promise to preserve the original encoding, metadata,
or animation; use `attachments={"original.jpg": jpeg_bytes}` for exact bytes.

### Named memory content

```python
import share

report = "name,value\nalpha,12\nbeta,34\n".encode("utf-8")
result = share.present(attachments={"report.csv": report})
```

Filenames must be nonempty strings other than `.` or `..`, without `/`, `\`,
or NUL. Use a meaningful extension so the destination can identify the content.
Empty buffers are valid empty files. Each attachment has its own subdirectory,
so names that differ only in case remain distinct on case-insensitive storage.

Buffers are copied before `open()` returns. Later mutation of a source buffer
does not change the shared image or attachment. Binary data is passed directly
through the Python buffer protocol; it is never encoded as JSON or Base64.
For a large file that already exists on disk, use `files` to avoid loading its
contents into Python memory.

## Waiting, results and cleanup

`present(..., timeout=300)` opens the interface and waits for completion. A user
cancellation returns `ShareResult(completed=False, ...)`, including a macOS
service reporting `NSUserCancelledError`. Real service errors raise `OSError`.
Invalid arguments fail before presentation. A timeout raises `TimeoutError`.
If a foreground presentation context is unavailable, opening raises `RuntimeError`
and releases any files prepared for that request.

`activity` is the platform's result information: an activity type string on iOS,
a localized service title on macOS, or `None` when no service was selected. Use
it for diagnostics, not portable branching or delivery confirmation. Even a
successful system service does not prove that an eventual recipient received
or read the content.

Use `open()` when the caller needs explicit lifetime control:

```python
import share

with share.open(attachments={"notes.txt": b"Notes"}) as request:
    result = request.wait(timeout=120)
```

`request.done` queries completion. `wait()` can be called repeatedly and a wait
timeout does not close the request. `close()` is idempotent and closes the Python
handle; waiting on a closed handle raises `ValueError`. Context-manager exit
also closes it.

On iOS, `close()` requests cancellation of the visible share sheet, including
any service UI it is presenting. It returns before the dismissal animation
finishes; the native request keeps attachments and file access alive until the
dismissal completes. A close requested while the sheet is opening takes effect
after presentation finishes. The caller does not need to close the sheet by hand.

If UIKit already removed the local sheet during a handoff, the request retains
its resources until the activity reports completion. On macOS, closing an
unselected picker cancels it immediately, while an already selected service
keeps its resources until completion. Closing cannot undo content a service has
already sent. `present()` also closes its request on timeout; `wait()` alone
leaves it open. Original local files are never deleted.

Temporary directories use the `cocoa_py_share_` prefix. Host cache managers can
recognize that prefix. iPad presentation includes a popover anchor. On macOS,
the library opens a small window whose Share button presents the system picker;
desktop scripts must service the main event loop, as described in
[macOS execution](macos.md).
