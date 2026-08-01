from __future__ import annotations

from pathlib import Path
import struct

import pytest

from dualsphysics_studio.core import mesh_guard
from dualsphysics_studio.core.mesh_guard import (
    MeshPreflightError,
    MeshRisk,
    inspect_mesh_file,
    validate_loaded_dataset,
)


def _write_ascii_stl(path: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "solid triangle",
                "  facet normal 0 0 1",
                "    outer loop",
                "      vertex 0 0 0",
                "      vertex 1 0 0",
                "      vertex 0 1 0",
                "    endloop",
                "  endfacet",
                "endsolid triangle",
            )
        ),
        encoding="ascii",
    )


def _write_binary_stl(path: Path, triangles: int = 2) -> None:
    header = b"DualSPHysics Studio".ljust(80, b"\0")
    triangle = struct.pack(
        "<12fH",
        0,
        0,
        1,
        0,
        0,
        0,
        1,
        0,
        0,
        0,
        1,
        0,
        0,
    )
    path.write_bytes(header + struct.pack("<I", triangles) + triangle * triangles)


def _write_tetrahedron_stl(path: Path) -> None:
    triangles = (
        ((0, 0, -1), ((0, 0, 0), (0, 1, 0), (1, 0, 0))),
        ((0, -1, 0), ((0, 0, 0), (1, 0, 0), (0, 0, 1))),
        ((-1, 0, 0), ((0, 0, 0), (0, 0, 1), (0, 1, 0))),
        ((1, 1, 1), ((1, 0, 0), (0, 1, 0), (0, 0, 1))),
    )
    lines = ["solid tetrahedron"]
    for normal, vertices in triangles:
        lines.append(f"facet normal {normal[0]} {normal[1]} {normal[2]}")
        lines.append("outer loop")
        lines.extend(
            f"vertex {vertex[0]} {vertex[1]} {vertex[2]}"
            for vertex in vertices
        )
        lines.extend(("endloop", "endfacet"))
    lines.append("endsolid tetrahedron")
    path.write_text("\n".join(lines), encoding="ascii")


def test_ascii_and_binary_stl_are_counted_without_vtk(tmp_path: Path) -> None:
    ascii_path = tmp_path / "ascii.stl"
    binary_path = tmp_path / "binary.stl"
    _write_ascii_stl(ascii_path)
    _write_binary_stl(binary_path)

    ascii_report = inspect_mesh_file(ascii_path)
    binary_report = inspect_mesh_file(binary_path)

    assert ascii_report.risk is MeshRisk.SAFE
    assert ascii_report.estimated_points == 3
    assert ascii_report.estimated_cells == 1
    assert binary_report.estimated_points == 6
    assert binary_report.estimated_cells == 2


def test_mesh_doctor_reports_open_surface_without_forcing_confirmation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "open.stl"
    _write_ascii_stl(target)

    report = inspect_mesh_file(target)

    assert report.quality is not None
    assert report.quality.analyzed
    assert report.quality.boundary_edges == 3
    assert report.quality.watertight is False
    assert not report.quality.requires_confirmation
    assert not report.requires_confirmation
    assert "开放表面" in report.quality.short_status()
    assert "Mesh Doctor" in report.summary()


def test_mesh_doctor_degeneracy_tolerance_is_scale_relative(
    tmp_path: Path,
) -> None:
    target = tmp_path / "tiny.stl"
    target.write_text(
        "solid tiny\n"
        "facet normal 0 0 1\n"
        "outer loop\n"
        "vertex 0 0 0\n"
        "vertex 0.000000001 0 0\n"
        "vertex 0 0.000000001 0\n"
        "endloop\n"
        "endfacet\n"
        "endsolid tiny\n",
        encoding="ascii",
    )

    quality = inspect_mesh_file(target).quality

    assert quality is not None
    assert quality.degenerate_triangles == 0
    assert quality.surface_area == pytest.approx(5e-19)


def test_mesh_doctor_reports_watertight_tetrahedron_volume(tmp_path: Path) -> None:
    target = tmp_path / "tetrahedron.stl"
    _write_tetrahedron_stl(target)

    quality = inspect_mesh_file(target).quality

    assert quality is not None
    assert quality.watertight
    assert quality.boundary_edges == 0
    assert quality.nonmanifold_edges == 0
    assert quality.connected_components == 1
    assert quality.degenerate_triangles == 0
    assert quality.enclosed_volume == pytest.approx(1 / 6)


