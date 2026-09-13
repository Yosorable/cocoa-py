# Scene text input

`TextField` and `TextView` are scene nodes that use UIKit on iOS and AppKit on
macOS for editing. They support system input methods, Unicode, selection,
plain-text paste, editing menus and native undo/redo. Importing or constructing
a control does not create a window or activate a keyboard. `TextInputSession`
provides the same editing state for interfaces drawn by the application.

```python
from scene import Scene, TextField, TextView, run

class Editor(Scene):
    def setup(self):
        self.name = TextField(280, 44, position=(160, 120),
                              placeholder="Name", content_type="name",
                              max_length=24, return_key="next",
                              submit_behavior="next")
        self.notes = TextView(280, 160, position=(160, 260),
                              placeholder="Write a few lines…",
                              on_change=self.notes_changed)
        self.add(self.name, self.notes)

    def notes_changed(self, control):
        if not control.is_composing:
            print(control.text)

run(Editor, title="Editor")
```

Run `python tools/scene_text_input_demo.py` for name, search, secure entry,
multiline editing, focus navigation and PNG export in one example.

## Text and editing

`TextField(width=240, height=44, text="", **options)` accepts one line.
`TextView(width=320, height=160, text="", **options)` accepts multiple lines
and scrolls its document. Both accept the usual Node position, scale, rotation
in radians, opacity and parent/child operations. Their origin is the center.

| Property or method | Behavior |
| --- | --- |
| `text` | Python `str`; native edits become visible at scene frame boundaries. Assigning a value is silent, ends composition and clears the undo history. |
| `selection = (start, end)` | Half-open range in Python Unicode code-point indices, as in `text[start:end]`. Must satisfy `0 <= start <= end <= len(text)`. |
| `marked_range` | The provisional input-method range, with the same indexing, or `None`. |
| `is_composing` | Whether the input method is still composing a word. |
| `focused` | Whether the native editor is active. |
| `focus(select_all=None)` | Request focus after the control is visible in a running scene. `None` uses `select_all_on_focus`. Returns the control. |
| `blur()` | End editing and cancel pending focus. Returns the control. |
| `select_all()` | Select all text. Returns the control. |
| `replace_selection(text)` | Replace through native editing and its undo history when mounted. Returns the control. Disabled/read-only controls reject this method. |
| `undo()`, `redo()` | Use the mounted editor's undo manager; no effect when the corresponding history is empty. Return the control. |
| `close()` | Permanently release the editor and cached textures. Idempotent. |

CRLF and CR line endings normalize to LF. In a single-line field, line breaks
normalize to spaces. `max_length=None` is unlimited; an integer from 0 to
10,000,000 limits **extended grapheme clusters**, using the OS's Unicode rules.
For example, an accented letter or joined family emoji counts as one character.
Selections still use Python indices, so their unit differs from this limit.

Typing or pasting a replacement that exceeds the limit is rejected as a whole.
An input method may temporarily exceed it while composing; confirmed text is
then truncated at a grapheme boundary. Programmatic text assignment and lowering
the limit also truncate. A detached `replace_selection()` uses this programmatic
model because there is no native undo manager yet.

The current text can include unconfirmed input. Use `is_composing` when applying
validation or processing a search during `on_change`; `on_submit` is delivered
after composition has finished. Do not assign `control.text` back to itself in
each frame, because explicit text assignment intentionally ends composition.

## Options

Except `secure`, all options in this table are mutable properties. Style changes
do not replace the editor's text or discard an active composition. Dimensions,
padding and transformed coordinates must be finite and no larger than
10,000,000 points in magnitude; texture allocation limits also apply.

