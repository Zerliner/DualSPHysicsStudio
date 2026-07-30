from __future__ import annotations

import json
from pathlib import Path

import pytest

from dlc_tools.output_auditor.app import main
from dlc_tools.output_auditor.audit import audit_directory, write_report


def test_audit_reports_categories_gaps_zero_files_and_failed_session(
    tmp_path: Path,
) -> None:
    (tmp_path / "Part_0000.bi4").write_bytes(b"0")
    (tmp_path / "Part_0001.bi4").write_bytes(b"1")
    (tmp_path / "Part_0003.bi4").write_bytes(b"3")
    (tmp_path / "vtk").mkdir()
    (tmp_path / "vtk" / "PartAll_0000.vtk").write_text(
        "# vtk DataFile Version 3.0",
        encoding="utf-8",
    )
    (tmp_path / "vtk" / "PartAll_0001.vtk").write_bytes(b"")
    run_directory = tmp_path / ".dss-run"
    run_directory.mkdir()
    (run_directory / "run.json").write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "status": "failed",
                "backend": "gpu",
                "source": {"sha256": "abc"},
                "stages": {
                    "gencase": {"status": "succeeded"},
                    "gpu": {"status": "failed"},
                    "partvtk": {"status": "skipped"},
                },
            }
        ),
        encoding="utf-8",
    )

    report = audit_directory(tmp_path)

    assert report.total_files == 6
    assert report.recognized_output_files == 6
    assert report.zero_byte_file_count == 1
    assert report.session is not None
    assert report.session["status"] == "failed"
    part_sequence = next(
        sequence
        for sequence in report.sequences
        if sequence.prefix.casefold() == "part"
    )
    assert part_sequence.frame_count == 3
    assert part_sequence.missing_count == 1
    assert part_sequence.missing_indices == (2,)
    assert any("缺少 1 个帧编号" in warning for warning in report.warnings)
    assert any("运行记录状态为 failed" in warning for warning in report.warnings)
    assert any("运行阶段 gpu 状态为 failed" in warning for warning in report.warnings)


def test_audit_is_bounded_and_json_export_is_atomic(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"Part_{index:04d}.vtk").write_text(
            str(index),
            encoding="utf-8",
        )

    report = audit_directory(tmp_path, max_files=3)

    assert report.truncated
    assert report.total_files == 3
    assert any("安全上限 3" in warning for warning in report.warnings)

    output = write_report(report, tmp_path / "audit.json")
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["truncated"]
    assert not list(tmp_path.glob(".audit.json.*.tmp"))


def test_cli_smoke_test_does_not_open_gui() -> None:
    assert main(["--smoke-test"]) == 0


def test_audit_does_not_follow_directory_links(tmp_path: Path) -> None:
    external = tmp_path.parent / f"{tmp_path.name}-external"
    external.mkdir()
    (external / "Part_9999.vtk").write_text("outside", encoding="utf-8")
    link = tmp_path / "linked-results"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory links are not available in this test environment")
    (tmp_path / "Part_0000.vtk").write_text("inside", encoding="utf-8")

    report = audit_directory(tmp_path)

    assert report.total_files == 1
