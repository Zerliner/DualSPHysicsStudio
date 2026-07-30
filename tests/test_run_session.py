from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from dualsphysics_studio.pipeline import (
    ResourceEstimate,
    RunResult,
    RunSession,
    ToolInfo,
    estimate_case,
    find_case_sessions,
    live_stage_progress,
)


@dataclass
class _Point:
    x: float
    y: float
    z: float


@dataclass
class _Bounds:
    minimum: _Point
    maximum: _Point


@dataclass
class _Facts:
    dp: float
    definition_bounds: _Bounds


@dataclass
class _Field:
    key: str
    category: str
    value: str


@dataclass
class _Document:
    facts: _Facts
    parameter_fields: tuple[_Field, ...]


def _document() -> _Document:
    return _Document(
        _Facts(0.1, _Bounds(_Point(0, 0, 0), _Point(1, 0.5, 0.2))),
        (
            _Field("TimeMax", "execution", "2.0"),
            _Field("TimeOut", "execution", "0.5"),
        ),
    )


class ResourceEstimateTests(unittest.TestCase):
    def test_preflight_reports_domain_upper_bound_and_frames(self) -> None:
        estimate = estimate_case(_document())

        self.assertEqual(estimate.phase, "preflight")
        self.assertEqual(estimate.domain_grid_particles, 11 * 6 * 3)
        self.assertEqual(estimate.output_frames, 5)
        self.assertIsNone(estimate.actual_particles)
        self.assertEqual(estimate.confidence, "low")
        self.assertGreater(estimate.ram_high_bytes or 0, estimate.ram_low_bytes or 0)

    def test_gencase_counts_replace_the_domain_upper_bound_as_basis(self) -> None:
        estimate = estimate_case(
            _document(),
            particle_summary={
                "particle_counts": {
                    "boundary": 200,
                    "fixed": 180,
                    "moving": 10,
                    "floating": 10,
                    "fluid": 800,
                }
            },
        )

        self.assertEqual(estimate.phase, "post-gencase")
        self.assertEqual(estimate.actual_particles, 1000)
        self.assertEqual(estimate.particle_basis, 1000)
        self.assertEqual(estimate.confidence, "medium")

    def test_matching_history_provides_a_bounded_runtime(self) -> None:
        estimate = estimate_case(
            _document(),
            source_sha256="abc",
            backend="gpu",
            history=(
                {
                    "source": {"sha256": "abc"},
                    "backend": "gpu",
                    "actual": {"total_runtime_seconds": 100.0},
                },
                {
                    "source": {"sha256": "other"},
                    "backend": "gpu",
                    "actual": {"total_runtime_seconds": 1.0},
                },
            ),
        )

        self.assertEqual(estimate.runtime_low_seconds, 80.0)
        self.assertEqual(estimate.runtime_high_seconds, 125.0)

    def test_live_progress_is_best_effort_and_bounded(self) -> None:
        self.assertEqual(
            live_stage_progress("gpu", "Part_0003 stored", expected_frames=10),
            (4, 10),
        )
        self.assertEqual(
            live_stage_progress("partvtk", "saved PartAll_9999.vtk", expected_frames=10),
            (10, 10),
        )
        self.assertIsNone(live_stage_progress("gencase", "working", expected_frames=10))


