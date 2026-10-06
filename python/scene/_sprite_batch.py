"""Compact, explicitly updated sprites without per-image scene nodes."""
from __future__ import annotations

from _cocoa import _scene_accel as _accel

from ._clip import ClipRect
from ._common import _IDENTITY, _apply, _mul
from ._node import Node
from ._sprite import Sprite, SpriteFrame
from .gpu import Texture


def _field(number):
    def get(self):
        return _accel._batch_get(self._data, self.index, number)

    def set(self, value):
        _accel._batch_set(self._data, self.index, number, value)

    return property(get, set)


class BatchSprite:
    """A drawing record owned by a SpriteBatch, with a stable integer index.

    This is not a Node: it has no actions, input handlers or scene children.
    Its optional parent is an earlier record in the same batch; transforms,
    opacity, visibility and clipping inherit through that parent. Depth and
    tint remain independent. Equal depths follow the parent before its
    descendants, preserving insertion order between siblings.

    ``clip`` is a ClipRect in batch coordinates, before the sprite transform.
    Textures are retained but borrowed: closing a batch never closes textures.
    """

    __slots__ = ("_data", "index")
    affine_transform = _field(0)
    sprite_size = _field(1)
    anchor = _field(2)
    _uv_rect = _field(3)
    tint = _field(4)
    opacity = _field(5)
    z = _field(6)
    visible = _field(7)
    _additive = _field(8)
    flip_x = _field(9)
    flip_y = _field(10)

    def __init__(self, data, index):
        self._data, self.index = data, index

    @property
    def texture(self):
        return _accel._batch_get(self._data, self.index, 11)

    @property
    def frame(self):
        return SpriteFrame(self.texture, self._uv_rect)

    @frame.setter
    def frame(self, source):
        if isinstance(source, Texture):
            source = SpriteFrame(source, (0., 0., 1., 1.))
        if not isinstance(source, SpriteFrame) or not isinstance(source.texture, Texture):
            raise TypeError("source must be a Texture or SpriteFrame")
        uv = tuple(float(value) for value in source.uv_rect)
        # Validate UVs before retaining the replacement texture.
        _accel._batch_set(self._data, self.index, 3, uv)
        _accel._batch_set(self._data, self.index, 11, source.texture)

    @property
    def blend(self):
        return "additive" if self._additive else "alpha"

    @blend.setter
    def blend(self, value):
        if value not in ("alpha", "additive"):
            raise ValueError("blend must be 'alpha' or 'additive'")
        self._additive = value == "additive"

    @property
    def clip(self):
        return _accel._batch_get(self._data, self.index, 12)

    @clip.setter
    def clip(self, value):
        if value is not None and not isinstance(value, ClipRect):
            raise TypeError("clip must be a ClipRect or None")
        _accel._batch_set(self._data, self.index, 12, value)

    @property
    def parent_index(self):
        return _accel._batch_get(self._data, self.index, 13)


