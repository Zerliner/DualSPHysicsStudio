"""Small, Qt-free adapters used by the desktop user interface.

The core package deliberately owns the XML data model.  The UI consumes it
through these helpers so it can also display lightweight objects returned by
plugins, tests, and future DualSPHysics adapters.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Any


CATEGORY_ORDER = ("fluid", "fixed", "floating", "other")

CATEGORY_LABELS = {
    "fluid": "流体",
    "fixed": "固定边界",
    "floating": "浮动刚体",
    "other": "其他/只读",
}

# Deliberately high-contrast on the dark viewport background.
CATEGORY_COLORS = {
    "fluid": (0.10, 0.55, 0.95),
    "fixed": (0.72, 0.75, 0.80),
    "floating": (1.00, 0.55, 0.16),
    "other": (0.65, 0.47, 0.82),
    "vtk": (0.22, 0.90, 0.64),
}


@dataclass(frozen=True, slots=True)
class SceneTreeEntry:
    """Presentation data for one object in the model tree."""

    key: str
    name: str
    kind: str
    category: str
    marker: str
    editable: bool
    source: Any


@dataclass(frozen=True, slots=True)
class BoxSpec:
    """A VTK-friendly box representation."""

    center: tuple[float, float, float]
    size: tuple[float, float, float]
    rotation: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class DiagnosticRow:
    code: str
    severity: str
    severity_label: str
    message: str
    path: str
    line: int | None


def value_of(obj: Any, *names: str, default: Any = None) -> Any:
    """Return the first present mapping key or object attribute."""

    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            try:
                return getattr(obj, name)
            except Exception:
                # A third-party property should not make the whole tree fail.
                continue
    return default


def normalize_category(value: Any, obj: Any = None) -> str:
    """Map common DualSPHysics marker names to the four UI groups."""

    if obj is not None:
        floating = value_of(
            obj,
            "is_floating",
            "floating",
            "floating_body",
            default=False,
        )
        if isinstance(floating, bool) and floating:
            return "floating"
        if floating not in (None, False, "", 0, "0") and not isinstance(floating, Sequence):
            return "floating"

    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if any(token in text for token in ("float", "rigid", "body", "漂浮", "浮动")):
        return "floating"
    if any(token in text for token in ("fluid", "water", "mkfluid", "流体", "水体")):
        return "fluid"
    if any(token in text for token in ("bound", "fixed", "wall", "tank", "slope", "边界", "固定")):
        return "fixed"
    return "other"


def scene_object_category(obj: Any) -> str:
    raw = value_of(
        obj,
        "category",
        "marker_type",
        "mk_type",
        "material",
        "role",
        default="",
    )
    return normalize_category(raw, obj)


def scene_object_kind(obj: Any) -> str:
    raw = str(
        value_of(obj, "kind", "geometry_type", "shape_type", "type", default="unknown")
    ).strip().lower()
    if raw.startswith("draw"):
        raw = raw[4:]
    if "box" in raw:
        return "box"
    if "prism" in raw:
        return "prism"
    return raw or "unknown"


def scene_object_marker(obj: Any) -> str:
    marker = value_of(obj, "marker", "mk", "mkbound", "mkfluid", "marker_id", default="")
    if marker in (None, ""):
        return ""
    return str(marker)


def scene_object_name(obj: Any, index: int = 0) -> str:
    raw = value_of(obj, "name", "shapeout", "label", "file", "id", default="")
    if isinstance(raw, Mapping):
        raw = raw.get("file", "")
    text = str(raw or "").strip()
    if text:
        return Path(text).stem
    kind_label = {"box": "方箱", "prism": "棱柱"}.get(scene_object_kind(obj), "对象")
    marker = scene_object_marker(obj)
    return f"{kind_label} mk={marker}" if marker else f"{kind_label} {index + 1}"


def scene_object_key(obj: Any, index: int = 0) -> str:
    explicit = value_of(obj, "key", "uid", "object_id", "xml_id", "id", default="")
    if explicit not in (None, ""):
        return str(explicit)
    return f"{scene_object_category(obj)}:{scene_object_marker(obj)}:{scene_object_name(obj, index)}:{index}"


def scene_object_editable(obj: Any) -> bool:
    explicit = value_of(obj, "editable", "is_editable", default=None)
    if explicit is not None:
        return bool(explicit)
    reason = value_of(obj, "readonly_reason", "read_only_reason", default="")
    return scene_object_kind(obj) in {"box", "prism"} and not bool(reason)


def build_scene_entries(objects: Iterable[Any]) -> list[SceneTreeEntry]:
    entries: list[SceneTreeEntry] = []
    seen: dict[str, int] = {}
    for index, obj in enumerate(objects or ()):
        base_key = scene_object_key(obj, index)
        count = seen.get(base_key, 0)
        seen[base_key] = count + 1
        key = base_key if count == 0 else f"{base_key}#{count + 1}"
        entries.append(
            SceneTreeEntry(
                key=key,
                name=scene_object_name(obj, index),
                kind=scene_object_kind(obj),
                category=scene_object_category(obj),
                marker=scene_object_marker(obj),
                editable=scene_object_editable(obj),
                source=obj,
            )
        )
    return entries


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _xyz(value: Any) -> tuple[float, float, float] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        components = (value.get("x"), value.get("y"), value.get("z"))
    elif all(hasattr(value, axis) for axis in "xyz"):
        components = (value.x, value.y, value.z)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 3:
        components = (value[0], value[1], value[2])
    else:
        return None
    numbers = tuple(_number(component) for component in components)
    if any(number is None for number in numbers):
        return None
    return numbers  # type: ignore[return-value]


def box_spec(obj: Any) -> BoxSpec | None:
    """Extract either center/size or corner/size box conventions."""

    size = _xyz(value_of(obj, "size", "dimensions", "lengths", default=None))
    bounds = value_of(obj, "bounds", "bbox", "bounding_box", default=None)
    center = _xyz(value_of(obj, "center", default=None))

    if bounds is not None:
        if isinstance(bounds, Mapping):
            minimum = _xyz(bounds.get("min") or bounds.get("minimum") or bounds.get("pointmin"))
            maximum = _xyz(bounds.get("max") or bounds.get("maximum") or bounds.get("pointmax"))
        elif isinstance(bounds, Sequence) and len(bounds) >= 6:
            # VTK order: xmin, xmax, ymin, ymax, zmin, zmax.
            numbers = tuple(_number(item) for item in bounds[:6])
            if all(number is not None for number in numbers):
                minimum = (numbers[0], numbers[2], numbers[4])  # type: ignore[arg-type]
                maximum = (numbers[1], numbers[3], numbers[5])  # type: ignore[arg-type]
            else:
                minimum = maximum = None
        else:
            minimum = _xyz(value_of(bounds, "minimum", "min", "pointmin", default=None))
            maximum = _xyz(value_of(bounds, "maximum", "max", "pointmax", default=None))
        if minimum and maximum:
            size = tuple(maximum[i] - minimum[i] for i in range(3))
            center = tuple((maximum[i] + minimum[i]) / 2 for i in range(3))

    if size is None:
        return None
    if center is None:
        corner = _xyz(value_of(obj, "point", "position", "origin", "corner", "pos", default=None))
        if corner is None:
            corner = (0.0, 0.0, 0.0)
        center = tuple(corner[i] + size[i] / 2 for i in range(3))

    rotation = _xyz(value_of(obj, "rotation", "angles", "rotate", default=None)) or (0.0, 0.0, 0.0)
    if any(component <= 0 for component in size):
        return None
    return BoxSpec(center=center, size=size, rotation=rotation)


def vertices_of(obj: Any) -> list[tuple[float, float, float]]:
    raw = value_of(obj, "vertices", "points", "point_list", default=())
    result: list[tuple[float, float, float]] = []
    for item in raw or ():
        point = _xyz(item)
        if point is not None:
            result.append(point)
    return result


def prism_faces(vertex_count: int) -> list[tuple[int, ...]]:
    """Create faces for DualSPHysics' two-corresponding-cross-sections order."""

    if vertex_count < 6 or vertex_count % 2:
        return []
    half = vertex_count // 2
    front = tuple(range(half - 1, -1, -1))
    back = tuple(range(half, vertex_count))
    sides = []
    for index in range(half):
        next_index = (index + 1) % half
        sides.append((index, next_index, next_index + half, index + half))
    return [front, back, *sides]


