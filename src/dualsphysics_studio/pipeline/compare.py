"""Source-aware comparisons for persisted DualSPHysics run sessions."""

from __future__ import annotations

from dataclasses import dataclass
import csv
from io import StringIO
from typing import Any, Callable, Iterable

from .session import RunSession


@dataclass(frozen=True, slots=True)
class ComparisonColumn:
    session: RunSession
    label: str
    compatible: bool
    compatibility_note: str


@dataclass(frozen=True, slots=True)
class ComparisonRow:
    key: str
    label: str
    unit: str
    values: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class RunComparison:
    baseline: RunSession
    columns: tuple[ComparisonColumn, ...]
    rows: tuple[ComparisonRow, ...]
    excluded_count: int = 0

    def formatted_value(self, row: ComparisonRow, column: int) -> str:
        value = row.values[column]
        if value is None:
            return "不可用"
        text = _format_value(value, row.unit)
        if column == 0 or not isinstance(value, (int, float)):
            return text
        baseline = row.values[0]
        if not isinstance(baseline, (int, float)) or baseline == 0:
            return text
        delta = (float(value) - float(baseline)) / abs(float(baseline)) * 100
        return f"{text}（{delta:+.1f}%）"

    def to_csv(self) -> str:
        output = StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            ["指标", "单位", *(column.label for column in self.columns)]
        )
        writer.writerow(
            [
                "兼容性",
                "",
                *(
                    "基线"
                    if index == 0
                    else "可直接比较"
                    if column.compatible
                    else column.compatibility_note
                    for index, column in enumerate(self.columns)
                ),
            ]
        )
        for row in self.rows:
            writer.writerow(
                [
                    row.label,
                    row.unit,
                    *(value if value is not None else "" for value in row.values),
                ]
            )
        return output.getvalue()


@dataclass(frozen=True, slots=True)
class _Metric:
    key: str
    label: str
    unit: str
    getter: Callable[[RunSession], Any]


def compare_sessions(
    baseline: RunSession,
    sessions: Iterable[RunSession],
    *,
    compatible_only: bool = True,
) -> RunComparison:
    """Compare persisted values while explicitly tracking comparability."""

    candidates: list[RunSession] = []
    seen = {_session_identity(baseline)}
    excluded = 0
    for session in sessions:
        identity = _session_identity(session)
        if identity in seen:
            continue
        seen.add(identity)
        compatible, _note = _compatibility(baseline, session)
        if compatible_only and not compatible:
            excluded += 1
            continue
        candidates.append(session)

    ordered = [baseline, *candidates]
    columns: list[ComparisonColumn] = []
    for index, session in enumerate(ordered):
        compatible, note = _compatibility(baseline, session)
        if index == 0:
            compatible = True
            note = "基线"
        columns.append(
            ComparisonColumn(
                session=session,
                label=_session_label(session, baseline=index == 0),
                compatible=compatible,
                compatibility_note=note,
            )
        )

    rows: list[ComparisonRow] = []
    for metric in _METRICS:
        values = tuple(metric.getter(session) for session in ordered)
        if metric.key != "status" and not any(value is not None for value in values):
            continue
        rows.append(
            ComparisonRow(
                key=metric.key,
                label=metric.label,
                unit=metric.unit,
                values=values,
            )
        )
    return RunComparison(
        baseline=baseline,
        columns=tuple(columns),
        rows=tuple(rows),
        excluded_count=excluded,
    )


def _compatibility(
    baseline: RunSession,
    candidate: RunSession,
) -> tuple[bool, str]:
    notes: list[str] = []
    baseline_sha = baseline.source_sha256.strip().casefold()
    candidate_sha = candidate.source_sha256.strip().casefold()
    if not baseline_sha or not candidate_sha:
        notes.append("XML SHA 缺失")
    elif baseline_sha != candidate_sha:
        notes.append("XML SHA 不同")
    baseline_backend = baseline.backend.strip().casefold()
    candidate_backend = candidate.backend.strip().casefold()
    if not baseline_backend or not candidate_backend:
        notes.append("求解后端缺失")
    elif baseline_backend != candidate_backend:
        notes.append("求解后端不同")
    return (not notes, "；".join(notes) or "可直接比较")


