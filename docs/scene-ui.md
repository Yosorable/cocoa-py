# Scene UI foundations

Scene UI uses the same node tree, point units, and top-left screen origin as
other scene content. These APIs are available in the current source checkout.

## Clipping

Set `node.clip` to `ClipRect(x, y, width, height, radius=0)` or an
`(x, y, width, height)` tuple. The rectangle is in the node's local coordinates
and clips both the node and its descendants. Ancestor clips intersect it.
`None` removes the node's clip.

```python
from scene import ClipRect, Group, Rect

panel = Group(x=160, y=180, clip=ClipRect(-120, -80, 240, 160, radius=16))
panel.add(Rect(400, 240, fill="#2488cc"))
self.add(panel)
```

`ClipRect` is immutable. Replace it to resize or move a clip. Dimensions and
corner radius must be non-negative and all values finite. Radius is clamped
to half the shorter edge; zero width or height produces an empty clip.
Rotation, scale, and nested transforms apply to the clipping geometry.

Clipping is preserved through z sorting, shape/path/texture/particle batches,
cached layers, and image capture. Clipped descendants cannot receive a hit
outside the clip. Changing a clip invalidates ancestor `Layer` caches, and
clipped bounds constrain the texture allocated for cached content. As with
other scene hit tests, geometric queries use the last collected transforms.

Active `TextField` and `TextView` editors use matching native masks and hit
regions. Updating the clip or moving the editor preserves its native instance
and ongoing input-method composition. The native editor's existing placement
and overlay limitations are described in [text input](scene-text-input.md).

`Scene.capture()` includes screen-space clipping and the camera. Capturing a
node directly includes its own clip and descendants, and excludes ancestor
clips along with ancestor transforms. Capture restores the live scene's
interaction geometry afterward.

## Screen layers

Use `self.ui` inside a Scene to add screen-fixed content:

```python
from scene import Label

self.ui.add(Label("Score: 0", x=100, y=40))
```

`Scene.ui` lazily creates a `ScreenLayer(order=1)`. Its coordinates are viewport
points and do not change when the camera or Scene moves, rotates, or scales.
It inherits the Scene's opacity and visibility. A world-space clip does not
clip screen layers. Set a clip on the screen layer itself when needed.

For multiple planes, add `ScreenLayer(order=...)` directly to the Scene.
World content occupies plane 0; positive planes draw above it and negative
planes behind it. Plane order takes priority over each node's `z`, and nodes
within a plane follow normal z ordering. Hit testing follows the same order.
The order must be a signed 32-bit integer. A ScreenLayer must be a direct
Scene child; its own children can contain ordinary nested groups and layouts.

Use `Scene.size`, `Scene.safe_area`, and the `resize(width, height)` callback to
place content for the current viewport. Scene screenshots include screen
layers, with crop offsets and output scaling applied consistently.

## Scrolling

`ScrollView(width, height, ...)` provides a centered, clipped viewport. Its
`add()`, `remove()`, and `clear()` methods operate on `view.content`, an ordinary
Group. With automatic content sizing, the measured top-left of that group maps
to the viewport's top-left. Set `content_size=(width, height)` for an explicit
canvas starting at content coordinate `(0, 0)`, or assign `None` to return to
automatic measurement.

```python
from scene import Rect, ScrollView

view = ScrollView(280, 360, x=160, y=240, radius=12,
                  background="#182333", content_size=(280, 900))
for index in range(12):
    view.add(Rect(240, 56, x=140, y=32 + index * 72, radius=8,
                  fill="#3478b8"))
self.ui.add(view)
```

The following options can be configured on the view:

| Option | Behavior |
| --- | --- |
| `direction` | `"vertical"` (default), `"horizontal"`, or `"both"` |
| `bounces` | Elastic drag feedback at the content edges, enabled by default |
| `inertia` | Momentum after a content drag, enabled by default |
| `scrollbars` | `"auto"` (default), `"always"`, or `"never"` |
| `enabled` | Enables user scrolling; programmatic positioning remains available |
| `radius` | Corner radius of the managed viewport clip |
| `background`, `scrollbar_color` | Fill and indicator appearance |

`offset` is an `(x, y)` pair measured rightward/downward from the content's
top-left. Assign it or call `scroll_to(x=None, y=None, animated=False,
duration=0.25)`. `scroll_by(dx=0, dy=0, ...)` accepts the same animation options.
Offsets are clamped to the available range. `ensure_visible(node, margin=8,
animated=False)` reveals a descendant; `stop_scrolling()` cancels an interaction
or animation and returns immediately within bounds.

The read-only `max_offset` pair reports the current horizontal and vertical
limits, including content changes made before the next frame. Shrinking content
clamps the current offset to the new range during layout.

