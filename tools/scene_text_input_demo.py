"""Try native Scene text editing on macOS or an iPhone host.

Run with python tools/scene_text_input_demo.py. The Save PNG button exports
text-input.png to the current directory. Passwords remain masked in the image.
"""
import scene


class TextInputDemo(scene.Scene):
    def setup(self):
        self.stage = scene.Group()
        self.add(self.stage)
        self.stage.add(scene.Label("Make it yours", size=29, y=-290),
                       scene.Label("Type, select, paste, undo. Try your own language.",
                                   size=13, y=-255, color="#b6c6dd"))
        self.name = scene.TextField(320, 46, y=-185, placeholder="Your name",
                                   max_length=24, content_type="name", return_key="next",
                                   submit_behavior="next", autocapitalization="words")
        self.search = scene.TextField(320, 46, y=-100, placeholder="Search for something",
                                     return_key="search", on_submit=self.search_submitted)
        self.password = scene.TextField(320, 46, y=-15, placeholder="Enter a password",
                                       secure=True, content_type="new_password",
                                       return_key="next", submit_behavior="next")
        self.notes = scene.TextView(320, 140, y=118, placeholder="A thought, a story, a few lines…",
                                   max_length=500, on_change=self.notes_changed)
        for title, control, label_y in (("NAME", self.name, -224), ("SEARCH", self.search, -139),
                                       ("PASSWORD", self.password, -54), ("NOTES", self.notes, 29)):
            self.stage.add(scene.Label(title, size=12, y=label_y, color="#98aac5"), control)
            control.on_focus = self.highlight
            control.on_blur = self.unhighlight
        self.status = scene.Label("Tap an input to start", size=13, y=211, color="#b6c6dd")
        self.stage.add(self.status)
        self.buttons = []
        for title, x, handler in (("Select notes", -110, self.select_notes),
                                  ("Clear notes", 0, self.clear_notes),
                                  ("Save PNG", 110, self.save_image)):
            button = scene.Rect(101, 40, radius=8, x=x, y=257, fill="#2a4367")
            button.add(scene.Label(title, size=13))
            self.buttons.append((button, handler))
            self.stage.add(button)
        self.resize(*self.size)

    def resize(self, width, height):
        top, left, bottom, right = self.safe_area
        available_width, available_height = width - left - right, height - top - bottom
        self.stage.position = (left + available_width / 2, top + available_height / 2)
        self.stage.scale = max(0.05, min((available_width - 24) / 350,
                                       (available_height - 24) / 620, 1.4))

    def highlight(self, control):
        control.border_color = "#4da8ff"
        self.status.text = "Select text to copy, paste or replace it"

    def unhighlight(self, control):
        control.border_color = "#aab2bd"

    def search_submitted(self, control):
        query = control.text.strip()
        self.status.text = f"Search: {query[:30]}" if query else "Enter a search first"

    def notes_changed(self, control):
        self.status.text = "Choosing a word…" if control.is_composing else "Notes updated"

    def select_notes(self):
        self.notes.focus(select_all=True)

    def clear_notes(self):
        self.notes.text = ""
        self.status.text = "Notes cleared"

    def save_image(self):
        try:
            self.capture().save("text-input.png")
            self.status.text = "Saved text-input.png"
        except (OSError, ValueError, RuntimeError) as error:
            self.status.text = "Could not save the image"
            print(error)

    def touch_began(self, touch):
        for button, handler in self.buttons:
            if button.contains_point(*touch.position):
                handler()
                return


if __name__ == "__main__":
    scene.run(TextInputDemo, title="Scene text input", background="#142138")
