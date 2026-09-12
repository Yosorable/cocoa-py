# Scene capture and image export

Capture renders the current scene or a node subtree into an owned CPU image.
It uses the Metal renderer shared by macOS and iPhoneOS. Neither capture
nor PNG encoding requires Pillow or NumPy. Capture does not create files;
`ImageData.save()` is the explicit file-writing operation.

## Capture a scene

Call from a running scene's `setup()`, `update()` or input callbacks:

```python
image = self.capture()
image.save("scene.png")
```

`Scene.capture(*, rect=None, size=None, background=None)` captures that scene's
current content, including its camera transform. It does not capture system
window decorations or compose a transition with another scene.

- `rect` is `(x, y, width, height)` in screen coordinates, in logical points.
  The origin is the top left, x goes right, and y goes down. The default is the
  full scene viewport. Pixels outside the content receive the background color.
- `size` is the output `(width, height)` in integer pixels. By default, the
  rectangle is rendered at the window's display scale. An explicit size controls
  output resolution independently of the display. A different aspect ratio
  stretches the rectangle to fill that size.
- `background` accepts the same color forms as scene drawing. The default is the
  scene background; `(0, 0, 0, 0)` requests transparency. Background colors and
  drawing are composited with premultiplied alpha internally, then converted to
  straight RGBA for the returned image.

For example, a 200-point square can be exported as a 600-pixel image:

```python
image = self.capture(rect=(20, 40, 200, 200), size=(600, 600))
png_bytes = image.to_png()
```

## Capture a node or drawing canvas

Every `Node` has the same `capture()` method. For nodes other than the root
scene, **an explicit `rect` in the node's local coordinates is required**. This
keeps the output stable for a blank canvas, changing text, particles and strokes
near the edges. Capture includes the node and its descendants, their visibility,
and their opacity. It excludes the node's own placement/rotation/scale, its
ancestors' transforms/opacity/visibility, the camera, and siblings. The default
background is transparent.

```python
# ink holds strokes whose local coordinates range from 0 to 280.
image = self.ink.capture(
    rect=(0, 0, 280, 280),
    size=(560, 560),
    background="#ffffff",
)
image.save("drawing.png")
```

The node must be attached to a running scene. Capture of a detached node, a
closed scene, or a node with a non-invertible transform raises an exception.
Small nonzero scales remain valid for local capture; a transform whose inverse
cannot be represented with finite values raises `ValueError`.
The dimensions must be finite and positive. Output is limited to 16,384 pixels
per axis and 67,108,864 total pixels before allocating Metal resources.
Layers clip their temporary textures to the capture rectangle before rasterizing.
Clipping preserves the existing Layer appearance for nonuniform transforms.
Tile maps use the capture rectangle for visibility, so a complete map can be
exported even when its logical dimensions exceed the window.
Intermediate render textures and glyph atlases are also checked before allocation:
they must fit 16,384 pixels per axis and 256 MiB of base pixel storage. Extreme
shader or text magnification that exceeds these limits raises `ValueError`, even
if the requested output image is small. Available GPU memory can impose lower
limits.

## ImageData

`scene.ImageData(width, height, rgba)` stores an immutable copy of packed RGBA8
pixels. Capture returns this type. Rows run from top to bottom with no padding;
each pixel contains red, green, blue, alpha, in that order. RGB is straight
(unassociated with alpha). A captured fully transparent pixel has zero RGB.

| Member | Result |
| --- | --- |
| `width`, `height`, `size` | Integer pixel dimensions. |
| `rgba` | Owned immutable pixel `bytes`, `width * height * 4` bytes long. |
| `to_png()` | PNG-encoded bytes in memory, using Apple ImageIO. |
| `save(path)` | Write PNG to a writable path. An existing file is replaced; a non-PNG extension is rejected. |
| `to_numpy(copy=True)` | Optional NumPy `uint8` array shaped `(height, width, 4)`. |
| `to_pil()` | Optional independent Pillow image in `RGBA` mode. |

The default NumPy result is a writable copy. `to_numpy(copy=False)` returns a
read-only view that retains the pixel bytes. The image, a view, and encoded PNG
bytes all remain valid after the originating scene, window or texture closes.
`ImageData` needs no `close()` call.

For model input, export directly to an array without encoding or writing a PNG:

```python
rgba = self.ink.capture(
    rect=(0, 0, 280, 280), size=(280, 280), background="#000000"
).to_numpy()
```

Apply the model's own grayscale, cropping, scaling and centering requirements
after capture. Capturing at 28 by 28 alone does not replace MNIST preprocessing.

## Texture readback

Low-level GPU users can call `texture.to_image(window, unpremultiply=True)` on
`bgra8` or `rgba8` textures. Pass the open window that encoded the texture's
rendering. It submits that window's queued offscreen draws, waits for the GPU,
and returns `ImageData`. Other texture formats raise an exception.

The default handles premultiplied pixels produced by scene rendering and image
loading. For a custom texture that already contains straight RGBA colors, pass
`unpremultiply=False`. Readback still converts BGRA channel order to RGBA and
removes any transfer row padding.

## Timing and ownership

Capture is an explicit synchronous operation on the scene's rendering thread.
It waits for earlier draws, renders the requested image, and waits for readback.
Use it when an image is needed, such as an export button or completed stroke.
Call between render, compute and blit passes; active passes and recursive capture
raise `RuntimeError`.

Capture does not call `update()`, advance actions, simulate physics, spawn
particles, move the camera or dispatch input. It restores node world transforms
used for touch handling and invalidates render caches as needed. Temporary
capture buffers and textures are released after capture, including failure paths.
Capture uses separate text and glyph caches, so exporting different resolutions
does not retain additional font atlases in the live scene. Layers keep their live
textures, and shaders first initialized during capture are restored so their next
normal render uses the display resolution. Already initialized shaders use their
current textures without advancing their simulation or feedback state.
Custom node rendering callbacks retain their usual responsibility for avoiding
application state changes during rendering.

## Device example

Run `python tools/scene_capture_demo.py` on macOS, or copy that script to an
iPhone host that has the matching `cocoa-py` build installed. Draw on the canvas,
save the drawing, then save the complete scene. The files are `drawing.png` and
`scene.png` in the script's current working directory. Drawing and exporting
continue to work after rotating or resizing the window.

The desktop regression tests exercise actual Metal rendering and ImageIO:

```sh
COCOA_PY_UI_TESTS=1 python -m unittest discover -s tests -p 'test_scene*.py' -v
```

These tests do not run an iOS simulator. Validate touch input, rotation, display
scale and the saved images separately on an iPhone.
