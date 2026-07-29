# Changelog

All notable changes to DualSPHysics Studio are documented in this file.

## [1.0.1] - 2026-07-29

### Added

- Added a resizable case-doctor detail window with complete diagnostic text,
  XML path, resolved source line, nearby XML context, and an explicit source
  navigation action; double-clicking the location column keeps direct
  navigation available in the compact inspector table.
- Added bounded STL and legacy VTK preflight with file, point, cell, and
  conservative memory estimates before VTK allocates a dataset.
- Added a case-doctor tab with severity summaries, filtering, source-line
  navigation, and direct access to the existing GenCase quick check.
- Added diagnostics for definition bounds, duplicate or incomplete execution
  parameters, unsupported known enum values, non-positive time/constants, and
  output intervals longer than the simulation.
- Added a searchable parameter workbench for existing `dp`, physical constants,
  gravity components, and execution parameters, using the canonical lossless
  edit and undo history.

### Security

- Large meshes now require explicit confirmation above the documented soft
  budget and are blocked above the 4 GiB or 25-million-element hard budgets.
- Loaded datasets are rejected before rendering when their counts or bounds are
  invalid, non-finite, or above the application hard budget.

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
