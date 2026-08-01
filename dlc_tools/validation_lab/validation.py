"""Dependency-free, source-traceable validation of two numeric CSV series."""

from __future__ import annotations

from bisect import bisect_left
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import html
import json
from math import isfinite, sqrt
import os
from pathlib import Path
import re
from statistics import fmean
from typing import Any, Iterable


REPORT_SCHEMA_VERSION = 1
MAX_CSV_BYTES = 256 * 1024 * 1024
MAX_CSV_ROWS = 1_000_000
SAMPLE_BYTES = 64 * 1024
MAX_SVG_POINTS = 1200


class ValidationError(ValueError):
    """Raised when source data cannot support a defensible comparison."""


@dataclass(frozen=True, slots=True)
class SeriesProvenance:
    role: str
    source_name: str
    source_sha256: str
    source_bytes: int
    encoding: str
    delimiter: str
    time_column: str
    value_column: str
    time_scale: float
    value_scale: float
    value_offset: float
    rows_read: int
    rows_used: int
    rows_dropped: int
    source_was_sorted: bool


@dataclass(frozen=True, slots=True)
class NumericSeries:
    name: str
    times: tuple[float, ...]
    values: tuple[float, ...]
    provenance: SeriesProvenance


@dataclass(frozen=True, slots=True)
class AlignedSeries:
    times: tuple[float, ...]
    reference: tuple[float, ...]
    simulation: tuple[float, ...]

    @property
    def errors(self) -> tuple[float, ...]:
        return tuple(
            simulation - reference
            for reference, simulation in zip(
                self.reference,
                self.simulation,
                strict=True,
            )
        )


@dataclass(frozen=True, slots=True)
class ValidationMetrics:
    samples: int
    start_time: float
    end_time: float
    mae: float
    rmse: float
    bias: float
    max_abs_error: float
    correlation: float | None
    reference_range: float
    normalized_rmse: float | None
    reference_min: float
    reference_max: float
    simulation_min: float
    simulation_max: float
    reference_abs_peak: float
    reference_abs_peak_time: float
    simulation_abs_peak: float
    simulation_abs_peak_time: float
    abs_peak_value_error: float
    abs_peak_time_error: float


@dataclass(frozen=True, slots=True)
class ValidationReport:
    schema_version: int
    generated_at: str
    name: str
    unit: str
    interpolation: str
    reference: SeriesProvenance
    simulation: SeriesProvenance
    metrics: ValidationMetrics
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["warnings"] = list(self.warnings)
        return value


def read_csv_headers(
    source: str | os.PathLike[str],
) -> tuple[tuple[str, ...], str, str]:
    path, size = _source_file(source)
    encoding, sample = _detect_encoding(path)
    delimiter = _detect_delimiter(sample)
    try:
        with path.open("r", encoding=encoding, newline="") as stream:
            reader = csv.reader(stream, delimiter=delimiter)
            headers = next(reader)
    except (OSError, UnicodeError, csv.Error, StopIteration) as error:
        raise ValidationError(f"无法读取 CSV 表头：{error}") from error
    normalized = tuple(header.strip() for header in headers)
    if not normalized or any(not header for header in normalized):
        raise ValidationError("CSV 表头包含空列名。")
    if len(set(normalized)) != len(normalized):
        raise ValidationError("CSV 表头包含重复列名。")
    if size <= 0:
        raise ValidationError("CSV 文件为空。")
    return normalized, delimiter, encoding


