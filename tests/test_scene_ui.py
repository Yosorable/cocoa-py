"""Scene UI geometry, event contracts, and real Metal integration."""
import math
import os
from types import SimpleNamespace
import unittest

from _cocoa import _scene_accel
from scene import ClipRect, Group, Layer, NineSlice, ParticleEmitter, Path, Rect, Scene, ScreenLayer, ShaderNode, Sprite, Toggle, Touch, TouchPhase, gesture, gpu
from scene._common import _IDENTITY, _apply, _matrix


def pixel(image, x, y):
    offset = (y * image.width + x) * 4
    return tuple(image.rgba[offset:offset + 4])


class ClipGeometryTests(unittest.TestCase):
    def collect(self, root, previous=0):
        return _scene_accel.collect(root, _IDENTITY, 1., 1.,
                                    SimpleNamespace(screen_scale=1.), previous)

    def test_nested_transformed_clips_gate_native_and_python_hit_tests(self):
        root = Group()
        outer = Group(clip=ClipRect(-20, -15, 40, 30, 6), x=75, y=60, rotation=.4, scale=(1.5, .8))
        inner = Group(clip=(-5, -20, 15, 40))
        shape = Rect(200, 200)
        root.add(outer.add(inner.add(shape)))
        self.collect(root)
        for local, expected in (((0, 0), True), ((-10, 0), False), ((15, 0), False), ((0, 18), False)):
            point = _apply(outer._world_transform, local)
            with self.subTest(local=local):
                self.assertEqual(shape.contains_point(*point), expected)
                self.assertEqual(_scene_accel.contains_point(shape, *point), expected)
                self.assertIs(_scene_accel.hit_test([shape], *point), shape if expected else None)
                self.assertEqual(_scene_accel.hit_test_all([shape], *point), [shape] if expected else [])

    def test_clip_changes_invalidate_static_render_and_cached_context(self):
        root = Group()
        parent = Group(clip=(0, 0, 20, 20))
        shape = Rect(200, 200)
        root.add(parent.add(shape))
        first = self.collect(root)
        self.assertEqual(self.collect(root, first[-1])[0], None)
        parent.clip = (30, 30, 20, 20)
        changed = self.collect(root, first[-1])
        self.assertIsNotNone(changed[0])
        self.assertFalse(shape.contains_point(10, 10))
        self.assertTrue(shape.contains_point(40, 40))
        parent.clip = None
        self.collect(root)
        self.assertEqual(shape._world_clips, ())

    def test_degenerate_clips_are_empty_and_invalid_dimensions_rejected(self):
        root = Group()
        shape = Rect(200, 200, clip=(0, 0, 0, 20))
        root.add(shape)
        self.collect(root)
        self.assertFalse(shape.contains_point(0, 10))
        shape.clip = (0, 0, 20, 20)
        shape.scale = 0
        self.collect(root)
        self.assertFalse(shape.contains_point(0, 0))
        for args in ((0, 0, -1, 10), (0, 0, 10, 10, -1), (math.nan, 0, 1, 1), (0, 0, math.inf, 1)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                ClipRect(*args)

    def test_screen_layers_require_a_scene_parent_and_integer_plane_order(self):
        layer = ScreenLayer()
        with self.assertRaises(ValueError):
            Group().add(layer)
        with self.assertRaises(TypeError):
            ScreenLayer(order=1.5)
        scene = Scene()
        self.addCleanup(scene.close)
        scene.add(layer)
        self.assertIs(scene.ui, scene.ui)
        self.assertIs(scene.ui.parent, scene)


class PointerCaptureTests(unittest.TestCase):
    def setUp(self):
        self.scene = Scene()
        self.scene._init(SimpleNamespace(size=(400, 300), scale=1.), "#000000", SimpleNamespace())
        self.addCleanup(self.scene._close)
        self.node = Rect(100, 100, x=50, y=50)
        self.scene.add(self.node)
        frame = _scene_accel.collect(self.scene, _IDENTITY, 1., 1., SimpleNamespace())
        self.scene._interactive_nodes = frame[4]
        self.events = []
        for name in ("began", "moved", "ended", "cancelled"):
            setattr(self.node, "on_touch_" + name, lambda touch, name=name: self.events.append(name))

    def touch(self, phase, x=50, y=50, identifier=1):
        self.scene._pointer_router.feed(Touch(identifier, (x, y), (50, 50), TouchPhase(phase), 10.))

    def test_removing_captured_node_cancels_once_and_suppresses_late_events(self):
        self.touch("began")
        self.touch("moved", 55, 55)
        self.scene.remove(self.node)
        self.touch("moved", 60, 60)
        self.touch("ended", 60, 60)
        self.assertEqual(self.events, ["began", "moved", "cancelled"])
        self.assertFalse(self.scene._touch_owners)
        self.assertFalse(self.scene._pointer_router.active)

    def test_callback_can_remove_its_node_during_touch_began(self):
        def began(touch):
            self.events.append("began")
            self.scene.remove(self.node)
        self.node.on_touch_began = began
        self.touch("began")
        self.touch("ended")
        self.assertEqual(self.events, ["began", "cancelled"])
        self.assertFalse(self.scene._touch_owners)

    def test_cancellation_callback_can_change_scene_content(self):
        def cancelled(touch):
            self.events.append("cancelled")
            self.scene.clear()
        self.node.on_touch_cancelled = cancelled
        self.touch("began")
        self.scene._pointer_router.cancel_all()
        self.assertEqual(self.events, ["began", "cancelled"])
        self.assertFalse(self.scene._pointer_router.active)

    def test_scene_touches_receive_explicit_cancel_with_legacy_fallback(self):
        ended = []
        self.scene.touch_ended = ended.append
        self.touch("began", 300, 200)
        self.scene._pointer_router.cancel_all()
        self.assertEqual(len(ended), 1)
        self.assertEqual(ended[0].phase, TouchPhase.CANCELLED)
        self.assertEqual(ended[0].position, (300, 200))

    def test_tap_gesture_is_cancelled_on_removal_and_can_be_reused(self):
        taps = []
        tap = gesture.Tap(lambda node, touch: taps.append(touch.id))
        self.node.gestures = [tap]
        self.touch("began")
        self.scene.remove(self.node)
        self.assertFalse(tap.is_active)
        self.touch("ended")
        self.assertEqual(taps, [])
        self.scene.add(self.node)
        self.touch("began", identifier=2)
        self.touch("ended", identifier=2)
        self.assertEqual(taps, [2])

    def test_declining_gesture_cannot_dispatch_to_a_detached_node_handler(self):
        scene, node = self.scene, self.node
        class RemovingGesture(gesture.Gesture):
            def touch_began(self, touch):
                scene.remove(node)
                return False
        node.gestures = [RemovingGesture()]
        self.touch("began")
        self.assertNotIn("began", self.events)
        self.assertFalse(scene._touch_owners)

    def test_shutdown_cancels_before_the_stop_callback(self):
        self.scene.stop = lambda: self.events.append("stop")
        self.touch("began")
        self.scene._close()
        self.assertEqual(self.events, ["began", "cancelled", "stop"])


@unittest.skipUnless(os.environ.get("COCOA_PY_UI_TESTS") == "1",
                     "Set COCOA_PY_UI_TESTS=1 for desktop Scene UI validation.")
class ClipRenderingTests(unittest.TestCase):
    def setUp(self):
        self.window = gpu.Window("cocoa-py Scene UI validation")
        self.addCleanup(self.window.close)
        self.scene = Scene()
        self.scene._init(self.window, "#000000")
        self.addCleanup(self.scene._close)

    def capture(self, **kw):
        return self.scene.capture(rect=(0, 0, 100, 100), size=(100, 100), **kw)

    def white_texture(self):
        texture = gpu.Texture.render_target(16, 16)
        self.addCleanup(texture.close)
        with self.window.frame(clear_color=(1, 1, 1, 1), target_texture=texture):
            pass
        return texture

    def test_sprite_nine_slice_and_shader_share_transformed_clip_in_direct_and_cached_rendering(self):
        texture = self.white_texture()
        source = """
        #include <metal_stdlib>
        using namespace metal;
        vertex float4 clip_vertex(uint id [[vertex_id]]) {
            constexpr float2 p[] = {{-1,-1}, {3,-1}, {-1,3}};
            return float4(p[id], 0, 1);
        }
        fragment float4 clip_fragment() { return float4(1); }
        """
        factories = (
            lambda: Sprite(texture, size=(80, 80)),
            lambda: NineSlice(texture, insets=(4, 4, 4, 4), size=(80, 80)),
            lambda: ShaderNode(source=source, size=(80, 80), vertex="clip_vertex", fragment="clip_fragment", draw_count=3, dynamic=False),
        )
        transform = _matrix((50, 50), .3, (1.1, .8))
        for make in factories:
            for cached in (False, True):
                with self.subTest(kind=make, cached=cached):
                    group = Group(x=50, y=50, rotation=.3, scale=(1.1, .8), clip=ClipRect(-20, -20, 40, 40, 10))
                    node = make()
                    group.add(node)
                    root = Layer() if cached else Group()
                    root.add(group)
                    self.scene.add(root)
                    self.scene._render()
                    image = self.scene.capture(rect=(0, 0, 100, 100), size=(200, 100))
                    for local, white in (((0, 0), True), ((-15, 0), True), ((-19, -19), False), ((27, 0), False)):
                        x, y = _apply(transform, local)
                        actual = pixel(image, round(x * 2), round(y))
                        expected = (255, 255, 255, 255) if white else (0, 0, 0, 255)
                        self.assertEqual(actual, expected, local)
                    self.scene.remove(root)
                    root.close()

    def test_circle_and_textured_particles_obey_rounded_clip_during_scaled_capture(self):
        texture = self.white_texture()
        for source in (None, texture):
            with self.subTest(textured=source is not None):
                group = Group(x=50, y=50, clip=ClipRect(-20, -20, 40, 40, 12))
                particles = ParticleEmitter(rate=0, max_particles=1, speed=0, gravity=(0, 0),
                    lifetime=10, size=80, size_over_life=(1, 1), opacity_over_life=(1, 1), texture=source)
                group.add(particles)
                self.scene.add(group)
                self.scene._render()
                particles.emit(1)
                self.scene._frame(.1)
                image = self.scene.capture(rect=(0, 0, 100, 100), size=(200, 100))
                self.assertEqual(pixel(image, 100, 50), (255, 255, 255, 255))
                self.assertEqual(pixel(image, 68, 50), (255, 255, 255, 255))
                self.assertEqual(pixel(image, 62, 31), (0, 0, 0, 255))
                self.assertEqual(pixel(image, 150, 50), (0, 0, 0, 255))
                self.scene.remove(group)
                group.close()

    def test_clip_survives_global_z_sort_and_does_not_leak_to_siblings(self):
        clipped = Group(clip=(10, 10, 30, 30))
        clipped.add(Rect(200, 200, x=50, y=50, fill="#ff0000", z=2))
        self.scene.add(clipped, Rect(200, 200, fill="#0000ff", z=1))
        image = self.capture()
        self.assertEqual(pixel(image, 20, 20), (255, 0, 0, 255))
        self.assertEqual(pixel(image, 70, 20), (0, 0, 255, 255))
        clipped.children[0].z = 0
        image = self.capture()
        self.assertEqual(pixel(image, 20, 20), (0, 0, 255, 255))

    def test_rounded_clip_capture_scaling_and_hit_state_restoration(self):
        shape = Rect(200, 200, x=50, y=50, fill="#ff0000", clip=ClipRect(-30, -30, 60, 60, 30))
        self.scene.add(shape)
        self.scene._render()
        saved = shape._world_clips
        image = self.scene.capture(rect=(20, 20, 60, 60), size=(120, 60))
        self.assertEqual(pixel(image, 60, 30), (255, 0, 0, 255))
        self.assertEqual(pixel(image, 2, 2), (0, 0, 0, 255))
        self.assertEqual(shape._world_clips, saved)
        self.assertFalse(shape.contains_point(21, 21))

    def test_clip_inside_layer_and_clip_on_layer_use_correct_coordinate_spaces(self):
        layer = Layer(x=50, y=50, clip=ClipRect(-15, -15, 30, 30))
        inner = Group(clip=(-10, -10, 20, 20))
        inner.add(Rect(100, 100, fill="#ff0000"))
        layer.add(inner)
        self.scene.add(layer)
        image = self.capture()
        self.assertEqual(pixel(image, 50, 50), (255, 0, 0, 255))
        self.assertEqual(pixel(image, 62, 50), (0, 0, 0, 255))
        self.assertEqual(pixel(image, 80, 50), (0, 0, 0, 255))

    def test_path_stencil_holes_remain_correct_under_clipping(self):
        path = Path(fill="#ff000080", fill_rule="even_odd", clip=(20, 10, 60, 80))
        for x, y, w in ((10, 10, 80), (35, 35, 30)):
            path.move_to(x, y).line_to(x + w, y).line_to(x + w, y + w).line_to(x, y + w).close_path()
        self.scene.add(path)
        image = self.capture()
        self.assertEqual(pixel(image, 15, 20), (0, 0, 0, 255))
        self.assertEqual(pixel(image, 50, 50), (0, 0, 0, 255))
        self.assertAlmostEqual(pixel(image, 25, 25)[0], 128, delta=1)

    def test_screen_plane_overlays_world_independently_of_camera_and_z(self):
        self.scene.add(Rect(2000, 2000, x=50, y=50, z=100000, fill="#0000ff"))
        button = Rect(20, 20, x=40, y=30, z=-1000, fill="#ff0000")
        self.scene.ui.add(button)
        self.scene.position = (25, 20)
        self.scene.scale = 1.3
        self.scene.camera.zoom = 1.8
        self.scene.camera.rotation = .3
        self.scene._render()
        self.assertEqual(button.world_position, (40., 30.))
        self.assertIs(self.scene.hit_test(40, 30), button)
        image = self.capture()
        self.assertEqual(pixel(image, 40, 30), (255, 0, 0, 255))
        crop = self.scene.capture(rect=(30, 20, 20, 20), size=(40, 40))
        self.assertEqual(pixel(crop, 20, 20), (255, 0, 0, 255))
        self.scene.ui.order = -1
        self.scene._render()
        image = self.capture()
        self.assertEqual(pixel(image, 40, 30), (0, 0, 255, 255))
        self.assertIsNot(self.scene.hit_test(40, 30), button)

    def test_world_clip_does_not_clip_screen_ui(self):
        self.scene.clip = (0, 0, 1, 1)
        self.scene.ui.add(Rect(20, 20, x=50, y=50, fill="#ff0000"))
        image = self.capture()
        self.assertEqual(pixel(image, 50, 50), (255, 0, 0, 255))

    def test_rotated_and_reflected_clip_renders_in_local_coordinates(self):
        parent = Group(x=50, y=50, rotation=math.pi / 4, clip=(-20, -20, 40, 40))
        parent.add(Rect(300, 300, fill="#ff0000"))
        self.scene.add(parent)
        image = self.capture()
        self.assertEqual(pixel(image, 50, 25), (255, 0, 0, 255))
        self.assertEqual(pixel(image, 28, 28), (0, 0, 0, 255))
        parent.rotation = 0
        parent.scale = (-1, 1)
        parent.clip = (0, -10, 20, 20)
        image = self.capture()
        self.assertEqual(pixel(image, 40, 50), (255, 0, 0, 255))
        self.assertEqual(pixel(image, 60, 50), (0, 0, 0, 255))

    def test_clip_change_invalidates_ancestor_layer_and_bounds_limit_allocation(self):
        parent = Layer(x=50, y=50)
        child = Group(clip=(-10, -10, 20, 20))
        child.add(Rect(50000, 50000, fill="#ff0000"))
        self.scene.add(parent.add(child))
        self.scene._render()
        self.assertLess(parent._tex.size[0], 200)
        self.assertFalse(parent._dirty)
        child.clip = (-20, -20, 40, 40)
        self.assertTrue(parent._dirty)
        self.scene._render()
        self.assertFalse(parent._dirty)
        image = self.capture()
        self.assertEqual(pixel(image, 65, 50), (255, 0, 0, 255))

    def test_layer_own_clip_bounds_allocation_and_empty_clip_releases_cache(self):
        layer = Layer(x=50, y=50, clip=ClipRect(-10, -10, 20, 20, 4))
        child = Rect(50000, 50000, x=24990, y=24990, fill="#ff0000")
        self.scene.add(layer.add(child))
        self.scene._render()
        self.assertLess(layer._tex.size[0], 200)
        self.assertLess(layer._tex.size[1], 200)
        image = self.capture()
        self.assertEqual(pixel(image, 50, 50), (255, 0, 0, 255))
        self.assertEqual(pixel(image, 65, 50), (0, 0, 0, 255))
        self.assertIs(self.scene.hit_test(50, 50), child)
        layer.clip = (-10, -10, 0, 20)
        self.scene._render()
        self.assertIsNone(layer._tex)
        self.assertEqual(layer._hit_snapshot, [])
        layer.clip = (-60000, -60000, 20, 20)
        self.scene._render()
        self.assertIsNone(layer._tex)
        layer.clip = (-10, -10, 20, 20)
        self.scene._render()
        self.assertLess(layer._tex.size[0], 200)

    def test_cropping_layer_preserves_content_placement_under_nonuniform_transforms(self):
        layer = Layer(x=20, y=50, scale=(2, 1))
        # The off-center crop must not change the anchor of the existing quad.
        layer.add(Rect(200, 100, fill="#0000ff"), Rect(6, 10, x=40, fill="#ff0000", z=1))
        self.scene.add(layer)
        before = self.capture()
        self.assertEqual(pixel(before, 80, 50), (255, 0, 0, 255))
        layer.clip = (20, -15, 30, 30)
        self.scene._render()
        snapshot = (layer._tex, layer._cache_anchor, layer._clip_basis)
        after = self.capture()
        self.assertEqual(pixel(after, 80, 50), (255, 0, 0, 255))
        self.assertEqual((layer._tex, layer._cache_anchor, layer._clip_basis), snapshot)
        layer.position = (10, 50)
        self.scene._render()
        self.assertIs(layer._tex, snapshot[0])
        self.assertEqual(pixel(self.capture(), 70, 50), (255, 0, 0, 255))
        # Same average scale, different shear/anisotropy: the crop must rebuild.
        layer.scale = (1, 2)
        self.scene._render()
        self.assertNotEqual(layer._clip_basis, snapshot[2])
        self.assertEqual(pixel(self.capture(), 50, 50), (0, 0, 255, 255))

    def test_cropped_layer_control_uses_the_same_geometry_for_drawing_and_activation(self):
        layer = Layer(x=20, y=50, scale=(2, 1), clip=(20, -15, 30, 30))
        toggle = Toggle(width=10, height=10, x=40, z=1)
        layer.add(Rect(200, 100), toggle)
        self.scene.add(layer)
        self.scene._render()
        point = toggle.world_position
        self.assertEqual(point, (80., 50.))
        current = toggle._current_geometry()[0]
        self.assertAlmostEqual(current[4], point[0])
        self.assertAlmostEqual(current[5], point[1])
        self.assertIs(self.scene.hit_test(*point), toggle)
        for phase in (TouchPhase.BEGAN, TouchPhase.ENDED):
            self.scene._pointer_router.feed(Touch(1, point, point, phase, 1.))
            self.scene._dispatch_ui_events()
        self.assertTrue(toggle.value)


if __name__ == "__main__":
    unittest.main()
