from __future__ import annotations

import json
from math import sqrt
from pathlib import Path

import pytest

from dlc_tools.validation_lab.app import main
from dlc_tools.validation_lab import validation
from dlc_tools.validation_lab.validation import (
    ValidationError,
    align_series,
    calculate_metrics,
    create_report,
    load_csv_series,
    read_csv_headers,
    write_validation_bundle,
)


def _csv(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _series_pair(tmp_path: Path):
    reference = load_csv_series(
        _csv(
            tmp_path / "reference.csv",
            "time,value\n0,0\n1,1\n2,4\n",
        ),
        role="reference",
        time_column="time",
        value_column="value",
    )
    simulation = load_csv_series(
        _csv(
            tmp_path / "simulation.csv",
            "time,value\n0,0\n2,4\n",
        ),
        role="simulation",
        time_column="time",
        value_column="value",
    )
    return reference, simulation


def test_header_detection_and_transforms_preserve_provenance(
    tmp_path: Path,
) -> None:
    source = _csv(
        tmp_path / "spaced.csv",
        " time ; value \n2;3\nbad;5\n0;1\n1;2\n",
    )

    headers, delimiter, encoding = read_csv_headers(source)
    series = load_csv_series(
        source,
        role="reference",
        time_column="time",
        value_column="value",
        time_scale=2.0,
        value_scale=10.0,
        value_offset=-1.0,
    )

    assert headers == ("time", "value")
    assert delimiter == ";"
    assert encoding == "utf-8-sig"
    assert series.times == (0.0, 2.0, 4.0)
    assert series.values == (9.0, 19.0, 29.0)
    assert series.provenance.rows_read == 4
    assert series.provenance.rows_dropped == 1
    assert not series.provenance.source_was_sorted
    assert len(series.provenance.source_sha256) == 64
    assert series.provenance.source_name == "spaced.csv"


def test_alignment_interpolates_only_on_reference_grid(
    tmp_path: Path,
) -> None:
    reference, simulation = _series_pair(tmp_path)

    aligned = align_series(reference, simulation)
    metrics = calculate_metrics(aligned)

    assert aligned.times == (0.0, 1.0, 2.0)
    assert aligned.simulation == (0.0, 2.0, 4.0)
    assert metrics.samples == 3
    assert metrics.mae == pytest.approx(1 / 3)
    assert metrics.rmse == pytest.approx(sqrt(1 / 3))
    assert metrics.bias == pytest.approx(1 / 3)
    assert metrics.max_abs_error == 1.0
    assert metrics.normalized_rmse == pytest.approx(sqrt(1 / 3) / 4)


def test_alignment_respects_requested_overlap_window(tmp_path: Path) -> None:
    reference, simulation = _series_pair(tmp_path)

    with pytest.raises(ValidationError, match="少于2个采样点"):
        align_series(reference, simulation, start_time=1.5, end_time=2.0)

    with pytest.raises(ValidationError, match="没有重叠区间"):
        align_series(reference, simulation, start_time=3.0)


def test_duplicate_transformed_times_are_rejected(tmp_path: Path) -> None:
    source = _csv(
        tmp_path / "duplicate.csv",
        "time,value\n0,1\n0,2\n1,3\n",
    )

    with pytest.raises(ValidationError, match="重复值"):
        load_csv_series(
            source,
            role="reference",
            time_column="time",
            value_column="value",
        )


def test_source_change_during_analysis_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _csv(
        tmp_path / "changing.csv",
        "time,value\n0,1\n1,2\n",
    )
    original = validation.read_csv_headers

    def mutate_after_header(path: Path):
        result = original(path)
        source.write_text(
            "time,value\n0,1\n1,2\n2,3\n",
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(validation, "read_csv_headers", mutate_after_header)

    with pytest.raises(ValidationError, match="分析期间发生变化"):
        validation.load_csv_series(
            source,
            role="reference",
            time_column="time",
            value_column="value",
        )


def test_bundle_is_unique_atomic_and_does_not_leak_source_paths(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "private source"
    source_root.mkdir()
    reference = load_csv_series(
        _csv(
            source_root / "reference.csv",
            "time,value\n0,0\n1,1\n2,4\n",
        ),
        role="reference",
        time_column="time",
        value_column="value",
    )
    simulation = load_csv_series(
        _csv(
            source_root / "simulation.csv",
            "time,value\n0,0\n2,4\n",
        ),
        role="simulation",
        time_column="time",
        value_column="value",
    )
    aligned = align_series(reference, simulation)
    report = create_report(
        "<水槽验证>",
        reference,
        simulation,
        aligned,
        unit="m",
    )
    output = tmp_path / "reports"
    output.mkdir()

    first = write_validation_bundle(report, aligned, output)
    second = write_validation_bundle(report, aligned, output)

    assert first != second
    assert sorted(path.name for path in first.iterdir()) == [
        "comparison.csv",
        "report.html",
        "validation.json",
    ]
    payload_text = (first / "validation.json").read_text(encoding="utf-8")
    payload = json.loads(payload_text)
    assert payload["reference"]["source_name"] == "reference.csv"
    assert payload["reference"]["source_sha256"]
    assert str(source_root) not in payload_text
    assert (first / "comparison.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    html = (first / "report.html").read_text(encoding="utf-8")
    assert "&lt;水槽验证&gt;" in html
    assert "<svg" in html
    assert "绝对峰值数值误差" in html
    assert not list(output.glob(".*.tmp"))


def test_report_warns_about_dropped_and_unsorted_rows(tmp_path: Path) -> None:
    reference = load_csv_series(
        _csv(
            tmp_path / "reference.csv",
            "time,value\n2,4\nbad,3\n0,0\n1,1\n",
        ),
        role="reference",
        time_column="time",
        value_column="value",
    )
    simulation = load_csv_series(
        _csv(
            tmp_path / "simulation.csv",
            "time,value\n0,0\n1,1\n2,4\n",
        ),
        role="simulation",
        time_column="time",
        value_column="value",
    )

    report = create_report(
        "quality",
        reference,
        simulation,
        align_series(reference, simulation),
    )

    assert any("丢弃" in warning for warning in report.warnings)
    assert any("排序" in warning for warning in report.warnings)


def test_validation_lab_cli_smoke_test_does_not_open_gui() -> None:
    assert main(["--smoke-test"]) == 0