class RunSessionTests(unittest.TestCase):
    def test_session_is_case_local_relative_and_atomically_updated(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Case_Def.xml"
            source.write_text("<case/>", encoding="utf-8")
            executable = root / "GenCase_win64.exe"
            executable.write_bytes(b"tool")
            output = root / "Case_out"
            session = RunSession.create(
                source_xml=source,
                output_root=output,
                backend="gpu",
                workflow=("gencase", "gpu", "partvtk"),
                tools={
                    "gencase": ToolInfo(
                        name="GenCase",
                        executable=executable,
                        version="5.4",
                        available=True,
                        compatible=True,
                    )
                },
            )

            self.assertEqual(session.record_path, output / ".dss-run" / "run.json")
            self.assertEqual(session.source_snapshot.read_text(encoding="utf-8"), "<case/>")
            payload = json.loads(session.record_path.read_text(encoding="utf-8"))
            self.assertNotIn(str(root), json.dumps(payload))
            self.assertEqual(
                payload["tools"]["gencase"]["executable_sha256"],
                "7c9bbe5ec9b3fb774e8fa0f54247e93c34ddf8e5d16fe3073420de0ae81a262d",
            )

            session.add_estimate(ResourceEstimate("preflight", "low"))
            session.start_stage("gencase")
            manifest_path = output / "Case.manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")
            result = RunResult(
                status="succeeded",
                return_code=0,
                command=("GenCase",),
                working_directory=root,
                output_prefix=output / "Case",
                started_at="2026-07-29T00:00:00+00:00",
                finished_at="2026-07-29T00:00:01+00:00",
                summary={"particle_counts": {"boundary": 10, "fluid": 90}},
                manifest_path=manifest_path,
            )
            session.finish_stage("gencase", result)
            session.skip_remaining(after="gencase", reason="test")
            session.finalize("succeeded")

            loaded = RunSession.load(session.record_path)
            self.assertEqual(loaded.status, "succeeded")
            self.assertEqual(
                loaded.stages["gencase"]["manifest"], "Case.manifest.json"
            )
            self.assertEqual(loaded.stages["gpu"]["status"], "skipped")
            self.assertEqual(loaded.actual["particle_counts"]["fluid"], 90)

    def test_diagnostic_export_includes_xml_by_default_and_can_exclude_it(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Case_Def.xml"
            source.write_text("<case private='yes'/>", encoding="utf-8")
            session = RunSession.create(
                source_xml=source,
                output_root=root / "Case_out",
                backend="cpu",
                workflow=("gencase",),
            )
            session.append_log("gencase", "stdout", f"source={source}")

            included = session.export_diagnostic(
                root / "included.zip",
                redaction_paths=(root,),
            )
            with ZipFile(included) as archive:
                self.assertIn("source/Case_Def.xml", archive.namelist())
                log = archive.read("logs/gencase.stdout.log").decode("utf-8")
                self.assertNotIn(str(root), log)
                self.assertIn("<LOCAL_PATH_1>", log)

            excluded = session.export_diagnostic(
                root / "excluded.zip",
                include_xml=False,
                redaction_paths=(root,),
            )
            with ZipFile(excluded) as archive:
                self.assertNotIn("source/Case_Def.xml", archive.namelist())

    def test_case_history_discovers_newest_records(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Case_Def.xml"
            source.write_text("<case/>", encoding="utf-8")
            first = RunSession.create(
                source_xml=source,
                output_root=root / "Case_out",
                backend="cpu",
                workflow=("gencase",),
            )
            second = RunSession.create(
                source_xml=source,
                output_root=root / "Case_out_20260729-120000",
                backend="gpu",
                workflow=("gencase",),
            )
            first.created_at = "2026-07-28T00:00:00+00:00"
            first.write()
            second.created_at = "2026-07-29T00:00:00+00:00"
            second.write()

            sessions = find_case_sessions(source)

            self.assertEqual([item.backend for item in sessions], ["gpu", "cpu"])

    def test_running_session_is_recovered_as_interrupted(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Case_Def.xml"
            source.write_text("<case/>", encoding="utf-8")
            session = RunSession.create(
                source_xml=source,
                output_root=root / "Case_out",
                backend="gpu",
                workflow=("gencase", "gpu", "partvtk"),
            )
            session.start_stage("gencase")

            recovered = RunSession.load(session.record_path)
            self.assertTrue(recovered.mark_interrupted())
            self.assertEqual(recovered.status, "interrupted")
            self.assertEqual(recovered.stages["gencase"]["status"], "interrupted")
            self.assertFalse(recovered.mark_interrupted())


if __name__ == "__main__":
    unittest.main()
