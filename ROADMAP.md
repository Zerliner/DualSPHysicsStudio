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

1. Collect reproducible bug reports from the first public release.
2. Add an opt-in diagnostic export containing version, stage logs, and
   non-sensitive environment facts while excluding case content, credentials,
   and personal paths by default.
3. Add application-level byte and complexity guards around user-opened STL and
   legacy VTK files, with compatibility tests for valid engineering models.
4. Exercise the portable build in CI or a controlled release workflow and add
   Authenticode signing when a publisher certificate is available.
5. Keep Python and pinned dependencies current through reviewed, audited patch
   releases.

Exit evidence: reproduced issues have regression tests; the complete Windows
suite and frozen smoke test pass; package privacy, license, and checksum audits
remain clean.

## v1.1 — XML authoring completeness

1. Expand parameter help and validation coverage for frequently used
   DualSPHysics v5.4 XML sections.
2. Improve structure-tree navigation, search, diagnostics, and error
   localization without replacing the XML source as the source of truth.
3. Extend direct geometry editing only where round-trip behavior can preserve
   comments, unsupported attributes, and unrelated formatting.
4. Add small, generic fixtures for every newly supported editing behavior.

Exit evidence: every supported edit round-trips losslessly outside the intended
change and has a focused fixture-based regression test.

## v1.2 — simulation workflow

1. Make GenCase, CPU/GPU solver, and PartVTK stages easier to inspect through
   structured command previews, progress, duration, and result summaries.
2. Add a case-local run history that records hashes and stage outcomes without
   storing private paths in the repository or release package.
3. Improve recovery guidance for interrupted, failed, or conflicting stages
   while preserving the no-delete/no-overwrite rule.
4. Add explicit capability and version diagnostics for configured
   DualSPHysics installations.

Exit evidence: a generic sample case can complete the configured stage sequence
with reviewable manifests, deterministic conflict behavior, and tested failure
recovery.

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

## Recommended first development ticket

Implement the STL/VTK application-level resource guard as the first v1.0.x
change. It closes the only retained parser-hardening note from the v1.0.0
security review, has a narrow scope, and can be verified with small synthetic
fixtures before broader editor or simulation features begin.