def faces_of(obj: Any, vertex_count: int) -> list[tuple[int, ...]]:
    raw = value_of(obj, "faces", "polygons", default=None)
    if raw:
        result: list[tuple[int, ...]] = []
        for face in raw:
            try:
                indices = tuple(int(item) for item in face)
            except (TypeError, ValueError):
                continue
            if len(indices) >= 3 and min(indices) >= 0 and max(indices) < vertex_count:
                result.append(indices)
        if result:
            return result
    return prism_faces(vertex_count)


_PROPERTY_LABELS = {
    "name": "名称",
    "shapeout": "ShapeOut",
    "kind": "几何类型",
    "geometry_type": "几何类型",
    "category": "对象类别",
    "marker_type": "Marker 类型",
    "marker": "Marker",
    "mk": "Marker",
    "mkbound": "边界 Marker",
    "mkfluid": "流体 Marker",
    "point": "起点",
    "position": "位置",
    "center": "中心",
    "size": "尺寸",
    "rotation": "旋转角",
    "vertices": "顶点",
    "boxfill": "面类型",
    "box_fill": "面类型",
    "role": "对象角色",
    "editable": "可图形编辑",
    "readonly_reason": "只读原因",
}


def _public_items(obj: Any) -> list[tuple[str, Any]]:
    if isinstance(obj, Mapping):
        return [(str(key), value) for key, value in obj.items() if not str(key).startswith("_")]
    if is_dataclass(obj):
        return [(field.name, getattr(obj, field.name)) for field in fields(obj) if not field.name.startswith("_")]
    try:
        values = vars(obj)
    except TypeError:
        values = {}
    return [(key, value) for key, value in values.items() if not key.startswith("_")]