| Options | Values and defaults |
| --- | --- |
| `placeholder` | Empty string by default. |
| `font_size`, `font` | 18 points; `None` for the system font. An unavailable named font falls back to the system font. |
| `text_color`, `placeholder_color` | `"#17202a"`, `"#78818b"`. Accept scene colors. |
| `background`, `border_color` | `"#ffffff"`, `"#aab2bd"`. Transparent colors are supported. |
| `border_width`, `corner_radius` | 1 and 8 local points. |
| `padding` | 10 local points; a scalar or `(top, left, bottom, right)`. |
| `alignment` | `"natural"` (default), `"left"`, `"center"`, `"right"`. Natural alignment supports RTL text. |
| `enabled`, `read_only` | `True`, `False`. Read-only text remains selectable; disabled controls cannot acquire focus. |
| `max_length` | `None` by default; see the Unicode rules above. |
| `secure` | `False`; available only on TextField and fixed at construction. Uses the native secure editor, disables correction on iOS, and masks snapshots. |
| `select_all_on_focus` | `False`. |
| `submit_behavior` | `"blur"` (TextField default), `"stay"`, `"next"`, or `"newline"` (TextView default, TextView only). |
| `keyboard_type` | `"default"`, `"ascii"`, `"number"`, `"decimal"`, `"phone"`, `"email"`, `"url"`. |
| `return_key` | `"done"` (TextField default), `"default"` (TextView default), `"go"`, `"search"`, `"send"`, `"next"`. |
| `autocapitalization` | `"sentences"` (default), `"none"`, `"words"`, `"all"`. |
| `autocorrection`, `spell_check` | Both `True`; follow the OS's supported languages and user settings. |
| `content_type` | `None` (default), `"name"`, `"username"`, `"password"`, `"new_password"`, `"one_time_code"`, `"email"`, `"telephone"`, `"url"`. |
| `keyboard_appearance` | `"default"`, `"light"`, `"dark"`. |
| `avoid_keyboard` | `True`; automatically moves an obscured active editor above the iOS software keyboard. |

Keyboard type, return-key label, capitalization, content type and keyboard
appearance are iOS hints. They do not validate or restrict pasted text. Autofill
availability is controlled by iOS and the host app; a hint alone does not enable
associated-domain credentials. macOS uses its hardware keyboard and input methods.
On macOS secure fields use the system secure editor's own correction policy.

The return-key label is separate from its action: `return_key="next"` changes
the iOS label; use `submit_behavior="next"` to move focus. For multiline text,
`submit_behavior="newline"` inserts a newline. On iOS the keyboard accessory
offers previous/next controls and a hide-keyboard button, including on number
pads. Hiding the keyboard confirms any composition and ends editing without
submitting or triggering `on_submit`; text is retained. The return key keeps
its configured submit or newline behavior. On macOS Tab/Shift-Tab move focus
and Escape ends editing.

## Callbacks and focus

Change scene controls on the Python scene-loop thread. Pass callbacks at
construction or assign them later. Each receives the control:
`on_change`, `on_selection_change`, `on_submit`, `on_focus`, `on_blur`.
They run on the Python scene-loop thread at frame boundaries, never inside a
native editor delegate. Native events use a bounded queue of 256 entries per
control. Adjacent changes can coalesce and the oldest entries are dropped on
overflow; callbacks are notifications of current state, not an edit-history log.
Programmatic text and selection assignments do not emit change callbacks.
Removing or closing a control discards its queued callbacks.

`Scene.focused_input` returns the current control or `None`.
`Scene.focus_next_input(current=None, reverse=False)` cycles through visible,
enabled, editable controls in scene-tree order and returns the requested target.
`Scene.dismiss_keyboard()` ends editing. Touching outside a text control also
ends editing. Tapping a control activates its native editor; subsequent selection
and editing gestures are handled by the system.

`Scene.keyboard_frame` is `(x, y, width, height)` in window points, or `None`.
Override `Scene.keyboard_changed(frame)` for custom layout. On macOS it is
`None`. Set `avoid_keyboard=False` when managing your own layout. Default
avoidance outside a vertical/two-axis ScrollView moves only the editor; for an
unrotated multiline control that is too tall, it reduces the editing viewport
while retaining font size and scrolling. It does not move the camera.
Automatic avoidance applies immediately on keyboard appearance, frame changes,
and dismissal. Only the system keyboard itself keeps its platform animation.

