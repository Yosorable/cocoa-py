"""scene._camera — Camera for scene viewport control."""
from __future__ import annotations

import math

from ._common import _IDENTITY, _apply, _invert, _matrix, _mul


class Camera:
    """Scene camera: pan, zoom, rotate, follow."""

    def __init__(self, scene):
        self._scene = scene
        self._x = None
        self._y = None
        self._zoom = 1.0
        self._rotation = 0.0
        self._follow_node = None
        self._follow_lerp = 0.1
        self._follow_offset = (0.0, 0.0)

    def _default_x(self):
        return self._scene.width * 0.5

    def _default_y(self):
        return self._scene.height * 0.5

    @property
    def x(self):
        return self._default_x() if self._x is None else self._x

    @x.setter
    def x(self, v):
        self._x = float(v)

    @property
    def y(self):
        return self._default_y() if self._y is None else self._y

    @y.setter
    def y(self, v):
        self._y = float(v)

    @property
    def position(self):
        return (self.x, self.y)

    @position.setter
    def position(self, v):
        self._x, self._y = float(v[0]), float(v[1])

    @property
    def zoom(self):
        return self._zoom

    @zoom.setter
    def zoom(self, v):
        self._zoom = float(v)

    @property
    def rotation(self):
        return self._rotation

    @rotation.setter
    def rotation(self, v):
        self._rotation = float(v)

    def follow(self, node, *, lerp=0.1, offset=(0, 0)):
        """Smoothly follow a node each frame."""
        self._follow_node = node
        self._follow_lerp = float(lerp)
        self._follow_offset = (float(offset[0]), float(offset[1]))

    def unfollow(self):
        self._follow_node = None

    def reset(self):
        self._x = None
        self._y = None
        self._zoom = 1.0
        self._rotation = 0.0
        self._follow_node = None

    def _update(self, dt):
        node = self._follow_node
        if node is None:
            return
        tx, ty = self._node_scene_position(node)
        tx += self._follow_offset[0]
        ty += self._follow_offset[1]
        t = min(1.0, self._follow_lerp)
        px, py = self.x, self.y
        self._x = px + (tx - px) * t
        self._y = py + (ty - py) * t

    @staticmethod
    def _node_scene_position(node):
        chain = []
        n = node
        while n is not None:
            chain.append(n)
            n = n.parent
        m = _IDENTITY
        for n in reversed(chain):
            m = _mul(m, _matrix((n.x, n.y), n.rotation, n.scale))
        return (m[4], m[5])

    def _transform(self, scene_w, scene_h):
        cx, cy = scene_w * 0.5, scene_h * 0.5
        px = cx if self._x is None else self._x
        py = cy if self._y is None else self._y

        if px == cx and py == cy and self._zoom == 1.0 and self._rotation == 0.0:
            return _IDENTITY

        z = self._zoom
        r = -self._rotation
        c_r, s_r = math.cos(r), math.sin(r)
        a = c_r * z
        b = s_r * z
        tx = -a * px + b * py + cx
        ty = -b * px - a * py + cy
        return (a, b, -b, a, tx, ty)

    def screen_to_world(self, sx, sy):
        m = self._scene._camera_root_transform()
        return _apply(_invert(m), (sx, sy))

    def world_to_screen(self, wx, wy):
        m = self._scene._camera_root_transform()
        return _apply(m, (wx, wy))
