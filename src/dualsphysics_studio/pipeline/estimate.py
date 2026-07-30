"""Conservative resource estimates for one DualSPHysics case.

The estimator intentionally exposes ranges and evidence instead of presenting
heuristics as exact requirements.  It accepts the existing document model and
GenCase summaries, so XML remains the only editable source of truth.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import ctypes
import math
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping


MIB = 1024 * 1024
GIB = 1024 * MIB

# Deliberately broad Studio safety envelopes.  Actual DualSPHysics allocation
# depends on solver options and is replaced by measured log values when known.
RAM_BASE_BYTES = 256 * MIB
RAM_BYTES_PER_PARTICLE = (256, 1024)
VRAM_BASE_BYTES = 128 * MIB
VRAM_BYTES_PER_PARTICLE = (160, 640)
DISK_BYTES_PER_PARTICLE_FRAME = (48, 256)


@dataclass(frozen=True, slots=True)
class ResourceEstimate:
    """A serialisable resource estimate with explicit confidence and evidence."""

    phase: str
    confidence: str
    dp: float | None = None
    dimensions: int | None = None
    domain_grid_particles: int | None = None
    actual_particles: int | None = None
    output_frames: int | None = None
    ram_low_bytes: int | None = None
    ram_high_bytes: int | None = None
    vram_low_bytes: int | None = None
    vram_high_bytes: int | None = None
    disk_low_bytes: int | None = None
    disk_high_bytes: int | None = None
    runtime_low_seconds: float | None = None
    runtime_high_seconds: float | None = None
    available_ram_bytes: int | None = None
    available_disk_bytes: int | None = None
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    measured: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResourceEstimate":
        data = dict(value)
        for key in ("assumptions", "warnings", "evidence"):
            data[key] = tuple(str(item) for item in data.get(key, ()))
        data["measured"] = dict(data.get("measured") or {})
        allowed = cls.__dataclass_fields__
        return cls(**{key: item for key, item in data.items() if key in allowed})

    @property
    def particle_basis(self) -> int | None:
        return self.actual_particles or self.domain_grid_particles

    def metric_rows(self) -> tuple[tuple[str, str, str, str], ...]:
        """Return UI-ready label, value, confidence, and evidence rows."""

        rows: list[tuple[str, str, str, str]] = []
        if self.actual_particles is not None:
            rows.append(
                ("实际粒子数", f"{self.actual_particles:,}", "实测", "GenCase Case.out")
            )
        elif self.domain_grid_particles is not None:
            rows.append(
                (
                    "定义域网格上限",
                    f"{self.domain_grid_particles:,}",
                    "低",
                    "definition 范围和 dp；不等于真实粒子数",
                )
            )
        if self.output_frames is not None:
            rows.append(
                (
                    "预计输出帧",
                    f"{self.output_frames:,}",
                    "中",
                    "TimeMax / TimeOut，包含初始帧",
                )
            )
        _append_range_row(
            rows,
            "系统内存需求",
            self.ram_low_bytes,
            self.ram_high_bytes,
            _confidence_label(self.confidence),
            "粒子数安全区间；求解日志实测值优先",
        )
        _append_range_row(
            rows,
            "GPU 显存需求",
            self.vram_low_bytes,
            self.vram_high_bytes,
            _confidence_label(self.confidence),
            "与求解选项有关；首次运行可能偏宽",
        )
        _append_range_row(
            rows,
            "输出磁盘",
            self.disk_low_bytes,
            self.disk_high_bytes,
            _confidence_label(self.confidence),
            "粒子数、输出帧和历史文件大小",
        )
        if self.available_ram_bytes is not None:
            rows.append(
                (
                    "当前可用物理内存",
                    format_bytes(self.available_ram_bytes),
                    "实测",
                    "运行预检时的 Windows 系统状态",
                )
            )
        if self.available_disk_bytes is not None:
            rows.append(
                (
                    "目标磁盘可用空间",
                    format_bytes(self.available_disk_bytes),
                    "实测",
                    "运行预检时的目标卷状态",
                )
            )
        if self.runtime_low_seconds is not None and self.runtime_high_seconds is not None:
            rows.append(
                (
                    "预计运行时间",
                    f"{_duration(self.runtime_low_seconds)} – "
                    f"{_duration(self.runtime_high_seconds)}",
                    "历史校准",
                    "相同 XML、后端和已完成本机运行",
                )
            )
        else:
            rows.append(
                ("预计运行时间", "数据不足", "未知", "求解开始后根据实际进度更新")
            )
        for warning in self.warnings:
            rows.append(("资源警告", warning, "需确认", "预检比较结果"))
        return tuple(rows)


def estimate_case(
    document: Any,
    *,
    output_directory: str | Path | None = None,
    particle_summary: Mapping[str, Any] | None = None,
    source_sha256: str | None = None,
    backend: str | None = None,
    history: Iterable[Mapping[str, Any]] = (),
) -> ResourceEstimate:
    """Estimate resources from the current document and optional measured data."""

    facts = _value(document, "facts")
    dp = _finite_positive(_value(facts, "dp"))
    bounds = _value(facts, "definition_bounds", "bounds")
    spans = _bounds_spans(bounds)
    dimensions: int | None = None
    domain_particles: int | None = None
    assumptions: list[str] = []
    warnings: list[str] = []
    evidence: list[str] = []

    if dp is not None and spans is not None:
        active_spans = [span for span in spans if span > max(dp * 0.5, 1e-12)]
        dimensions = 2 if len(active_spans) <= 2 else 3
        counts = [
            max(1, math.floor(span / dp + 1e-9) + 1)
            for span in spans
        ]
        domain_particles = math.prod(counts)
        evidence.append("由 definition 范围和 dp 计算定义域网格上限")
        assumptions.append("定义域网格上限不是几何实际填充粒子数")
    else:
        warnings.append("缺少可计算的 dp 或 definition 范围，无法估算粒子上限")

    parameters = _parameter_values(document)
    time_max = _finite_positive(parameters.get("timemax"))
    time_out = _finite_positive(parameters.get("timeout"))
    frames = None
    if time_max is not None and time_out is not None:
        frames = max(1, math.floor(time_max / time_out + 1e-9) + 1)
        evidence.append("输出帧按 TimeMax / TimeOut 并包含初始帧估算")
    else:
        warnings.append("TimeMax 或 TimeOut 不可用，无法估算输出帧")

    actual_particles = _actual_particle_count(particle_summary)
    phase = "post-gencase" if actual_particles is not None else "preflight"
    confidence = "medium" if actual_particles is not None else "low"
    particle_basis = actual_particles or domain_particles
    if actual_particles is not None:
        evidence.append("粒子数来自 GenCase Case.out")

    ram_low = ram_high = vram_low = vram_high = None
    disk_low = disk_high = None
    if particle_basis is not None:
        ram_low = RAM_BASE_BYTES + particle_basis * RAM_BYTES_PER_PARTICLE[0]
        ram_high = RAM_BASE_BYTES + particle_basis * RAM_BYTES_PER_PARTICLE[1]
        vram_low = VRAM_BASE_BYTES + particle_basis * VRAM_BYTES_PER_PARTICLE[0]
        vram_high = VRAM_BASE_BYTES + particle_basis * VRAM_BYTES_PER_PARTICLE[1]
        if frames is not None:
            disk_low = particle_basis * frames * DISK_BYTES_PER_PARTICLE_FRAME[0]
            disk_high = particle_basis * frames * DISK_BYTES_PER_PARTICLE_FRAME[1]
        assumptions.append("内存和磁盘为 Studio 保守区间，不是官方容量保证")

    available_ram = available_physical_memory()
    available_disk = available_disk_space(output_directory)
    if ram_high is not None and available_ram is not None and ram_high > available_ram:
        warnings.append("内存需求上界超过当前可用物理内存")
    if disk_high is not None and available_disk is not None and disk_high > available_disk:
        warnings.append("磁盘需求上界超过目标磁盘当前可用空间")

    runtime_low, runtime_high = _history_runtime(
        history,
        source_sha256=source_sha256,
        backend=backend,
    )
    if runtime_low is not None:
        evidence.append("运行时间来自相同 XML 和后端的本机历史记录")

    measured = _measured_values(particle_summary)
    if measured:
        confidence = "measured"
        ram_value = measured.get("allocated_cpu_memory_bytes")
        vram_value = measured.get("allocated_gpu_memory_bytes") or measured.get(
            "requested_gpu_memory_bytes"
        )
        if isinstance(ram_value, int):
            ram_low = ram_high = ram_value
        if isinstance(vram_value, int):
            vram_low = vram_high = vram_value

    return ResourceEstimate(
        phase=phase,
        confidence=confidence,
        dp=dp,
        dimensions=dimensions,
        domain_grid_particles=domain_particles,
        actual_particles=actual_particles,
        output_frames=frames,
        ram_low_bytes=ram_low,
        ram_high_bytes=ram_high,
        vram_low_bytes=vram_low,
        vram_high_bytes=vram_high,
        disk_low_bytes=disk_low,
        disk_high_bytes=disk_high,
        runtime_low_seconds=runtime_low,
        runtime_high_seconds=runtime_high,
        available_ram_bytes=available_ram,
        available_disk_bytes=available_disk,
        assumptions=tuple(assumptions),
        warnings=tuple(warnings),
        evidence=tuple(evidence),
        measured=measured,
    )


def live_stage_progress(
    stage: str,
    line: str,
    *,
    expected_frames: int | None = None,
) -> tuple[int, int] | None:
    """Best-effort determinate progress from official stage output lines."""

    import re

    if expected_frames is None or expected_frames <= 0:
        return None
    if stage in {"cpu", "gpu"}:
        match = re.search(r"\bPart[_ ]?(\d{4,})\b", line, re.IGNORECASE)
    elif stage == "partvtk":
        match = re.search(r"\bPartAll[_ ]?(\d{4,})\b", line, re.IGNORECASE)
    else:
        return None
    if match is None:
        return None
    completed = min(expected_frames, int(match.group(1)) + 1)
    return completed, expected_frames


def available_physical_memory() -> int | None:
    """Return available physical RAM on Windows without adding a dependency."""

    if not hasattr(ctypes, "windll"):
        return None

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
    except (AttributeError, OSError):
        return None
    return int(status.ullAvailPhys)


def available_disk_space(path: str | Path | None) -> int | None:
    if path is None:
        return None
    candidate = Path(path).expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        return int(shutil.disk_usage(candidate).free)
    except OSError:
        return None


def format_bytes(value: int | None) -> str:
    if value is None:
        return "未知"
    if value >= GIB:
        return f"{value / GIB:.2f} GiB"
    if value >= MIB:
        return f"{value / MIB:.1f} MiB"
    if value >= 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value} B"


def _append_range_row(
    rows: list[tuple[str, str, str, str]],
    label: str,
    low: int | None,
    high: int | None,
    confidence: str,
    evidence: str,
) -> None:
    if low is None or high is None:
        rows.append((label, "数据不足", "未知", evidence))
        return
    value = format_bytes(low) if low == high else f"{format_bytes(low)} – {format_bytes(high)}"
    rows.append((label, value, confidence, evidence))


def _value(value: Any, *names: str) -> Any:
    for name in names:
        if value is None:
            return None
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            result = getattr(value, name)
            return result() if callable(result) else result
    return None


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _bounds_spans(bounds: Any) -> tuple[float, float, float] | None:
    minimum = _value(bounds, "minimum")
    maximum = _value(bounds, "maximum")
    if minimum is None or maximum is None:
        return None
    try:
        spans = tuple(
            float(_value(maximum, axis)) - float(_value(minimum, axis))
            for axis in ("x", "y", "z")
        )
    except (TypeError, ValueError):
        return None
    if any(not math.isfinite(span) or span < 0 for span in spans):
        return None
    return spans  # type: ignore[return-value]


def _parameter_values(document: Any) -> dict[str, str]:
    fields = _value(document, "parameter_fields") or ()
    result: dict[str, str] = {}
    for field in fields:
        if str(_value(field, "category") or "").casefold() != "execution":
            continue
        key = str(_value(field, "key") or "").strip().casefold()
        value = str(_value(field, "value") or "").strip()
        if key:
            result[key] = value
    return result


def _actual_particle_count(summary: Mapping[str, Any] | None) -> int | None:
    if not summary:
        return None
    direct = summary.get("particles") or summary.get("particle_count")
    if isinstance(direct, (int, float)) and direct > 0:
        return int(direct)
    counts = summary.get("particle_counts")
    if not isinstance(counts, Mapping):
        return None
    boundary = counts.get("boundary")
    fluid = counts.get("fluid")
    if isinstance(boundary, (int, float)) and isinstance(fluid, (int, float)):
        return int(boundary + fluid)
    values = [
        counts.get(key)
        for key in ("fixed", "moving", "floating", "fluid")
        if isinstance(counts.get(key), (int, float))
    ]
    return int(sum(values)) if values else None


def _measured_values(summary: Mapping[str, Any] | None) -> dict[str, Any]:
    if not summary:
        return {}
    keys = (
        "allocated_cpu_memory_bytes",
        "allocated_gpu_memory_bytes",
        "requested_gpu_memory_bytes",
        "total_runtime_seconds",
        "simulation_runtime_seconds",
        "device",
    )
    return {key: summary[key] for key in keys if key in summary}


def _history_runtime(
    history: Iterable[Mapping[str, Any]],
    *,
    source_sha256: str | None,
    backend: str | None,
) -> tuple[float | None, float | None]:
    if not source_sha256 or not backend:
        return None, None
    samples: list[float] = []
    for record in history:
        source = record.get("source") or {}
        if source.get("sha256") != source_sha256:
            continue
        if str(record.get("backend", "")).casefold() != backend.casefold():
            continue
        actual = record.get("actual") or {}
        value = actual.get("total_runtime_seconds")
        if isinstance(value, (int, float)) and value > 0:
            samples.append(float(value))
    if not samples:
        return None, None
    low = min(samples) * 0.8
    high = max(samples) * 1.25
    return low, high


def _duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def _confidence_label(value: str) -> str:
    return {
        "low": "低",
        "medium": "中",
        "measured": "含实测",
    }.get(value, value)