Inside a vertical/two-axis ScrollView, the container manages avoidance for the
whole content subtree. The editor follows the scene's geometry without
an independent UIKit lift animation, preserving its size and composition.
Keyboard dismissal immediately clamps offsets outside the new scroll range. See
[Scene UI foundations](scene-ui.md#scrolling) for the shared layout
contract. `avoid_keyboard=False` disables keyboard-driven reveal for that input;
ordinary focus navigation can still reveal an offscreen node.

Removing a control releases its native editor while preserving its latest text
and selection, so it can be attached again. Reparenting within one scene preserves
active composition. Hiding or disabling an active control ends editing. Scene
switches and window closure release or suspend editors automatically.

## Rendering and capture

Inactive controls render as native-text textures in the scene and obey its
composition and Layer caches. The focused editor is a native overlay **above
the scene's Metal content**. It is not clipped or filtered by scene Layers and
cannot be occluded by a higher-z sprite; end editing before showing scene content
that must cover it. Position, rotation and column scales follow the same quad
decomposition as Sprite; arbitrary shear and reflection are not supported.

`Scene.capture()` and `control.capture(rect=...)` include the text, background,
border and placeholder even while focused. They render at the requested scale
without changing native focus, selection, composition or undo state. Secure
fields export bullets. Captures omit the caret, selection highlight, keyboard
and native editing menus. Multiline snapshots use the current document scroll
offset; single-line snapshots show the aligned, clipped text. They use the
control's scene geometry, before automatic keyboard avoidance.

## Input sessions for custom interfaces

`TextInputSession` is not a Node and is not added to the scene graph. It owns
native text input while your scene draws the text, caret and selection:

```python
from scene import Label, Scene, TextInputSession, run

class CustomInput(Scene):
    def setup(self):
        self.output = Label("Tap to type", position=(180, 160))
        self.add(self.output)
        self.input = TextInputSession(on_change=self.changed)

    def touch_began(self, touch):
        self.input.caret_rect = (180, 160, 2, 22)
        self.input.begin(self)

    def changed(self, session):
        self.output.text = session.text

run(CustomInput)
```

The example uses a fixed input-method anchor. A complete custom editor should
update it to the actual drawn caret whenever the text, selection or layout
changes. `caret_rect=(x, y, width, height)` is in **window points**, with a
top-left origin; convert your custom layout or camera coordinates accordingly.
The rectangle must have positive dimensions. It positions native candidate UI
and does not resize or draw an input box. Updating it preserves composition.

Construct with `TextInputSession(text="", multiline=False, secure=False,
caret_rect=(0, 0, 1, 20), on_next=None, on_previous=None, **editing_options)`.
Editing options are `enabled`, `read_only`, `max_length`, `keyboard_type`,
`return_key`, `autocapitalization`, `autocorrection`, `spell_check`,
`content_type`, `keyboard_appearance`, `select_all_on_focus`, `submit_behavior`,
and the five change/focus/submit callbacks described above. Appearance and Node
constructor options are not accepted. Multiline sessions default to newline
insertion, while single-line sessions default to submitting and ending input.

| Operation | Behavior |
| --- | --- |
| `begin(scene, select_all=None)` | Bind to a running Scene and request input on its next onscreen frame. Returns the session. Repeated calls reuse the editor; binding another Scene releases the previous editor. |
| `active` / `focused` | Whether this session currently owns native text input focus. |
| `end()` / `blur()` | Confirm composition and end editing. Keep the text, selection and native editor for reuse. |
| `commit_composition()` | Confirm provisional text while keeping input active. Useful before pointer-driven selection changes. |
| `text`, `selection`, `marked_range`, `is_composing` | Same Unicode model and programmatic-update rules as the components. |
| `select_all()`, `replace_selection(text)`, `undo()`, `redo()` | Same editing operations as the components. Native history follows platform editor behavior. |
| `close()` | Permanently release the editor; scene shutdown also closes all bound sessions. |

Callbacks receive the **session**, on the scene-loop thread. `on_next` and
`on_previous` handle navigation requests, including macOS Tab/Shift-Tab; they
do not implicitly move focus to unrelated scene nodes. On iOS the accessory
toolbar provides a hide-keyboard button, including on number pads. It confirms
composition and ends input without triggering `on_submit`. The OS may use a hardware
keyboard without displaying a software keyboard, so `active` does not imply a
non-None `keyboard_frame`.

Background taps are delivered to your scene without ending a session. Handle
pointer selection and decide when to call `end()`. Explicit
`Scene.dismiss_keyboard()` ends both sessions and input components. Only one
editor can own native focus in a window; sessions and components can exchange
focus. Scene transitions suspend input, and scene/window shutdown releases
native resources even though sessions are outside the node tree.

Sessions do not draw native controls, intercept scene pointer events, or appear
in captures. Captures include only your scene's drawing. Provide your own
selection gestures and editing buttons; for example, paste with
`session.replace_selection(clipboard.read_text() or "")`. A secure session uses
secure native input, but **your drawing code must mask its text**. Text and
selection remain available to the application.

Handle `Scene.keyboard_changed(frame)` to keep your custom interface above the
keyboard. Sessions do not automatically move or scale scene content. Input
methods use a native editor internally; no visible TextField/TextView component
is required. This API accepts text, not raw gameplay key events.

This is a plain-text editing API. It does not provide a rich-text document model,
custom input methods, or a general keyboard-event API for gameplay.
