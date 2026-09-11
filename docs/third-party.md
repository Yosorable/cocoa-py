# Third-party code

## Box2D

`native/physics/box2d` contains the Box2D C engine vendored by the original Pythona
implementation. Its version function reports 3.2.0. Existing copyright notices
and SPDX identifiers are retained; see its [MIT license](../native/physics/box2d/LICENSE)
and the [upstream project](https://github.com/erincatto/box2d).

The exact upstream commit of the inherited snapshot was not recorded. Do not
substitute current upstream sources without running physics and integration
checks. The license is included in source distributions, wheels and embedded
host distribution metadata.

## Dependencies

Apple frameworks are supplied by the operating system and SDK, not redistributed
as part of this project. NumPy is a build dependency and an optional runtime
extra required by Core ML. The project does not vendor NumPy or Rubicon-ObjC.