def format_value(value: Any, *, limit: int = 260) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, Enum):
        normalized = str(value.value)
        return {
            "drawbox": "方箱（drawbox）",
            "drawprism": "棱柱（drawprism）",
            "fluid": "流体",
            "fixed_boundary": "固定边界",
            "floating": "浮动刚体",
            "bound": "边界",
        }.get(normalized, normalized)
    if isinstance(value, float):
        return f"{value:.8g}"
    if isinstance(value, Mapping):
        text = ", ".join(f"{key}={format_value(item, limit=60)}" for key, item in value.items())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if value and all(_xyz(item) is not None for item in value):
            text = f"{len(value)} 个点"
        else:
            text = ", ".join(format_value(item, limit=60) for item in value)
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def property_rows(obj: Any) -> list[tuple[str, str, str, Any]]:
    """Return ``(field, Chinese label, display value, raw value)`` rows."""

    rows: list[tuple[str, str, str, Any]] = []
    priority = {name: index for index, name in enumerate(_PROPERTY_LABELS)}
    items = _public_items(obj)
    items.sort(key=lambda item: (priority.get(item[0], 10_000), item[0]))
    ignored = {
        "source_node",
        "xml_node",
        "element",
        "raw_xml",
        "source_range",
        "element_path",
        "marker_path",
        "point_paths",
        "size_path",
        "shapeout_path",
        "floating_path",
        "floating_center_path",
        "source_order",
        "id",
    }
    for field_name, raw in items:
        if field_name in ignored or callable(raw):
            continue
        label = _PROPERTY_LABELS.get(field_name, field_name)
        rows.append((field_name, label, format_value(raw), raw))
    if not rows:
        rows.append(("value", "内容", format_value(obj), obj))
    return rows


