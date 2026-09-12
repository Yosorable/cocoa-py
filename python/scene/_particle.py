"""scene._particle — ParticleEmitter node."""
from __future__ import annotations

import math
import struct

from . import gpu
from ._common import _apply, _avg_scale, _color, _mul, _matrix, _IDENTITY, _rot
from ._engine import Cmd, KIND_PARTICLE, Texture
from ._node import Node

# ───────────────────────────────────────────────────────────────────────────
# ParticleEmitter
# ───────────────────────────────────────────────────────────────────────────

_PARTICLE_PACK = struct.Struct("<16f")   # matches Metal Particle struct (64 bytes)
_PARTICLE_UNI  = struct.Struct("<16f")   # matches ParticleUniforms (64 bytes)
_PARTICLE_BYTES = _PARTICLE_PACK.size    # 64
_DEAD_PARTICLE = _PARTICLE_PACK.pack(0,0, 0,0, -999,1, 1,1, 0,0,0,0, 1,0, 0,0)

class ParticleEmitter(Node):
    """GPU-accelerated particle emitter node.

    Python side spawns particles into a ring buffer each frame.
    GPU vertex shader computes physics (displacement + gravity + decay).
    Rendered via draw_instanced — zero per-particle CPU cost after spawn.
    """

    def __init__(self, *,
                 rate=100,
                 burst=0,
                 max_particles=2000,
                 speed=(120, 280),
                 direction=-90,
                 spread=30,
                 gravity=(0, 80),
                 lifetime=(1.5, 3.0),
                 colors=None,
                 size=(2, 6),
                 size_over_life=(1.0, 0.2),
                 opacity_over_life=(1.0, 0.0),
                 blend="additive",
                 texture=None,
                 emit_area=(0, 0),
                 **kw):
        super().__init__(**kw)
        self.texture = texture
        self.emit_area = (float(emit_area[0]), float(emit_area[1]))
        self.rate = float(rate)
        self.burst = int(burst)
        self.max_particles = int(max_particles)
        self.particle_speed = (float(speed[0]), float(speed[1])) if not isinstance(speed, (int, float)) else (float(speed), float(speed))
        self.direction = float(direction)
        self.spread = float(spread)
        self.gravity = (float(gravity[0]), float(gravity[1])) if not isinstance(gravity, (int, float)) else (0.0, float(gravity))
        self.lifetime = (float(lifetime[0]), float(lifetime[1])) if not isinstance(lifetime, (int, float)) else (float(lifetime), float(lifetime))
        self._colors = None
        self.colors = colors or ["#ffffff"]
        self.particle_size = (float(size[0]), float(size[1])) if not isinstance(size, (int, float)) else (float(size), float(size))
        self.size_over_life = (float(size_over_life[0]), float(size_over_life[1]))
        self.opacity_over_life = (float(opacity_over_life[0]), float(opacity_over_life[1]))
        self.blend = blend
        # Internal state
        self._time = 0.0
        self._emit_accum = 0.0
        self._cursor = 0
        self._pending_burst = 0
        self._pbuf = None
        self._ubuf = None
        self._renderer = None
        self._rand = __import__('random').Random()

    @property
    def colors(self):
        return self._color_strs

    @colors.setter
    def colors(self, v):
        self._color_strs = list(v) if v else ["#ffffff"]
        self._colors = [_color(c) for c in self._color_strs]

    def emit(self, count=None):
        """Trigger a one-shot burst of particles."""
        self._pending_burst += count if count is not None else max(self.burst, 1)

    def _init_gpu(self, renderer):
        self._renderer = renderer
        n = self.max_particles
        self._pbuf = gpu.Buffer(n * _PARTICLE_BYTES)
        self._ubuf = gpu.Buffer(64)
        self._pbuf.write(_DEAD_PARTICLE * n)

    def _spawn(self, count, world):
        """Write `count` new particles to the ring buffer."""
        if self._pbuf is None:
            return
        rand = self._rand
        center = _apply(world, (0, 0))
        s = _avg_scale(world)
        cx, cy = center
        aw, ah = self.emit_area[0] * 0.5, self.emit_area[1] * 0.5
        dir_rad = math.radians(self.direction)
        spread_rad = math.radians(self.spread)
        smin, smax = self.particle_speed
        lmin, lmax = self.lifetime
        szmin, szmax = self.particle_size
        sol0, sol1 = self.size_over_life
        ool0, ool1 = self.opacity_over_life
        colors = self._colors
        nc = len(colors)
        t = self._time
        buf = self._pbuf
        cursor = self._cursor
        mx = self.max_particles
        data = bytearray()
        first_cursor = cursor
        for _ in range(count):
            a = dir_rad + rand.uniform(-spread_rad, spread_rad)
            spd = rand.uniform(smin, smax) * s
            vx, vy = math.cos(a) * spd, math.sin(a) * spd
            lt = rand.uniform(lmin, lmax)
            sz = rand.uniform(szmin, szmax) * s
            col = colors[rand.randint(0, nc - 1)]
            sz0 = sz * sol0
            sz1 = sz * sol1
            px = cx + rand.uniform(-aw, aw) if aw > 0 else cx
            py = cy + rand.uniform(-ah, ah) if ah > 0 else cy
            data += _PARTICLE_PACK.pack(
                px, py, vx, vy, t, lt, sz0, sz1,
                col[0], col[1], col[2], col[3],
                ool0, ool1, 0, 0,
            )
            cursor = (cursor + 1) % mx
        if data:
            offset = first_cursor * _PARTICLE_BYTES
            end = first_cursor + count
            if end <= mx:
                buf.write(bytes(data), offset=offset)
            else:
                split = mx - first_cursor
                buf.write(bytes(data[:split * _PARTICLE_BYTES]), offset=offset)
                buf.write(bytes(data[split * _PARTICLE_BYTES:]), offset=0)
        self._cursor = cursor

    def _tick_self(self, dt):
        self._time += dt
        if self._pbuf is not None:
            world = self._world_transform
            if world is not _IDENTITY:
                to_spawn = self._pending_burst
                self._pending_burst = 0
                if self.rate > 0:
                    self._emit_accum += self.rate * dt
                    batch = int(self._emit_accum)
                    if batch > 0:
                        self._emit_accum -= batch
                        to_spawn += batch
                if to_spawn > 0:
                    to_spawn = min(to_spawn, self.max_particles)
                    self._spawn(to_spawn, world)
        super()._tick_self(dt)

    def _collect(self, cmds, renderer, transform, opacity, order):
        if not self.visible or opacity <= 0.001:
            return
        world = _mul(transform, _matrix((self.x, self.y), self.rotation, self.scale))
        op = opacity * self.opacity
        self._world_transform = world
        self._world_opacity = op
        if self._pbuf is None:
            self._init_gpu(renderer)
        # KIND_PARTICLE marker — sorted with all other cmds by z
        order[0] += 1
        cmd = Cmd(self.z, order[0], KIND_PARTICLE,
            0, 0, 0, 0, 0, (0,0,0,0), (0,0,0,0), (0,0,0,0), (0,0,0,0), None)
        cmd._emitter = self  # stash ref for renderer
        cmds.append(cmd)
        for child in self.children:
            child._collect(cmds, renderer, world, op, order)

    def _emit(self, cmds, renderer, world, opacity, order):
        """Called by C accel on first frame to trigger GPU init."""
        if self._pbuf is None:
            self._init_gpu(renderer)

    def _render_particles(self, frame, renderer, opacity, *, msaa=False,
                          resolution=None, transform=None):
        """Called during the render pass to issue the instanced draw call."""
        if self._pbuf is None:
            return
        res = renderer.window.size if resolution is None else resolution
        transform = _IDENTITY if transform is None else transform
        gx, gy = self.gravity
        self._ubuf.write(_PARTICLE_UNI.pack(
            res[0], res[1], self._time, gx, gy, opacity, 0, 0,
            *transform, 0, 0))
        tex = self.texture
        if tex is not None:
            frame.set_pipeline(renderer._pp_tex_ms if msaa else renderer._pp_tex)
            frame.set_fragment_texture(tex, 0)
        else:
            frame.set_pipeline(renderer._pp_ms if msaa else renderer._pp)
        frame.set_vertex_buffer(self._pbuf, 0)
        frame.set_vertex_buffer(self._ubuf, 1)
        frame.draw_instanced("triangle", 0, 6, self.max_particles)

    def _bounds(self):
        return None

    def close(self):
        if self._pbuf is not None:
            self._pbuf.close()
            self._pbuf = None
        if self._ubuf is not None:
            self._ubuf.close()
            self._ubuf = None
        self._renderer = None
        self.texture = None
        super().close()