Drag the content or a visible scrollbar thumb. Nested views give the inner
view the first opportunity to scroll; at an edge, an outer view that can move
takes precedence over an inner bounce. On macOS, wheel and trackpad deltas
scroll the view under the pointer, with unused distance passed to ancestors.
Trackpad momentum comes from AppKit and is not amplified by a second fling.

`on_scroll(view)` reports changed offsets; adjacent changes are coalesced.
`on_scroll_begin(view)` and `on_scroll_end(view)` bracket an input or animated
scroll session, including momentum. Events run on the scene thread after
pointer handling. `dragging` and `scrolling` expose the current state.

A scroll drag cancels a child's pending tap. Inactive text inputs focus only
after a completed tap, and dragging the surrounding content keeps editing
active. Focusing an offscreen input reveals it. When a keyboard overlaps the
viewport, the available scrolling range expands without changing the view's
width or height. Native text editors keep their selection and composition.

Keyboard appearance, frame changes, and dismissal update avoidance immediately,
without animating scene content or the editor separately. Scroll content,
clipping, hit geometry, and the active native editor use the same layout. On
dismissal, the scroll offset is clamped to the new range when needed; it is not
forced back to the position before editing. The system keyboard retains its own
animation. `Scene.keyboard_frame` and `keyboard_changed` report the destination
keyboard rectangle, once per change.

ScrollView is a stacking context: child z values order content within the
view, its background is below the content, and its indicators are above it.
The whole view uses `view.z` when ordered against siblings, so a dialog above
the view also covers its scrollbars.

Cached `Layer` content retains interaction geometry and projects it along with
the cached texture. ScrollView invalidates ancestor layers when its managed
content or appearance changes. Other arbitrary changes to cached drawings
continue to require `Layer.invalidate()`. Capture preserves the live layer's
interaction snapshot.

## Paragraph labels

```python
from scene import Label

paragraph = Label(
    "A paragraph with Unicode text: 世界 👩🏽‍💻",
    size=20, max_width=280, alignment="left",
    line_spacing=4, max_lines=3, overflow="ellipsis",
)
width, height = paragraph.measure()
```

`Label` uses CoreText shaping and the same layout for measurement and drawing.
It supports fallback fonts, composed emoji, combining characters, explicit
newlines, and word or character wrapping. All geometry is in local points;
node, ancestor, and camera scales do not change `measure()`.

| Option | Behavior |
| --- | --- |
| `max_width=None` | Measure the longest explicit line. A positive number defines a paragraph box of that width, including when the text is shorter. |
| `alignment="left"` | Align each line to `"left"`, `"center"`, or `"right"` within the box. |
| `wrap="word"` | Wrap at words; `"char"` wraps at composed clusters; `"none"` uses only explicit newlines. An overwide cluster is clipped without splitting it. |
| `line_spacing=0` | Additional nonnegative spacing between lines, with no extra space after the last line. |
| `max_lines=None` | No line-count constraint. A positive integer limits the number of visible lines. |
| `overflow="clip"` | Clip overflow; `"ellipsis"` uses an ellipsis on the last visible line when text remains, or on an overwide unwrapped line. |

`line_count` reports the number of laid-out lines. `truncated` reports omitted
or clipped text. Empty text has zero intrinsic width and one line of font
metrics; an explicit paragraph width still applies. CRLF, CR, LF and Unicode
paragraph/line separators produce explicit line breaks, including blank lines.

`measure()` works before the node is attached and does not allocate a Metal
texture or open a window. Bitmap padding is excluded from layout and hit
bounds. Constrained paragraphs clip horizontal glyph overhang at their box;
unconstrained labels retain a small raster margin for overhangs. Font, text,
and layout changes invalidate cached ancestor Layers. Invalid or excessively
large raster layouts raise `ValueError` before requesting a Metal texture.
Rendered paragraphs use a bounded LRU, and capture uses an isolated cache.

Existing labels now receive shaped text and typographic bounds instead of
glyph-atlas padding and estimated widths. This can slightly change automatic
stack spacing. Label drawing also supports full affine transforms, and scaled
labels use the same local bounds in Python and native hit testing.

## Buttons, sliders, and switches

```python
from scene import Button, ControlStyle, Slider, Toggle

button = Button("Continue", width=180, x=160, y=60,
                on_click=lambda button: advance())
slider = Slider(.5, minimum=0, maximum=1, step=.05,
                x=160, y=130,
                on_change=lambda slider, value: set_volume(value),
                on_commit=lambda slider, value: save_volume(value))
toggle = Toggle(True, x=160, y=200,
                on_change=lambda toggle, value: set_music_enabled(value))
self.ui.add(button, slider, toggle)
```

