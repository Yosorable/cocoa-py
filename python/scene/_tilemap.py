"""scene._tilemap — TileSet / TileMap and Tiled map loaders."""
from __future__ import annotations

import json
import math
import struct

from ._common import _apply, _avg_scale, _color, _rot, _matrix, _mul, _invert
from ._collision import _test_colliders
from ._engine import Cmd, KIND_TEX, Texture, _quad_verts_uv
from ._node import Node
from ._sprite import _acquire_sprite_texture, _release_sprite_texture, SpriteAtlas


# ───────────────────────────────────────────────────────────────────────────
# TileSet / TileMap
# ───────────────────────────────────────────────────────────────────────────

class TileSet:
    """A collection of tiles cut from a texture atlas.

    Parameters
    ----------
    source : str or Texture
        Image file path or Texture object.
    tile_size : tuple[int, int]
        (width, height) of each tile in pixels.
    margin : int
        Outer margin of the atlas in pixels.
    spacing : int
        Spacing between tiles in pixels.
    """

    def __init__(self, source, tile_size, *, margin=0, spacing=0):
        if isinstance(source, str):
            self._texture_path = source
            self.texture = _acquire_sprite_texture(source)
        elif isinstance(source, Texture):
            self._texture_path = None
            self.texture = source
        else:
            raise TypeError("source must be a file path or Texture")

        self.tile_width = int(tile_size[0])
        self.tile_height = int(tile_size[1])
        tw, th = self.texture.size

        self._uv: list[tuple[float, float, float, float]] = []  # (u0, v0, u1, v1)
        y = margin
        while y + self.tile_height <= th - margin:
            x = margin
            while x + self.tile_width <= tw - margin:
                u0 = x / tw
                v0 = y / th
                u1 = (x + self.tile_width) / tw
                v1 = (y + self.tile_height) / th
                self._uv.append((u0, v0, u1, v1))
                x += self.tile_width + spacing
            y += self.tile_height + spacing

    @property
    def count(self):
        return len(self._uv)

    def uv(self, tile_id):
        """Return (u0, v0, u1, v1) for a tile index."""
        return self._uv[tile_id]

    def close(self):
        if self._texture_path is not None:
            _release_sprite_texture(self._texture_path)
            self._texture_path = None
        self.texture = None