def diagnostic_row(diagnostic: Any) -> DiagnosticRow:
    if isinstance(diagnostic, str):
        return DiagnosticRow("", "info", "提示", diagnostic.strip(), "", None)
    raw_severity = value_of(diagnostic, "severity", "level", default="提示")
    severity = str(getattr(raw_severity, "value", raw_severity)).strip().lower()
    normalized_severity = {
        "warn": "warning",
        "hint": "info",
    }.get(severity, severity)
    severity_label = {
        "error": "错误",
        "warning": "警告",
        "warn": "警告",
        "info": "提示",
        "hint": "建议",
    }.get(severity, severity or "提示")
    message = str(value_of(diagnostic, "message", "text", "description", default=diagnostic)).strip()
    path = str(value_of(diagnostic, "path", "node_path", "xpath", default="")).strip()
    line = value_of(diagnostic, "line", "line_number", default=None)
    if line in (None, ""):
        details = value_of(diagnostic, "details", default={})
        if isinstance(details, Mapping):
            line = details.get("line")
    try:
        resolved_line = int(line) if line not in (None, "") else None
    except (TypeError, ValueError):
        resolved_line = None
    code = str(value_of(diagnostic, "code", default="")).strip()
    return DiagnosticRow(
        code,
        normalized_severity
        if normalized_severity in {"error", "warning", "info"}
        else "info",
        severity_label,
        message,
        path,
        resolved_line,
    )


def diagnostic_rows(diagnostics: Iterable[Any] | None) -> list[DiagnosticRow]:
    return [
        row
        for row in (diagnostic_row(item) for item in diagnostics or ())
        if row.message
    ]


def diagnostic_counts(
    diagnostics: Iterable[Any] | None,
) -> dict[str, int]:
    counts = {"error": 0, "warning": 0, "info": 0}
    for row in diagnostic_rows(diagnostics):
        counts[row.severity] += 1
    return counts


def diagnostic_text(diagnostic: Any) -> str:
    row = diagnostic_row(diagnostic)
    location = row.path
    if row.line is not None:
        location = (
            f"{location}，第 {row.line} 行"
            if location
            else f"第 {row.line} 行"
        )
    return f"[{row.severity_label}] {row.message}" + (
        f"\n  位置：{location}" if location else ""
    )


def diagnostics_to_text(diagnostics: Iterable[Any] | None) -> str:
    messages = [diagnostic_text(item) for item in diagnostics or ()]
    messages = [message for message in messages if message]
    return "\n\n".join(messages) if messages else "未发现需要注意的问题。"


def object_explanation(obj: Any, document: Any = None) -> str:
    """Return trustworthy offline text without inventing unknown semantics."""

    for name in ("offline_explanation", "explanation", "description", "help_text"):
        value = value_of(obj, name, default="")
        if value:
            return str(value)

    if document is not None:
        for method_name in ("explain_object", "explain", "describe_object"):
            method = getattr(document, method_name, None)
            if callable(method):
                try:
                    result = method(obj)
                except Exception:
                    continue
                if result:
                    return str(result)

    kind = scene_object_kind(obj)
    category = CATEGORY_LABELS[scene_object_category(obj)]
    marker = scene_object_marker(obj) or "未指定"
    if kind == "box":
        detail = "drawbox 方箱预览；属性中的起点、尺寸与面类型共同决定 GenCase 粒子化结果。"
    elif kind == "prism":
        detail = "drawprism 棱柱预览；顶点按前后对应截面顺序连接，最终粒子以 GenCase 输出为准。"
    else:
        detail = "暂无可靠的离线几何解释；请在 XML 源码中核对该命令。"
    editable = "可在图形属性中编辑。" if scene_object_editable(obj) else "当前对象仅支持源码编辑。"
    return f"类别：{category}\nMarker：{marker}\n\n{detail}\n{editable}"


def parse_editor_value(text: str, original: Any) -> Any:
    """Conservatively coerce a property editor string back to its scalar type."""

    if isinstance(original, bool):
        normalized = text.strip().lower()
        if normalized in {"是", "true", "1", "yes", "on"}:
            return True
        if normalized in {"否", "false", "0", "no", "off"}:
            return False
        raise ValueError("请输入“是”或“否”")
    if isinstance(original, int) and not isinstance(original, bool):
        return int(text.strip())
    if isinstance(original, float):
        value = float(text.strip())
        if not isfinite(value):
            raise ValueError("数值必须是有限值")
        return value
    # Structured values are intentionally not guessed from free-form input.
    if isinstance(original, (Mapping, list, tuple, set)):
        raise ValueError("请使用专用几何编辑器修改此结构化属性")
    return text
