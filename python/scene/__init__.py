"""scene — 2D game & animation engine backed by Metal."""

from . import action, gesture, gpu, transition
from ._common import (
    Alignment, CollisionInfo, FillRule, LineCap, LineJoin, Orientation, Touch,
    TouchPhase, linear_gradient, radial_gradient, radial_point,
)
from ._node import Group, Layer, Node
from ._clip import ClipRect
from ._screen_layer import ScreenLayer
from ._scroll import ScrollView
from ._layout import HStack, Spacer, VStack, ZStack
from ._shapes import Circle, Line, Rect
from ._path_node import Path, Polygon
from ._text import Image, Label
from ._controls import Button, ControlStyle, Slider, Toggle
from ._events import KeyEvent, WindowState
from ._text_input import TextField, TextView, TextInputSession
from ._sprite import NineSlice, Sprite, SpriteAtlas
from ._particle import ParticleEmitter
from ._tilemap import TileMap, TileSet, load_tiled
from ._shader import ShaderNode
from ._composite import RadialLabels, RadialTicks
from ._camera import Camera
from ._scene import Scene, run
from ._image_data import ImageData


_PHYSICS_NAMES = {
    'PhysicsBody', 'PhysicsJoint', 'PhysicsWorld', 'ChainShape',
    'RevoluteJoint', 'DistanceJoint', 'PrismaticJoint',
    'WeldJoint', 'WheelJoint', 'MotorJoint',
}

def __getattr__(name):
    if name in _PHYSICS_NAMES:
        from . import _physics
        obj = getattr(_physics, name)
        globals()[name] = obj
        return obj
    raise AttributeError(f"module 'scene' has no attribute {name!r}")