class TileMap(Node):
    """Tile-based map node for grid-based 2D games.

    Parameters
    ----------
    tileset : TileSet
        The tileset to draw tiles from.
    columns : int
        Number of columns in the grid.
    rows : int
        Number of rows in the grid.
    layers : int
        Number of layers (default 1). Layer 0 is drawn first.
    origin : str
        Grid origin: ``'top-left'`` (default) or ``'bottom-left'``.
    mode : str
        ``'orthogonal'`` (default) for rectangular grids,
        ``'isometric'`` for diamond/isometric projection.

    Grid data is a flat list per layer. A value of -1 means empty (no tile).

    Example::

        ts = scene.TileSet('tiles.png', (16, 16))
        tm = scene.TileMap(ts, 10, 8)
        tm.set_tile(0, 3, 2, 5)   # layer 0, col 3, row 2 → tile 5
        self.add(tm)

        # Isometric map
        tm = scene.TileMap(ts, 10, 10, mode='isometric')
    """

    def __init__(self, tileset, columns, rows, *, layers=1, origin='top-left',
                 mode='orthogonal', **kw):
        super().__init__(**kw)
        self.tileset: TileSet = tileset
        self.columns = int(columns)
        self.rows = int(rows)
        self.origin = origin
        self.mode = mode  # 'orthogonal' or 'isometric'
        self._layers: list[list[int]] = [
            [-1] * (self.columns * self.rows) for _ in range(layers)
        ]
        self._solid: set[int] = set()  # tile IDs that are solid
        self._anims: dict[int, tuple[list[int], float]] = {}  # tile_id → (frame_ids, fps)
        self._anim_time = 0.0
        self._dirty = True
        self._cached_cmds: list | None = None
        self._owns_tileset = False

    @property
    def layer_count(self):
        return len(self._layers)

    @property
    def map_width(self):
        if self.mode == 'isometric':
            return (self.columns + self.rows) * self.tileset.tile_width // 2
        return self.columns * self.tileset.tile_width

    @property
    def map_height(self):
        if self.mode == 'isometric':
            return (self.columns + self.rows) * self.tileset.tile_height // 2
        return self.rows * self.tileset.tile_height

    def fill(self, layer, data):
        """Fill an entire layer from an iterable of tile IDs (row-major, -1 = empty)."""
        lyr = self._layers[layer]
        for i, v in enumerate(data):
            if i >= len(lyr):
                break
            lyr[i] = int(v)
        self._dirty = True

    def set_tile(self, layer, col, row, tile_id):
        self._layers[layer][row * self.columns + col] = int(tile_id)
        self._dirty = True

    def get_tile(self, layer, col, row):
        return self._layers[layer][row * self.columns + col]

    def world_to_tile(self, wx, wy):
        """Convert world coordinates to (col, row)."""
        lx, ly = self.convert_from_world(wx, wy)
        tw, th = self.tileset.tile_width, self.tileset.tile_height
        if self.mode == 'isometric':
            # Isometric: origin at top-center of the map diamond
            # Shift so (0,0) tile center is at local (rows*tw/2, th/2)
            ox = self.rows * tw * 0.5
            rx = (lx - ox) / tw
            ry = ly / th
            col = int(math.floor(rx + ry))
            row = int(math.floor(ry - rx))
        else:
            col = int(lx // tw)
            row = int(ly // th)
            if self.origin == 'bottom-left':
                row = self.rows - 1 - row
        return (col, row)

    def tile_to_local(self, col, row):
        """Return the local (x, y) of the center of a tile cell."""
        tw, th = self.tileset.tile_width, self.tileset.tile_height
        if self.mode == 'isometric':
            # Isometric projection: diamond layout
            # Origin at top-center, col goes right-down, row goes left-down
            ox = self.rows * tw * 0.5
            lx = ox + (col - row) * tw * 0.5
            ly = (col + row) * th * 0.5 + th * 0.5
            return (lx, ly)
        if self.origin == 'bottom-left':
            row = self.rows - 1 - row
        return (col * tw + tw * 0.5, row * th + th * 0.5)

    # ── animated tiles ──

    def set_animated(self, tile_id, frames, fps=4):
        """Register an animated tile.

        Parameters
        ----------
        tile_id : int
            The tile ID that triggers animation (as placed in the grid).
        frames : list[int]
            Sequence of tile IDs to cycle through.
        fps : float
            Playback speed in frames per second.

        Example::

            tm.set_animated(10, [10, 11, 12, 13], fps=6)  # water animation
        """
        self._anims[tile_id] = (list(frames), float(fps))

    def remove_animated(self, tile_id):
        """Remove animation for a tile ID."""
        self._anims.pop(tile_id, None)

    def _resolve_tile(self, tile_id):
        """Return the display tile ID, accounting for animation."""
        anim = self._anims.get(tile_id)
        if anim is None:
            return tile_id
        frames, fps = anim
        idx = int(self._anim_time * fps) % len(frames)
        return frames[idx]

    def _tick_self(self, dt):
        if self._anims:
            self._anim_time += dt
            self._cache = None  # animated tiles invalidate cache every frame
        super()._tick_self(dt)

    def _snap(self):
        return (self.x, self.y, self.rotation, self.scale, self.opacity, self.z,
                id(self.tileset), self._dirty, self._anim_time if self._anims else 0)

    def _bounds(self):
        if self.mode == 'isometric':
            w = float(self.map_width)
            h = float(self.map_height)
            # Isometric diamond is centered horizontally
            return (0, 0, w, h)
        return (0, 0, float(self.map_width), float(self.map_height))

    def _emit(self, cmds, renderer, world, opacity, order):
        ts = self.tileset
        if ts.texture is None:
            return

        tw, th = ts.tile_width, ts.tile_height
        htw, hth = tw * 0.5, th * 0.5

        # Compute world-space scale and rotation once
        sx = math.hypot(world[0], world[1])
        sy = math.hypot(world[2], world[3])
        rot = _rot(world)
        hw_px = htw * sx
        hh_px = hth * sy

        # Visible rect culling: get camera bounds in local space
        scene_node = self
        while scene_node.parent is not None:
            scene_node = scene_node.parent
        if hasattr(scene_node, '_window'):
            sw, sh = scene_node._window.size
        else:
            sw, sh = 1024, 768

        inv = _invert(world)
        corners = [_apply(inv, p) for p in ((0, 0), (sw, 0), (sw, sh), (0, sh))]
        min_lx = min(c[0] for c in corners)
        max_lx = max(c[0] for c in corners)
        min_ly = min(c[1] for c in corners)
        max_ly = max(c[1] for c in corners)

        has_anims = bool(self._anims)
        resolve = self._resolve_tile

        if self.mode == 'isometric':
            self._emit_iso(cmds, ts, world, opacity, order, tw, th, htw, hth,
                           sx, sy, rot, hw_px, hh_px, has_anims, resolve,
                           min_lx, max_lx, min_ly, max_ly)
        else:
            col0 = max(0, int(min_lx // tw))
            col1 = min(self.columns - 1, int(max_lx // tw))
            row0 = max(0, int(min_ly // th))
            row1 = min(self.rows - 1, int(max_ly // th))

            for layer_data in self._layers:
                for r in range(row0, row1 + 1):
                    for c in range(col0, col1 + 1):
                        tile_id = layer_data[r * self.columns + c]
                        if tile_id < 0:
                            continue
                        display_id = resolve(tile_id) if has_anims else tile_id
                        lx = c * tw + htw
                        ly = r * th + hth
                        wcx, wcy = _apply(world, (lx, ly))
                        u0, v0, u1, v1 = ts._uv[display_id]
                        order[0] += 1
                        cmds.append(Cmd(self.z, order[0], KIND_TEX,
                            wcx, wcy, hw_px, hh_px, rot,
                            (KIND_TEX, 0, 0, 0), (0, 0, opacity, 0),
                            (0, 0, 0, 0), (1, 1, 1, 1), ts.texture,
                            _vb=_quad_verts_uv(wcx, wcy, hw_px, hh_px, rot, u0, v0, u1, v1)))

        self._dirty = False

    def _emit_iso(self, cmds, ts, world, opacity, order, tw, th, htw, hth,
                  sx, sy, rot, hw_px, hh_px, has_anims, resolve,
                  min_lx, max_lx, min_ly, max_ly):
        """Emit commands for isometric tile rendering."""
        ox = self.rows * tw * 0.5  # x-offset for isometric origin

        # Iterate all tiles; cull per-tile against visible rect.
        # Isometric layout means rows/cols don't map to neat axis-aligned ranges,
        # so we iterate all and skip tiles whose local center is outside the padded
        # visible rect. The padding accounts for tile dimensions.
        pad_x = tw
        pad_y = th
        vx0 = min_lx - pad_x
        vx1 = max_lx + pad_x
        vy0 = min_ly - pad_y
        vy1 = max_ly + pad_y

        for layer_data in self._layers:
            for r in range(self.rows):
                for c in range(self.columns):
                    tile_id = layer_data[r * self.columns + c]
                    if tile_id < 0:
                        continue
                    # Isometric local center
                    lx = ox + (c - r) * htw
                    ly = (c + r) * hth + hth
                    # Cull
                    if lx < vx0 or lx > vx1 or ly < vy0 or ly > vy1:
                        continue
                    display_id = resolve(tile_id) if has_anims else tile_id
                    wcx, wcy = _apply(world, (lx, ly))
                    u0, v0, u1, v1 = ts._uv[display_id]
                    order[0] += 1
                    cmds.append(Cmd(self.z, order[0], KIND_TEX,
                        wcx, wcy, hw_px, hh_px, rot,
                        (KIND_TEX, 0, 0, 0), (0, 0, opacity, 0),
                        (0, 0, 0, 0), (1, 1, 1, 1), ts.texture,
                        _vb=_quad_verts_uv(wcx, wcy, hw_px, hh_px, rot, u0, v0, u1, v1)))

    # ── collision ──

    def set_solid(self, *tile_ids):
        """Mark tile IDs as solid (collidable)."""
        self._solid.update(tile_ids)

    def set_not_solid(self, *tile_ids):
        """Remove solid flag from tile IDs."""
        self._solid.difference_update(tile_ids)

    def is_solid(self, col, row, layer=0):
        """Check if the tile at (col, row) is solid."""
        if not (0 <= col < self.columns and 0 <= row < self.rows):
            return False
        tid = self._layers[layer][row * self.columns + col]
        return tid >= 0 and tid in self._solid

    def is_solid_at(self, wx, wy, layer=0):
        """Check if the world position falls on a solid tile."""
        col, row = self.world_to_tile(wx, wy)
        return self.is_solid(col, row, layer)

    def collide_node(self, node, layer=0):
        """Test a node's collider against solid tiles.

        Returns the first CollisionInfo with hit=True, or None.
        Checks all solid tiles that overlap the node's bounding area.
        """
        coll = node._collider()
        if coll is None:
            return None

        tw, th = self.tileset.tile_width, self.tileset.tile_height
        wt = self._world_transform
        inv = _invert(wt)
        sx_map = math.hypot(wt[0], wt[1])
        sy_map = math.hypot(wt[2], wt[3])

        # Node's world-space bounding box → local tile coords
        if coll[0] == 'circle':
            _, cx, cy, cr = coll
            r_local = cr / min(sx_map, sy_map) if min(sx_map, sy_map) > 1e-6 else cr
            lc = _apply(inv, (cx, cy))
            min_lx, max_lx = lc[0] - r_local, lc[0] + r_local
            min_ly, max_ly = lc[1] - r_local, lc[1] + r_local
        else:
            # OBB: use center ± half-extents as conservative AABB
            _, cx, cy, hw, hh, _ = coll
            ext = max(hw, hh)
            e_local = ext / min(sx_map, sy_map) if min(sx_map, sy_map) > 1e-6 else ext
            lc = _apply(inv, (cx, cy))
            min_lx, max_lx = lc[0] - e_local, lc[0] + e_local
            min_ly, max_ly = lc[1] - e_local, lc[1] + e_local

        col0 = max(0, int(min_lx // tw))
        col1 = min(self.columns - 1, int(max_lx // tw))
        row0 = max(0, int(min_ly // th))
        row1 = min(self.rows - 1, int(max_ly // th))

        layer_data = self._layers[layer]
        rot_map = _rot(wt)
        best = None

        for r in range(row0, row1 + 1):
            for c in range(col0, col1 + 1):
                tid = layer_data[r * self.columns + c]
                if tid < 0 or tid not in self._solid:
                    continue
                # Build OBB collider for this tile in world space
                lx = c * tw + tw * 0.5
                ly = r * th + th * 0.5
                wcx, wcy = _apply(wt, (lx, ly))
                tile_hw = tw * sx_map * 0.5
                tile_hh = th * sy_map * 0.5
                tile_coll = ('obb', wcx, wcy, tile_hw, tile_hh, rot_map)
                info = _test_colliders(coll, tile_coll)
                if info is not None and info.hit:
                    if best is None or info.depth > best.depth:
                        best = info
        return best

    def collide_node_all(self, node, layer=0):
        """Test a node against all solid tiles. Returns list of (col, row, CollisionInfo)."""
        coll = node._collider()
        if coll is None:
            return []

        tw, th = self.tileset.tile_width, self.tileset.tile_height
        wt = self._world_transform
        inv = _invert(wt)
        sx_map = math.hypot(wt[0], wt[1])
        sy_map = math.hypot(wt[2], wt[3])

        if coll[0] == 'circle':
            _, cx, cy, cr = coll
            r_local = cr / min(sx_map, sy_map) if min(sx_map, sy_map) > 1e-6 else cr
            lc = _apply(inv, (cx, cy))
            min_lx, max_lx = lc[0] - r_local, lc[0] + r_local
            min_ly, max_ly = lc[1] - r_local, lc[1] + r_local
        else:
            _, cx, cy, hw, hh, _ = coll
            ext = max(hw, hh)
            e_local = ext / min(sx_map, sy_map) if min(sx_map, sy_map) > 1e-6 else ext
            lc = _apply(inv, (cx, cy))
            min_lx, max_lx = lc[0] - e_local, lc[0] + e_local
            min_ly, max_ly = lc[1] - e_local, lc[1] + e_local

        col0 = max(0, int(min_lx // tw))
        col1 = min(self.columns - 1, int(max_lx // tw))
        row0 = max(0, int(min_ly // th))
        row1 = min(self.rows - 1, int(max_ly // th))

        layer_data = self._layers[layer]
        rot_map = _rot(wt)
        results = []

        for r in range(row0, row1 + 1):
            for c in range(col0, col1 + 1):
                tid = layer_data[r * self.columns + c]
                if tid < 0 or tid not in self._solid:
                    continue
                lx = c * tw + tw * 0.5
                ly = r * th + th * 0.5
                wcx, wcy = _apply(wt, (lx, ly))
                tile_hw = tw * sx_map * 0.5
                tile_hh = th * sy_map * 0.5
                tile_coll = ('obb', wcx, wcy, tile_hw, tile_hh, rot_map)
                info = _test_colliders(coll, tile_coll)
                if info is not None and info.hit:
                    results.append((c, r, info))
        return results

    def close(self):
        self._cached_cmds = None
        if self._owns_tileset and self.tileset is not None:
            self.tileset.close()
        self.tileset = None
        self._layers.clear()
        self._solid.clear()
        self._anims.clear()
        super().close()


# ── Tiled map loader ──

def load_tiled(path, **kw):
    """Load a Tiled map file (.json or .tmx) and return a TileMap.

    Parameters
    ----------
    path : str
        Path to a Tiled JSON (.json) or TMX (.tmx) map file.
    **kw
        Extra keyword arguments passed to the TileMap constructor
        (e.g. ``x``, ``y``, ``z``, ``origin``).

    Returns
    -------
    TileMap
        A fully configured TileMap with layers, solid tiles, and animations.

    The tileset image path is resolved relative to the map file.
    Only tile layers are imported; object layers are ignored.

    Example::

        tm = scene.load_tiled('level1.json')
        self.add(tm)
    """
    import os
    base_dir = os.path.dirname(os.path.abspath(path))

    if path.lower().endswith('.tmx'):
        return _load_tmx(path, base_dir, kw)
    else:
        return _load_tiled_json(path, base_dir, kw)


def _resolve_image(image_path, base_dir):
    """Resolve tileset image path relative to the map file directory."""
    import os
    if os.path.isabs(image_path):
        return image_path
    return os.path.join(base_dir, image_path)


def _load_tiled_json(path, base_dir, kw):
    """Load a Tiled JSON map."""
    import os

    with open(path, 'r') as f:
        data = json.load(f)

    map_w = data['width']
    map_h = data['height']

    # Parse tilesets
    tilesets_info = []  # (firstgid, TileSet, animations)
    for ts_data in data.get('tilesets', []):
        firstgid = ts_data['firstgid']

        # Handle external tileset reference
        if 'source' in ts_data and 'image' not in ts_data:
            tsx_path = _resolve_image(ts_data['source'], base_dir)
            ts_data = _load_external_tileset_json(tsx_path)

        image = _resolve_image(ts_data['image'], base_dir)
        tw = ts_data.get('tilewidth', data['tilewidth'])
        th = ts_data.get('tileheight', data['tileheight'])
        margin = ts_data.get('margin', 0)
        spacing = ts_data.get('spacing', 0)

        ts = TileSet(image, (tw, th), margin=margin, spacing=spacing)

        # Parse tile animations
        anims = {}
        for tile in ts_data.get('tiles', []):
            if 'animation' in tile:
                local_id = tile['id']
                frames = [frame['tileid'] for frame in tile['animation']]
                # Use average duration for fps
                durations = [frame['duration'] for frame in tile['animation']]
                avg_ms = sum(durations) / len(durations) if durations else 250
                fps = 1000.0 / avg_ms
                anims[local_id] = (frames, fps)

        tilesets_info.append((firstgid, ts, anims))

    # Use the first tileset (most common case)
    if not tilesets_info:
        raise ValueError("No tilesets found in Tiled map")

    # Sort by firstgid
    tilesets_info.sort(key=lambda x: x[0])

    # For single-tileset maps (most common), use it directly
    # For multi-tileset maps, use the first one and remap GIDs
    primary_firstgid, primary_ts, primary_anims = tilesets_info[0]

    # Parse tile layers
    tile_layers = []
    for layer in data.get('layers', []):
        if layer.get('type') != 'tilelayer':
            continue
        layer_data = layer.get('data', [])
        # Convert global IDs to local IDs (subtract firstgid, 0 → -1)
        local_data = []
        for gid in layer_data:
            # Strip flip flags (top 3 bits)
            gid = gid & 0x1FFFFFFF
            if gid == 0:
                local_data.append(-1)
            else:
                local_data.append(gid - primary_firstgid)
        tile_layers.append(local_data)

    if not tile_layers:
        raise ValueError("No tile layers found in Tiled map")

    # Detect orientation
    orientation = data.get('orientation', 'orthogonal')
    mode = 'isometric' if orientation == 'isometric' else 'orthogonal'
    kw.setdefault('mode', mode)

    # Create TileMap
    tm = TileMap(primary_ts, map_w, map_h, layers=len(tile_layers), **kw)
    tm._owns_tileset = True
    for i, layer_data in enumerate(tile_layers):
        tm.fill(i, layer_data)

    # Register animations
    for local_id, (frames, fps) in primary_anims.items():
        tm.set_animated(local_id, frames, fps)

    return tm


def _load_external_tileset_json(path):
    """Load an external Tiled tileset (.json/.tsj)."""
    with open(path, 'r') as f:
        return json.load(f)


def _load_tmx(path, base_dir, kw):
    """Load a Tiled TMX map."""
    import os
    import xml.etree.ElementTree as ET

    tree = ET.parse(path)
    root = tree.getroot()

    map_w = int(root.get('width'))
    map_h = int(root.get('height'))

    # Parse tilesets
    tilesets_info = []
    for ts_el in root.findall('tileset'):
        firstgid = int(ts_el.get('firstgid'))

        # Handle external tileset reference (.tsx)
        source = ts_el.get('source')
        if source:
            tsx_path = _resolve_image(source, base_dir)
            ts_el = _load_external_tileset_tsx(tsx_path)

        tw = int(ts_el.get('tilewidth'))
        th = int(ts_el.get('tileheight'))
        margin = int(ts_el.get('margin', 0))
        spacing = int(ts_el.get('spacing', 0))

        # Get image path
        img_el = ts_el.find('image')
        if img_el is None:
            continue
        image = _resolve_image(img_el.get('source'), base_dir)

        ts = TileSet(image, (tw, th), margin=margin, spacing=spacing)

        # Parse tile animations
        anims = {}
        for tile_el in ts_el.findall('tile'):
            anim_el = tile_el.find('animation')
            if anim_el is not None:
                local_id = int(tile_el.get('id'))
                frames = []
                durations = []
                for frame_el in anim_el.findall('frame'):
                    frames.append(int(frame_el.get('tileid')))
                    durations.append(int(frame_el.get('duration')))
                avg_ms = sum(durations) / len(durations) if durations else 250
                fps = 1000.0 / avg_ms
                anims[local_id] = (frames, fps)

        tilesets_info.append((firstgid, ts, anims))

    if not tilesets_info:
        raise ValueError("No tilesets found in Tiled map")

    tilesets_info.sort(key=lambda x: x[0])
    primary_firstgid, primary_ts, primary_anims = tilesets_info[0]

    # Parse tile layers
    tile_layers = []
    for layer_el in root.findall('layer'):
        data_el = layer_el.find('data')
        if data_el is None:
            continue

        encoding = data_el.get('encoding', '')
        compression = data_el.get('compression', '')

        if encoding == 'csv' or encoding == '':
            # CSV or inline
            text = data_el.text.strip()
            gids = [int(v) for v in text.replace('\n', ',').split(',') if v.strip()]
        elif encoding == 'base64':
            import base64
            raw = base64.b64decode(data_el.text.strip())
            if compression == 'zlib':
                import zlib
                raw = zlib.decompress(raw)
            elif compression == 'gzip':
                import gzip
                raw = gzip.decompress(raw)
            elif compression:
                raise ValueError(f"Unsupported TMX compression: {compression}")
            gids = list(struct.unpack(f'<{len(raw) // 4}I', raw))
        else:
            raise ValueError(f"Unsupported TMX encoding: {encoding}")

        # Convert GIDs to local IDs
        local_data = []
        for gid in gids:
            gid = gid & 0x1FFFFFFF
            if gid == 0:
                local_data.append(-1)
            else:
                local_data.append(gid - primary_firstgid)
        tile_layers.append(local_data)

    if not tile_layers:
        raise ValueError("No tile layers found in Tiled map")

    # Detect orientation
    orientation = root.get('orientation', 'orthogonal')
    mode = 'isometric' if orientation == 'isometric' else 'orthogonal'
    kw.setdefault('mode', mode)

    tm = TileMap(primary_ts, map_w, map_h, layers=len(tile_layers), **kw)
    for i, layer_data in enumerate(tile_layers):
        tm.fill(i, layer_data)

    for local_id, (frames, fps) in primary_anims.items():
        tm.set_animated(local_id, frames, fps)

    return tm


def _load_external_tileset_tsx(path):
    """Load an external Tiled tileset (.tsx) and return the XML element."""
    import xml.etree.ElementTree as ET
    return ET.parse(path).getroot()
