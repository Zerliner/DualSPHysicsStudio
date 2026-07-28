"""Interpret the editable subset of DualSPHysics 5.4 XML."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import re
import xml.etree.ElementTree as ET

from .models import (
    BoundingBox,
    Diagnostic,
    DocumentFacts,
    GeometryType,
    MarkerKind,
    ObjectRole,
    SceneObject,
    Severity,
    Vec3,
)
from .source import build_element_paths, is_element, iter_elements, local_name


_PLAIN_NUMBER = re.compile(
    r"^[\t\r\n ]*[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[Ee][+-]?\d+)?[\t\r\n ]*$"
)
_PRISM_MASK_LIST = re.compile(r"^[\t\r\n ]*(?:0|[1-9]\d*(?:[\t\r\n ]*\|[\t\r\n ]*[1-9]\d*)*)[\t\r\n ]*$")


@dataclass(slots=True)
class SemanticModel:
    objects: tuple[SceneObject, ...]
    diagnostics: tuple[Diagnostic, ...]
    facts: DocumentFacts
    path_by_identity: dict[int, str]
    element_by_path: dict[str, ET.Element]


@dataclass(frozen=True, slots=True)
class _FloatingInfo:
    marker: int
    path: str
    center_path: str | None
    center: Vec3 | None


def parse_number(value: str | None) -> float | None:
    if value is None or not _PLAIN_NUMBER.match(value):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_integer(value: str | None) -> int | None:
    number = parse_number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def parse_vec3(element: ET.Element | None) -> Vec3 | None:
    if element is None:
        return None
    values = tuple(parse_number(element.get(axis)) for axis in ("x", "y", "z"))
    if any(value is None for value in values):
        return None
    return Vec3(values[0], values[1], values[2])  # type: ignore[arg-type]


def analyze(root: ET.Element) -> SemanticModel:
    path_by_identity, element_by_path = build_element_paths(root)
    diagnostics: list[Diagnostic] = []

    def path_of(element: ET.Element | None) -> str | None:
        return path_by_identity.get(id(element)) if element is not None else None

    definition = next(iter(iter_elements(root, "definition")), None)
    dp = parse_number(definition.get("dp")) if definition is not None else None
    if definition is None:
        diagnostics.append(
            Diagnostic("MISSING_DEFINITION", Severity.ERROR, "缺少 geometry/definition 节点")
        )
    elif dp is None or dp <= 0:
        diagnostics.append(
            Diagnostic(
                "INVALID_DP",
                Severity.ERROR,
                "粒子间距 dp 必须是大于 0 的数值；表达式对象只能在源码中编辑",
                path_of(definition),
            )
        )

    definition_bounds: BoundingBox | None = None
    if definition is not None:
        point_min = _direct_child(definition, "pointmin")
        point_max = _direct_child(definition, "pointmax")
        minimum = parse_vec3(point_min)
        maximum = parse_vec3(point_max)
        if minimum is not None and maximum is not None:
            definition_bounds = BoundingBox(minimum, maximum)

    floating_by_marker: dict[int, list[_FloatingInfo]] = {}
    for floating in iter_elements(root, "floating"):
        floating_path = path_of(floating)
        marker = parse_integer(floating.get("mkbound"))
        if marker is None:
            diagnostics.append(
                Diagnostic(
                    "INVALID_FLOATING_MARKER",
                    Severity.ERROR,
                    "floating.mkbound 必须是整数；该关联只能在源码中编辑",
                    floating_path,
                )
            )
            continue
        center_element = _direct_child(floating, "center")
        center = parse_vec3(center_element)
        if center_element is None or center is None:
            diagnostics.append(
                Diagnostic(
                    "INVALID_FLOATING_CENTER",
                    Severity.WARNING,
                    f"浮体 mkbound={marker} 缺少可解析的 center",
                    floating_path,
                )
            )
        info = _FloatingInfo(marker, floating_path or "", path_of(center_element), center)
        floating_by_marker.setdefault(marker, []).append(info)

    for marker, entries in floating_by_marker.items():
        if len(entries) > 1:
            diagnostics.append(
                Diagnostic(
                    "DUPLICATE_FLOATING_MARKER",
                    Severity.ERROR,
                    f"mkbound={marker} 被 {len(entries)} 个 floating 重复引用，图形编辑已禁用",
                    entries[0].path,
                )
            )

    objects: list[SceneObject] = []
    pending_shapeout: list[int] = []
    marker_kind: MarkerKind | None = None
    marker: int | None = None
    marker_path: str | None = None
    marker_is_expression = False
    transform_active = False
    transform_reason: str | None = None

    mainlists = list(iter_elements(root, "mainlist"))
    if not mainlists:
        diagnostics.append(
            Diagnostic("MISSING_MAINLIST", Severity.ERROR, "缺少 geometry/commands/mainlist")
        )

    for mainlist in mainlists:
        for command in list(mainlist):
            if not is_element(command):
                continue
            command_name = local_name(command.tag)
            command_path = path_of(command)

            if command_name in ("setmkfluid", "setmkbound"):
                marker_kind = (
                    MarkerKind.FLUID if command_name == "setmkfluid" else MarkerKind.BOUND
                )
                marker = parse_integer(command.get("mk"))
                marker_path = command_path
                marker_is_expression = marker is None
                if marker is None:
                    diagnostics.append(
                        Diagnostic(
                            "INVALID_MARKER",
                            Severity.ERROR,
                            f"{command_name}.mk 必须是整数；后续对象只能在源码中编辑",
                            command_path,
                        )
                    )
                continue

            if command_name == "matrixreset":
                transform_active = False
                transform_reason = None
                continue
            if command_name in ("move", "rotate", "scale", "matrix", "setmatrix"):
                if not _is_identity_transform(command_name, command):
                    transform_active = True
                    transform_reason = f"对象受前置 {command_name} 状态变换影响"
                continue

            if command_name in ("drawbox", "drawprism"):
                object_index = len(objects)
                object_id = f"object:{object_index}"
                floating_entries = (
                    floating_by_marker.get(marker, [])
                    if marker_kind is MarkerKind.BOUND and marker is not None
                    else []
                )
                floating = floating_entries[0] if len(floating_entries) == 1 else None
                role = (
                    ObjectRole.FLUID
                    if marker_kind is MarkerKind.FLUID
                    else ObjectRole.FLOATING
                    if floating is not None
                    else ObjectRole.FIXED_BOUNDARY
                )
                readonly_reasons: list[str] = []
                if marker_kind is None:
                    readonly_reasons.append("前方没有 setmkfluid/setmkbound")
                    diagnostics.append(
                        Diagnostic(
                            "MISSING_MARKER",
                            Severity.ERROR,
                            "绘制命令前缺少 setmkfluid/setmkbound",
                            command_path,
                        )
                    )
                if marker_is_expression:
                    readonly_reasons.append("marker 使用表达式")
                if len(floating_entries) > 1:
                    readonly_reasons.append("floating 关联不唯一")
                if transform_active:
                    readonly_reasons.append(transform_reason or "使用状态变换命令")

                if command_name == "drawbox":
                    scene = _parse_box(
                        command,
                        command_path or "",
                        object_id,
                        object_index,
                        marker_kind,
                        marker,
                        marker_path,
                        role,
                        floating,
                        path_of,
                        readonly_reasons,
                        diagnostics,
                    )
                else:
                    scene = _parse_prism(
                        command,
                        command_path or "",
                        object_id,
                        object_index,
                        marker_kind,
                        marker,
                        marker_path,
                        role,
                        floating,
                        path_of,
                        readonly_reasons,
                        diagnostics,
                    )
                objects.append(scene)
                pending_shapeout.append(object_index)
                continue

            if command_name == "shapeout":
                filename = command.get("file")
                if not pending_shapeout:
                    diagnostics.append(
                        Diagnostic(
                            "ORPHAN_SHAPEOUT",
                            Severity.WARNING,
                            "shapeout 前没有可关联的 drawbox/drawprism",
                            command_path,
                        )
                    )
                else:
                    for object_index in pending_shapeout:
                        objects[object_index] = replace(
                            objects[object_index],
                            shapeout=filename,
                            shapeout_path=command_path,
                        )
                    pending_shapeout.clear()
                if not filename:
                    diagnostics.append(
                        Diagnostic(
                            "EMPTY_SHAPEOUT",
                            Severity.WARNING,
                            "shapeout.file 为空；GenCase 可能不会生成对应 VTK",
                            command_path,
                        )
                    )
                continue

            if _contains_supported_draw(command):
                diagnostics.append(
                    Diagnostic(
                        "NESTED_GEOMETRY_SOURCE_ONLY",
                        Severity.INFO,
                        f"{command_name} 内嵌的几何命令仅支持源码编辑",
                        command_path,
                    )
                )

    _append_relationship_diagnostics(objects, floating_by_marker, diagnostics)
    _append_shapeout_diagnostics(objects, diagnostics)
    _append_parameter_diagnostics(root, path_by_identity, diagnostics)
    _append_bounds_diagnostics(objects, definition_bounds, dp, diagnostics)
    _append_marker_range_diagnostics(root, objects, path_by_identity, diagnostics)

    coefficient_h = _constant_number(root, "coefh")
    smoothing_length = (
        coefficient_h * math.sqrt(3.0 * dp * dp)
        if coefficient_h is not None and dp is not None and dp > 0
        else None
    )
    speed_sound_value = _constant_number(root, "speedsound")
    speed_sound_element = next(iter(iter_elements(root, "speedsound")), None)
    speed_sound_auto = (
        speed_sound_element is not None
        and speed_sound_element.get("auto", "false").strip().lower() == "true"
    )
    if speed_sound_auto or speed_sound_value in (None, 0.0):
        system_speed = _constant_number(root, "speedsystem")
        coefficient_sound = _constant_number(root, "coefsound")
        sound_speed = (
            system_speed * coefficient_sound
            if system_speed is not None and coefficient_sound is not None
            else None
        )
    else:
        sound_speed = speed_sound_value

    facts = DocumentFacts(
        dp=dp,
        smoothing_length=smoothing_length,
        sound_speed=sound_speed,
        definition_bounds=definition_bounds,
        object_count=len(objects),
        floating_count=sum(len(entries) for entries in floating_by_marker.values()),
    )
    return SemanticModel(
        objects=tuple(objects),
        diagnostics=tuple(diagnostics),
        facts=facts,
        path_by_identity=path_by_identity,
        element_by_path=element_by_path,
    )


def _parse_box(
    command: ET.Element,
    command_path: str,
    object_id: str,
    object_index: int,
    marker_kind: MarkerKind | None,
    marker: int | None,
    marker_path: str | None,
    role: ObjectRole,
    floating: _FloatingInfo | None,
    path_of,
    readonly_reasons: list[str],
    diagnostics: list[Diagnostic],
) -> SceneObject:
    point_element = _direct_child(command, "point")
    size_element = _direct_child(command, "size")
    boxfill_element = _direct_child(command, "boxfill")
    point = parse_vec3(point_element)
    size = parse_vec3(size_element)
    if point_element is None or size_element is None:
        readonly_reasons.append("drawbox 缺少 point 或 size")
        diagnostics.append(
            Diagnostic(
                "MALFORMED_DRAWBOX",
                Severity.ERROR,
                "drawbox 必须包含 point 和 size",
                command_path,
            )
        )
    elif point is None or size is None:
        readonly_reasons.append("坐标或尺寸使用表达式")
        diagnostics.append(
            Diagnostic(
                "EXPRESSION_GEOMETRY",
                Severity.INFO,
                "drawbox 使用变量或表达式，已切换为仅源码编辑",
                command_path,
            )
        )
    elif size.x <= 0 or size.y <= 0 or size.z <= 0:
        readonly_reasons.append("尺寸必须大于 0")
        diagnostics.append(
            Diagnostic(
                "INVALID_BOX_SIZE",
                Severity.ERROR,
                "drawbox 的 x/y/z 尺寸必须全部大于 0",
                path_of(size_element),
            )
        )

    allowed_children = {"boxfill", "point", "size"}
    unsupported = [
        local_name(child.tag)
        for child in list(command)
        if is_element(child) and local_name(child.tag) not in allowed_children
    ]
    if unsupported:
        readonly_reasons.append("包含不受支持的子命令：" + ", ".join(unsupported))

    vertices: tuple[Vec3, ...] = ()
    if point is not None and size is not None:
        vertices = _box_vertices(point, size)
    return SceneObject(
        id=object_id,
        geometry_type=GeometryType.DRAWBOX,
        marker_kind=marker_kind,
        marker=marker,
        role=role,
        element_path=command_path,
        marker_path=marker_path,
        vertices=vertices,
        point_paths=(path_of(point_element),) if point_element is not None else (),
        size_path=path_of(size_element),
        box_fill=(boxfill_element.text or "").strip() if boxfill_element is not None else None,
        floating_path=floating.path if floating else None,
        floating_center_path=floating.center_path if floating else None,
        floating_center=floating.center if floating else None,
        editable=not readonly_reasons,
        readonly_reason="；".join(readonly_reasons) or None,
        source_order=object_index,
    )


def _parse_prism(
    command: ET.Element,
    command_path: str,
    object_id: str,
    object_index: int,
    marker_kind: MarkerKind | None,
    marker: int | None,
    marker_path: str | None,
    role: ObjectRole,
    floating: _FloatingInfo | None,
    path_of,
    readonly_reasons: list[str],
    diagnostics: list[Diagnostic],
) -> SceneObject:
    point_elements = [
        child
        for child in list(command)
        if is_element(child) and local_name(child.tag) == "point"
    ]
    points = [parse_vec3(point) for point in point_elements]
    if len(point_elements) < 6 or len(point_elements) % 2:
        readonly_reasons.append("drawprism 顶点数量必须是大于等于 6 的偶数")
        diagnostics.append(
            Diagnostic(
                "INVALID_PRISM_POINTS",
                Severity.ERROR,
                "drawprism 需要前后两个截面的对应顶点（至少 6 个且总数为偶数）",
                command_path,
            )
        )
    if any(point is None for point in points):
        readonly_reasons.append("顶点使用变量或表达式")
        diagnostics.append(
            Diagnostic(
                "EXPRESSION_GEOMETRY",
                Severity.INFO,
                "drawprism 使用变量或表达式，已切换为仅源码编辑",
                command_path,
            )
        )
    unsupported = [
        local_name(child.tag)
        for child in list(command)
        if is_element(child) and local_name(child.tag) != "point"
    ]
    if unsupported:
        readonly_reasons.append("包含不受支持的子命令：" + ", ".join(unsupported))
    if any(
        parse_number(value) is None
        and not (name.casefold() == "mask" and _PRISM_MASK_LIST.fullmatch(value))
        for name, value in command.attrib.items()
    ):
        readonly_reasons.append("drawprism 属性使用表达式")

    vertices = tuple(point for point in points if point is not None)
    return SceneObject(
        id=object_id,
        geometry_type=GeometryType.DRAWPRISM,
        marker_kind=marker_kind,
        marker=marker,
        role=role,
        element_path=command_path,
        marker_path=marker_path,
        vertices=vertices,
        point_paths=tuple(path_of(point) for point in point_elements),
        floating_path=floating.path if floating else None,
        floating_center_path=floating.center_path if floating else None,
        floating_center=floating.center if floating else None,
        editable=not readonly_reasons,
        readonly_reason="；".join(readonly_reasons) or None,
        source_order=object_index,
    )


def _direct_child(element: ET.Element, name: str) -> ET.Element | None:
    for child in list(element):
        if is_element(child) and local_name(child.tag) == name:
            return child
    return None


def _box_vertices(point: Vec3, size: Vec3) -> tuple[Vec3, ...]:
    return tuple(
        Vec3(point.x + dx * size.x, point.y + dy * size.y, point.z + dz * size.z)
        for dx, dy, dz in (
            (0, 0, 0),
            (1, 0, 0),
            (1, 1, 0),
            (0, 1, 0),
            (0, 0, 1),
            (1, 0, 1),
            (1, 1, 1),
            (0, 1, 1),
        )
    )


def _is_identity_transform(name: str, element: ET.Element) -> bool:
    if name == "move":
        values = [parse_number(element.get(axis, "0")) for axis in ("x", "y", "z")]
        return all(value == 0 for value in values)
    if name == "rotate":
        angle = parse_number(element.get("ang", element.get("angle", "0")))
        return angle == 0
    if name == "scale":
        values = [parse_number(element.get(axis, "1")) for axis in ("x", "y", "z")]
        return all(value == 1 for value in values)
    return False


def _contains_supported_draw(element: ET.Element) -> bool:
    return any(
        is_element(descendant) and local_name(descendant.tag) in ("drawbox", "drawprism")
        for descendant in element.iter()
        if descendant is not element
    )


def _append_relationship_diagnostics(
    objects: list[SceneObject],
    floating_by_marker: dict[int, list[_FloatingInfo]],
    diagnostics: list[Diagnostic],
) -> None:
    used_bound_markers = {
        scene.marker
        for scene in objects
        if scene.marker_kind is MarkerKind.BOUND and scene.marker is not None
    }
    for marker, entries in floating_by_marker.items():
        if marker not in used_bound_markers:
            diagnostics.append(
                Diagnostic(
                    "ORPHAN_FLOATING",
                    Severity.WARNING,
                    f"floating mkbound={marker} 没有关联到可识别的边界几何",
                    entries[0].path,
                )
            )


def _append_shapeout_diagnostics(
    objects: list[SceneObject], diagnostics: list[Diagnostic]
) -> None:
    grouped: dict[str, list[SceneObject]] = {}
    for scene in objects:
        if scene.shapeout:
            grouped.setdefault(scene.shapeout.casefold(), []).append(scene)
    for scenes in grouped.values():
        paths = {scene.shapeout_path for scene in scenes}
        if len(paths) > 1:
            diagnostics.append(
                Diagnostic(
                    "DUPLICATE_SHAPEOUT",
                    Severity.WARNING,
                    f"shapeout 文件名“{scenes[0].shapeout}”重复，输出可能相互覆盖",
                    scenes[0].shapeout_path,
                )
            )


def _append_parameter_diagnostics(
    root: ET.Element,
    path_by_identity: dict[int, str],
    diagnostics: list[Diagnostic],
) -> None:
    parameters: dict[str, ET.Element] = {}
    for parameter in iter_elements(root, "parameter"):
        key = parameter.get("key")
        if key:
            parameters[key.casefold()] = parameter

    step = parameters.get("stepalgorithm")
    verlet = parameters.get("verletsteps")
    if step is not None and verlet is not None and parse_integer(step.get("value")) == 2:
        diagnostics.append(
            Diagnostic(
                "INACTIVE_VERLET_STEPS",
                Severity.INFO,
                "StepAlgorithm=2（Symplectic）时 VerletSteps 不生效",
                path_by_identity.get(id(verlet)),
            )
        )

    shifting = parameters.get("shifting")
    if shifting is not None and parse_integer(shifting.get("value")) == 0:
        for key in ("shiftcoef", "shifttfs"):
            parameter = parameters.get(key)
            if parameter is not None:
                diagnostics.append(
                    Diagnostic(
                        "INACTIVE_SHIFT_PARAMETER",
                        Severity.INFO,
                        f"Shifting=0 时 {parameter.get('key')} 不生效",
                        path_by_identity.get(id(parameter)),
                    )
                )

    deprecated = parameters.get("partsoutmax")
    if deprecated is not None:
        diagnostics.append(
            Diagnostic(
                "DEPRECATED_PARTSOUTMAX",
                Severity.WARNING,
                "PartsOutMax 在 DualSPHysics v5.4 中已弃用，建议移除",
                path_by_identity.get(id(deprecated)),
            )
        )

    for constant_name in ("hswl", "speedsound"):
        element = next(iter(iter_elements(root, constant_name)), None)
        if (
            element is not None
            and element.get("auto", "false").strip().lower() == "true"
            and parse_number(element.get("value")) == 0
        ):
            diagnostics.append(
                Diagnostic(
                    "AUTO_PLACEHOLDER",
                    Severity.INFO,
                    f"{constant_name} 的 value=0 是 auto=true 下的占位值，将由 GenCase 自动计算",
                    path_by_identity.get(id(element)),
                )
            )


def _append_bounds_diagnostics(
    objects: list[SceneObject],
    definition_bounds: BoundingBox | None,
    dp: float | None,
    diagnostics: list[Diagnostic],
) -> None:
    if definition_bounds is None:
        return
    tolerance = dp or 0.0
    for scene in objects:
        bounds = scene.bounds
        if bounds is None:
            continue
        if (
            bounds.minimum.x < definition_bounds.minimum.x - tolerance
            or bounds.minimum.y < definition_bounds.minimum.y - tolerance
            or bounds.minimum.z < definition_bounds.minimum.z - tolerance
            or bounds.maximum.x > definition_bounds.maximum.x + tolerance
            or bounds.maximum.y > definition_bounds.maximum.y + tolerance
            or bounds.maximum.z > definition_bounds.maximum.z + tolerance
        ):
            diagnostics.append(
                Diagnostic(
                    "OBJECT_OUTSIDE_DEFINITION",
                    Severity.WARNING,
                    f"对象“{scene.name}”超出 geometry/definition 范围",
                    scene.element_path,
                )
            )


def _append_marker_range_diagnostics(
    root: ET.Element,
    objects: list[SceneObject],
    path_by_identity: dict[int, str],
    diagnostics: list[Diagnostic],
) -> None:
    mkconfig = next(iter(iter_elements(root, "mkconfig")), None)
    if mkconfig is None:
        return
    bound_count = parse_integer(mkconfig.get("boundcount"))
    fluid_count = parse_integer(mkconfig.get("fluidcount"))
    for scene in objects:
        count = fluid_count if scene.marker_kind is MarkerKind.FLUID else bound_count
        if scene.marker is not None and count is not None and not 0 <= scene.marker < count:
            diagnostics.append(
                Diagnostic(
                    "MARKER_OUT_OF_RANGE",
                    Severity.ERROR,
                    f"对象“{scene.name}”的本地 marker={scene.marker} 超出 mkconfig 范围 0..{count - 1}",
                    scene.marker_path,
                )
            )


def _constant_number(root: ET.Element, name: str) -> float | None:
    element = next(iter(iter_elements(root, name)), None)
    return parse_number(element.get("value")) if element is not None else None
