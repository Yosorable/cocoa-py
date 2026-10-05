# Pointer input

This page describes additions in the current source. They are not included in
the published `0.1.0a7` wheels.

Scenes receive hover independently of contact and dragging. AppKit supplies
mouse movement; UIKit uses `UIHoverGestureRecognizer` for supported pointing
devices. A finger touching the screen remains a `Touch` and does not synthesize
hover.

```python
import scene


class Demo(scene.Scene):
    def setup(self):
        self.preview = scene.Circle(12, fill="orange")
        self.preview.visible = False
        self.add(self.preview)

    def pointer_moved(self, event: scene.PointerEvent):
        self.preview.position = self.camera.screen_to_world(*event.position)
        self.preview.visible = True

    def pointer_exited(self, event: scene.PointerEvent):
        self.preview.visible = False

    def touch_began(self, touch: scene.Touch):
        if touch.button == 1:
            self.preview.visible = False


scene.run(Demo)
```

`PointerEvent` is immutable and contains `position`, `prev_position`, `phase`,
`timestamp`, and `cancelled`. Coordinates are viewport points, like `Touch`;
camera or node transforms are applied explicitly with `screen_to_world` or
`convert_from_world`. `pointer_moved` receives `entered` and `moved` phases;
`pointer_exited` receives `exited`. `Scene.pointer_position` is the current
hovered position, or `None` after exit or loss of input ownership.

Losing focus, entering the background, changing text-input ownership, or closing
a scene cancels its current hover once. The exit event has `cancelled=True`.
Queued samples from an earlier input epoch are discarded. Native consecutive
move samples may coalesce; entry and exit boundaries are retained. Hover does
not create a touch, press a control, or acquire drag capture.

`Touch.button` identifies the initiating button: `0` is primary, `1` secondary,
`2` middle, and larger values represent additional AppKit buttons. Direct touch
and Pencil contact use `0`. `Touch.source` is `touch`, `mouse`, or `pencil`;
trackpads use `mouse`. Both fields have defaults, so existing positional `Touch`
construction continues to work. Secondary and other non-primary contacts go to
the scene's touch callbacks and never activate standard scene controls.

`Scene.scroll(event)` receives wheel/trackpad deltas remaining after the hit
`ScrollView` and its scrollable ancestors have consumed their portion.
`ScrollEvent` contains `position`, `delta`, `precise`, `momentum`, and `timestamp`.
Its delta is measured in viewport points with the same sign as a scroll-view
offset change. For a custom list, apply `event.delta[1]` to its vertical offset.
Fully consumed scrolling does not also reach the scene.

Low-level users can call `gpu.Window.consume_pointer_events()` to drain the
native hover queue. Touches and scrolling retain separate queues. All scene
callbacks run on the scene thread; native UI callbacks only enqueue values.
