# Clipboard

Import `clipboard` directly on iOS or macOS. Importing it does not read the
clipboard, request permission, import Pillow, or initialize other cocoa-py
modules. Clipboard functions run their native work on the main thread.

These APIs are available in `0.1.0a4` and later. The earlier `0.1.0a3` release
contains the original text and typed-byte API.

## Text and URLs

```python
import clipboard

clipboard.write_text("Hello")
assert clipboard.read_text() == "Hello"

clipboard.write_url("https://example.org/", local_only=True)
assert clipboard.read_url() == "https://example.org/"
assert clipboard.read_text() == "https://example.org/"
```

`write_url()` accepts an absolute URL, including custom schemes and file URLs,
and provides a plain-text fallback in the same item. It does not open the URL
or access a referenced file. Relative paths and unescaped invalid URL characters
are rejected. `read_url()` reads typed URL data. URL reads and `has_urls()`
require an advertised `public.url` or `public.file-url`
representation. URL objects, strings and UTF-8 data in these representations are
accepted. This contract is the same on iOS and macOS.

The OS may add representations: on iOS, `write_text("https://example.org/")`
can also make `public.url` available. In that case, `read_url()` returns the URL
and `has_urls()` is true. The module does not independently detect URLs in text,
and does not hide URL representations supplied by the OS. Available types may
differ between platforms; they do not reveal which write API originally created
the item. Use `write_url()` when you want to explicitly supply both URL and text
representations without depending on this OS behavior.

## Binary data and multiple representations

```python
clipboard.write_bytes(b"\x00\xffpayload", type="org.example.binary", local_only=True)
assert clipboard.read_bytes("org.example.binary") == b"\x00\xffpayload"

clipboard.write_item({
    "public.html": "<b>Hello</b>".encode("utf-8"),
    "public.utf8-plain-text": "Hello".encode("utf-8"),
})
assert clipboard.read_text() == "Hello"
assert clipboard.read_bytes("public.html") == b"<b>Hello</b>"
```

`write_item()` accepts a nonempty mapping from UTI strings to buffers. The
representations describe **one item** so the receiving application can choose a
format it understands. Every write replaces all old items; repeated
`write_bytes()` calls do not accumulate representations.

Buffers may be `bytes`, `bytearray`, or C-contiguous buffer-protocol objects
such as `memoryview`. Strided buffers are rejected; materialize them explicitly
with `bytes(view)` when needed. The bridge copies buffers into owned native
storage before releasing the GIL, without JSON or Base64. Mutating an input
after writing cannot change the clipboard. This is not a zero-copy guarantee.

A missing representation returns `None`; an existing empty one returns `b""`.
All mapping entries and options are checked before replacing the old contents.

## Images

```python
from PIL import Image

with Image.new("RGBA", (128, 128), (20, 100, 200, 128)) as image:
    clipboard.write_image(image, local_only=True)

image = clipboard.read_image()
if image is not None:
    try:
        print(image.size)
    finally:
        image.close()

png = clipboard.read_image(as_bytes=True)
if png is not None:
    clipboard.write_image(png)
```

`read_image()` returns a detached Pillow image, or `None` when no image is
available. Install Pillow separately, or install `cocoa-py[images]>=0.1.0a4`.
Pillow is loaded only for Pillow conversion.
`read_image(as_bytes=True)` returns PNG bytes, and `write_image(encoded_buffer)`
accepts OS-decodable image data, both without Pillow. File paths are not accepted.

Image convenience calls produce a still PNG with display orientation applied.
Pillow writes use the image's current frame. Animation, original metadata,
encoding and exact original byte layout are not preserved; use
`read_bytes(type)`/`write_bytes(data, type=...)` for raw representations.
Image decoding errors on write are reported before replacing the clipboard.

## Options and metadata

Every writing function accepts the same keyword-only options:

| Option | iOS | macOS |
| --- | --- | --- |
| `local_only=False` | Allow Universal Clipboard according to system settings. | Same. |
| `local_only=True` | Keep contents on this device. | Use AppKit's current-host-only option. |
| `expires_in=None` | No explicit expiration. | Supported. |
| `expires_in=seconds` | OS-managed expiration in 0.001..31536000 seconds. | Raise `NotImplementedError` before modifying contents. |

`local_only` must be a bool; expiration must be a finite number, not a bool.
Expiration is managed by iOS, not by a Python thread or timer.

`types()` returns the first item's UTI list, including representations added by
the OS. `has_text()`, `has_image()`, and
`has_urls()` query advertised types across **all** items without fetching their
contents. Thus `has_urls()` can be true while `read_url()` is `None` when only a
later item contains a URL. All content read functions address the first item.
macOS `types()` and byte reads now follow this same first-item contract as iOS.

`change_count()` returns the OS change counter without fetching content. Compare
it for inequality, not an exact increment. Another application may replace the
clipboard between any two calls; the counter is not a transaction lock.
`clear()` removes all items.

Content reads may show the operating system's paste-permission UI. Presence
queries do not grant permission or guarantee that advertised data will decode.
On macOS, call from the Python main thread, or provide a running AppKit event
loop when calling from another thread.

## Validation

The macOS regression suite uses an isolated named pasteboard with the production
native bridge and public Python wrapper. It covers binary ownership, images,
orientation, URL types, first-item semantics, multiple representations,
validation and unsupported-option behavior without changing the user's clipboard.
Install the `images` extra (Pillow) when running the complete regression suite.
Real iOS paste permission, OS-managed expiry and Universal Clipboard transfer
require device checks. Cross-application interoperability remains subject to the
receiving application's supported representations.
