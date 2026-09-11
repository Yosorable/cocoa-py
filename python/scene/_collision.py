"""scene._collision — Collision detection and spatial hashing."""
from __future__ import annotations

import math

import _scene_accel
from ._common import CollisionInfo

_COLL_TYPE = {'circle': 0, 'obb': 1}


def _collider_to_tuple(c):
    """Convert collider descriptor to (type_int, cx, cy, p0, p1, p2)."""
    if c[0] == 'circle':
        return (0, c[1], c[2], c[3], 0.0, 0.0)
    else:  # obb
        return (1, c[1], c[2], c[3], c[4], c[5])


def _test_colliders(a, b):
    at = _collider_to_tuple(a)
    bt = _collider_to_tuple(b)
    r = _scene_accel.test_colliders(at[0], at[1], at[2], at[3], at[4], at[5],
                                     bt[0], bt[1], bt[2], bt[3], bt[4], bt[5])
    if r is None:
        return CollisionInfo(False, (0, 0), 0, (0, 0))
    return CollisionInfo(True, (r[0], r[1]), r[2], (r[3], r[4]))


# ── Spatial Hash (broad phase) ──

class _SpatialHash:
    __slots__ = ('_cell_size', '_grid')

    def __init__(self, nodes, cell_size=None):
        if cell_size is None:
            max_r = 0
            for n in nodes:
                c = n._collider()
                if c is None:
                    continue
                if c[0] == 'circle':
                    max_r = max(max_r, c[3])
                else:
                    max_r = max(max_r, math.hypot(c[3], c[4]))
            cell_size = max(max_r * 2, 64)
        self._cell_size = cell_size
        self._grid: dict[tuple[int, int], list] = {}
        for n in nodes:
            self._insert(n)

    def _insert(self, node):
        c = node._collider()
        if c is None:
            return
        if c[0] == 'circle':
            cx, cy, r = c[1], c[2], c[3]
        else:
            cx, cy = c[1], c[2]
            r = math.hypot(c[3], c[4])
        cs = self._cell_size
        x0, y0 = int(math.floor((cx - r) / cs)), int(math.floor((cy - r) / cs))
        x1, y1 = int(math.floor((cx + r) / cs)), int(math.floor((cy + r) / cs))
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                key = (gx, gy)
                if key not in self._grid:
                    self._grid[key] = []
                self._grid[key].append(node)

    def potential_pairs(self, set_a_ids, set_b_ids):
        seen = set()
        pairs = []
        for cell_nodes in self._grid.values():
            for i in range(len(cell_nodes)):
                id_i = id(cell_nodes[i])
                for j in range(i + 1, len(cell_nodes)):
                    id_j = id(cell_nodes[j])
                    key = (id_i, id_j) if id_i < id_j else (id_j, id_i)
                    if key in seen:
                        continue
                    a_in_a = id_i in set_a_ids
                    b_in_b = id_j in set_b_ids
                    if a_in_a and b_in_b:
                        seen.add(key)
                        pairs.append((cell_nodes[i], cell_nodes[j]))
                    elif id_i in set_b_ids and id_j in set_a_ids:
                        seen.add(key)
                        pairs.append((cell_nodes[j], cell_nodes[i]))
        return pairs