All three are ordinary scene nodes, drawn by Metal and included in captures.
They expose `width`, `height`, `enabled`, `focusable`, `focused`, `pressed`,
`style`, `focus(show_ring=True)`, `blur()`, and `on_focus(control)` / `on_blur(control)`.
Touch activation gives focus without a persistent focus ring. Programmatic
focus shows the ring by default; `focus_visible` exposes this visual state.
Controls isolate their internal drawing order, so an overlay can cover the
whole widget regardless of its decorative children's `z` values. Decorations
can be added as child nodes. Setting `interactive=False` on decorations lets
the control own their touches.

`ControlStyle` is immutable. Use `dataclasses.replace(style, ...)` or a new
style to change `background`, `active`, `pressed`, `foreground`, `border`,
`focus`, `radius`, `border_width`, `focus_width`, `disabled_opacity`, `padding`,
`track_width`, or `thumb_size`. Color and geometry values are validated.
Control size, style, value, and state changes invalidate cached ancestor Layers.

`Button(text="", width=160, height=44, font_size=18, font=None, ...)` fires
`on_click(button)` only after a release inside the visible control. Moving
outside removes the pressed appearance; returning inside before release
restores it. Cancellation, scrolling, disabling, closing, and removal suppress
the click. Only one touch can press a control at a time. Its decorative
`label` can be customized; `button.text` forwards to `button.label.text`.
The default label is centered, constrained to the available width, and
truncated to one line.

`Toggle(value=False, width=56, height=34, ...)` fires
`on_change(toggle, value)` after a valid activation. Assigning `value` is silent;
`set_value(value, notify=True)` explicitly requests a change callback.

`Slider(value=0, width=220, height=40, minimum=0, maximum=1, step=None,
direction="horizontal", ...)` clamps values to its inclusive range. Steps are
anchored to `minimum`, and both endpoints remain reachable. `step=None` gives
continuous values. `minimum == maximum` is a valid constant range. Use
`set_range(minimum, maximum, step=None)` to update both limits atomically.
For a vertical slider, set `direction="vertical"` and a suitable width/height;
values increase from bottom to top.

Slider `on_change(slider, value)` reports live changes. Adjacent pending change
events coalesce to the latest value. `on_commit(slider, value)` reports a
completed adjustment, even if the value did not change. Cancellation keeps
the live value and calls `on_cancel(slider, value)` without a commit.
Programmatic value/range changes end the active adjustment silently and
discard stale change/commit callbacks. `set_value(value, notify=True)` opts
into a change callback. A callback can update a value, disable, close, or remove
its control without allowing a queued stale activation afterward.

Inside a ScrollView, a slider waits for a tap or a drag along its own local
axis before changing value. A perpendicular drag can scroll the container.
After a slider wins, it keeps the gesture until release or cancellation,
including if the finger changes direction or leaves the viewport. A vertical
slider in a vertical ScrollView takes vertical drags that begin on the slider;
drag the surrounding content to scroll.

## Control focus

`Scene.focused_node` returns the focused scene control or native text editor,
or `None`. Headless `TextInputSession` remains available through
`Scene.focused_input` and does not become a node. `Scene.focus_next(current=None,
reverse=False)` traverses eligible controls and text editors in node insertion
order, skipping hidden, disabled, nonfocusable, closed, and read-only entries.
It reveals the target in ancestor ScrollViews. Native editor Next/Previous
navigation uses this same list; `focus_next_input()` retains its input-only
behavior for applications that explicitly call it.

`control.focus()` can be requested before attachment and is applied when the
scene prepares its layout. Successful touch activation gives a control focus;
starting a native text editor clears ordinary control focus. `clear_focus()`
clears control focus and dismisses the active keyboard. Losing visibility,
disabling, detaching, or closing a focused control clears focus. Capture does
not apply pending focus requests or reveal their targets.

## Hardware keyboard events

```python
class PlayScene(Scene):
    def key_down(self, event):
        if event.key == "escape":
            self.show_pause_menu()
            return True

    def update(self, dt):
        if self.is_key_down("a"):
            self.player.x -= 120 * dt

    def key_up(self, event):
        if event.cancelled:
            self.clear_temporary_key_action(event.code)
```

Scene receives `key_down(KeyEvent)` and `key_up(KeyEvent)` for hardware keys
that its focused UI does not consume. `KeyEvent` is immutable and contains
`key`, `code`, `phase` (`"down"` or `"up"`), `modifiers`, `repeat`, `cancelled`,
and a monotonic `timestamp` in seconds.

`key` is a lowercase logical character or a name such as `"space"`, `"enter"`,
`"escape"`, `"tab"`, `"backspace"`, `"delete"`, `"left"`, `"right"`, `"up"`,
`"down"`, `"home"`, `"end"`, `"page_up"`, `"page_down"`, or `"f1"`–`"f24"`.
Unrecognized nonprinting keys use `"key_<code>"`. `code` is a physical USB HID
usage in the Keyboard/Keypad usage page on both platforms. It distinguishes
main Return from keypad Enter and remains independent of the keyboard layout.
Left and right modifiers have separate codes and key names, such as
`"left_shift"` and `"right_shift"`.

