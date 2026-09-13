"""Screen-coordinate content with an explicit render-plane order."""
import operator

from ._node import Group


class ScreenLayer(Group):
    """A direct Scene child unaffected by camera or Scene transforms.

    Coordinates are viewport points with the origin at the top-left. ``order``
    selects a render plane: the world is 0, positive planes overlay it, and
    negative planes are behind it. Within a plane, normal node z order applies.
    Visibility and opacity still inherit from the Scene. Scene.ui is a lazily
    created ScreenLayer with order 1.
    """
    _screen_space = True

    def __init__(self, *, order=1, **kwargs):
        super().__init__(**kwargs)
        self.order = order

    @property
    def order(self):
        return self._screen_layer

    @order.setter
    def order(self, value):
        value = operator.index(value)
        if not -(2 ** 31) <= value < 2 ** 31:
            raise ValueError("ScreenLayer order must fit a signed 32-bit integer")
        self._screen_layer = value

    def _validate_parent(self, parent):
        from ._scene import Scene
        if not isinstance(parent, Scene):
            raise ValueError("ScreenLayer must be a direct child of Scene")
