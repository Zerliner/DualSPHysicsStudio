from __future__ import annotations

import json
from pathlib import Path

from dlc_tools.mesh_doctor.app import main, write_mesh_report
from dualsphysics_studio.core.mesh_guard import inspect_mesh_file


def _write_triangle(path: Path) -> None:
    path.write_text(
        "solid test\n"
        "facet normal 0 0 1\n"
        "outer loop\n"
        "vertex 0 0 0\n"
        "vertex 1 0 0\n"
        "vertex 0 1 0\n"
        "endloop\n"
        "endfacet\n"
        "endsolid test\n",
        encoding="ascii",
    )


def test_mesh_doctor_exports_structured_atomic_report(tmp_path: Path) -> None:
    mesh = tmp_path / "triangle.stl"
    _write_triangle(mesh)

    target = write_mesh_report(
        inspect_mesh_file(mesh),
        tmp_path / "mesh-doctor.json",
    )
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["kind"] == "stl"
    assert payload["quality"]["analyzed"]
    assert payload["quality"]["boundary_edges"] == 3
    assert payload["quality"]["watertight"] is False
    assert not list(tmp_path.glob(".mesh-doctor.json.*.tmp"))


def test_mesh_doctor_cli_smoke_test_does_not_open_gui() -> None:
    assert main(["--smoke-test"]) == 0