`modifiers` is a frozenset of `"shift"`, `"control"`, `"alt"`, `"command"`,
`"caps_lock"`, `"fn"`, and `"keypad"`, when supplied by the platform. The
function/numeric-pad flags describe the platform event and do not always mean
that a separate modifier key is physically held. Command shortcuts continue
through the native responder chain. System-reserved shortcuts may never reach
the scene.

`keys_down` and `key_codes_down` are immutable sets of unconsumed game keys.
`is_key_down(name_or_code)` queries either set. A control-owned press never
becomes a game press if focus changes before release. Interruptions send one
cancelled release for every held game key, clear the sets, and suppress late
repeats/releases. Queue overflow also cancels input rather than leaving keys
or touches stuck.

Focused Buttons and Toggles activate on Space/Enter release. Tab and Shift-Tab
navigate controls and editors. Sliders use arrow keys, Home/End, and Page Up/Down;
Shift accelerates an adjustment by a factor of ten. Sliders commit when the
last adjustment key is released. Escape first clears ordinary control focus;
otherwise it reaches `Scene.key_down`. Return `True` to handle Escape; an
unhandled Escape retains the default behavior of closing the scene window.

macOS uses native key-repeat events. UIKit hardware keys also have a bounded
repeat timer: `Scene.key_repeat_delay=.5` and `key_repeat_interval=.05`, in
seconds. A frame emits at most one timer repeat per held key, so delayed frames
do not replay a burst of repeats. Native repeats, when provided, take precedence.
Button/Toggle activation ignores repeated presses.

These APIs do not open the software keyboard or replace text input. While a
native editor or headless input session has focus, composition, editing keys,
and editing shortcuts stay with that editor. Entering text input cancels held
game keys. The scene surface resumes hardware-key input after editing ends.

## Window lifecycle

`Scene.window_state` is an immutable `WindowState(active, foreground, focused)`.
Override `window_state_changed(state)` to observe changes. Notifications are
deduplicated, and the property already contains the new state during the
callback. Read the initial state in `setup()`; initial state is not replayed as
a change notification.

| State | Meaning |
| --- | --- |
| `active` | The iOS window's UIScene is active, or the macOS application is active. |
| `foreground` | The iOS UIScene is in the foreground, or the macOS window is not minimized and its application is not hidden. |
| `focused` | This window is the platform's key window. It is independent of `active`. |

Loss of activity, foreground presentation, or window focus cancels pointer
capture, gestures, scrolling, control adjustments, and held keys. Ordinary
control focus is cleared and native editing ends without submitting. Lifecycle
callbacks run on the scene's Python thread, after interaction cleanup; native
observers do not call Python.

iOS scene updates, actions, physics, transitions, and GPU presentation suspend
while its UIScene is inactive or in the background. Resuming uses a zero time
step before returning to normal frame deltas. Native Metal entry points also
guard against activity changing during a frame: unsubmitted work is discarded,
the frame fence is released, and already-submitted work is scheduled before
background suspension. Explicit GPU passes or capture while suspended raise
`RuntimeError` (the private native subtype is `WindowSuspendedError`).

macOS scenes keep animating when another application is active. Set
`pause_when_inactive=True` to pause their updates/actions/physics as well.
Hidden/minimized windows suspend scene frames. Window focus loss alone does
not pause animations in other visible windows of the same active application.
State changes wake the display wait; waiting remains bounded while the display
link is paused. Closing a window removes its observers, and scene transitions
discard input belonging to the outgoing scene.

`safe_area_changed(insets)` reports safe-area changes even when viewport size
does not change. Insets use `(top, left, bottom, right)` in points. The existing
`resize(width, height)` callback reports size changes. Layout and input reveal
logic use the new metrics before rendering.

## Touch cancellation

`Touch.timestamp` contains a monotonic event timestamp in seconds. Existing
four-argument `Touch` construction remains valid; its default timestamp is 0.

Implement `Scene.touch_cancelled(touch)` or `node.on_touch_cancelled(touch)`
to discard an interrupted interaction. Removing or closing a captured node
cancels its touch and suppresses subsequent events for that touch. Scene
shutdown cancels active touches before `stop()`, and scene transitions cancel
outgoing touches. Cancellation also resets attached gesture recognizers.

For compatibility, the default Scene cancellation callback calls
`touch_ended()` with phase `TouchPhase.CANCELLED`. Nodes without a cancellation
handler similarly fall back to `on_touch_ended()`. Check the phase before
treating an ended callback as a successful user action.
