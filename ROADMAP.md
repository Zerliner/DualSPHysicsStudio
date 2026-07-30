# DualSPHysics Studio Roadmap

This roadmap keeps the Windows x64, DualSPHysics v5.4, offline-first baseline
and develops the smallest verifiable end-to-end workflow before expanding
scope. Version labels are planning buckets, not delivery-date commitments.

## Release invariants

- DualSPHysics executables and cases remain user-supplied and are never bundled.
- The independent OSGB-to-STL portable suite remains a separate `DLC` package
  that Studio discovers after user installation.
- External executables run only after an explicit user action and only from a
  configured or explicitly selected trusted root.
- XML edits remain reviewable and backed up before overwrite.
- Offline editing, visualization, and simulation orchestration do not require
  AI or a network connection.
- Every release keeps a human-reviewable changelog, dependency audit, portable
  smoke test, checksums, and required corresponding-source assets.

## v1.0.x — stabilization

1. Completed in v1.0.1: add application-level byte and complexity guards around
   user-opened STL and legacy VTK files, including preflight estimates,
   confirmation budgets, hard limits, and post-load validation.
2. Completed in v1.0.1: add a case doctor with severity filtering, source
   navigation, high-confidence bounds and execution-parameter checks, and a
   bridge to the GenCase quick check.
3. Completed in v1.0.1: expose an initial searchable parameter workbench for
   existing scalar values while retaining XML as the only source of truth.
4. Completed in v1.0.2: add conservative resource estimates that distinguish
   definition-domain upper bounds, GenCase particle measurements, solver
   memory reports, history-calibrated duration, and unavailable data.
5. Completed in v1.0.2: add a run center and an explicitly confirmed complete
   GenCase -> CPU/GPU -> PartVTK workflow while preserving direct stage actions.
6. Completed in v1.0.2: add case-local atomic run records, XML snapshots,
   stage logs, tool hashes, interrupted-run recovery, and privacy-aware
   diagnostic exports. XML is included by default and can be explicitly
   excluded for private cases; local paths are always redacted from exports.
7. Collect reproducible bug reports from the public releases.
8. Exercise the portable build in CI or a controlled release workflow and add
   Authenticode signing when a publisher certificate is available.
9. Keep Python and pinned dependencies current through reviewed, audited patch
   releases.

Exit evidence: reproduced issues have regression tests; the complete Windows
suite and frozen smoke test pass; package privacy, license, and checksum audits
remain clean.

## v1.1 — XML authoring completeness

1. Expand the v1.0.1 parameter workbench help and validation coverage to
   additional frequently used DualSPHysics v5.4 XML sections.
2. Extend structure-tree navigation beyond the v1.0.1 diagnostic source
   localization without replacing the XML source as the source of truth.
3. Extend direct geometry editing only where round-trip behavior can preserve
   comments, unsupported attributes, and unrelated formatting.
4. Add small, generic fixtures for every newly supported editing behavior.

Exit evidence: every supported edit round-trips losslessly outside the intended
change and has a focused fixture-based regression test.

## v1.2 — run insight and recovery

The base simulation workflow moved forward into v1.0.2.

1. Compare estimates and actual measurements across multiple selected runs.
2. Add richer filtering and recovery guidance for interrupted or failed
   sessions while preserving the no-delete/no-overwrite rule.
3. Add explicit capability diagnostics for configured DualSPHysics
   installations and solver options.
4. Validate history calibration on documented representative CPU and GPU
   cases before narrowing any resource range.

Exit evidence: comparisons remain source-hash and backend aware, recovery always
creates a new safe run, and representative cases document estimate error bands.

## v1.3 — results visualization

1. Add time-step navigation for generated VTK results.
2. Introduce bounded particle filters, scalar coloring, clipping, and basic
   engineering measurements.
3. Keep visualization read-only with respect to solver outputs unless a future
   export action is explicitly requested and reviewed.
4. Validate performance and memory behavior on documented representative
   result sizes before increasing supported limits.

Exit evidence: representative result sets load within documented resource
budgets and visualization controls have deterministic automated or fixture
coverage.

## DLC interface maturity

This work can proceed alongside the version buckets without merging the
independent OSGB-to-STL project into Studio.

1. Document a minimal optional metadata manifest for tool name, version,
   executable, license file, and supported inputs.
2. Preserve legacy EXE discovery for the current portable suite.
3. Show trust/source information before first launch and never auto-run a tool.
4. Add compatibility tests using inert fake executables rather than shipping
   third-party binaries.

Exit evidence: the current OSGB-to-STL package remains auto-detected, legacy
packages keep working, and manifest-based tools expose clearer provenance
without expanding their execution authority.

## Recommended next development ticket

After validating the v1.0.2 portable package, add run-to-run comparison using
the case-local session records. Keep comparisons source-hash and backend aware,
and do not narrow resource ranges until representative evidence supports it.