def test_mesh_doctor_sums_oppositely_oriented_closed_components(
    tmp_path: Path,
) -> None:
    target = tmp_path / "two-tetrahedra.stl"
    faces = (
        ((0, 0, 0), (0, 1, 0), (1, 0, 0)),
        ((0, 0, 0), (1, 0, 0), (0, 0, 1)),
        ((0, 0, 0), (0, 0, 1), (0, 1, 0)),
        ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    )
    records: list[bytes] = []
    for face in faces:
        records.append(
            struct.pack(
                "<12fH",
                0,
                0,
                0,
                *(value for vertex in face for value in vertex),
                0,
            )
        )
        reversed_shifted = tuple(
            (vertex[0] + 2, vertex[1], vertex[2])
            for vertex in (face[0], face[2], face[1])
        )
        records.append(
            struct.pack(
                "<12fH",
                0,
                0,
                0,
                *(value for vertex in reversed_shifted for value in vertex),
                0,
            )
        )
    target.write_bytes(
        b"two tetrahedra".ljust(80, b"\0")
        + struct.pack("<I", len(records))
        + b"".join(records)
    )

    quality = inspect_mesh_file(target).quality

    assert quality is not None
    assert quality.watertight
    assert quality.connected_components == 2
    assert quality.enclosed_volume == pytest.approx(1 / 3)


def test_mesh_doctor_requires_confirmation_for_duplicate_faces(
    tmp_path: Path,
) -> None:
    target = tmp_path / "duplicates.stl"
    _write_binary_stl(target, triangles=2)

    report = inspect_mesh_file(target)

    assert report.risk is MeshRisk.SAFE
    assert report.quality is not None
    assert report.quality.duplicate_triangles == 1
    assert report.quality.requires_confirmation
    assert report.requires_confirmation


def test_mesh_doctor_skips_deep_scan_above_triangle_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "budget.stl"
    _write_binary_stl(target, triangles=2)
    monkeypatch.setattr(mesh_guard, "MESH_QUALITY_MAX_TRIANGLES", 1)

    quality = inspect_mesh_file(target).quality

    assert quality is not None
    assert not quality.analyzed
    assert "1 面" in quality.summary()


def test_legacy_vtk_header_reports_points_and_cells(tmp_path: Path) -> None:
    target = tmp_path / "particles.vtk"
    target.write_text(
        "\n".join(
            (
                "# vtk DataFile Version 3.0",
                "sample",
                "ASCII",
                "DATASET POLYDATA",
                "POINTS 3 float",
                "0 0 0 1 0 0 0 1 0",
                "POLYGONS 1 4",
                "3 0 1 2",
            )
        ),
        encoding="ascii",
    )

    report = inspect_mesh_file(target)

    assert report.kind == "vtk"
    assert report.estimated_points == 3
    assert report.estimated_cells == 1
    assert "预计点数：3" in report.summary()
    assert report.quality is not None
    assert not report.quality.analyzed


def test_soft_budget_warns_and_hard_budget_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "mesh.stl"
    _write_ascii_stl(target)

    monkeypatch.setattr(mesh_guard, "MESH_FILE_WARNING_BYTES", 100)
    warning = inspect_mesh_file(target)
    assert warning.risk is MeshRisk.WARNING
    assert warning.requires_confirmation

    monkeypatch.setattr(mesh_guard, "MAX_MESH_FILE_BYTES", 120)
    blocked = inspect_mesh_file(target)
    assert blocked.risk is MeshRisk.BLOCKED
    assert blocked.blocked


def test_damaged_meshes_fail_before_vtk_allocates(tmp_path: Path) -> None:
    target = tmp_path / "damaged.stl"
    target.write_bytes(b"not an STL")

    with pytest.raises(MeshPreflightError, match="不是完整的二进制"):
        inspect_mesh_file(target)


class _Dataset:
    def __init__(self, points: int, cells: int, bounds) -> None:
        self._points = points
        self._cells = cells
        self._bounds = bounds

    def GetNumberOfPoints(self) -> int:
        return self._points

    def GetNumberOfCells(self) -> int:
        return self._cells

    def GetBounds(self):
        return self._bounds


def test_loaded_dataset_rejects_nonfinite_bounds() -> None:
    with pytest.raises(MeshPreflightError, match="NaN"):
        validate_loaded_dataset(_Dataset(3, 1, (0, 1, 0, 1, 0, float("nan"))))

    stats = validate_loaded_dataset(_Dataset(3, 1, (0, 1, 0, 1, 0, 1)))
    assert stats.points == 3
    assert stats.cells == 1
