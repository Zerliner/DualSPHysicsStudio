"""Public data types for the editor-independent document model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import sqrt
from pathlib import Path
from typing import Any, Iterator


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class GeometryType(str, Enum):
    DRAWBOX = "drawbox"
    DRAWPRISM = "drawprism"


class MarkerKind(str, Enum):
    FLUID = "fluid"
    BOUND = "bound"


class ObjectRole(str, Enum):
    FLUID = "fluid"
    FIXED_BOUNDARY = "fixed_boundary"
    FLOATING = "floating"


@dataclass(frozen=True, slots=True)
class Vec3:
    x: float
    y: float
    z: float

    @classmethod
    def from_value(cls, value: "Vec3 | tuple[float, float, float] | list[float]") -> "Vec3":
        if isinstance(value, cls):
            return value
        if len(value) != 3:
            raise ValueError("三维坐标必须包含 x、y、z 三个值")
        return cls(float(value[0]), float(value[1]), float(value[2]))

    def __iter__(self) -> Iterator[float]:
        yield self.x
        yield self.y
        yield self.z

    def __getitem__(self, index: int) -> float:
        return (self.x, self.y, self.z)[index]

    def __add__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: "Vec3") -> "Vec3":
        return Vec3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, factor: float) -> "Vec3":
        return Vec3(self.x * factor, self.y * factor, self.z * factor)

    def length(self) -> float:
        return sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


@dataclass(frozen=True, slots=True)
class BoundingBox:
    minimum: Vec3
    maximum: Vec3

    @classmethod
    def from_points(cls, points: tuple[Vec3, ...] | list[Vec3]) -> "BoundingBox":
        if not points:
            raise ValueError("至少需要一个点才能计算包围盒")
        return cls(
            Vec3(
                min(point.x for point in points),
                min(point.y for point in points),
                min(point.z for point in points),
            ),
            Vec3(
                max(point.x for point in points),
                max(point.y for point in points),
                max(point.z for point in points),
            ),
        )

    @property
    def size(self) -> Vec3:
        return self.maximum - self.minimum

    @property
    def center(self) -> Vec3:
        return (self.minimum + self.maximum) * 0.5


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    severity: Severity
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class XmlParameterField:
    """One existing scalar XML field exposed by the parameter workbench."""

    key: str
    category: str
    element_path: str
    attribute: str
    value: str

    @property
    def id(self) -> str:
        return f"{self.element_path}@{self.attribute}"


@dataclass(frozen=True, slots=True)
class SceneObject:
    """A directly editable ``drawbox`` or ``drawprism`` command.

    ``element_path`` and the child paths are stable XPath-like source locators.
    They intentionally use one-based sibling indices and do not depend on an XML
    library's object identity.
    """

    id: str
    geometry_type: GeometryType
    marker_kind: MarkerKind | None
    marker: int | None
    role: ObjectRole
    element_path: str
    marker_path: str | None
    vertices: tuple[Vec3, ...]
    point_paths: tuple[str, ...]
    size_path: str | None = None
    shapeout: str | None = None
    shapeout_path: str | None = None
    box_fill: str | None = None
    floating_path: str | None = None
    floating_center_path: str | None = None
    floating_center: Vec3 | None = None
    editable: bool = True
    readonly_reason: str | None = None
    source_order: int = 0

    @property
    def name(self) -> str:
        if self.shapeout:
            return self.shapeout
        marker = "?" if self.marker is None else str(self.marker)
        return f"{self.geometry_type.value} mk={marker}"

    @property
    def kind(self) -> str:
        return "box" if self.geometry_type is GeometryType.DRAWBOX else "prism"

    @property
    def is_floating(self) -> bool:
        return self.role is ObjectRole.FLOATING

    @property
    def bounds(self) -> BoundingBox | None:
        return BoundingBox.from_points(self.vertices) if self.vertices else None

    @property
    def bounding_box(self) -> BoundingBox | None:
        return self.bounds

    @property
    def center(self) -> Vec3 | None:
        bounds = self.bounds
        return bounds.center if bounds else None

    @property
    def position(self) -> Vec3 | None:
        if not self.vertices:
            return None
        if self.geometry_type is GeometryType.DRAWBOX:
            return self.vertices[0]
        return self.center

    @property
    def size(self) -> Vec3 | None:
        bounds = self.bounds
        return bounds.size if bounds else None


@dataclass(frozen=True, slots=True)
class DocumentFacts:
    dp: float | None
    smoothing_length: float | None
    sound_speed: float | None
    definition_bounds: BoundingBox | None
    object_count: int
    floating_count: int


@dataclass(frozen=True, slots=True)
class SaveResult:
    path: Path
    backup_path: Path | None
    bytes_written: int


@dataclass(frozen=True, slots=True)
class NodeExplanation:
    title: str
    summary: str
    units: str | None = None
    effective: bool | None = None
    reliability: str = "known"
    notes: tuple[str, ...] = ()
