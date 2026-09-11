# cocoa-py

Native Apple platform modules for Python, extracted from Pythona and designed
to be used independently of the app.

## Preview status

**Version 0.1.0a1 contains the standalone `coreml` extension only.** It provides
Core ML model compilation, inspection, and inference on macOS. The `audio`,
`scene`, `photos`, and other system modules are planned for subsequent previews
and are not included in this release.

This preview targets macOS 14 or later and standard CPython 3.14 with the GIL.
An Apple Silicon wheel is provided. The source distribution requires Apple's
Xcode command-line tools and the macOS SDK to build.

The source originated in an iOS app, but this preview does not yet provide an
iOS embedding package or integration instructions.

## Install

```sh
python3.14 -m pip install 'cocoa-py[coreml]==0.1.0a1'
```

The `coreml` extra installs NumPy, which the native extension needs at runtime.
The distribution name is `cocoa-py`; the public module is imported directly:

```python
import coreml
import numpy as np

compiled_path = coreml.compile("model.mlpackage")
model = coreml.load(compiled_path, compute_units="all")
print(coreml.describe(model))

# Replace the input name, shape and dtype with those required by your model.
result = coreml.predict(model, {"input": np.zeros((1, 3, 224, 224), dtype=np.float32)})
print(result)
```

Models are supplied by the caller. Importing `coreml` initializes its NumPy
interface; models are compiled or loaded only by explicit API calls.

## API

| Function | Purpose |
| --- | --- |
| `compile(model_path, output_dir=None)` | Compile a `.mlmodel` or `.mlpackage`, returning the `.mlmodelc` directory. |
| `load(path, compute_units="all")` | Load a compiled model and return a `Model` handle. |
| `describe(model)` | Inspect model inputs and outputs, including shapes and data types. |
| `predict(model, inputs)` | Run inference with a dictionary of named inputs. |
| `batch_predict(model, inputs_list)` | Run a batch of input dictionaries. |
| `metadata(model)` | Read author, version, license and description metadata. |

`compute_units` accepts `all`, `cpuAndNeuralEngine`, `cpuAndGPU`, or `cpuOnly`.
The system chooses available hardware within that selection.

Tensor inputs support NumPy `float16`, `float32`, `float64`, and `int32` arrays.
Color image inputs use `uint8` RGB or RGBA arrays; grayscale images use `uint8`
or `float16` according to the model. Color image outputs use RGBA. Tensor
outputs preserve their supported dtype and are returned as contiguous arrays.
Scalar inputs may be integers, floats, or strings where accepted by the model.

This API provides stateless inference. Stateful models, model updates, and
sequence inputs and outputs are not exposed by this preview.

## Build and test

```sh
python3.14 -m pip install build
MACOSX_DEPLOYMENT_TARGET=14.0 python3.14 -m build
python3.14 -m pip install --force-reinstall '.[coreml]'
python3.14 -m unittest discover -s tests -v
```

The source distribution includes small generated test models. Tests exercise
real Core ML compilation and inference on the Mac; no simulator is required.

## License

MIT. Copyright (c) 2026 Yosorable.