def _session_identity(session: RunSession) -> str:
    return str(session.record_path).casefold()


def _session_label(session: RunSession, *, baseline: bool) -> str:
    prefix = "基线 · " if baseline else ""
    created = session.created_at[:19].replace("T", " ")
    return (
        f"{prefix}{created} · {session.backend.upper()} · "
        f"{session.session_id[-8:]}"
    )


def _actual(key: str) -> Callable[[RunSession], Any]:
    return lambda session: session.actual.get(key)


def _solver_wall_seconds(session: RunSession) -> Any:
    return session.actual.get(f"{session.backend.casefold()}_wall_seconds")


def _particle_value(key: str) -> Callable[[RunSession], Any]:
    def getter(session: RunSession) -> Any:
        counts = session.actual.get("particle_counts")
        return counts.get(key) if isinstance(counts, dict) else None

    return getter


def _particle_total(session: RunSession) -> int | None:
    counts = session.actual.get("particle_counts")
    if not isinstance(counts, dict):
        return None
    boundary = counts.get("boundary")
    fluid = counts.get("fluid")
    if (
        isinstance(boundary, (int, float))
        and boundary >= 0
        and isinstance(fluid, (int, float))
        and fluid >= 0
    ):
        return int(boundary + fluid)
    values = [
        int(counts[key])
        for key in ("fixed", "moving", "floating", "fluid")
        if isinstance(counts.get(key), (int, float)) and counts[key] >= 0
    ]
    return sum(values) if values else None


def _format_value(value: Any, unit: str) -> str:
    if unit == "bytes":
        size = float(value)
        for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
            if abs(size) < 1024 or suffix == "TiB":
                return f"{size:.1f} {suffix}" if suffix != "B" else f"{int(size)} B"
            size /= 1024
    if unit == "seconds":
        seconds = float(value)
        if seconds >= 3600:
            return f"{seconds / 3600:.2f} h"
        if seconds >= 60:
            return f"{seconds / 60:.2f} min"
        return f"{seconds:.2f} s"
    if unit == "count":
        return f"{int(value):,}"
    return str(value)


_METRICS = (
    _Metric("status", "会话状态", "", lambda session: session.status),
    _Metric("particle_total", "粒子总数", "count", _particle_total),
    _Metric("fixed_particles", "固定边界粒子", "count", _particle_value("fixed")),
    _Metric("moving_particles", "运动边界粒子", "count", _particle_value("moving")),
    _Metric("floating_particles", "浮体粒子", "count", _particle_value("floating")),
    _Metric("fluid_particles", "流体粒子", "count", _particle_value("fluid")),
    _Metric("boundary_particles", "边界粒子", "count", _particle_value("boundary")),
    _Metric(
        "total_runtime_seconds",
        "求解总耗时",
        "seconds",
        _actual("total_runtime_seconds"),
    ),
    _Metric(
        "simulation_runtime_seconds",
        "模拟阶段耗时",
        "seconds",
        _actual("simulation_runtime_seconds"),
    ),
    _Metric("solver_wall_seconds", "Studio 观测耗时", "seconds", _solver_wall_seconds),
    _Metric(
        "allocated_cpu_memory_bytes",
        "CPU 已分配内存",
        "bytes",
        _actual("allocated_cpu_memory_bytes"),
    ),
    _Metric(
        "allocated_gpu_memory_bytes",
        "GPU 已分配显存",
        "bytes",
        _actual("allocated_gpu_memory_bytes"),
    ),
    _Metric(
        "requested_gpu_memory_bytes",
        "GPU 请求显存",
        "bytes",
        _actual("requested_gpu_memory_bytes"),
    ),
    _Metric("steps", "计算步数", "count", _actual("steps")),
    _Metric(
        "reported_part_files",
        "Part 文件数",
        "count",
        _actual("reported_part_files"),
    ),
)


__all__ = [
    "ComparisonColumn",
    "ComparisonRow",
    "RunComparison",
    "compare_sessions",
]
