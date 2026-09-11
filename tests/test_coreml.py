"""Numerical regression tests for the installed coreml extension."""
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest
import zipfile
import coreml
import numpy as np

class CoreMLBridgeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.temporary = TemporaryDirectory(prefix='pythona_coreml_tests_')
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        archive_path = Path(__file__).with_name('CoreMLFixtures') / 'CoreMLFixtures.zip'
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(cls.root)
        cls.models = {}
        for name in ('ColorToTensor', 'TensorToColor', 'Gray8', 'Gray16'):
            source = cls.root / (name + ('.mlpackage' if name == 'Gray16' else '.mlmodel'))
            compiled = coreml.compile(model_path=source, output_dir=None)
            cls.models[name] = coreml.load(path=Path(compiled), compute_units='cpuOnly')

    def color_image(self):
        image = np.arange(45, dtype=np.uint8).reshape(3, 5, 3)
        image += np.array([20, 70, 150], dtype=np.uint8)
        return image

    def test_rgb_input_preserves_channels_and_noncontiguous_views(self):
        image = self.color_image()[:, ::-1]
        result = coreml.predict(self.models['ColorToTensor'], {'input': image})['output']
        np.testing.assert_array_equal(result, image.transpose(2, 0, 1))

    def test_rgba_input_ignores_alpha_for_model_color_channels(self):
        rgb = self.color_image()
        rgba = np.concatenate([rgb, np.full((3, 5, 1), 255, dtype=np.uint8)], axis=2)
        result = coreml.predict(self.models['ColorToTensor'], {'input': rgba})['output']
        np.testing.assert_array_equal(result, rgb.transpose(2, 0, 1))

    def test_color_output_is_rgba_and_matches_pil_channel_order(self):
        rgb = self.color_image()
        tensor = rgb.transpose(2, 0, 1).astype(np.float64)
        result = coreml.predict(self.models['TensorToColor'], {'input': tensor})['output']
        self.assertEqual(result.dtype, np.uint8)
        self.assertEqual(result.shape, (3, 5, 4))
        np.testing.assert_array_equal(result[:, :, :3], rgb)

    def test_gray8_handles_padded_rows_and_singleton_channels(self):
        gray = np.arange(15, dtype=np.uint8).reshape(3, 5)
        for image in (gray, gray[..., None], gray[:, ::-1]):
            result = coreml.predict(self.models['Gray8'], {'input': image})['output']
            self.assertEqual(result.dtype, np.uint8)
            np.testing.assert_array_equal(result, image.reshape(3, 5))

    def test_gray16_preserves_fractional_and_negative_values(self):
        gray = np.linspace(-2, 5, 15, dtype=np.float16).reshape(3, 5)
        for image in (gray, gray[..., None], gray[:, ::-1], gray.astype('>f2')):
            result = coreml.predict(self.models['Gray16'], {'input': image})['output']
            self.assertEqual(result.dtype, np.float16)
            np.testing.assert_array_equal(result, image.reshape(3, 5) + np.float16(0.25))

    def test_describe_reports_image_output_dtype_shape_and_optionality(self):
        description = coreml.describe(self.models['Gray16'])
        for field in (description['inputs'][0], description['outputs'][0]):
            self.assertEqual(field['type'], 'image')
            self.assertEqual(field['dtype'], 'float16')
            self.assertEqual(field['shape'], [3, 5])
            self.assertEqual(field['color_layout'], 'GRAYSCALE')
            self.assertFalse(field['is_optional'])
        output = coreml.describe(self.models['TensorToColor'])['outputs'][0]
        self.assertEqual(output['shape'], [3, 5, 4])
        self.assertEqual(output['color_layout'], 'RGBA')

    def test_batch_matches_single_predictions(self):
        images = [np.full((3, 5), value, dtype=np.float16) for value in (-1.5, 2.5)]
        model = self.models['Gray16']
        outputs = coreml.batch_predict(model, [{'input': image} for image in images])
        for image, result in zip(images, outputs, strict=True):
            np.testing.assert_array_equal(result['output'], coreml.predict(model, {'input': image})['output'])
        self.assertEqual(coreml.batch_predict(model, []), [])

    def test_invalid_image_inputs_fail_without_silent_casting(self):
        model = self.models['ColorToTensor']
        for image in (np.zeros((3, 5, 3), dtype=np.float32), np.zeros((3, 5, 3), dtype=np.int16)):
            with self.assertRaisesRegex(TypeError, 'uint8'):
                coreml.predict(model, {'input': image})
        for image in (np.zeros((3, 5), dtype=np.uint8), np.zeros((3, 5, 2), dtype=np.uint8), np.zeros((0, 5, 3), dtype=np.uint8), np.zeros((4, 5, 3), dtype=np.uint8)):
            with self.assertRaises(ValueError):
                coreml.predict(model, {'input': image})
        with self.assertRaisesRegex(TypeError, 'float16'):
            coreml.predict(self.models['Gray16'], {'input': np.zeros((3, 5), dtype=np.float32)})

    def test_input_names_and_path_arguments_report_python_errors(self):
        model = self.models['ColorToTensor']
        with self.assertRaises(KeyError):
            coreml.predict(model, {})
        with self.assertRaises(KeyError):
            coreml.predict(model, {'typo': self.color_image()})
        with self.assertRaises(TypeError):
            coreml.predict(model, {3: self.color_image()})
        with self.assertRaises(FileNotFoundError):
            coreml.load(self.root / 'missing.mlmodelc')
        with self.assertRaises(TypeError):
            coreml.compile(None)
        with self.assertRaises(ValueError):
            coreml.load('bad\x00path')
        with self.assertRaises(OverflowError):
            coreml.predict(model, {'input': 1 << 100})
        for tensor in (np.array(1.0), np.empty((3, 0, 5))):
            with self.assertRaises(ValueError):
                coreml.predict(self.models['TensorToColor'], {'input': tensor})
if __name__ == '__main__':
    unittest.main(verbosity=2)