class SpriteBatch(Node):
    """Draw many explicitly positioned images through one scene node.

    ``add_sprite`` returns a BatchSprite handle. ``set_transforms(indices,
    matrices)`` updates six-coefficient affine matrices with one native call;
    it validates the entire update before changing any record. Repeated
    indices are allowed and the last matrix wins. Node transform and opacity
    apply to every record. Record depths participate in the usual scene sort.

    Individual records do not enter input, layout or action traversal. Use
    ordinary nodes for those features. No NumPy dependency is required.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.interactive = False
        self._batch_data = _accel._batch_new()
        self._sprites = []

    @property
    def sprites(self):
        return tuple(self._sprites)

    def add_sprite(self, source, *, size=None, anchor=(.5, .5),
                   affine_transform=_IDENTITY, opacity=1, z=0, tint=(1, 1, 1, 1),
                   blend="alpha", flip_x=False, flip_y=False, visible=True,
                   parent=None, clip=None):
        """Append a sprite. Parent handles must belong to this batch.

        A failed append leaves earlier records unchanged. ``frame`` changes
        the texture and UVs; update ``sprite_size`` separately to resize it.
        """
        _accel._batch_check_writable(self._batch_data)
        if parent is not None and (not isinstance(parent, BatchSprite) or parent._data is not self._batch_data):
            raise ValueError("parent must belong to this batch")
        # Prepare a detached record first, keeping failed appends atomic.
        pending = _accel._batch_new()
        candidate = BatchSprite(pending, _accel._batch_append(pending))
        candidate.frame = source
        if size is None:
            u0, v0, u1, v1 = candidate._uv_rect
            width, height = candidate.texture.size
            size = (abs(u1-u0) * width, abs(v1-v0) * height)
        for name, value in (("sprite_size", size), ("anchor", anchor),
                            ("affine_transform", affine_transform), ("opacity", opacity),
                            ("z", z), ("tint", tint), ("blend", blend),
                            ("flip_x", flip_x), ("flip_y", flip_y),
                            ("visible", visible), ("clip", clip)):
            setattr(candidate, name, value)
        sprite = BatchSprite(self._batch_data, len(self._sprites))
        self._sprites.append(sprite)
        try:
            sprite.index = _accel._batch_copy(self._batch_data, pending, -1 if parent is None else parent.index)
        except BaseException:
            self._sprites.pop()
            raise
        return sprite

    def set_transforms(self, indices, matrices, *, transform=None):
        """Copy finite affine matrices, optionally prepending a shared transform.

        ``transform`` is composed with each matrix before copying. Both input
        and composed coefficients must be finite; any failure leaves the
        entire batch unchanged. Explicitly invalidate a containing Layer
        after editing cached content, as for ordinary Sprite properties.
        """
        _accel._batch_transforms(self._batch_data, indices, matrices, transform)

    def set_poses(self, indices, poses):
        """Atomically copy (x, y, rotation, scale_x, scale_y) sprite poses.

        Rotation uses radians, with the same component composition as Node.
        All input values and the resulting matrices must be finite.
        """
        _accel._batch_transforms(self._batch_data, indices, poses, None, True)

    def update_sprites(self, indices, *, transforms=None, poses=None, sizes=None,
                       tints=None, opacities=None, depths=None, visible=None, transform=None):
        """Atomically update selected geometry and appearance in one call.

        Each supplied sequence has one value per index. Transform rows contain
        six affine coefficients; pose rows contain x, y, rotation in radians,
        scale_x and scale_y. Supply either transforms or poses. Sizes and tints
        contain two and four numbers per row; the other columns contain scalar
        values. A shared transform may be prepended to every supplied pose.
        Omitted columns retain their current values. Repeated indices use the
        last supplied values. Inputs are copied, and numeric values must be
        finite. A conversion error leaves all supplied columns unchanged.
        """
        _accel._batch_update(self._batch_data, indices, transforms, poses, sizes,
                             tints, opacities, depths, visible, transform)

    def _clear_records(self):
        _accel._batch_check_writable(self._batch_data)
        # Finalizers see a consistent replacement even if they edit the batch.
        previous = self._batch_data, self._sprites, self._cache, getattr(self, "_c_cache", None)
        self._batch_data = _accel._batch_new()
        self._sprites = []
        self._cache = None
        self._c_cache = None
        del previous

    def clear(self):
        """Detach records and scene children; earlier handles stop affecting this batch."""
        self._clear_records()
        return super().clear()

    def close(self):
        _accel._batch_check_writable(self._batch_data)
        try:
            super().close()
        finally:
            self._clear_records()

    def _snap(self):
        return (*super()._snap(), self._batch_data, _accel._batch_revision(self._batch_data))

    def _bounds(self):
        # Measurement is only needed for capture and Layer allocation, not
        # during ordinary batch collection. Include inherited visibility and
        # intersect each instance's batch-space clipping before taking a union.
        transforms, visible, clips, bounds = [], [], [], []
        for item in self._sprites:
            parent = item.parent_index
            matrix = _mul(_IDENTITY if parent < 0 else transforms[parent], item.affine_transform)
            shown = item.visible and (parent < 0 or visible[parent])
            regions = () if parent < 0 else clips[parent]
            if item.clip is not None:
                regions += (item.clip,)
            transforms.append(matrix)
            visible.append(shown)
            clips.append(regions)
            if not shown:
                continue
            w, h = item.sprite_size
            ax, ay = item.anchor
            x0, y0 = -ax*w, -ay*h
            points = [_apply(matrix, (x, y)) for x in (x0, x0+w) for y in (y0, y0+h)]
            x0, y0 = min(p[0] for p in points), min(p[1] for p in points)
            x1, y1 = max(p[0] for p in points), max(p[1] for p in points)
            for clip in regions:
                x0, y0 = max(x0, clip.x), max(y0, clip.y)
                x1, y1 = min(x1, clip.x + clip.width), min(y1, clip.y + clip.height)
            if x1 > x0 and y1 > y0:
                bounds.append((x0, y0, x1, y1))
        if bounds:
            return (min(b[0] for b in bounds), min(b[1] for b in bounds),
                    max(b[2] for b in bounds), max(b[3] for b in bounds))

    def _emit(self, cmds, renderer, world, opacity, order):
        data = self._batch_data
        _accel._batch_begin_collect(data)
        try:
            self._emit_records(cmds, renderer, world, opacity, order)
        finally:
            _accel._batch_end_collect(data)

    def _emit_records(self, cmds, renderer, world, opacity, order):
        count = len(self._sprites)
        worlds, opacities, shown, contexts = ([None] * count for _ in range(4))
        parents = getattr(renderer, "_collect_clips", ())
        for index in _accel._batch_order(self._batch_data):
            item = self._sprites[index]
            parent = item.parent_index
            pop = opacity if parent < 0 else opacities[parent]
            visible = item.visible and pop > .001 and (parent < 0 or shown[parent])
            transform = _mul(world if parent < 0 else worlds[parent], item.affine_transform)
            op = pop * item.opacity
            clips = parents if parent < 0 else contexts[parent]
            if visible and item.clip is not None:
                clips += (item.clip._state(world),)
            worlds[index] = transform
            opacities[index] = op
            shown[index] = visible
            contexts[index] = clips
            if not visible:
                continue
            sprite = Sprite(item.frame, size=item.sprite_size, anchor=item.anchor,
                            flip_x=item.flip_x, flip_y=item.flip_y, tint=item.tint,
                            blend=item.blend, z=item.z)
            sprite._emit(cmds, renderer, transform, op, order)
            cmds[-1]._clips = clips

    def _collect_unclipped(self, cmds, renderer, transform, opacity, order):
        # The Python fallback preserves per-record clips too; Node's generic
        # command cache assumes every command has the same clipping context.
        if not self.visible or opacity <= .001:
            return
        world = _mul(transform, self._local_matrix())
        op = opacity * self.opacity
        self._world_transform, self._world_opacity = world, op
        self._emit(cmds, renderer, world, op, order)
        for child in self.children:
            child._collect(cmds, renderer, world, op, order)
