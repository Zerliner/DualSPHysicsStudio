"""Small dependency-free 3-D geometry helpers."""

from __future__ import annotations

from collections.abc import Sequence
from math import cos, isfinite, pi, sin

from .models import Vec3


def centroid(points: tuple[Vec3, ...] | list[Vec3]) -> Vec3:
    if not points:
        raise ValueError("至少需要一个点")
    count = float(len(points))
    return Vec3(
        sum(point.x for point in points) / count,
        sum(point.y for point in points) / count,
        sum(point.z for point in points) / count,
    )


def prism_mass_properties(points: Sequence[Vec3]) -> tuple[float, Vec3]:
    """Return the signed volume and volume centroid of a closed drawprism.

    Faces use GenCase's two-corresponding-sections ordering.  Non-planar
    quadrilateral faces are triangulated consistently with the Studio preview.
    """

    vertices = tuple(points)
    if len(vertices) < 6 or len(vertices) % 2:
        raise ValueError("drawprism 顶点数量必须是大于等于 6 的偶数")
    half = len(vertices) // 2
    faces = [
        tuple(range(half - 1, -1, -1)),
        tuple(range(half, len(vertices))),
        *[
            (index, (index + 1) % half, (index + 1) % half + half, index + half)
            for index in range(half)
        ],
    ]
    reference = centroid(list(vertices))
    span = max(
        max(point[axis] for point in vertices)
        - min(point[axis] for point in vertices)
        for axis in range(3)
    )
    area_tolerance = max(span * span * 1e-12, 1e-24)
    volume_tolerance = max(span * span * span * 1e-12, 1e-30)
    signed_volume = 0.0
    moment = Vec3(0.0, 0.0, 0.0)
    for face in faces:
        anchor = vertices[face[0]]
        for index in range(1, len(face) - 1):
            second = vertices[face[index]]
            third = vertices[face[index + 1]]
            edge_a = second - anchor
            edge_b = third - anchor
            normal = Vec3(
                edge_a.y * edge_b.z - edge_a.z * edge_b.y,
                edge_a.z * edge_b.x - edge_a.x * edge_b.z,
                edge_a.x * edge_b.y - edge_a.y * edge_b.x,
            )
            if normal.length() <= area_tolerance:
                raise ValueError("顶点修改会产生零面积棱柱面")
            relative_anchor = anchor - reference
            relative_second = second - reference
            relative_third = third - reference
            cross = Vec3(
                relative_second.y * relative_third.z
                - relative_second.z * relative_third.y,
                relative_second.z * relative_third.x
                - relative_second.x * relative_third.z,
                relative_second.x * relative_third.y
                - relative_second.y * relative_third.x,
            )
            tetra_volume = (
                relative_anchor.x * cross.x
                + relative_anchor.y * cross.y
                + relative_anchor.z * cross.z
            ) / 6.0
            tetra_center = (reference + anchor + second + third) * 0.25
            signed_volume += tetra_volume
            moment = moment + tetra_center * tetra_volume
    if not isfinite(signed_volume) or abs(signed_volume) <= volume_tolerance:
        raise ValueError("顶点修改会产生零体积或数值无效的棱柱")
    center = moment * (1.0 / signed_volume)
    if not all(isfinite(component) for component in center):
        raise ValueError("无法计算修改后棱柱的有效体积质心")
    return signed_volume, center


def axis_vector(axis: str | Vec3 | tuple[float, float, float] | list[float]) -> Vec3:
    if isinstance(axis, str):
        values = {
            "x": Vec3(1.0, 0.0, 0.0),
            "y": Vec3(0.0, 1.0, 0.0),
            "z": Vec3(0.0, 0.0, 1.0),
        }
        try:
            return values[axis.lower()]
        except KeyError as exc:
            raise ValueError("旋转轴必须是 x、y、z 或三维向量") from exc
    return Vec3.from_value(axis)


def rotate_point(
    point: Vec3,
    angle_degrees: float,
    axis: str | Vec3 | tuple[float, float, float] | list[float] = "z",
    pivot: Vec3 | tuple[float, float, float] | list[float] = Vec3(0.0, 0.0, 0.0),
) -> Vec3:
    """Rotate a point using Rodrigues' formula."""

    vector = axis_vector(axis)
    length = vector.length()
    if length == 0:
        raise ValueError("旋转轴长度不能为 0")
    unit = Vec3(vector.x / length, vector.y / length, vector.z / length)
    center = Vec3.from_value(pivot)
    relative = point - center
    radians = float(angle_degrees) * pi / 180.0
    cosine = cos(radians)
    sine = sin(radians)
    dot = relative.x * unit.x + relative.y * unit.y + relative.z * unit.z
    cross = Vec3(
        unit.y * relative.z - unit.z * relative.y,
        unit.z * relative.x - unit.x * relative.z,
        unit.x * relative.y - unit.y * relative.x,
    )
    rotated = relative * cosine + cross * sine + unit * (dot * (1.0 - cosine))
    return rotated + center


def scale_point(point: Vec3, factors: Vec3, pivot: Vec3) -> Vec3:
    relative = point - pivot
    return Vec3(
        pivot.x + relative.x * factors.x,
        pivot.y + relative.y * factors.y,
        pivot.z + relative.z * factors.z,
    )


def snap_point(point: Vec3, spacing: float) -> Vec3:
    if spacing <= 0:
        raise ValueError("吸附间距必须大于 0")
    return Vec3(
        round(point.x / spacing) * spacing,
        round(point.y / spacing) * spacing,
        round(point.z / spacing) * spacing,
    )
