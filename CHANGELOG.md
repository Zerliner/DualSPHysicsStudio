# Changelog

All notable changes to DualSPHysics Studio are documented in this file.

## [1.0.3] - 2026-07-30

### Added

- Added source-hash and solver-backend-aware run comparison with one selected
  baseline, all compatible case sessions, raw CSV export, and explicit reasons
  when incompatible records are shown.
- Added optional `dss-extension.json` schema 1 metadata for extension name,
  version, publisher, executable, license, homepage, description, and supported
  input extensions while preserving legacy EXE discovery.
- Added the independently packaged DSS Output Auditor DLC. It performs bounded,
  read-only file inventory, Part/VTK/BI4 frame-gap and zero-byte checks,
  `.dss-run` stage inspection, and atomic JSON report export.
- Added Mesh Doctor to Studio's existing STL/legacy VTK preflight. Within a
  documented budget it reports STL boundary and non-manifold edges, duplicate
  and degenerate faces, orientation, normals, connected components, area,
  bounds, watertightness, and enclosed volume.
- Added the independently packaged DSS Mesh Doctor DLC, which uses the same
  preflight core as Studio and exports atomic JSON reports.
- Added the independently packaged DSS PostLab DLC for discovering nine known
  official post-processors, previewing and running path-free argument-array
  recipes without a shell, and recording tool hashes, logs, status, and output
  inventories in a unique task directory.
- Added the independently packaged DSS Validation Lab DLC for source-hashed CSV
  alignment, linear interpolation on the reference time grid, common error and
  peak metrics, data-quality warnings, and JSON/CSV/offline-HTML report bundles.
- Added a packaged manifest example and DLC build workflow.
- Added the Python/Tcl/Tk runtime license and acknowledgements to the
  independently packaged auditor.

### Changed

- Extension menu entries now show provenance and require a trust confirmation
  before the first launch in each Studio session.
- Invalid manifests remain visible as disabled entries instead of silently
  launching an EXE from the same directory.
- The 3D-load confirmation now covers high-risk Mesh Doctor findings as well as
  resource budgets. An open STL surface alone remains informational because
  terrain and sheet boundaries can be intentionally open.

### Security

- Manifest executable and license paths are resolved strictly inside the
  manifest directory; absolute paths, traversal, missing files, unsupported
  URLs, oversized metadata, and invalid input-extension declarations are
  rejected.
- The output auditor does not follow directory links, never edits source
  results, and stops at a documented 250,000-file scan ceiling.
- Mesh Doctor topology work is capped at 256 MiB and 100,000 triangles.
- PostLab rejects absolute/traversal recipe paths, requires output placement in
  a new task directory, and invokes only fixed official executable names under
  the selected `bin/windows` directory.
- Validation Lab caps CSV size and row count, rejects duplicate transformed
  timestamps, does not extrapolate, and omits source absolute paths from its
  portable report metadata.

## [1.0.2] - 2026-07-29

### Added

- Added conservative preflight resource estimates from `dp`, definition
  bounds, `TimeMax`, and `TimeOut`, followed by GenCase particle-count
  correction and measured solver-memory parsing.
- Added a run center with session history, stage status, determinate particle
  frame progress, dynamic ETA, resource evidence, output access, and record
  export actions.
- Added an explicitly confirmed one-click workflow that always executes
  GenCase, the user-selected CPU or GPU solver, and PartVTK in order.
- Added case-local `.dss-run` records with atomic `run.json` updates, the exact
  XML snapshot, stage logs, tool and configuration hashes, relative manifest
  references, interrupted-run recovery, and checksum-bearing diagnostic ZIPs.
- Added “create a new run from record”; it repeats preflight into a new output
  directory and never replays an old command over existing outputs.

### Changed

- Manual GenCase, CPU/GPU, and PartVTK actions now join or create the same
  reproducible output session used by the one-click workflow.
- Diagnostic exports include the XML snapshot by default. Users with case
  privacy requirements can explicitly exclude it in the export dialog.

### Security

- Diagnostic exports redact local absolute paths from records, manifests, and
  logs while continuing to exclude credentials and arbitrary environment
  variables.
- Heuristic capacity estimates never silently select a solver, overwrite an
  output, or claim guaranteed capacity; hard execution failures still come
  from validated inputs, trusted tools, and observed output conflicts.

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
