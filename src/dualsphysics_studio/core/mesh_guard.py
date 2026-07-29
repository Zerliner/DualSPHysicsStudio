"""Dependency-free resource preflight for user-opened STL and legacy VTK files."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from pathlib import Path
import re
import struct
from typing import Any


MIB = 1024 * 1024
GIB = 1024 * MIB

# These are application-level rendering budgets, not format limits.  A warning
# can be accepted once by the user; a hard limit protects the desktop process.
MESH_FILE_WARNING_BYTES = 512 * MIB
MAX_MESH_FILE_BYTES = 4 * GIB
MESH_ELEMENT_WARNING_COUNT = 5_000_000
MAX_MESH_ELEMENT_COUNT = 25_000_000
VTK_PREFLIGHT_SCAN_BYTES = 16 * MIB


class MeshRisk(str, Enum):
    SAFE = "safe"
    WARNING = "warning"
    BLOCKED = "blocked"


class MeshPreflightError(ValueError):
    """Raised when a file cannot be safely identified before VTK reads it."""


@dataclass(frozen=True, slots=True)
class MeshPreflightReport:
    path: Path
    kind: str
    file_bytes: int
    estimated_points: int | None
    estimated_cells: int | None
    estimated_peak_bytes: int
    risk: MeshRisk
    reasons: tuple[str, ...] = ()

    @property
    def requires_confirmation(self) -> bool:
        return self.risk is MeshRisk.WARNING

    @property
    def blocked(self) -> bool:
        return self.risk is MeshRisk.BLOCKED

    def summary(self) -> str:
        details = [
            f"文件大小：{format_bytes(self.file_bytes)}",
            f"保守内存估算：约 {format_bytes(self.estimated_peak_bytes)}",
        ]
        if self.estimated_points is not None:
            details.append(f"预计点数：{self.estimated_points:,}")
        if self.estimated_cells is not None:
            label = "三角形数" if self.kind == "stl" else "预计单元数"
            details.append(f"{label}：{self.estimated_cells:,}")
        if self.reasons:
            details.extend(f"• {reason}" for reason in self.reasons)
        return "\n".join(details)


@dataclass(frozen=True, slots=True)
class MeshDatasetStats:
    points: int
    cells: int
    bounds: tuple[float, float, float, float, float, float]


def format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TiB"


def inspect_mesh_file(path: str | Path) -> MeshPreflightReport:
    """Inspect bounded metadata without asking VTK to allocate the dataset."""

    target = Path(path)
    if not target.is_file():
        raise MeshPreflightError("文件不存在。")
    try:
        file_bytes = target.stat().st_size
    except OSError as exc:
        raise MeshPreflightError(f"无法读取文件大小：{exc}") from exc
    if file_bytes <= 0:
        raise MeshPreflightError("文件为空。")

    suffix = target.suffix.casefold()
    if suffix == ".stl":
        points, cells = _inspect_stl(target, file_bytes)
        kind = "stl"
    elif suffix == ".vtk":
        points, cells = _inspect_legacy_vtk(target)
        kind = "vtk"
    else:
        raise MeshPreflightError("仅支持 .stl 和 legacy .vtk 文件。")

    estimated_peak = _estimate_peak_bytes(file_bytes, points, cells)
    risk, reasons = _classify_risk(file_bytes, points, cells, estimated_peak)
    return MeshPreflightReport(
        path=target.resolve(),
        kind=kind,
        file_bytes=file_bytes,
        estimated_points=points,
        estimated_cells=cells,
        estimated_peak_bytes=estimated_peak,
        risk=risk,
        reasons=reasons,
    )


def validate_loaded_dataset(dataset: Any) -> MeshDatasetStats:
    """Validate the VTK output before an actor is attached to the renderer."""

    if dataset is None:
        raise MeshPreflightError("读取器没有返回数据集。")
    try:
        points = int(dataset.GetNumberOfPoints())
        cells = int(dataset.GetNumberOfCells())
    except (AttributeError, TypeError, ValueError) as exc:
        raise MeshPreflightError("读取器返回了无法识别的数据集。") from exc
    if points <= 0:
        raise MeshPreflightError("文件中没有可显示的点。")
    if cells < 0:
        raise MeshPreflightError("数据集单元数量无效。")
    if points > MAX_MESH_ELEMENT_COUNT or cells > MAX_MESH_ELEMENT_COUNT:
        raise MeshPreflightError(
            "读取后的模型超过硬上限："
            f"{points:,} 个点、{cells:,} 个单元；未加入场景。"
        )
    try:
        raw_bounds = dataset.GetBounds()
        bounds = tuple(float(value) for value in raw_bounds)
    except (AttributeError, TypeError, ValueError) as exc:
        raise MeshPreflightError("模型没有可读取的三维包围盒。") from exc
    if len(bounds) != 6 or not all(isfinite(value) for value in bounds):
        raise MeshPreflightError("模型包围盒包含 NaN 或无穷值。")
    if any(bounds[index * 2] > bounds[index * 2 + 1] for index in range(3)):
        raise MeshPreflightError("模型包围盒的最小值大于最大值。")
    return MeshDatasetStats(points, cells, bounds)  # type: ignore[arg-type]


def _inspect_stl(path: Path, file_bytes: int) -> tuple[int | None, int | None]:
    try:
        with path.open("rb") as stream:
            header = stream.read(84)
    except OSError as exc:
        raise MeshPreflightError(f"无法读取 STL 文件头：{exc}") from exc

    if len(header) >= 84:
        triangles = struct.unpack("<I", header[80:84])[0]
        expected_size = 84 + triangles * 50
        # Some exporters append a short footer.  A bounded trailer is still a
        # reliable binary STL count and avoids misclassifying "solid" headers.
        if expected_size <= file_bytes and file_bytes - expected_size <= 4096:
            return triangles * 3, triangles

    lower_header = header.lower()
    if not lower_header.lstrip().startswith(b"solid"):
        raise MeshPreflightError("STL 既不是完整的二进制文件，也没有 ASCII solid 文件头。")
    if file_bytes > MESH_FILE_WARNING_BYTES:
        return None, None
    facets = _count_ascii_stl_facets(path)
    if facets <= 0:
        raise MeshPreflightError("ASCII STL 中没有 facet normal 三角面。")
    return facets * 3, facets


def _count_ascii_stl_facets(path: Path) -> int:
    marker = b"facet normal"
    count = 0
    overlap = b""
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(MIB):
                searchable = (overlap + chunk).lower()
                count += searchable.count(marker)
                overlap = searchable[-(len(marker) - 1) :]
    except OSError as exc:
        raise MeshPreflightError(f"无法扫描 ASCII STL：{exc}") from exc
    return count


_VTK_POINTS = re.compile(rb"(?im)^\s*POINTS\s+([0-9]+)\b")
_VTK_CELL_BLOCK = re.compile(
    rb"(?im)^\s*(?:CELLS|VERTICES|LINES|POLYGONS|TRIANGLE_STRIPS)\s+([0-9]+)\b"
)


def _inspect_legacy_vtk(path: Path) -> tuple[int | None, int | None]:
    try:
        with path.open("rb") as stream:
            payload = stream.read(VTK_PREFLIGHT_SCAN_BYTES)
    except OSError as exc:
        raise MeshPreflightError(f"无法读取 VTK 文件头：{exc}") from exc
    first_line = payload.splitlines()[0].strip().lower() if payload else b""
    if not first_line.startswith(b"# vtk datafile version"):
        raise MeshPreflightError("文件不是可识别的 legacy VTK 数据集。")
    header_lines = payload.splitlines()[:5]
    if not any(line.strip().upper() in {b"ASCII", b"BINARY"} for line in header_lines):
        raise MeshPreflightError("legacy VTK 文件头缺少 ASCII/BINARY 声明。")
    if re.search(rb"(?im)^\s*DATASET\s+\S+", payload) is None:
        raise MeshPreflightError("legacy VTK 文件头缺少 DATASET 声明。")

    point_match = _VTK_POINTS.search(payload)
    point_count = int(point_match.group(1)) if point_match else None
    cell_counts = [int(match.group(1)) for match in _VTK_CELL_BLOCK.finditer(payload)]
    cell_count = sum(cell_counts) if cell_counts else None
    return point_count, cell_count


def _estimate_peak_bytes(
    file_bytes: int, points: int | None, cells: int | None
) -> int:
    # VTK stores coordinates, connectivity, arrays and rendering buffers.  This
    # is deliberately conservative and is presented as an estimate, not a quota.
    structured = (points or 0) * 48 + (cells or 0) * 64
    return max(file_bytes * 3, file_bytes + structured)


def _classify_risk(
    file_bytes: int,
    points: int | None,
    cells: int | None,
    estimated_peak: int,
) -> tuple[MeshRisk, tuple[str, ...]]:
    blocked: list[str] = []
    warnings: list[str] = []
    if file_bytes > MAX_MESH_FILE_BYTES:
        blocked.append(
            f"文件超过 {format_bytes(MAX_MESH_FILE_BYTES)} 的应用硬上限"
        )
    for label, value in (("点数", points), ("单元数", cells)):
        if value is not None and value > MAX_MESH_ELEMENT_COUNT:
            blocked.append(f"{label}超过 {MAX_MESH_ELEMENT_COUNT:,} 的应用硬上限")
        elif value is not None and value > MESH_ELEMENT_WARNING_COUNT:
            warnings.append(f"{label}超过 {MESH_ELEMENT_WARNING_COUNT:,}，交互可能变慢")
    if file_bytes > MESH_FILE_WARNING_BYTES:
        warnings.append(
            f"文件超过 {format_bytes(MESH_FILE_WARNING_BYTES)}，读取可能占用大量内存"
        )
    if estimated_peak > 2 * GIB:
        warnings.append("保守内存估算超过 2 GiB")
    if blocked:
        return MeshRisk.BLOCKED, tuple(blocked)
    if warnings:
        return MeshRisk.WARNING, tuple(dict.fromkeys(warnings))
    return MeshRisk.SAFE, ()
