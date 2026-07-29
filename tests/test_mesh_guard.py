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
