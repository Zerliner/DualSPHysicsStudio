"""Dependency-free resource preflight for user-opened STL and legacy VTK files."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from math import isfinite, sqrt
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
MESH_QUALITY_MAX_FILE_BYTES = 256 * MIB
MESH_QUALITY_MAX_TRIANGLES = 100_000


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
    quality: "MeshQualityReport | None" = None

    @property
    def requires_confirmation(self) -> bool:
        return self.risk is MeshRisk.WARNING or bool(
            self.quality and self.quality.requires_confirmation
        )

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
        if self.quality is not None:
            details.extend(("", "Mesh Doctor：", self.quality.summary()))
        return "\n".join(details)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "path": str(self.path),
            "kind": self.kind,
            "file_bytes": self.file_bytes,
            "estimated_points": self.estimated_points,
            "estimated_cells": self.estimated_cells,
            "estimated_peak_bytes": self.estimated_peak_bytes,
            "risk": self.risk.value,
            "reasons": list(self.reasons),
            "quality": self.quality.to_dict() if self.quality else None,
        }


@dataclass(frozen=True, slots=True)
class MeshQualityReport:
    analyzed: bool
    triangle_count: int | None = None
    unique_vertices: int | None = None
    boundary_edges: int | None = None
    nonmanifold_edges: int | None = None
    inconsistent_edges: int | None = None
    duplicate_triangles: int | None = None
    degenerate_triangles: int | None = None
    connected_components: int | None = None
    normal_mismatches: int | None = None
    invalid_normals: int | None = None
    surface_area: float | None = None
    enclosed_volume: float | None = None
    bounds: tuple[float, float, float, float, float, float] | None = None
    notes: tuple[str, ...] = ()

    @property
    def watertight(self) -> bool | None:
        if (
            not self.analyzed
            or self.boundary_edges is None
            or self.nonmanifold_edges is None
        ):
            return None
        return (
            (self.triangle_count or 0) > 0
            and self.boundary_edges == 0
            and self.nonmanifold_edges == 0
            and (self.inconsistent_edges or 0) == 0
            and (self.duplicate_triangles or 0) == 0
            and (self.degenerate_triangles or 0) == 0
        )

    @property
    def requires_confirmation(self) -> bool:
        return self.analyzed and any(
            (value or 0) > 0
            for value in (
                self.nonmanifold_edges,
                self.inconsistent_edges,
                self.duplicate_triangles,
                self.degenerate_triangles,
            )
        )

    def short_status(self) -> str:
        if not self.analyzed:
            return "拓扑检查未执行"
        if self.requires_confirmation:
            return "发现拓扑质量问题"
        if self.watertight:
            return "封闭表面"
        if self.boundary_edges:
            return f"开放表面（{self.boundary_edges:,} 条边界边）"
        return "未发现高风险拓扑问题"

    def summary(self) -> str:
        if not self.analyzed:
            return "\n".join(self.notes or ("未执行拓扑质量检查。",))
        details: list[str] = []
        if self.triangle_count is not None:
            details.append(f"分析三角形：{self.triangle_count:,}")
        if self.unique_vertices is not None:
            details.append(f"唯一顶点：{self.unique_vertices:,}")
        if self.watertight is not None:
            details.append(
                "封闭性检查：" + ("通过" if self.watertight else "未通过")
            )
        if self.boundary_edges is not None:
            details.append(f"边界边：{self.boundary_edges:,}")
        if self.nonmanifold_edges is not None:
            details.append(f"非流形边：{self.nonmanifold_edges:,}")
        if self.inconsistent_edges is not None:
            details.append(f"方向不一致边：{self.inconsistent_edges:,}")
        if self.duplicate_triangles is not None:
            details.append(f"重复三角形：{self.duplicate_triangles:,}")
        if self.degenerate_triangles is not None:
            details.append(f"退化三角形：{self.degenerate_triangles:,}")
        if self.connected_components is not None:
            details.append(f"连通分量：{self.connected_components:,}")
        if self.normal_mismatches is not None:
            details.append(f"面法向不一致：{self.normal_mismatches:,}")
        if self.invalid_normals is not None:
            details.append(f"无效或零法向：{self.invalid_normals:,}")
        if self.surface_area is not None:
            details.append(f"表面积（原坐标单位²）：{self.surface_area:.6g}")
        if self.enclosed_volume is not None:
            details.append(f"封闭体积（原坐标单位³）：{self.enclosed_volume:.6g}")
        if self.bounds is not None:
            extents = tuple(
                self.bounds[index * 2 + 1] - self.bounds[index * 2]
                for index in range(3)
            )
            details.append(
                "包围盒尺寸（原坐标单位）："
                + " × ".join(f"{value:.6g}" for value in extents)
            )
        details.extend(f"• {note}" for note in self.notes)
        return "\n".join(details)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["notes"] = list(self.notes)
        value["watertight"] = self.watertight
        value["requires_confirmation"] = self.requires_confirmation
        return value


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
        points, cells, quality = _inspect_stl(target, file_bytes)
        kind = "stl"
    elif suffix == ".vtk":
        points, cells = _inspect_legacy_vtk(target)
        kind = "vtk"
        quality = MeshQualityReport(
            analyzed=False,
            notes=(
                "legacy VTK 预检只验证格式、规模和资源风险；"
                "粒子或通用数据集不套用 STL 封闭性规则。",
            ),
        )
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
        quality=quality,
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


def _inspect_stl(
    path: Path,
    file_bytes: int,
) -> tuple[int | None, int | None, MeshQualityReport]:
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
            quality = _quality_or_skipped(
                path,
                file_bytes=file_bytes,
                triangles=triangles,
                binary=True,
            )
            return triangles * 3, triangles, quality

    lower_header = header.lower()
    if not lower_header.lstrip().startswith(b"solid"):
        raise MeshPreflightError("STL 既不是完整的二进制文件，也没有 ASCII solid 文件头。")
    if file_bytes > MESH_FILE_WARNING_BYTES:
        return (
            None,
            None,
            MeshQualityReport(
                analyzed=False,
                notes=("ASCII STL 超过资源警告阈值，未执行拓扑质量扫描。",),
            ),
        )
    facets = _count_ascii_stl_facets(path)
    if facets <= 0:
        raise MeshPreflightError("ASCII STL 中没有 facet normal 三角面。")
    quality = _quality_or_skipped(
        path,
        file_bytes=file_bytes,
        triangles=facets,
        binary=False,
    )
    return facets * 3, facets, quality


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


def _quality_or_skipped(
    path: Path,
    *,
    file_bytes: int,
    triangles: int,
    binary: bool,
) -> MeshQualityReport:
    if file_bytes > MESH_QUALITY_MAX_FILE_BYTES:
        return MeshQualityReport(
            analyzed=False,
            triangle_count=triangles,
            notes=(
                "文件超过 Mesh Doctor 的 256 MiB 深度检查预算；"
                "资源预检仍然有效。",
            ),
        )
    if triangles > MESH_QUALITY_MAX_TRIANGLES:
        return MeshQualityReport(
            analyzed=False,
            triangle_count=triangles,
            notes=(
                f"三角形超过 Mesh Doctor 的 "
                f"{MESH_QUALITY_MAX_TRIANGLES:,} 面深度检查预算；"
                "资源预检仍然有效。",
            ),
        )
    accumulator = _StlQualityAccumulator()
    try:
        records = (
            _binary_stl_records(path, triangles)
            if binary
            else _ascii_stl_records(path)
        )
        for normal, vertices in records:
            accumulator.add(normal, vertices)
    except (OSError, UnicodeError, ValueError, struct.error) as exc:
        return MeshQualityReport(
            analyzed=False,
            triangle_count=triangles,
            notes=(f"拓扑质量扫描未完成：{exc}",),
        )
    if accumulator.triangle_count != triangles:
        return MeshQualityReport(
            analyzed=False,
            triangle_count=triangles,
            notes=(
                "STL 声明的三角形数量与可解析记录不一致，"
                "未给出拓扑结论。",
            ),
        )
    return accumulator.report()


_Vec3 = tuple[float, float, float]
_Triangle = tuple[_Vec3, _Vec3, _Vec3]
_Edge = tuple[_Vec3, _Vec3]


class _StlQualityAccumulator:
    def __init__(self) -> None:
        self.triangle_count = 0
        self.vertices: set[_Vec3] = set()
        self.faces: set[tuple[_Vec3, _Vec3, _Vec3]] = set()
        self.edges: dict[_Edge, list[int]] = {}
        self.parents: list[int] = []
        self.valid_face_indices: list[int] = []
        self.degenerate = 0
        self.duplicates = 0
        self.normal_mismatches = 0
        self.invalid_normals = 0
        self.area = 0.0
        self.face_volumes: dict[int, float] = {}
        self.minimum = [float("inf")] * 3
        self.maximum = [float("-inf")] * 3

    def add(self, normal: _Vec3, vertices: _Triangle) -> None:
        index = self.triangle_count
        self.triangle_count += 1
        self.parents.append(index)
        if not all(isfinite(value) for vertex in vertices for value in vertex):
            self.degenerate += 1
            return

        for vertex in vertices:
            self.vertices.add(vertex)
            for axis, value in enumerate(vertex):
                self.minimum[axis] = min(self.minimum[axis], value)
                self.maximum[axis] = max(self.maximum[axis], value)

        face = tuple(sorted(vertices))
        if face in self.faces:
            self.duplicates += 1
        else:
            self.faces.add(face)

        vector_a = _subtract(vertices[1], vertices[0])
        vector_b = _subtract(vertices[2], vertices[0])
        cross = _cross(vector_a, vector_b)
        cross_length = sqrt(_dot(cross, cross))
        edge_scale = max(
            _dot(vector_a, vector_a),
            _dot(vector_b, vector_b),
            _dot(_subtract(vertices[2], vertices[1]), _subtract(vertices[2], vertices[1])),
        )
        is_degenerate = (
            len(set(vertices)) < 3
            or not isfinite(cross_length)
            or not isfinite(edge_scale)
            or cross_length <= edge_scale * 1e-12
        )
        if is_degenerate:
            self.degenerate += 1
        else:
            self.valid_face_indices.append(index)
            self.area += cross_length / 2.0
            self.face_volumes[index] = (
                _dot(vertices[0], _cross(vertices[1], vertices[2])) / 6.0
            )

        normal_length = sqrt(_dot(normal, normal)) if all(
            isfinite(value) for value in normal
        ) else 0.0
        if normal_length <= 0.0:
            self.invalid_normals += 1
        elif cross_length > 0.0:
            alignment = _dot(normal, cross) / (normal_length * cross_length)
            if alignment < 0.5:
                self.normal_mismatches += 1

        if is_degenerate:
            return
        for left, right in (
            (vertices[0], vertices[1]),
            (vertices[1], vertices[2]),
            (vertices[2], vertices[0]),
        ):
            edge, direction = _canonical_edge(left, right)
            record = self.edges.get(edge)
            if record is None:
                self.edges[edge] = [1, direction, index]
            else:
                record[0] += 1
                record[1] += direction
                self._union(index, record[2])

    def report(self) -> MeshQualityReport:
        boundary = sum(record[0] == 1 for record in self.edges.values())
        nonmanifold = sum(record[0] > 2 for record in self.edges.values())
        inconsistent = sum(
            record[0] == 2 and abs(record[1]) == 2
            for record in self.edges.values()
        )
        components = len(
            {self._find(index) for index in self.valid_face_indices}
        )
        watertight = (
            bool(self.valid_face_indices)
            and boundary == 0
            and nonmanifold == 0
            and inconsistent == 0
            and self.duplicates == 0
            and self.degenerate == 0
        )
        component_volumes: dict[int, float] = {}
        if watertight:
            for index, value in self.face_volumes.items():
                root = self._find(index)
                component_volumes[root] = component_volumes.get(root, 0.0) + value
        notes: list[str] = []
        if self.degenerate:
            notes.append(f"发现 {self.degenerate:,} 个退化或非有限三角形。")
        if self.duplicates:
            notes.append(f"发现 {self.duplicates:,} 个重复三角形。")
        if nonmanifold:
            notes.append(f"发现 {nonmanifold:,} 条被三个以上面共享的非流形边。")
        if inconsistent:
            notes.append(f"发现 {inconsistent:,} 条相邻面方向不一致的边。")
        if boundary:
            notes.append(
                f"表面开放，共有 {boundary:,} 条边界边；"
                "地形或片状边界可能本来就允许开放。"
            )
        if components > 1:
            notes.append(f"模型包含 {components:,} 个互不连通的表面分量。")
        if self.normal_mismatches:
            notes.append(
                f"{self.normal_mismatches:,} 个 STL 面法向与顶点绕序不一致。"
            )
        if self.invalid_normals:
            notes.append(f"{self.invalid_normals:,} 个 STL 面法向为零或无效。")
        bounds = (
            (
                self.minimum[0],
                self.maximum[0],
                self.minimum[1],
                self.maximum[1],
                self.minimum[2],
                self.maximum[2],
            )
            if self.vertices
            else None
        )
        return MeshQualityReport(
            analyzed=True,
            triangle_count=self.triangle_count,
            unique_vertices=len(self.vertices),
            boundary_edges=boundary,
            nonmanifold_edges=nonmanifold,
            inconsistent_edges=inconsistent,
            duplicate_triangles=self.duplicates,
            degenerate_triangles=self.degenerate,
            connected_components=components,
            normal_mismatches=self.normal_mismatches,
            invalid_normals=self.invalid_normals,
            surface_area=self.area,
            enclosed_volume=(
                sum(abs(value) for value in component_volumes.values())
                if watertight
                else None
            ),
            bounds=bounds,
            notes=tuple(notes),
        )

    def _find(self, index: int) -> int:
        parent = self.parents[index]
        if parent != index:
            self.parents[index] = self._find(parent)
        return self.parents[index]

    def _union(self, left: int, right: int) -> None:
        left_root = self._find(left)
        right_root = self._find(right)
        if left_root != right_root:
            self.parents[right_root] = left_root


def _binary_stl_records(
    path: Path,
    triangles: int,
):
    with path.open("rb") as stream:
        stream.seek(84)
        for _index in range(triangles):
            payload = stream.read(50)
            if len(payload) != 50:
                raise ValueError("二进制 STL 三角形记录提前结束")
            values = struct.unpack("<12fH", payload)
            yield (
                (values[0], values[1], values[2]),
                (
                    (values[3], values[4], values[5]),
                    (values[6], values[7], values[8]),
                    (values[9], values[10], values[11]),
                ),
            )


def _ascii_stl_records(path: Path):
    normal: _Vec3 | None = None
    vertices: list[_Vec3] = []
    with path.open("r", encoding="ascii", errors="strict") as stream:
        for line in stream:
            fields = line.strip().split()
            if len(fields) >= 5 and fields[0].casefold() == "facet" and fields[1].casefold() == "normal":
                normal = tuple(float(value) for value in fields[2:5])  # type: ignore[assignment]
                vertices = []
            elif len(fields) >= 4 and fields[0].casefold() == "vertex":
                vertices.append(
                    tuple(float(value) for value in fields[1:4])  # type: ignore[arg-type]
                )
            elif fields and fields[0].casefold() == "endfacet":
                if normal is None or len(vertices) != 3:
                    raise ValueError("ASCII STL facet 缺少一个法向或三个顶点")
                yield normal, (vertices[0], vertices[1], vertices[2])
                normal = None
                vertices = []


def _subtract(left: _Vec3, right: _Vec3) -> _Vec3:
    return (
        left[0] - right[0],
        left[1] - right[1],
        left[2] - right[2],
    )


def _cross(left: _Vec3, right: _Vec3) -> _Vec3:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _dot(left: _Vec3, right: _Vec3) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _canonical_edge(left: _Vec3, right: _Vec3) -> tuple[_Edge, int]:
    if left <= right:
        return (left, right), 1
    return (right, left), -1


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