def load_csv_series(
    source: str | os.PathLike[str],
    *,
    role: str,
    time_column: str,
    value_column: str,
    time_scale: float = 1.0,
    value_scale: float = 1.0,
    value_offset: float = 0.0,
) -> NumericSeries:
    path, size = _source_file(source)
    try:
        initial_stat = path.stat()
    except OSError as error:
        raise ValidationError(f"无法读取 CSV 文件状态：{error}") from error
    for label, value in (
        ("time_scale", time_scale),
        ("value_scale", value_scale),
        ("value_offset", value_offset),
    ):
        if not isfinite(value):
            raise ValidationError(f"{label} 必须是有限数值。")
    if time_scale == 0:
        raise ValidationError("time_scale 不能为零。")
    headers, delimiter, encoding = read_csv_headers(path)
    if time_column not in headers or value_column not in headers:
        raise ValidationError("所选时间列或数值列不在 CSV 表头中。")
    if time_column == value_column:
        raise ValidationError("时间列和数值列不能相同。")

    pairs: list[tuple[float, float]] = []
    rows_read = 0
    dropped = 0
    digest = sha256()
    try:
        with path.open("rb") as binary:
            while chunk := binary.read(1024 * 1024):
                digest.update(chunk)
        with path.open("r", encoding=encoding, newline="") as stream:
            reader = csv.DictReader(stream, delimiter=delimiter)
            if reader.fieldnames is None:
                raise ValidationError("CSV 缺少表头。")
            reader.fieldnames = [field.strip() for field in reader.fieldnames]
            for row in reader:
                rows_read += 1
                if rows_read > MAX_CSV_ROWS:
                    raise ValidationError(
                        f"CSV 数据行超过 {MAX_CSV_ROWS:,} 的安全上限。"
                    )
                try:
                    raw_time = float(str(row.get(time_column, "")).strip())
                    raw_value = float(str(row.get(value_column, "")).strip())
                    time_value = raw_time * time_scale
                    value = raw_value * value_scale + value_offset
                except (TypeError, ValueError):
                    dropped += 1
                    continue
                if not isfinite(time_value) or not isfinite(value):
                    dropped += 1
                    continue
                pairs.append((time_value, value))
        final_stat = path.stat()
    except (OSError, UnicodeError, csv.Error) as error:
        raise ValidationError(f"无法读取 CSV 数据：{error}") from error
    if (
        initial_stat.st_size != size
        or final_stat.st_size != initial_stat.st_size
        or final_stat.st_mtime_ns != initial_stat.st_mtime_ns
    ):
        raise ValidationError("CSV 文件在分析期间发生变化，请稳定文件后重试。")
    if len(pairs) < 2:
        raise ValidationError("有效数值行少于2行，无法进行时序验证。")

    was_sorted = all(
        pairs[index][0] < pairs[index + 1][0]
        for index in range(len(pairs) - 1)
    )
    pairs.sort(key=lambda item: item[0])
    for left, right in zip(pairs, pairs[1:], strict=False):
        if left[0] == right[0]:
            raise ValidationError(
                f"时间列在变换后包含重复值：{left[0]:.12g}"
            )

    return NumericSeries(
        name=path.stem,
        times=tuple(item[0] for item in pairs),
        values=tuple(item[1] for item in pairs),
        provenance=SeriesProvenance(
            role=role,
            source_name=path.name,
            source_sha256=digest.hexdigest(),
            source_bytes=size,
            encoding=encoding,
            delimiter=delimiter,
            time_column=time_column,
            value_column=value_column,
            time_scale=time_scale,
            value_scale=value_scale,
            value_offset=value_offset,
            rows_read=rows_read,
            rows_used=len(pairs),
            rows_dropped=dropped,
            source_was_sorted=was_sorted,
        ),
    )


def align_series(
    reference: NumericSeries,
    simulation: NumericSeries,
    *,
    start_time: float | None = None,
    end_time: float | None = None,
) -> AlignedSeries:
    start = max(reference.times[0], simulation.times[0])
    end = min(reference.times[-1], simulation.times[-1])
    if start_time is not None:
        if not isfinite(start_time):
            raise ValidationError("起始时间必须是有限数值。")
        start = max(start, start_time)
    if end_time is not None:
        if not isfinite(end_time):
            raise ValidationError("结束时间必须是有限数值。")
        end = min(end, end_time)
    if start >= end:
        raise ValidationError("两组数据在所选时间窗口内没有重叠区间。")

    selected = [
        (time_value, value)
        for time_value, value in zip(
            reference.times,
            reference.values,
            strict=True,
        )
        if start <= time_value <= end
    ]
    if len(selected) < 2:
        raise ValidationError(
            "参考数据在重叠区间内少于2个采样点，无法计算指标。"
        )
    times = tuple(item[0] for item in selected)
    reference_values = tuple(item[1] for item in selected)
    simulation_values = tuple(
        _linear_interpolate(simulation, time_value)
        for time_value in times
    )
    return AlignedSeries(times, reference_values, simulation_values)


