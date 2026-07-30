from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from shiboken6 import delete

from dualsphysics_studio.pipeline.compare import compare_sessions
from dualsphysics_studio.pipeline.session import RunSession
from dualsphysics_studio.ui.run_comparison import RunComparisonDialog


def _session(
    root: Path,
    name: str,
    *,
    source_sha: str = "same-source",
    backend: str = "gpu",
    runtime: float = 100.0,
) -> RunSession:
    return RunSession(
        record_path=root / name / ".dss-run" / "run.json",
        session_id=f"20260730T000000Z-{name}",
        source_name="Case_Def.xml",
        source_sha256=source_sha,
        backend=backend,
        workflow=("gencase", backend, "partvtk"),
        status="succeeded",
        created_at=f"2026-07-30T00:00:0{len(name)}+00:00",
        actual={
            "particle_counts": {"fixed": 20, "fluid": 80},
            "total_runtime_seconds": runtime,
            "allocated_gpu_memory_bytes": 1024,
            "reported_part_files": 10,
        },
    )


def test_comparison_filters_by_source_and_backend_and_formats_delta(
    tmp_path: Path,
) -> None:
    baseline = _session(tmp_path, "base", runtime=100)
    faster = _session(tmp_path, "faster", runtime=80)
    changed_xml = _session(
        tmp_path,
        "changed",
        source_sha="different-source",
        runtime=70,
    )
    cpu = _session(tmp_path, "cpu", backend="cpu", runtime=60)

    comparison = compare_sessions(
        baseline,
        (baseline, faster, changed_xml, cpu),
    )

    assert len(comparison.columns) == 2
    assert comparison.excluded_count == 2
    runtime_row = next(
        row for row in comparison.rows if row.key == "total_runtime_seconds"
    )
    assert runtime_row.values == (100, 80)
    assert comparison.formatted_value(runtime_row, 1) == "1.33 min（-20.0%）"
    assert "粒子总数" in comparison.to_csv()
    assert "可直接比较" in comparison.to_csv()


def test_comparison_can_show_incompatible_columns_with_reasons(tmp_path: Path) -> None:
    baseline = _session(tmp_path, "base")
    incompatible = _session(
        tmp_path,
        "other",
        source_sha="other-source",
        backend="cpu",
    )

    comparison = compare_sessions(
        baseline,
        (baseline, incompatible),
        compatible_only=False,
    )

    assert len(comparison.columns) == 2
    assert not comparison.columns[1].compatible
    assert comparison.columns[1].compatibility_note == "XML SHA 不同；求解后端不同"


def test_comparison_does_not_double_count_boundary_particles(tmp_path: Path) -> None:
    baseline = _session(tmp_path, "base")
    baseline.actual["particle_counts"] = {
        "boundary": 20,
        "fixed": 15,
        "moving": 3,
        "floating": 2,
        "fluid": 80,
    }

    comparison = compare_sessions(baseline, (baseline,))

    total = next(row for row in comparison.rows if row.key == "particle_total")
    assert total.values == (100,)


@pytest.mark.parametrize(
    ("source_sha", "backend", "reason"),
    (
        ("", "gpu", "XML SHA 缺失"),
        ("same-source", "", "求解后端缺失"),
    ),
)
def test_comparison_rejects_records_with_missing_identity(
    tmp_path: Path,
    source_sha: str,
    backend: str,
    reason: str,
) -> None:
    baseline = _session(tmp_path, "base")
    incomplete = _session(
        tmp_path,
        "incomplete",
        source_sha=source_sha,
        backend=backend,
    )

    comparison = compare_sessions(
        baseline,
        (baseline, incomplete),
        compatible_only=False,
    )

    assert not comparison.columns[1].compatible
    assert comparison.columns[1].compatibility_note == reason


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_dialog_defaults_to_compatible_history_and_can_reveal_all(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    baseline = _session(tmp_path, "base")
    compatible = _session(tmp_path, "compatible", runtime=90)
    incompatible = _session(tmp_path, "incompatible", backend="cpu")
    dialog = RunComparisonDialog(
        (baseline, compatible, incompatible),
        selected_record=str(baseline.record_path),
    )

    assert dialog.compatible_only.isChecked()
    assert dialog.table.columnCount() == 3
    assert "已排除 1 条" in dialog.notice.text()

    dialog.compatible_only.setChecked(False)

    assert dialog.table.columnCount() == 4
    assert dialog.comparison is not None
    assert not dialog.comparison.columns[-1].compatible

    delete(dialog)
    qapp.processEvents()
