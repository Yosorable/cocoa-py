"""Batch records must preserve Sprite geometry, ordering and ownership."""

import gc
import math
import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace
import unittest
import weakref

from _cocoa import _scene_accel
from scene import ClipRect, Group, Layer, Node, Scene, Sprite, SpriteBatch, SpriteFrame, gpu
from scene._common import _IDENTITY, _matrix


class SpriteBatchTests(unittest.TestCase):
    def setUp(self):
        # Collection only needs texture metadata; these tests do not claim to
        # exercise Metal allocation, rasterization or GPU performance.
        self.texture = gpu.Texture(0, (23, 19))
        self.addCleanup(setattr, self.texture, "_handle", None)
        self.frame = SpriteFrame(self.texture, (.125, .25, .875, .75))
        self.renderer = SimpleNamespace(screen_scale=1.)

    def collect(self, root, previous=0):
        return _scene_accel.collect(root, _IDENTITY, 1., 1., self.renderer, previous)

    def equivalent(self, root, reference):
        actual = self.collect(root)
        expected = self.collect(reference)
        self.assertEqual(actual[:4], expected[:4])
        return actual

    def test_affine_uv_anchor_flip_blend_and_inherited_opacity(self):
        root, reference = Group(x=31, y=-7, rotation=.2, scale=(1.7, -.9), opacity=.8), Group(x=31, y=-7, rotation=.2, scale=(1.7, -.9), opacity=.8)
        batch = SpriteBatch(opacity=.7)
        group = Group(opacity=.7)
        root.add(batch)
        reference.add(group)
        for index in range(5):
            options = dict(size=(24.5, 18.75), anchor=(.2, .8),
                           affine_transform=(-1, .25, -.125, 1.1, index * 7, -3),
                           opacity=index * .2, z=5-index, flip_x=bool(index & 1),
                           flip_y=bool(index & 2), tint=(.3, .5, .7, .4),
                           blend="additive" if index & 1 else "alpha")
            batch.add_sprite(self.frame, **options)
            group.add(Sprite(self.frame, **options))
        self.equivalent(root, reference)
        self.equivalent(root, reference)  # Reuse native geometry caches.

    def test_parent_visibility_pose_opacity_and_clips_are_inherited(self):
        for visible, opacity in ((True, .7), (False, .7), (True, 0), (True, .001)):
            with self.subTest(visible=visible, opacity=opacity):
                clip = ClipRect(-4, 2, 18, 30, radius=3)
                root, reference = Group(x=15, rotation=.3), Group(x=15, rotation=.3)
                batch = SpriteBatch()
                root.add(batch)
                parent = batch.add_sprite(self.frame, opacity=opacity, visible=visible,
                    affine_transform=(1, .2, .4, 1, 8, 3), clip=clip)
                batch.add_sprite(self.frame, parent=parent, opacity=.4, z=.25,
                                 affine_transform=(.5, 0, 0, -.5, 4, 6))
                clipper = Group(clip=clip)
                reference.add(clipper)
                sprite = Sprite(self.frame, opacity=opacity, affine_transform=parent.affine_transform)
                sprite.visible = visible
                clipper.add(sprite)
                sprite.add(Sprite(self.frame, opacity=.4, z=.25,
                                  affine_transform=(.5, 0, 0, -.5, 4, 6)))
                self.equivalent(root, reference)
                self.equivalent(root, reference)

    def test_depths_interleave_other_nodes_and_preserve_stacking_contexts(self):
        for stacking in (False, True):
            root, reference = Group(), Group()
            batch, group = SpriteBatch(z=10), Group(z=10)
            batch._stacking_context = group._stacking_context = stacking
            root.add(batch)
            reference.add(group)
            for z in (3, 1, 3, 5):
                batch.add_sprite(self.frame, z=z, affine_transform=(1, 0, 0, 1, z * 10, 0))
                group.add(Sprite(self.frame, z=z, affine_transform=(1, 0, 0, 1, z * 10, 0)))
            root.add(Sprite(self.frame, z=3, x=17))
            reference.add(Sprite(self.frame, z=3, x=17))
            self.equivalent(root, reference)

    def test_late_child_keeps_parent_subtree_order_at_equal_depth(self):
        batch, reference = SpriteBatch(), Group()
        parent = batch.add_sprite(self.frame)
        first = Sprite(self.frame)
        reference.add(first)
        batch.add_sprite(self.frame, affine_transform=(1, 0, 0, 1, 20, 0))
        reference.add(Sprite(self.frame, x=20))
        batch.add_sprite(self.frame, parent=parent, affine_transform=(1, 0, 0, 1, 4, 0))
        first.add(Sprite(self.frame, x=4))
        self.equivalent(batch, reference)
        self.equivalent(batch, reference)

    def test_composition_matches_separate_float_operations_and_rejects_overflow(self):
        batch = SpriteBatch()
        item = batch.add_sprite(self.frame)
        a = (1.17, -.28, .39, -1.31, 80.27, -65.87)
        b = (.91, .43, -.7, 1.24, -6.33, 9.45)
        expected = (a[0]*b[0] + a[2]*b[1], a[1]*b[0] + a[3]*b[1],
                    a[0]*b[2] + a[2]*b[3], a[1]*b[2] + a[3]*b[3],
                    a[0]*b[4] + a[2]*b[5] + a[4], a[1]*b[4] + a[3]*b[5] + a[5])
        batch.set_transforms((0,), (b,), transform=a)
        self.assertEqual(item.affine_transform, expected)
        with self.assertRaises(ValueError):
            batch.set_transforms((0,), ((1e308, 0, 0, 1, 0, 0),), transform=(2, 0, 0, 1, 0, 0))
        self.assertEqual(item.affine_transform, expected)

    def test_capture_bounds_include_parent_pose_visibility_and_clips(self):
        batch = SpriteBatch()
        parent = batch.add_sprite(self.texture, size=(10, 8), anchor=(0, 0),
                                  affine_transform=(1, 0, 0, 1, 10, 20), clip=ClipRect(12, 0, 40, 40))
        child = batch.add_sprite(self.texture, size=(10, 8), anchor=(0, 0), parent=parent,
                                 affine_transform=(1, 0, 0, 1, 20, 0))
        self.assertEqual(batch._bounds(), (12, 20, 40, 28))
        child.visible = False
        self.assertEqual(batch._bounds(), (12, 20, 20, 28))
        parent.visible = False
        self.assertIsNone(batch._bounds())

    def test_component_poses_match_sprite_transforms_and_copy_strided_arrays(self):
        batch, reference = SpriteBatch(), Group()
        poses = ((10, 20, .37, -1.2, .8), (-5, 8, -.73, .5, 1.7))
        for x, y, angle, sx, sy in poses:
            batch.add_sprite(self.frame)
            reference.add(Sprite(self.frame, position=(x, y), rotation=angle, scale=(sx, sy)))
        batch.set_poses((0, 1), poses)
        self.equivalent(batch, reference)
        before = tuple(item.affine_transform for item in batch.sprites)
        with self.assertRaises(ValueError):
            batch.set_poses((0, 1), (poses[0], (1, 2, math.inf, 1, 1)))
        self.assertEqual(tuple(item.affine_transform for item in batch.sprites), before)

    def test_array_sequences_copy_values_without_retaining_the_source_buffer(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("NumPy is optional")
        batch = SpriteBatch()
        for _ in range(2):
            batch.add_sprite(self.frame)
        for dtype in ("float32", "float64", ">f8"):
            values = np.arange(24, dtype=dtype).reshape(2, 12)[:, ::2]
            batch.set_transforms((0, 1), values)
            expected = [tuple(float(x) for x in row) for row in values]
            values[:] = 0
            self.assertEqual([item.affine_transform for item in batch.sprites], expected)

    def test_geometry_and_appearance_update_matches_individual_sprites(self):
        batch, reference = SpriteBatch(), Group()
        items = [batch.add_sprite(self.frame) for _ in range(3)]
        sprites = [Sprite(self.frame) for _ in items]
        reference.add(*sprites)
        ids = (2, 0, 1, 0)
        poses = ((8, 2, .3, -1.2, .7), (5, 9, -.7, .6, 1.8),
                 (15, -4, 0, 1, 1), (3, 6, .2, 1.3, -1))
        sizes = ((17, 12), (25, 30), (16, 9), (21, 28))
        tints = ((1, .2, .4, .8), (.2, .4, .8, 1), (.8, .4, .1, .5), (.4, .3, .8, .6))
        opacities, depths, shown = (.8, .6, .2, .9), (3, 1, 2, 3), (True, False, True, True)
        batch.update_sprites(ids, poses=poses, sizes=sizes, tints=tints,
                             opacities=opacities, depths=depths, visible=shown)
        for i, pose, size, tint, opacity, depth, visible in zip(ids, poses, sizes, tints, opacities, depths, shown):
            sprite = sprites[i]
            sprite.position, sprite.rotation, sprite.scale = pose[:2], pose[2], pose[3:]
            sprite.sprite_size, sprite.tint = size, tint
            sprite.opacity, sprite.z, sprite.visible = opacity, depth, visible
        self.equivalent(batch, reference)
        self.equivalent(batch, reference)
        unchanged = [item.affine_transform for item in items]
        batch.update_sprites((1,), tints=((.1, .3, .6, 1),), visible=(False,))
        sprites[1].tint, sprites[1].visible = (.1, .3, .6, 1), False
        self.assertEqual([item.affine_transform for item in items], unchanged)
        self.equivalent(batch, reference)
        revision = batch._snap()
        batch.update_sprites(())
        batch.update_sprites((1,), tints=((.1, .3, .6, 1),), visible=(False,))
        self.assertEqual(batch._snap(), revision)

    def test_geometry_and_appearance_reject_invalid_updates_before_committing(self):
        batch = SpriteBatch()
        items = [batch.add_sprite(self.frame) for _ in range(2)]
        def state():
            return [(item.affine_transform, item.sprite_size, item.tint,
                     item.opacity, item.z, item.visible) for item in items]
        expected, revision = state(), batch._snap()
        common = dict(poses=((8, 2, .3, 1, 1), (15, -4, 0, 1, 1)),
                      sizes=((17, 12), (25, 30)), tints=((1, .2, .4, .8), (.2, .4, .8, 1)),
                      opacities=(.5, .9), depths=(2, 3), visible=(False, False))
        for invalid in (
            dict(sizes=((12, 17), (1,))), dict(tints=((1, 1, 1, 1), (1, 1, math.nan, 1))),
            dict(opacities=(.5, math.inf)), dict(depths=(2, object())), dict(visible=(True,)),
            dict(transforms=(_IDENTITY, _IDENTITY)),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises((ValueError, TypeError)):
                    batch.update_sprites((0, 1), **(common | invalid))
                self.assertEqual(state(), expected)
                self.assertEqual(batch._snap(), revision)
        for ids in ((0, 10), (-1, 1), (0,)):
            with self.assertRaises((ValueError, IndexError)):
                batch.update_sprites(ids, **common)
            self.assertEqual(state(), expected)

    def test_combined_update_composes_shared_transform_without_extra_rounding(self):
        def compose(a, b):
            return (a[0]*b[0] + a[2]*b[1], a[1]*b[0] + a[3]*b[1],
                    a[0]*b[2] + a[2]*b[3], a[1]*b[2] + a[3]*b[3],
                    a[0]*b[4] + a[2]*b[5] + a[4], a[1]*b[4] + a[3]*b[5] + a[5])
        batch = SpriteBatch()
        item = batch.add_sprite(self.frame)
        a = (1.17, -.28, .39, -1.31, 80.27, -65.87)
        b = (.91, .43, -.7, 1.24, -6.33, 9.45)
        batch.update_sprites((0,), transforms=(b,), transform=a, opacities=(.4,))
        self.assertEqual(item.affine_transform, compose(a, b))
        pose = (3.9, -17.3, .79, -.8, 1.37)
        batch.update_sprites((0,), poses=(pose,), transform=a)
        self.assertEqual(item.affine_transform, compose(a, _matrix(pose[:2], pose[2], pose[3:])))
        expected = item.affine_transform
        with self.assertRaises(ValueError):
            batch.update_sprites((0,), transforms=((1e308, 0, 0, 1, 0, 0),),
                                 transform=(2, 0, 0, 1, 0, 0), opacities=(.9,))
        self.assertEqual((item.affine_transform, item.opacity), (expected, .4))
        with self.assertRaises(ValueError):
            batch.update_sprites((0,), tints=((1, 0, 0, 1),), transform=a)

    def test_combined_update_copies_strided_and_non_native_endian_arrays(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("NumPy is optional")
        batch = SpriteBatch()
        items = [batch.add_sprite(self.frame) for _ in range(2)]
        for dtype in ("float32", "float64", ">f8"):
            matrices = np.arange(24, dtype=dtype).reshape(2, 12)[:, ::2]
            tints = np.arange(16, dtype=dtype).reshape(2, 8)[:, ::2]
            batch.update_sprites((0, 1), transforms=matrices, tints=tints)
            expected = [(tuple(float(x) for x in matrix), tuple(float(x) for x in tint))
                        for matrix, tint in zip(matrices, tints)]
            matrices[:] = tints[:] = 0
            self.assertEqual([(item.affine_transform, item.tint) for item in items], expected)

    def test_numeric_callbacks_can_change_source_sequences_without_invalid_memory_access(self):
        source = textwrap.dedent("""
            from scene import SpriteBatch, SpriteFrame, gpu
            texture = gpu.Texture(0, (23, 19))
            frame = SpriteFrame(texture, (0, 0, 1, 1))
            for operation in ("property", "transforms", "combined"):
                batch = SpriteBatch()
                item = batch.add_sprite(frame)
                ids, row, rows, colors = [0], [], [], [(1, .5, .25, 1)]
                class Number:
                    def __float__(self):
                        ids.clear()
                        row.clear()
                        rows.clear()
                        colors.clear()
                        for _ in range(20):
                            batch.add_sprite(frame)
                        return 2.
                row.extend([Number(), .25, -.5, 1., 12., -3.])
                rows.append(row)
                if operation == "property":
                    item.affine_transform = row
                elif operation == "transforms":
                    batch.set_transforms(ids, rows)
                else:
                    batch.update_sprites(ids, transforms=rows, tints=colors)
                    assert item.tint == (1, .5, .25, 1)
                assert item.affine_transform == (2., .25, -.5, 1., 12., -3.)
                batch.close()
            texture._handle = None
        """)
        result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_texture_finalizers_cannot_edit_or_reenter_an_active_collection(self):
        source = textwrap.dedent("""
            import faulthandler
            import gc
            from types import SimpleNamespace
            from scene import SpriteBatch, gpu
            from scene._common import _IDENTITY
            from _cocoa import _scene_accel
            faulthandler.enable()
            texture = gpu.Texture(0, (23, 19))
            renderer = SimpleNamespace(screen_scale=1.)
            def collect(batch):
                return _scene_accel.collect(batch, _IDENTITY, 1., 1., renderer)
            for operation in ('clear', 'close', 'append', 'update', 'collect'):
                batch = SpriteBatch()
                errors = []
                class OldTexture(gpu.Texture):
                    def __del__(self):
                        self._handle = None
                        gc.collect()
                        try:
                            if operation == 'append':
                                batch.add_sprite(texture)
                            elif operation == 'update':
                                batch.update_sprites((0,), visible=(False,))
                            elif operation == 'collect':
                                collect(batch)
                            else:
                                getattr(batch, operation)()
                        except RuntimeError as error:
                            errors.append(str(error))
                item = batch.add_sprite(OldTexture(0, (23, 19)))
                first = collect(batch)
                del first
                item.frame = texture
                assert collect(batch)[2] == 1, operation
                assert len(errors) == 1 and 'during collection' in errors[0], (operation, errors)
                assert len(batch.sprites) == 1 and item.visible, operation
                batch.add_sprite(texture)
                assert collect(batch)[2] == 2, operation
                batch.close()
            texture._handle = None
        """)
        result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True,
                                timeout=20, env={**os.environ, "MallocScribble": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_reference_cycles_are_collected_before_and_after_drawing(self):
        class OwnedClip(ClipRect):
            pass

        class OwnedTexture(gpu.Texture):
            def close(self):
                self._handle = None

        def make_cycle(kind, path):
            batch = SpriteBatch()
            if kind == "clip":
                resource = OwnedClip(0, 0, 20, 20)
                object.__setattr__(resource, "owner", batch)
                batch.add_sprite(self.frame, clip=resource)
            else:
                resource = OwnedTexture(0, (23, 19))
                resource.owner = batch
                # Multiple references to the same object must all be traversed.
                for _ in range(3):
                    batch.add_sprite(resource)
            if path == "native":
                self.collect(batch)
            elif path == "python":
                batch._collect([], self.renderer, _IDENTITY, 1., [0])
            return weakref.ref(batch), weakref.ref(resource)

        for kind in ("clip", "texture"):
            for path in ("uncollected", "native", "python"):
                with self.subTest(kind=kind, path=path):
                    references = make_cycle(kind, path)
                    gc.collect()
                    try:
                        self.assertTrue(all(reference() is None for reference in references))
                    finally:
                        if references[0]() is not None:
                            references[0]().clear()

    def test_dynamic_record_clips_invalidate_consecutive_frames(self):
        offset = [0.]

        class DynamicClip(ClipRect):
            def _state(self, world):
                return (*super()._state(world)[:6], offset[0], 0., 10., 10., 0.)

        clip = DynamicClip(0, 0, 10, 10)
        batch, reference = SpriteBatch(), Group(clip=clip)
        parent = batch.add_sprite(self.frame, clip=clip)
        batch.add_sprite(self.frame, parent=parent)
        reference.add(Sprite(self.frame))
        reference.add(Sprite(self.frame))
        previous = self.equivalent(batch, reference)
        self.assertIsNone(self.collect(batch, previous[-1])[0])
        for value in (5., 12., 0.):
            offset[0] = value
            updated = self.collect(batch, previous[-1])
            self.assertIsNotNone(updated[0], "Changed record clips must redraw a stationary batch")
            self.assertEqual(updated[:4], self.collect(reference)[:4])
            self.assertNotEqual(updated[3], previous[3])
            self.assertIsNone(self.collect(batch, updated[-1])[0])
            previous = updated

    def test_nested_collection_does_not_unlock_the_outer_clip_callback(self):
        batch = SpriteBatch()
        item = batch.add_sprite(self.frame)
        nested = False
        errors = []

        class ReentrantClip(ClipRect):
            def _state(clip, world):
                nonlocal nested
                if not nested:
                    nested = True
                    try:
                        self.collect(batch)
                    except RuntimeError as error:
                        errors.append(str(error))
                    finally:
                        nested = False
                    try:
                        item.visible = False
                    except RuntimeError as error:
                        errors.append(str(error))
                return super()._state(world)

        item.clip = ReentrantClip(0, 0, 20, 20)
        self.assertEqual(self.collect(batch)[2], 1)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("during collection" in error for error in errors))
        item.clip = None
        item.visible = False
        self.assertEqual(self.collect(batch)[2], 0)

    def test_node_clips_and_later_siblings_cannot_clear_a_collected_batch(self):
        root, batch = Group(), SpriteBatch()
        root.add(batch)
        batch.add_sprite(self.frame)

        class NodeClip(ClipRect):
            def _state(clip, world):
                with self.assertRaisesRegex(RuntimeError, "during collection"):
                    batch.clear()
                return super()._state(world)

        class LaterNode(Node):
            def _emit(node, *args):
                with self.assertRaisesRegex(RuntimeError, "during collection"):
                    self.collect(batch)
                with self.assertRaisesRegex(RuntimeError, "during collection"):
                    batch.clear()

        batch.clip = NodeClip(0, 0, 20, 20)
        root.add(LaterNode())
        self.assertEqual(self.collect(root)[2], 1)
        batch.clear()
        self.assertEqual(batch.sprites, ())

    def test_record_clip_can_detach_its_batch_without_destroying_the_current_frame(self):
        root = Group()

        class DetachingClip(ClipRect):
            def _state(clip, world):
                root.remove(root.children[0])
                gc.collect()
                return super()._state(world)

        def attach():
            batch = SpriteBatch()
            root.add(batch)
            batch.add_sprite(self.frame, clip=DetachingClip(0, 0, 20, 20))
            return weakref.ref(batch)

        reference = attach()
        self.assertEqual(self.collect(root)[2], 1)
        self.assertEqual(root.children, [])
        self.assertIsNone(reference())

    def test_python_collection_protects_records_and_skips_hidden_clips(self):
        batch = SpriteBatch()
        item = batch.add_sprite(self.frame)

        class EditingClip(ClipRect):
            def _state(clip, world):
                batch.clear()
                return super()._state(world)

        item.clip = EditingClip(0, 0, 20, 20)
        with self.assertRaisesRegex(RuntimeError, "during collection"):
            batch._collect([], self.renderer, _IDENTITY, 1., [0])
        item.visible = False
        commands = []
        batch._collect(commands, self.renderer, _IDENTITY, 1., [0])
        self.assertEqual(commands, [])
        item.clip = None
        item.visible = True
        batch._collect(commands, self.renderer, _IDENTITY, 1., [0])
        self.assertEqual(len(commands), 1)

    def test_clip_callback_rejects_combined_updates(self):
        batch = SpriteBatch()
        item = batch.add_sprite(self.frame)
        class EditingClip(ClipRect):
            def _state(self, world):
                batch.update_sprites((0,), visible=(False,))
                return super()._state(world)
        item.clip = EditingClip(0, 0, 20, 20)
        with self.assertRaisesRegex(RuntimeError, "during collection"):
            self.collect(batch)
        self.assertTrue(item.visible)
        item.clip = None
        self.assertEqual(self.collect(batch)[2], 1)

    def test_clip_callback_cannot_mutate_storage_while_it_is_read(self):
        batch = SpriteBatch()
        item = batch.add_sprite(self.frame)

        class EditingClip(ClipRect):
            def _state(self, world):
                item.visible = False
                return super()._state(world)

        item.clip = EditingClip(0, 0, 20, 20)
        with self.assertRaisesRegex(RuntimeError, "during collection"):
            self.collect(batch)
        self.assertTrue(item.visible)
        item.clip = None
        self.assertEqual(self.collect(batch)[2], 1)

    def test_clip_callbacks_cannot_clear_or_close_the_batch_being_collected(self):
        for operation in ("clear", "close"):
            batch = SpriteBatch()
            item = batch.add_sprite(self.frame)

            class ClosingClip(ClipRect):
                def _state(self, world):
                    getattr(batch, operation)()
                    return super()._state(world)

            item.clip = ClosingClip(0, 0, 20, 20)
            with self.assertRaisesRegex(RuntimeError, "during collection"):
                self.collect(batch)
            self.assertEqual(batch.sprites, (item,))
            item.clip = None
            self.assertEqual(self.collect(batch)[2], 1)
            batch.close()

    def test_hidden_clips_are_not_evaluated_even_when_other_sprites_are_visible(self):
        class UnusedClip(ClipRect):
            def _state(self, world):
                raise AssertionError("A hidden sprite must not evaluate its clip")

        batch = SpriteBatch()
        hidden = batch.add_sprite(self.frame, visible=False, clip=UnusedClip(0, 0, 20, 20))
        self.assertEqual(self.collect(batch)[2], 0)
        batch.add_sprite(self.frame, parent=hidden)
        batch.add_sprite(self.frame)
        self.assertEqual(self.collect(batch)[2], 1)

    def test_bulk_update_copies_inputs_and_invalid_last_pose_is_atomic(self):
        batch = SpriteBatch()
        items = [batch.add_sprite(self.frame) for _ in range(3)]
        indices = tuple(item.index for item in items)
        poses = [[1, 0, .2, 1, index * 9, 4] for index in indices]
        batch.set_transforms(indices, poses)
        expected = [item.affine_transform for item in items]
        poses[0][4] = 99
        self.assertEqual(items[0].affine_transform, expected[0])
        for invalid in ((1,) * 5, (1, 0, 0, 1, math.nan, 2), (1, 0, 0, 1, object(), 2)):
            with self.subTest(invalid=invalid):
                with self.assertRaises((ValueError, TypeError)):
                    batch.set_transforms(indices, [poses[0], poses[1], invalid])
                self.assertEqual([item.affine_transform for item in items], expected)
        for ids in ((0, 1, 99), (0, 1, -1), (0, 1)):
            with self.assertRaises((ValueError, IndexError)):
                batch.set_transforms(ids, poses)
            self.assertEqual([item.affine_transform for item in items], expected)
        batch.set_transforms((0, 0), (poses[0], poses[1]))
        self.assertEqual(items[0].affine_transform, tuple(poses[1]))

    def test_bulk_update_invalidates_static_frame_and_layer_signature(self):
        root, batch = Group(), SpriteBatch()
        root.add(batch)
        item = batch.add_sprite(self.frame)
        first = self.collect(root)
        unchanged = self.collect(root, first[-1])
        self.assertIsNone(unchanged[0])
        before = batch._snap()
        batch.set_transforms((item.index,), ((1, 0, 0, 1, 11, 3),))
        after = self.collect(root, first[-1])
        self.assertIsNotNone(after[0])
        self.assertNotEqual(after[0], first[0])
        self.assertNotEqual(batch._snap(), before)

    def test_clip_changes_and_hidden_items_cannot_reuse_stale_commands(self):
        root, reference = SpriteBatch(clip=(-5, -5, 40, 40)), Group(clip=(-5, -5, 40, 40))
        item = root.add_sprite(self.frame)
        clipper = Group()
        reference.add(clipper)
        sprite = Sprite(self.frame)
        clipper.add(sprite)
        for clip in (None, ClipRect(2, 3, 8, 12), ClipRect(-4, -2, 10, 4), None):
            item.clip = clipper.clip = clip
            self.equivalent(root, reference)
        for visible in (False, True, False):
            item.visible = sprite.visible = visible
            self.equivalent(root, reference)

    def test_clear_detaches_handles_and_releases_batch_texture_references(self):
        references = sys.getrefcount(self.texture)
        batch = SpriteBatch()
        item = batch.add_sprite(self.frame)
        self.assertGreater(sys.getrefcount(self.texture), references)
        batch.clear()
        self.assertEqual(self.collect(batch)[2], 0)
        item.visible = False
        replacement = batch.add_sprite(self.frame)
        self.assertTrue(replacement.visible)
        batch.close()
        del item, replacement
        self.assertEqual(sys.getrefcount(self.texture), references)
        self.assertEqual(self.texture._handle, 0)

    def test_failed_append_and_cross_batch_parent_leave_earlier_items_intact(self):
        batch = SpriteBatch()
        original = batch.add_sprite(self.frame)
        for options in (dict(tint=(1, 2)), dict(blend="unsupported"),
                        dict(affine_transform=(1, 0, 0, 1, math.inf, 0)),
                        dict(parent=SpriteBatch().add_sprite(self.frame))):
            with self.subTest(options=options):
                with self.assertRaises((TypeError, ValueError)):
                    batch.add_sprite(self.frame, **options)
                self.assertEqual(batch.sprites, (original,))

    def test_python_fallback_preserves_per_item_clipping(self):
        batch = SpriteBatch(x=12, rotation=.2, clip=(0, 0, 90, 70))
        item = batch.add_sprite(self.frame, clip=ClipRect(2, 3, 8, 12))
        batch.add_sprite(self.frame, parent=item, z=1)
        cmds = []
        batch._collect(cmds, self.renderer, _IDENTITY, 1., [0])
        native = self.collect(batch)
        self.assertEqual(b"".join(cmd._vb for cmd in cmds), native[0])
        self.assertEqual(cmds[0]._clips, cmds[1]._clips)
        self.assertEqual(cmds[0]._clips, native[3][0][1])

    def test_batch_subclasses_keep_instance_clips_and_node_callbacks(self):
        class AnimatedBatch(SpriteBatch):
            def _tick_self(self, dt):
                self.elapsed = getattr(self, "elapsed", 0) + dt
                super()._tick_self(dt)

        batch, reference = AnimatedBatch(), Group()
        batch.add_sprite(self.frame, clip=ClipRect(2, 3, 8, 12))
        clipper = Group(clip=ClipRect(2, 3, 8, 12))
        clipper.add(Sprite(self.frame))
        reference.add(clipper)
        batch._tick(.25)
        self.assertEqual(batch.elapsed, .25)
        self.equivalent(batch, reference)


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1", "Requires a desktop Metal window.")
class SpriteBatchCaptureTests(unittest.TestCase):
    def test_batch_matches_sprites_through_layer_and_offscreen_capture(self):
        window = gpu.Window("Sprite batch validation")
        self.addCleanup(window.close)
        root = Scene()
        root._init(window, "#101820")
        self.addCleanup(root._close)
        texture = gpu.Texture.render_target(16, 12)
        self.addCleanup(texture.close)
        with window.frame(clear_color=(.8, .3, .1, .7), target_texture=texture):
            pass
        layer = Layer(x=60, y=50, scale=.8)
        root.add(layer)
        group = Group()
        layer.add(group)
        options = dict(affine_transform=(-1, .3, .4, 1, 20, 10), opacity=.7,
                       tint=(.6, .8, 1, .5), blend="additive")
        group.add(Sprite(texture, **options))
        expected = root.capture(rect=(0, 0, 160, 120), size=(160, 120)).rgba
        layer.remove(group)
        batch = SpriteBatch()
        layer.add(batch)
        item = batch.add_sprite(texture, **options)
        self.assertEqual(root.capture(rect=(0, 0, 160, 120), size=(160, 120)).rgba, expected)
        batch.set_transforms((item.index,), ((1, 0, 0, 1, 45, 10),))
        layer.invalidate()
        changed = root.capture(rect=(0, 0, 160, 120), size=(160, 120)).rgba
        self.assertNotEqual(changed, expected)


if __name__ == "__main__":
    unittest.main()