def calculate_metrics(aligned: AlignedSeries) -> ValidationMetrics:
    if len(aligned.times) < 2:
        raise ValidationError("至少需要2个对齐采样点。")
    errors = aligned.errors
    mae = fmean(abs(value) for value in errors)
    rmse = sqrt(fmean(value * value for value in errors))
    bias = fmean(errors)
    maximum_error = max(abs(value) for value in errors)
    reference_range = max(aligned.reference) - min(aligned.reference)
    correlation = _correlation(aligned.reference, aligned.simulation)
    reference_peak_index = max(
        range(len(aligned.reference)),
        key=lambda index: abs(aligned.reference[index]),
    )
    simulation_peak_index = max(
        range(len(aligned.simulation)),
        key=lambda index: abs(aligned.simulation[index]),
    )
    reference_peak = aligned.reference[reference_peak_index]
    simulation_peak = aligned.simulation[simulation_peak_index]
    reference_peak_time = aligned.times[reference_peak_index]
    simulation_peak_time = aligned.times[simulation_peak_index]
    return ValidationMetrics(
        samples=len(aligned.times),
        start_time=aligned.times[0],
        end_time=aligned.times[-1],
        mae=mae,
        rmse=rmse,
        bias=bias,
        max_abs_error=maximum_error,
        correlation=correlation,
        reference_range=reference_range,
        normalized_rmse=rmse / reference_range if reference_range > 0 else None,
        reference_min=min(aligned.reference),
        reference_max=max(aligned.reference),
        simulation_min=min(aligned.simulation),
        simulation_max=max(aligned.simulation),
        reference_abs_peak=reference_peak,
        reference_abs_peak_time=reference_peak_time,
        simulation_abs_peak=simulation_peak,
        simulation_abs_peak_time=simulation_peak_time,
        abs_peak_value_error=simulation_peak - reference_peak,
        abs_peak_time_error=simulation_peak_time - reference_peak_time,
    )


def create_report(
    name: str,
    reference: NumericSeries,
    simulation: NumericSeries,
    aligned: AlignedSeries,
    *,
    unit: str = "",
) -> ValidationReport:
    title = name.strip()
    if not title or len(title) > 120:
        raise ValidationError("报告名称必须是1–120个字符。")
    if len(unit) > 40:
        raise ValidationError("单位不能超过40个字符。")
    warnings: list[str] = []
    for series in (reference, simulation):
        provenance = series.provenance
        if provenance.rows_dropped:
            warnings.append(
                f"{provenance.role}数据丢弃 "
                f"{provenance.rows_dropped:,} 行空值、非数值或非有限值。"
            )
        if not provenance.source_was_sorted:
            warnings.append(
                f"{provenance.role}数据原始时间顺序不是严格递增，"
                "分析前已按时间排序。"
            )
    if reference.provenance.time_scale != simulation.provenance.time_scale:
        warnings.append("两组数据使用了不同时间缩放因子，请确认单位换算。")
    return ValidationReport(
        schema_version=REPORT_SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        name=title,
        unit=unit.strip(),
        interpolation="以参考数据时间点为网格，对仿真数据进行区间内线性插值；不外推",
        reference=reference.provenance,
        simulation=simulation.provenance,
        metrics=calculate_metrics(aligned),
        warnings=tuple(warnings),
    )


