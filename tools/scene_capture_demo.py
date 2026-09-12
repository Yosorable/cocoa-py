"""Draw and export PNGs with scene alone, on macOS or an iPhone host.

Run with python tools/scene_capture_demo.py. Save buttons write drawing.png and
scene.png in the current working directory. No Pillow or NumPy is needed.
"""

from pathlib import Path

import scene


class CaptureDemo(scene.Scene):
    def setup(self):
        self.active_touch = None
        self.active_path = None
        self.stage = scene.Group()
        self.add(self.stage)
        self.stage.add(scene.Label("Draw and export", size=25, y=-234))
        self.stage.add(scene.Label("Draw below, then save a PNG", size=14, y=-202,
                                   color="#aab6cf"))
        self.board = scene.Rect(280, 280, y=-36, radius=8, fill="#ffffff")
        self.ink = scene.Group(x=-140, y=-140)
        self.board.add(self.ink)
        self.stage.add(self.board)
        self.buttons = []
        for text, x, y, handler in (
            ("Save drawing", -76, 142, self.save_drawing),
            ("Save scene", 76, 142, self.save_scene),
            ("Clear", 0, 194, self.clear_drawing),
        ):
            button = scene.Rect(140, 40, x=x, y=y, radius=8, fill="#28446d")
            button.add(scene.Label(text, size=15))
            self.stage.add(button)
            self.buttons.append((button, handler))
        self.status = scene.Label("Exports are saved beside your working files", size=12,
                                  y=238, color="#aab6cf")
        self.stage.add(self.status)
        self.resize(*self.size)

    def resize(self, width, height):
        top, left, bottom, right = self.safe_area
        width, height = width - left - right, height - top - bottom
        self.stage.position = (left + width / 2, top + height / 2)
        self.stage.scale = max(0.01, min((width - 24) / 330, (height - 24) / 510, 1.8))

    def _point(self, touch):
        x, y = self.ink.convert_from_world(*touch.position)
        return max(6, min(274, x)), max(6, min(274, y))

    def touch_began(self, touch):
        if self.active_touch is not None:
            return
        for button, handler in self.buttons:
            if button.contains_point(*touch.position):
                try:
                    handler()
                except (OSError, RuntimeError, ValueError) as error:
                    self.status.text = "Export failed; see console"
                    print(error)
                return
        if not self.board.contains_point(*touch.position):
            return
        self.active_touch = touch.id
        point = self._point(touch)
        self.active_path = scene.Path(fill=None, stroke="#174a89", stroke_width=10,
                                      cap="round", join="round").move_to(*point)
        self.ink.add(scene.Circle(5, position=point, fill="#174a89"), self.active_path)

    def touch_moved(self, touch):
        if touch.id == self.active_touch:
            self.active_path.line_to(*self._point(touch))

    def touch_ended(self, touch):
        if touch.id == self.active_touch:
            if touch.phase != "cancelled":
                self.touch_moved(touch)
            self.active_touch = None
            self.active_path = None

    def save_drawing(self):
        image = self.ink.capture(rect=(0, 0, 280, 280), size=(560, 560), background="#ffffff")
        image.save("drawing.png")
        self.status.text = "Saved drawing.png (560 x 560)"
        print("Drawing:", Path("drawing.png").resolve(), "RGBA bytes:", len(image.rgba))

    def save_scene(self):
        image = self.capture()
        image.save("scene.png")
        self.status.text = f"Saved scene.png ({image.width} x {image.height})"
        print("Scene:", Path("scene.png").resolve())

    def clear_drawing(self):
        for child in list(self.ink.children):
            self.ink.remove(child)
            child.close()
        self.status.text = "Canvas cleared"


if __name__ == "__main__":
    scene.run(CaptureDemo, title="Scene capture", background="#111d32")
