# Changelog

All notable changes to DualSPHysics Studio are documented in this file.

## [1.0.0] - 2026-07-28

Initial public release.

### Added

- Linked XML source, structure tree, parameter help, and 3D scene editing.
- Interactive preview and editing for `drawbox` and `drawprism` geometry.
- Read-only STL and VTK visualization.
- Fluid, fixed-boundary, and floating-body diagnostics.
- Local GenCase, CPU/GPU solver, and PartVTK workflow integration for DualSPHysics v5.4.
- Separately distributed portable-tool discovery through the `DLC` directory.
- Optional OpenAI Responses API assistance with payload preview and local credential storage.
- Windows x64 portable packaging and third-party license collection.

### Security

- Disabled DTD, external-entity, and network resolution while parsing XML.
- Added an 80 MiB pre-read limit plus XML character, element, and nesting limits.
- Restricted DualSPHysics executable discovery to explicit user configuration or `DUALSPHYSICS_ROOT`.
- Launched solvers and extension executables directly without a command shell.
- Prevented automatic deletion or overwrite of existing solver and VTK outputs.

### Notes

- DualSPHysics executables and simulation cases are not bundled.
- The independent OSGB-to-STL portable tool remains a separately distributed `DLC` package.