def write_validation_bundle(
    report: ValidationReport,
    aligned: AlignedSeries,
    output_parent: str | os.PathLike[str],
) -> Path:
    try:
        parent = Path(output_parent).expanduser().resolve(strict=True)
    except OSError as error:
        raise ValidationError(f"输出父目录不可用：{error}") from error
    if not parent.is_dir():
        raise ValidationError(f"输出父路径不是文件夹：{parent}")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", report.name).strip("-_")
    slug = slug[:40] or "report"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    final = _unique_bundle_path(parent, f"DSS-Validation-{stamp}-{slug}")
    temporary = parent / f".{final.name}.{os.getpid()}.tmp"
    temporary.mkdir()
    try:
        (temporary / "validation.json").write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _write_comparison_csv(temporary / "comparison.csv", aligned)
        (temporary / "report.html").write_text(
            _html_report(report, aligned),
            encoding="utf-8",
        )
        os.replace(temporary, final)
    except Exception:
        for path in sorted(temporary.glob("*"), reverse=True):
            if path.is_file():
                path.unlink()
        if temporary.exists():
            temporary.rmdir()
        raise
    return final


def _source_file(source: str | os.PathLike[str]) -> tuple[Path, int]:
    path = Path(source).expanduser()
    try:
        resolved = path.resolve(strict=True)
        size = resolved.stat().st_size
    except OSError as error:
        raise ValidationError(f"CSV 文件不可用：{error}") from error
    if not resolved.is_file():
        raise ValidationError(f"CSV 路径不是文件：{resolved}")
    if size <= 0:
        raise ValidationError("CSV 文件为空。")
    if size > MAX_CSV_BYTES:
        raise ValidationError("CSV 文件超过 256 MiB 安全上限。")
    return resolved, size


def _detect_encoding(path: Path) -> tuple[str, str]:
    with path.open("rb") as stream:
        sample_bytes = stream.read(SAMPLE_BYTES)
    for encoding in ("utf-8-sig", "gb18030", "cp1252"):
        try:
            return encoding, sample_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValidationError("CSV 编码无法识别；支持 UTF-8、GB18030 和 CP1252。")


def _unique_bundle_path(parent: Path, basename: str) -> Path:
    candidate = parent / basename
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = parent / f"{basename}-{counter}"
    return candidate


def _detect_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        return ","


def _linear_interpolate(series: NumericSeries, target: float) -> float:
    index = bisect_left(series.times, target)
    if index < len(series.times) and series.times[index] == target:
        return series.values[index]
    if index <= 0 or index >= len(series.times):
        raise ValidationError("插值目标位于仿真数据范围之外。")
    left_time = series.times[index - 1]
    right_time = series.times[index]
    fraction = (target - left_time) / (right_time - left_time)
    return series.values[index - 1] + fraction * (
        series.values[index] - series.values[index - 1]
    )


def _correlation(
    reference: Iterable[float],
    simulation: Iterable[float],
) -> float | None:
    left = tuple(reference)
    right = tuple(simulation)
    left_mean = fmean(left)
    right_mean = fmean(right)
    covariance = sum(
        (a - left_mean) * (b - right_mean)
        for a, b in zip(left, right, strict=True)
    )
    left_sum = sum((value - left_mean) ** 2 for value in left)
    right_sum = sum((value - right_mean) ** 2 for value in right)
    denominator = sqrt(left_sum * right_sum)
    return covariance / denominator if denominator > 0 else None


def _write_comparison_csv(path: Path, aligned: AlignedSeries) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("time", "reference", "simulation", "error"))
        writer.writerows(
            zip(
                aligned.times,
                aligned.reference,
                aligned.simulation,
                aligned.errors,
                strict=True,
            )
        )


