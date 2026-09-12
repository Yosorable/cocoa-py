"""Scene and node capture without stepping the scene lifecycle."""
from __future__ import annotations

import math
import operator
from collections import OrderedDict

from _cocoa import _metal, _scene_accel

from ._common import _matrix, _mul
from .gpu import Texture, normalize_color


def _rectangle(value):
    try:
        x, y, width, height = (float(v) for v in value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("rect must be (x, y, width, height).") from error
    if not all(math.isfinite(v) for v in (x, y, width, height)) or width <= 0 or height <= 0:
        raise ValueError("rect must be finite with positive width and height.")
    return x, y, width, height


def _pixel_size(value):
    try:
        width, height = (operator.index(v) for v in value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("size must contain two integer pixel dimensions.") from error
    # Validate before Metal allocation; oversized descriptors can assert in
    # the driver. Bound capture memory independently of the window size.
    if not (0 < width <= 16384 and 0 < height <= 16384) or width * height > 64 * 1024 * 1024:
        raise ValueError("Capture size must fit 16384 per axis and 64 megapixels.")
    return width, height


def _inverse(matrix):
    if not all(math.isfinite(v) for v in matrix):
        raise ValueError("The captured node must have a finite, invertible transform.")
    a, b, c, d, tx, ty = matrix
    sx, sy = max(abs(a), abs(b)), max(abs(c), abs(d))
    if sx == 0 or sy == 0:
        raise ValueError("The captured node must have a finite, invertible transform.")
    # Normalize each column before computing the determinant. Small valid
    # scales must not fall back to identity, and large scales must not overflow.
    a, b, c, d = a / sx, b / sx, c / sy, d / sy
    determinant = a * d - b * c
    if determinant == 0:
        raise ValueError("The captured node must have a finite, invertible transform.")
    ia, ib = d / determinant / sx, -b / determinant / sy
    ic, id_ = -c / determinant / sx, a / determinant / sy
    inverse = (ia, ib, ic, id_, -(ia * tx + ic * ty), -(ib * tx + id_ * ty))
    if not all(math.isfinite(v) for v in inverse):
        raise ValueError("The capture inverse transform cannot be represented with finite values.")
    return inverse


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _cold_shader_states(nodes):
    from ._shader import ShaderNode

    fields = ("_tex", "_uniforms_buf", "_globals_buf", "_pipe", "_lib")
    pending = list(nodes)
    visited = set()
    states = []
    while pending:
        node = pending.pop()
        if not isinstance(node, ShaderNode) or id(node) in visited:
            continue
        visited.add(id(node))
        pending.extend(node._user_textures.values())
        if node._pipe is None:
            states.append((node, {name: getattr(node, name) for name in fields},
                           node._renderer, node._dirty))
    return states


def capture(node, *, rect=None, size=None, background=None):
    from ._node import Layer
    from ._path_node import Path
    from ._text_input import _TextInput
    from ._scene import Scene

    root = node._tree_root()
    renderer = getattr(root, "_renderer", None)
    window = getattr(root, "_window", None)
    if not isinstance(root, Scene) or renderer is None or window is None or window.handle is None:
        raise RuntimeError("Capture requires a node attached to a running scene.")
    if getattr(renderer, "_capturing", False):
        raise RuntimeError("A scene capture is already in progress.")
    is_scene = node is root
    if rect is None:
        if not is_scene:
            raise ValueError("Node.capture() requires rect in the node's local coordinates.")
        rect = (0, 0, root.width, root.height)
    x, y, width, height = _rectangle(rect)
    if size is None:
        scaled = (width * window.scale, height * window.scale)
        if not all(math.isfinite(v) for v in scaled):
            raise ValueError("The capture dimensions must be finite.")
        size = tuple(max(1, round(v)) for v in scaled)
    pixel_width, pixel_height = _pixel_size(size)
    scale = max(pixel_width / width, pixel_height / height)
    if not math.isfinite(scale):
        raise ValueError("The capture scale must be finite.")
    if background is None:
        background = root.background if is_scene else (0, 0, 0, 0)
    color = normalize_color(background)
    if not all(math.isfinite(c) for c in color):
        raise ValueError("Capture background components must be finite.")
    r, g, b, a = color
    clear_color = (r * a, g * a, b * a, a)
    shift = _matrix((-x, -y), 0, 1)
    if is_scene:
        transform = _mul(shift, root._camera_root_transform())
        particle_transform = shift
    else:
        local = _matrix(node.position, node.rotation, node.scale)
        transform = _mul(shift, _inverse(local))
        ancestors = []
        current = node
        while current is not None:
            ancestors.append(current)
            current = current.parent
        world = root._camera_root_transform()
        for ancestor in reversed(ancestors):
            world = _mul(world, _matrix(ancestor.position, ancestor.rotation, ancestor.scale))
        particle_transform = _mul(shift, _inverse(world))

    texture = None
    started = completed = False
    old_scale = renderer.screen_scale
    old_slot = renderer._active_frame_slot
    old_pool, old_used = renderer._buf_pool, renderer._buf_used
    old_text, old_atlases = renderer._tc, renderer._ga
    old_viewport = renderer._capture_viewport
    states = [(child, child._world_transform, child._world_opacity,
               getattr(child, "_rendered_size", None)) for child in _walk(node)]
    layers = [(child, child._tex, child._lsize, child._lcenter, child._rscale,
               child._dirty, child._capture_center)
              for child, *_ in states if isinstance(child, Layer)]
    path_texture_fields = ("_tex", "_path_size", "_path_center", "_rscale",
                           "_texture_version", "_texture_pixel_scale", "_texture_size")
    paths = [(child, tuple(getattr(child, name) for name in path_texture_fields))
             for child, *_ in states if isinstance(child, Path)]
    input_texture_fields = ("_snapshot_texture", "_snapshot_key", "_snapshot_scale")
    inputs = [(child, tuple(getattr(child, name) for name in input_texture_fields))
              for child, *_ in states if isinstance(child, _TextInput)]
    shaders = _cold_shader_states(child for child, *_ in states)
    renderer._capturing = True
    renderer._active_frame_slot = None
    renderer._buf_pool, renderer._buf_used = [], []
    renderer._tc, renderer._ga = OrderedDict(), {}
    renderer._capture_viewport = (width, height)
    try:
        _metal.prepare_image_capture(window.handle)
        started = True
        # Cached layers must render at the capture resolution without replacing
        # the textures used by the live scene or changing their dirty state.
        for layer, *_ in layers:
            layer._tex = None
            layer._dirty = True
            layer._capture_center = None
        for path, _ in paths:
            path._tex = None
            path._texture_version = -1
        for control, _ in inputs:
            control._snapshot_texture = None
            control._snapshot_key = None
        # Capture collection uses different transforms and may rebuild cached
        # textures. Invalidate caches rather than restoring stale texture refs.
        for child, _, _, _ in states:
            child._drop_internal_caches(child)
        renderer.screen_scale = scale
        texture = Texture.render_target(pixel_width, pixel_height)
        result = _scene_accel.collect(node, transform, 1.0, scale, renderer, 0)
        if len(result) in (6, 9):
            result = result[:-1]
        vertices, quads, count, batches, _interactive = result[:5]
        meshes = {}
        if len(result) == 8:
            meshes = dict(mesh_vb=result[5], mesh_ib=result[6], mesh_batches=result[7])
        renderer.render_packed(
            vertices, quads, count, batches, clear_color=clear_color,
            target_texture=texture, viewport=(width, height), pixel_scale=scale,
            particle_transform=particle_transform, **meshes)
        image = texture.to_image(window)
        completed = True
        return image
    finally:
        if started and not completed:
            # A failed collection can leave glyph or layer draws queued. Finish
            # them before releasing capture resources, preserving the first error.
            try:
                _metal.prepare_image_capture(window.handle)
            except Exception:
                pass
        if texture is not None:
            texture.close()
        # Lazy shaders (including texture dependencies outside this subtree)
        # must initialize again at live resolution on the next normal render.
        for shader, resources, previous_renderer, dirty in shaders:
            for name, previous in resources.items():
                current = getattr(shader, name)
                if current is not None and current is not previous:
                    current.close()
                setattr(shader, name, previous)
            shader._renderer, shader._dirty = previous_renderer, dirty
        for layer, old_texture, logical_size, center, render_scale, dirty, capture_center in layers:
            if layer._tex is not None and layer._tex is not old_texture:
                layer._tex.close()
            layer._tex, layer._lsize, layer._lcenter = old_texture, logical_size, center
            layer._rscale, layer._dirty = render_scale, dirty
            layer._capture_center = capture_center
        for path, previous in paths:
            if path._tex is not None and path._tex is not previous[0]:
                path._tex.close()
            for name, value in zip(path_texture_fields, previous):
                setattr(path, name, value)
        for control, previous in inputs:
            if control._snapshot_texture is not None and control._snapshot_texture is not previous[0]:
                control._snapshot_texture.close()
            for name, value in zip(input_texture_fields, previous):
                setattr(control, name, value)
        for _, buffer in renderer._buf_pool + renderer._buf_used:
            buffer.close()
        for image in renderer._tc.values():
            image.close()
        for atlas in renderer._ga.values():
            atlas.close()
        renderer._tc, renderer._ga = old_text, old_atlases
        renderer._capture_viewport = old_viewport
        renderer._buf_pool, renderer._buf_used = old_pool, old_used
        renderer._active_frame_slot = old_slot
        for child, world, opacity, rendered_size in states:
            child._world_transform = world
            child._world_opacity = opacity
            child._drop_internal_caches(child)
            if hasattr(child, "_rendered_size"):
                child._rendered_size = rendered_size
        renderer.screen_scale = old_scale
        renderer._capturing = False
        root._render_fingerprint = 0
