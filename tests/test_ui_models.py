from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pytest

from dualsphysics_studio.ui.models import (
    box_spec,
    build_scene_entries,
    diagnostics_to_text,
    normalize_category,
    parse_editor_value,
    prism_faces,
    property_rows,
    vertices_of,
)


class Role(str, Enum):
    FLUID = "fluid"
    FIXED = "fixed_boundary"
    FLOATING = "floating"


@dataclass(slots=True)
class Point:
    x: float
    y: float
    z: float


@dataclass(slots=True)
class DummySceneObject:
    id: str
    kind: str
    role: Role
    marker: int
    shapeout: str
    vertices: tuple[Point, ...]
    editable: bool = True

    @property
    def name(self) -> str:
        return self.shapeout

    @property
    def is_floating(self) -> bool:
        return self.role is Role.FLOATING


@dataclass(slots=True)
class Bounds:
    minimum: Point
    maximum: Point


def test_category_normalization_handles_dualsphysics_roles() -> None:
    assert normalize_category("mkfluid") == "fluid"
    assert normalize_category("fixed_boundary") == "fixed"
    assert normalize_category("rigid body") == "floating"
    assert normalize_category("expression") == "other"


def test_scene_entries_group_identity_and_duplicate_keys() -> None:
    points = (Point(0, 0, 0), Point(1, 1, 1))
    first = DummySceneObject("same", "drawbox", Role.FLUID, 0, "Water", points)
    second = DummySceneObject("same", "drawbox", Role.FIXED, 0, "Tank", points)

    entries = build_scene_entries([first, second])

    assert [entry.category for entry in entries] == ["fluid", "fixed"]
    assert entries[0].name == "Water"
    assert entries[0].kind == "box"
    assert entries[0].key == "same"
    assert entries[1].key == "same#2"


def test_box_spec_accepts_corner_size_and_vtk_bounds() -> None:
    corner_box = {"kind": "box", "point": {"x": 1, "y": 2, "z": 3}, "size": (2, 4, 6)}
    spec = box_spec(corner_box)
    assert spec is not None
    assert spec.center == (2.0, 4.0, 6.0)
    assert spec.size == (2.0, 4.0, 6.0)

    bounded = box_spec({"bounds": (-1, 3, 2, 6, 10, 14), "size": (1, 1, 1)})
    assert bounded is not None
    assert bounded.center == (1.0, 4.0, 12.0)
    assert bounded.size == (4.0, 4.0, 4.0)

    object_bounds = box_spec({"bounds": Bounds(Point(0, 1, 2), Point(2, 5, 8))})
    assert object_bounds is not None
    assert object_bounds.center == (1.0, 3.0, 5.0)
    assert object_bounds.size == (2.0, 4.0, 6.0)


def test_invalid_box_expressions_degrade_without_guessing() -> None:
    assert box_spec({"point": (0, 0, 0), "size": ("$L", 1, 1)}) is None
    assert box_spec({"point": (0, 0, 0), "size": (1, 0, 1)}) is None


def test_dualsphysics_prism_faces_connect_corresponding_sections() -> None:
    faces = prism_faces(8)
    assert faces[0] == (3, 2, 1, 0)
    assert faces[1] == (4, 5, 6, 7)
    assert faces[2:] == [
        (0, 1, 5, 4),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (3, 0, 4, 7),
    ]
    assert prism_faces(5) == []


def test_vertices_accept_mapping_sequence_and_xyz_objects() -> None:
    obj = {
        "points": [
            {"x": 1, "y": 2, "z": 3},
            (4, 5, 6),
            Point(7, 8, 9),
            {"x": "expression", "y": 0, "z": 0},
        ]
    }
    assert vertices_of(obj) == [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0)]


def test_property_rows_are_chinese_first_and_do_not_expand_vertices() -> None:
    obj = DummySceneObject(
        "water",
        "drawprism",
        Role.FLUID,
        1,
        "Water",
        (Point(0, 0, 0), Point(1, 0, 0), Point(0, 1, 0)),
    )
    rows = property_rows(obj)
    labels = {field: label for field, label, _display, _raw in rows}
    displays = {field: display for field, _label, display, _raw in rows}
    assert labels["shapeout"] == "ShapeOut"
    assert labels["marker"] == "Marker"
    assert displays["vertices"] == "3 个点"


def test_diagnostics_format_severity_and_location() -> None:
    diagnostics = [
        {"severity": "warning", "message": "Shifting=0 时系数不生效", "path": "/case/execution"},
        {"severity": "error", "message": "marker 重复", "line": 42},
    ]
    text = diagnostics_to_text(diagnostics)
    assert "[警告] Shifting=0 时系数不生效" in text
    assert "位置：/case/execution" in text
    assert "[错误] marker 重复" in text
    assert "第 42 行" in text
    assert diagnostics_to_text([]) == "未发现需要注意的问题。"


def test_property_editor_conservative_type_conversion() -> None:
    assert parse_editor_value("12", 1) == 12
    assert parse_editor_value("0.025", 1.0) == pytest.approx(0.025)
    assert parse_editor_value("是", False) is True
    assert parse_editor_value("shape", "old") == "shape"
    with pytest.raises(ValueError):
        parse_editor_value("1, 2, 3", (0, 0, 0))