def _html_report(report: ValidationReport, aligned: AlignedSeries) -> str:
    metrics = report.metrics
    rows = (
        ("样本数", f"{metrics.samples:,}"),
        ("时间窗口", f"{metrics.start_time:.8g} – {metrics.end_time:.8g}"),
        ("MAE", _number(metrics.mae, report.unit)),
        ("RMSE", _number(metrics.rmse, report.unit)),
        ("Bias", _number(metrics.bias, report.unit)),
        ("最大绝对误差", _number(metrics.max_abs_error, report.unit)),
        (
            "相关系数",
            f"{metrics.correlation:.6g}"
            if metrics.correlation is not None
            else "不可用（至少一组数据无方差）",
        ),
        (
            "归一化 RMSE",
            f"{metrics.normalized_rmse:.4%}"
            if metrics.normalized_rmse is not None
            else "不可用（参考范围为零）",
        ),
        (
            "参考绝对峰值",
            f"{_number(metrics.reference_abs_peak, report.unit)}"
            f" @ {metrics.reference_abs_peak_time:.8g}",
        ),
        (
            "仿真绝对峰值",
            f"{_number(metrics.simulation_abs_peak, report.unit)}"
            f" @ {metrics.simulation_abs_peak_time:.8g}",
        ),
        (
            "绝对峰值数值误差",
            _number(metrics.abs_peak_value_error, report.unit),
        ),
        (
            "绝对峰值时间误差",
            f"{metrics.abs_peak_time_error:.8g}",
        ),
    )
    metric_html = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>"
        for label, value in rows
    )
    warnings = (
        "<ul>"
        + "".join(f"<li>{html.escape(item)}</li>" for item in report.warnings)
        + "</ul>"
        if report.warnings
        else "<p>未产生数据质量警告。</p>"
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{html.escape(report.name)}</title>
<style>
body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;max-width:1100px;margin:32px auto;color:#172033}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd3df;padding:8px;text-align:left}}
th{{background:#f2f5f9;width:32%}}.meta{{color:#4d596d}}svg{{width:100%;height:auto;border:1px solid #ccd3df;background:#fff}}
.reference{{fill:none;stroke:#1261a0;stroke-width:2}}.simulation{{fill:none;stroke:#d45b25;stroke-width:2}}
</style>
</head>
<body>
<h1>{html.escape(report.name)}</h1>
<p class="meta">生成时间：{html.escape(report.generated_at)}；方法：{html.escape(report.interpolation)}</p>
{_svg_plot(aligned)}
<p><span style="color:#1261a0">■</span> 参考数据　
<span style="color:#d45b25">■</span> 仿真数据</p>
<h2>指标</h2><table>{metric_html}</table>
<h2>数据质量</h2>{warnings}
<h2>来源</h2>
<p>参考：{html.escape(report.reference.source_name)}，
SHA-256 {html.escape(report.reference.source_sha256)}</p>
<p>仿真：{html.escape(report.simulation.source_name)}，
SHA-256 {html.escape(report.simulation.source_sha256)}</p>
</body></html>"""


def _svg_plot(aligned: AlignedSeries) -> str:
    width, height, margin = 1000.0, 360.0, 48.0
    values = (*aligned.reference, *aligned.simulation)
    minimum, maximum = min(values), max(values)
    if minimum == maximum:
        minimum -= 0.5
        maximum += 0.5
    start, end = aligned.times[0], aligned.times[-1]
    indices = _downsample_indices(len(aligned.times), MAX_SVG_POINTS)

    def points(series: tuple[float, ...]) -> str:
        rendered: list[str] = []
        for index in indices:
            x = margin + (aligned.times[index] - start) / (end - start) * (
                width - 2 * margin
            )
            y = height - margin - (series[index] - minimum) / (
                maximum - minimum
            ) * (height - 2 * margin)
            rendered.append(f"{x:.2f},{y:.2f}")
        return " ".join(rendered)

    return (
        f'<svg viewBox="0 0 {width:.0f} {height:.0f}" '
        'role="img" aria-label="参考与仿真曲线">'
        f'<polyline class="reference" points="{points(aligned.reference)}"/>'
        f'<polyline class="simulation" points="{points(aligned.simulation)}"/>'
        "</svg>"
    )


def _downsample_indices(length: int, maximum: int) -> tuple[int, ...]:
    if length <= maximum:
        return tuple(range(length))
    step = (length - 1) / (maximum - 1)
    return tuple(
        min(length - 1, round(index * step))
        for index in range(maximum)
    )


def _number(value: float, unit: str) -> str:
    suffix = f" {unit}" if unit else ""
    return f"{value:.8g}{suffix}"


__all__ = [
    "AlignedSeries",
    "NumericSeries",
    "SeriesProvenance",
    "ValidationError",
    "ValidationMetrics",
    "ValidationReport",
    "align_series",
    "calculate_metrics",
    "create_report",
    "load_csv_series",
    "read_csv_headers",
    "write_validation_bundle",
]
