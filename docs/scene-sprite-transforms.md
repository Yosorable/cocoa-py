# Sprite transforms

`Sprite` applies the complete inherited affine transform to each textured corner.
Combining a nonuniformly scaled parent with a rotated child retains the resulting
shear. Negative scale preserves reflected geometry. The same behavior applies to
the native collector and subclasses using the Python emitter.

```python
from math import pi
import scene

parent = scene.Group(scale=(2, 1))
sprite = scene.Sprite("leaf.png", rotation=pi / 4, anchor=(0, 0))
parent.add(sprite)
```

`flip_x` and `flip_y` reverse texture coordinates without changing the corners.
`scene.SpriteFrame(texture, (u0, v0, u1, v1))` is publicly exported for applications
that already have texture-region metadata; `SpriteAtlas` also returns these frames.

The collector still caches unchanged sprites. Point hit testing uses the inverse
world transform. This rendering correction does not change the existing OBB-based
collision approximation for sheared sprites.

The regression in `tests/test_scene_sprite_affine.py` checks inherited shear,
reflection, atlas UVs, texture flipping, point hits, and unchanged-frame reuse in
both rendering paths. All four cases fail on the previous implementation.
# Compositing

`Sprite(..., blend="additive")` adds the source RGB contribution to the
destination; the default `blend="alpha"` uses ordinary source-over compositing.
Tint and opacity still multiply the source contribution. Both modes retain
source-over alpha coverage, so transparent captures stay usable as textures.
Changing `blend` invalidates cached sprite commands, including custom subclasses.

`ParticleEmitter.blend` now selects the corresponding Metal pipeline instead of
being ignored. Its default is `"alpha"`, matching `Sprite`; choose `"additive"`
explicitly for fireworks, sparks, and other glowing particles. Clipping and
multisampled rendering use the same mode. Particle textures use premultiplied
alpha, as do loaded images and scene-rendered textures.
At the low-level API, `gpu.Pipeline(..., blend_mode="additive")` selects this
behavior; `blending=False` continues to disable blending entirely.
