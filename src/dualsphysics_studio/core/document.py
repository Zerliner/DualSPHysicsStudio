"""Lossless, safe and undoable DualSPHysics case document."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Mapping
import hashlib
import html
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Iterable, Sequence
import xml.etree.ElementTree as ET

from .errors import (
    BackupError,
    CaseDocumentError,
    GeometryEditError,
    ReadOnlyGeometryError,
    SourceLocationError,
    UnsafeXmlError,
    XmlSyntaxError,
)
from .geometry import prism_mass_properties, rotate_point, scale_point, snap_point
from .explanations import explain
from .models import (
    BoundingBox,
    Diagnostic,
    GeometryType,
    MarkerKind,
    ObjectRole,
    SaveResult,
    SceneObject,
    Severity,
    Vec3,
    XmlParameterField,
)
from .semantics import (
    INTEGER_PARAMETER_CHOICES,
    POSITIVE_PARAMETER_KEYS,
    SemanticModel,
    analyze,
    parse_integer,
    parse_number,
)
from .source import (
    MAX_XML_BYTES,
    SourceIndex,
    decode_xml_bytes,
    encode_xml_text,
    is_element,
    iter_elements,
    local_name,
    secure_parse,
)


_ATTRIBUTE_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_.:\-]*$")
_NUMBER_TOKEN = re.compile(
    r"^([+-]?)(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:[eE]([+-]?\d+))?$"
)
_BOX_BITS = (
    (0, 0, 0),
    (1, 0, 0),
    (1, 1, 0),
    (0, 1, 0),
    (0, 0, 1),
    (1, 0, 1),
    (1, 1, 1),
    (0, 1, 1),
)

# GenCase numbers drawprism faces as base, top, then the side faces in
# base-point order.  _BOX_BITS makes those sides front, right, back and left.
_BOX_FACE_NUMBERS = {
    "bottom": 1,
    "top": 2,
    "front": 3,
    "right": 4,
    "back": 5,
    "left": 6,
}
_DRAW_MODES = frozenset(("wire", "face", "solid", "full"))


def _boxfill_prism_spec(box_fill: str | None) -> tuple[str, str]:
    """Return ``(draw_mode, hidden-face mask)`` for a box-to-prism rewrite.

    GenCase's prism mask is a ``|``-separated list of *face numbers*, not a
    decimal bit field.  A drawbox face selection therefore becomes face mode
    plus the complement of the selected box faces.
    """

    value = re.sub(r"\s+", "", (box_fill or "").casefold())
    if not value:
        raise GeometryEditError("drawbox 缺少 boxfill，无法保真转换为 drawprism")
    if value == "solid":
        # GenCase drawbox/boxfill=solid includes the surface and the interior.
        # Its particle output matches drawprism in full (face + solid) mode.
        return ("full", "0")
    if "solid" in value:
        raise GeometryEditError(f"无法解析 drawbox.boxfill：{box_fill}")

    all_faces = frozenset(_BOX_FACE_NUMBERS)
    selected: set[str] = set()
    for term in value.split("|"):
        if not term:
            raise GeometryEditError(f"无法解析 drawbox.boxfill：{box_fill}")
        parts = term.split("^")
        if any(not part or part not in all_faces | {"all"} for part in parts):
            raise GeometryEditError(f"无法解析 drawbox.boxfill：{box_fill}")
        if len(parts) > 1:
            if parts[0] != "all" or "all" in parts[1:]:
                raise GeometryEditError(f"无法解析 drawbox.boxfill：{box_fill}")
            term_faces = set(all_faces).difference(parts[1:])
        elif parts[0] == "all":
            term_faces = set(all_faces)
        else:
            term_faces = {parts[0]}
        selected.update(term_faces)

    if not selected:
        raise GeometryEditError("drawbox.boxfill 没有选择任何面")
    hidden = sorted(
        (_BOX_FACE_NUMBERS[name] for name in all_faces.difference(selected))
    )
    return ("face", " | ".join(str(number) for number in hidden) or "0")


@dataclass(frozen=True, slots=True)
class _Replacement:
    start: int
    end: int
    value: str


class CaseDocument:
    """The editor's authoritative XML document.

    The semantic tree is always rebuilt from the authoritative source string.
    Invalid source typed in the editor can be staged in :attr:`draft_text`
    without destroying the last valid scene.
    """

    def __init__(
        self,
        text: str,
        *,
        path: str | Path | None = None,
        encoding: str = "utf-8",
        bom: bytes = b"",
    ) -> None:
        self.path = Path(path).resolve() if path is not None else None
        self.encoding = encoding
        self._bom = bom
        self._undo_stack: list[tuple[str, str]] = []
        self._redo_stack: list[tuple[str, str]] = []
        self._draft_text: str | None = None
        self._draft_diagnostics: tuple[Diagnostic, ...] = ()
        self._text = text
        self._root, self._source_index, self._model = self._build_state(text)
        self._saved_text = text

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        path: str | Path | None = None,
        encoding: str = "utf-8",
    ) -> "CaseDocument":
        return cls(text, path=path, encoding=encoding)

    @classmethod
    def from_file(cls, path: str | Path) -> "CaseDocument":
        source_path = Path(path).resolve()
        if source_path.stat().st_size > MAX_XML_BYTES:
            raise UnsafeXmlError(
                f"XML 文件超过读取上限 {MAX_XML_BYTES // (1024 * 1024)} MiB"
            )
        with source_path.open("rb") as source_file:
            data = source_file.read(MAX_XML_BYTES + 1)
        if len(data) > MAX_XML_BYTES:
            raise UnsafeXmlError(
                f"XML 文件超过读取上限 {MAX_XML_BYTES // (1024 * 1024)} MiB"
            )
        text, encoding, bom = decode_xml_bytes(data)
        return cls(text, path=source_path, encoding=encoding, bom=bom)

    # Friendly alias used by the UI and command-line smoke tests.
    load = from_file

    @staticmethod
    def _build_state(text: str) -> tuple[ET.Element, SourceIndex, SemanticModel]:
        root = secure_parse(text)
        source_index = SourceIndex(text)
        model = analyze(root)
        # A mismatch here would make local edits unsafe, so fail before exposing
        # the document rather than silently reserializing it.
        missing = set(model.element_by_path).difference(source_index.tokens)
        if missing:
            raise SourceLocationError(
                "XML 语义树与源码索引不一致：" + ", ".join(sorted(missing)[:3])
            )
        return root, source_index, model

    @property
    def text(self) -> str:
        return self._text

    @property
    def raw_text(self) -> str:
        return self._text

    @property
    def source_text(self) -> str:
        return self._text

    @property
    def editor_text(self) -> str:
        return self._draft_text if self._draft_text is not None else self._text

    @property
    def draft_text(self) -> str | None:
        return self._draft_text

    @property
    def root(self) -> ET.Element:
        return self._root

    @property
    def objects(self) -> tuple[SceneObject, ...]:
        return self._model.objects

    @property
    def scene_objects(self) -> tuple[SceneObject, ...]:
        return self._model.objects

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        return self._model.diagnostics + self._draft_diagnostics

    @property
    def semantic_diagnostics(self) -> tuple[Diagnostic, ...]:
        return self._model.diagnostics

    @property
    def draft_diagnostics(self) -> tuple[Diagnostic, ...]:
        return self._draft_diagnostics

    @property
    def facts(self):
        return self._model.facts

    @property
    def parameter_fields(self) -> tuple[XmlParameterField, ...]:
        """Return existing scalar fields without creating parallel XML state."""

        fields: list[XmlParameterField] = []
        for path, element in self._model.element_by_path.items():
            name = local_name(element.tag)
            if name == "definition" and "dp" in element.attrib:
                fields.append(
                    XmlParameterField(
                        "dp", "geometry", path, "dp", element.get("dp", "")
                    )
                )
                continue
            if "/constantsdef[" in path:
                if name == "gravity":
                    for axis in ("x", "y", "z"):
                        if axis in element.attrib:
                            fields.append(
                                XmlParameterField(
                                    f"gravity.{axis}",
                                    "constants",
                                    path,
                                    axis,
                                    element.get(axis, ""),
                                )
                            )
                elif "value" in element.attrib:
                    fields.append(
                        XmlParameterField(
                            name,
                            "constants",
                            path,
                            "value",
                            element.get("value", ""),
                        )
                    )
                continue
            if name == "parameter":
                key = (element.get("key") or "").strip() or "（未命名参数）"
                fields.append(
                    XmlParameterField(
                        key,
                        "execution",
                        path,
                        "value",
                        element.get("value", ""),
                    )
                )
        return tuple(fields)

    @property
    def dp(self) -> float | None:
        return self._model.facts.dp

    @property
    def dirty(self) -> bool:
        return self._text != self._saved_text or self._draft_text is not None

    @property
    def is_dirty(self) -> bool:
        return self.dirty

    @property
    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    @property
    def undo_label(self) -> str | None:
        return self._undo_stack[-1][1] if self._undo_stack else None

    @property
    def redo_label(self) -> str | None:
        return self._redo_stack[-1][1] if self._redo_stack else None

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def to_bytes(self) -> bytes:
        return encode_xml_text(self._text, self.encoding, self._bom)

    def get_object(self, object_id: str | int) -> SceneObject:
        if isinstance(object_id, int):
            try:
                return self.objects[object_id]
            except IndexError as exc:
                raise GeometryEditError(f"对象序号不存在：{object_id}") from exc
        for scene in self.objects:
            if scene.id == object_id:
                return scene
        raise GeometryEditError(f"对象不存在：{object_id}")

    def element(self, path: str) -> ET.Element:
        try:
            return self._model.element_by_path[path]
        except KeyError as exc:
            raise SourceLocationError(f"找不到 XML 节点：{path}") from exc

    def explain_node(self, path: str):
        """Return a conservative Chinese v5.4 explanation for one node."""

        return explain(self.element(path), self._root)

    def source_line(self, path: str) -> int:
        """Return the one-based source line for a stable element path."""

        token = self._source_index.require(path)
        return self._text.count("\n", 0, token.start) + 1

    def update_parameter_value(
        self, field: XmlParameterField | str, value: object
    ) -> None:
        """Update one workbench field through the canonical lossless patcher."""

        if isinstance(field, XmlParameterField):
            selected = field
        else:
            selected = next(
                (
                    candidate
                    for candidate in self.parameter_fields
                    if candidate.id == field or candidate.element_path == field
                ),
                None,
            )
            if selected is None:
                raise SourceLocationError(f"找不到 XML 参数：{field}")
        text = str(value).strip()
        if not text:
            raise SourceLocationError(f"{selected.key} 的值不能为空")
        normalized = selected.key.casefold()
        if normalized in POSITIVE_PARAMETER_KEYS:
            number = parse_number(text)
            if number is not None and number <= 0:
                raise SourceLocationError(f"{selected.key} 必须大于 0")
        choices = INTEGER_PARAMETER_CHOICES.get(normalized)
        integer = parse_integer(text)
        if choices is not None and integer is not None and integer not in choices:
            allowed = "、".join(str(item) for item in sorted(choices))
            raise SourceLocationError(f"{selected.key} 仅支持：{allowed}")
        self.update_attribute(
            selected.element_path,
            selected.attribute,
            text,
            description=f"修改参数 {selected.key}",
        )

    def stage_source(self, text: str) -> bool:
        """Try source-editor text while retaining the last valid scene on error."""

        try:
            state = self._build_state(text)
        except (UnsafeXmlError, XmlSyntaxError, SourceLocationError) as exc:
            self._draft_text = text
            line = getattr(exc, "line", None)
            column = getattr(exc, "column", None)
            self._draft_diagnostics = (
                Diagnostic(
                    "XML_DRAFT_ERROR",
                    Severity.ERROR,
                    str(exc),
                    details={"line": line, "column": column},
                ),
            )
            return False
        self._commit_prebuilt(text, state, "编辑 XML 源码")
        return True

    def replace_source(self, text: str, *, description: str = "编辑 XML 源码") -> None:
        state = self._build_state(text)
        self._commit_prebuilt(text, state, description)

    set_source_text = replace_source

    def discard_draft(self) -> None:
        self._draft_text = None
        self._draft_diagnostics = ()

    def update_attribute(
        self,
        element_path: str,
        attribute: str,
        value: object,
        *,
        description: str | None = None,
    ) -> None:
        """Update or add one attribute through a local source-text patch."""

        self._apply_attribute_changes(
            [(element_path, attribute, str(value))],
            description or f"修改 {local_name(self.element(element_path).tag)}.{attribute}",
        )

    def update_element_text(
        self, element_path: str, value: object, *, description: str | None = None
    ) -> None:
        token = self._source_index.require(element_path)
        if token.self_closing or token.content_span is None:
            raise SourceLocationError("自闭合元素没有可编辑文本")
        if token.has_element_children:
            raise SourceLocationError("包含子元素的节点不能作为纯文本属性编辑")
        start, end = token.content_span
        escaped = html.escape(str(value), quote=False)
        self._commit_patches(
            [_Replacement(start, end, escaped)],
            description or f"修改 {token.name} 文本",
        )

    def update_object_property(self, object_id: str | int, name: str, value: object) -> None:
        scene = self.get_object(object_id)
        normalized = name.strip().casefold()
        if normalized in ("shapeout", "name", "file"):
            self.rename_shapeout(scene.id, str(value))
            return
        if normalized in ("marker", "mk"):
            self.set_object_marker(scene.id, int(value))
            return
        if normalized in ("position", "point"):
            target = Vec3.from_value(value)  # type: ignore[arg-type]
            current = scene.position
            if current is None:
                raise GeometryEditError("对象没有可编辑的位置")
            self.translate_object(scene.id, target - current)
            return
        if normalized == "size":
            target_size = Vec3.from_value(value)  # type: ignore[arg-type]
            current_size = scene.size
            if current_size is None or min(current_size) <= 0:
                raise GeometryEditError("对象没有可编辑的尺寸")
            self.scale_object(
                scene.id,
                Vec3(
                    target_size.x / current_size.x,
                    target_size.y / current_size.y,
                    target_size.z / current_size.z,
                ),
            )
            return
        if normalized == "boxfill" and scene.geometry_type is GeometryType.DRAWBOX:
            boxfill_path = f"{scene.element_path}/boxfill[1]"
            self.update_element_text(boxfill_path, value, description=f"修改 {scene.name} 面类型")
            return
        raise GeometryEditError(f"不支持的图形属性：{name}")

    def rename_shapeout(self, object_id: str | int, filename: str) -> None:
        scene = self.get_object(object_id)
        self._ensure_editable(scene)
        if not scene.shapeout_path:
            raise GeometryEditError("对象没有独立 shapeout，需在源码中创建后才能重命名")
        shared = [obj for obj in self.objects if obj.shapeout_path == scene.shapeout_path]
        label = "、".join(obj.name for obj in shared)
        self._apply_attribute_changes(
            [(scene.shapeout_path, "file", filename)],
            f"重命名输出 {label}",
        )

    def set_object_marker(self, object_id: str | int, marker: int) -> None:
        scene = self.get_object(object_id)
        self._ensure_editable(scene)
        if marker < 0:
            raise GeometryEditError("marker 不能为负数")
        if scene.marker_path is None:
            raise GeometryEditError("对象缺少 setmkfluid/setmkbound")
        shared = [obj for obj in self.objects if obj.marker_path == scene.marker_path]
        if len(shared) > 1:
            raise GeometryEditError(
                "该 setmk 命令同时控制多个对象，不能从单对象属性面板修改；请编辑源码"
            )
        changes: list[tuple[str, str, str]] = [(scene.marker_path, "mk", str(marker))]
        if scene.floating_path:
            changes.append((scene.floating_path, "mkbound", str(marker)))
        self._apply_attribute_changes(changes, f"修改 {scene.name} marker")

    def add_box(
        self,
        position: Vec3 | Sequence[float] | Mapping[str, object],
        size: Vec3 | Sequence[float] | None = None,
        *,
        role: ObjectRole | str = ObjectRole.FIXED_BOUNDARY,
        marker: int | None = None,
        shapeout: str | None = None,
        box_fill: str = "solid",
        floating_density: float = 1000.0,
        floating_center: Vec3 | Sequence[float] | None = None,
    ) -> SceneObject:
        """Append one unambiguous ``setmk* / drawbox / shapeout`` group."""
        if isinstance(position, Mapping):
            payload = position
            position = payload.get("point", payload.get("position", (0.0, 0.0, 0.0)))  # type: ignore[assignment]
            size = payload.get("size", size)  # type: ignore[assignment]
            role = payload.get("role", payload.get("category", role))  # type: ignore[assignment]
            marker = payload.get("marker", payload.get("mk", marker))  # type: ignore[assignment]
            shapeout = payload.get("shapeout", payload.get("name", shapeout))  # type: ignore[assignment]
            box_fill = str(payload.get("box_fill", payload.get("boxfill", box_fill)))
            floating_density = float(
                payload.get("floating_density", payload.get("rhopbody", floating_density))
            )
            floating_center = payload.get("floating_center", floating_center)  # type: ignore[assignment]
        if size is None:
            raise GeometryEditError("新建 drawbox 需要 size")
        origin = Vec3.from_value(position)
        dimensions = Vec3.from_value(size)
        if min(dimensions) <= 0:
            raise GeometryEditError("drawbox 尺寸必须全部大于 0")
        object_role = _coerce_role(role)
        marker_kind = (
            MarkerKind.FLUID if object_role is ObjectRole.FLUID else MarkerKind.BOUND
        )
        resolved_marker = self._resolve_new_marker(marker_kind, marker)
        resolved_shapeout = self._resolve_new_shapeout(
            shapeout,
            "Fluid" if object_role is ObjectRole.FLUID else "Floating" if object_role is ObjectRole.FLOATING else "Boundary",
        )
        center = (
            Vec3.from_value(floating_center)
            if floating_center is not None
            else origin + dimensions * 0.5
        )
        lines = [
            f'<{"setmkfluid" if marker_kind is MarkerKind.FLUID else "setmkbound"} mk="{resolved_marker}" />',
            "<drawbox>",
            f"    <boxfill>{html.escape(box_fill, quote=False)}</boxfill>",
            f"    {_vec_tag('point', origin)}",
            f"    {_vec_tag('size', dimensions)}",
            "</drawbox>",
            f'<shapeout file="{_escape_attribute(resolved_shapeout, chr(34))}" />',
        ]
        self._insert_new_object(
            lines,
            object_role,
            resolved_marker,
            center,
            float(floating_density),
            f"新建长方体 {resolved_shapeout}",
        )
        return next(scene for scene in self.objects if scene.shapeout == resolved_shapeout)

    def add_prism(
        self,
        vertices: Sequence[Vec3 | Sequence[float]] | Mapping[str, object],
        *,
        role: ObjectRole | str = ObjectRole.FIXED_BOUNDARY,
        marker: int | None = None,
        shapeout: str | None = None,
        mask: int = 0,
        floating_density: float = 1000.0,
        floating_center: Vec3 | Sequence[float] | None = None,
    ) -> SceneObject:
        """Append one direct ``drawprism`` and its marker/output associations."""
        if isinstance(vertices, Mapping):
            payload = vertices
            raw_vertices = payload.get("vertices")
            if raw_vertices is None:
                raise GeometryEditError("新建 drawprism 需要 vertices")
            vertices = raw_vertices  # type: ignore[assignment]
            role = payload.get("role", payload.get("category", role))  # type: ignore[assignment]
            marker = payload.get("marker", payload.get("mk", marker))  # type: ignore[assignment]
            shapeout = payload.get("shapeout", payload.get("name", shapeout))  # type: ignore[assignment]
            mask = int(payload.get("mask", mask))
            floating_density = float(
                payload.get("floating_density", payload.get("rhopbody", floating_density))
            )
            floating_center = payload.get("floating_center", floating_center)  # type: ignore[assignment]
        points = tuple(Vec3.from_value(point) for point in vertices)
        if len(points) < 6 or len(points) % 2:
            raise GeometryEditError("drawprism 至少需要 6 个顶点且总数必须为偶数")
        if int(mask) != mask:
            raise GeometryEditError("drawprism.mask 必须是整数")
        object_role = _coerce_role(role)
        marker_kind = (
            MarkerKind.FLUID if object_role is ObjectRole.FLUID else MarkerKind.BOUND
        )
        resolved_marker = self._resolve_new_marker(marker_kind, marker)
        resolved_shapeout = self._resolve_new_shapeout(
            shapeout,
            "Fluid" if object_role is ObjectRole.FLUID else "Floating" if object_role is ObjectRole.FLOATING else "Boundary",
        )
        bounds = BoundingBox.from_points(points)
        center = (
            Vec3.from_value(floating_center)
            if floating_center is not None
            else bounds.center
        )
        lines = [
            f'<{"setmkfluid" if marker_kind is MarkerKind.FLUID else "setmkbound"} mk="{resolved_marker}" />',
            f'<drawprism mask="{int(mask)}">',
            *[f"    {_vec_tag('point', point)}" for point in points],
            "</drawprism>",
            f'<shapeout file="{_escape_attribute(resolved_shapeout, chr(34))}" />',
        ]
        self._insert_new_object(
            lines,
            object_role,
            resolved_marker,
            center,
            float(floating_density),
            f"新建棱柱 {resolved_shapeout}",
        )
        return next(scene for scene in self.objects if scene.shapeout == resolved_shapeout)

    def delete_object(self, object_id: str | int) -> None:
        """Delete an object only when all marker/output associations are unique."""

        scene = self.get_object(object_id)
        self._ensure_editable(scene)
        if scene.marker_path is None:
            raise GeometryEditError("对象缺少独立 setmk 命令，不能安全删除")
        marker_users = [obj for obj in self.objects if obj.marker_path == scene.marker_path]
        if len(marker_users) != 1:
            raise GeometryEditError("setmk 命令同时控制多个对象，已阻止删除")
        if scene.shapeout_path:
            shape_users = [obj for obj in self.objects if obj.shapeout_path == scene.shapeout_path]
            if len(shape_users) != 1:
                raise GeometryEditError("shapeout 同时输出多个对象，已阻止删除")
        if self._association_has_other_draw_references(scene):
            raise GeometryEditError("marker/shapeout 命令还关联其他绘制命令，已阻止删除")
        same_marker = [
            obj
            for obj in self.objects
            if obj is not scene
            and obj.marker_kind is scene.marker_kind
            and obj.marker == scene.marker
        ]
        if same_marker:
            raise GeometryEditError("同一 marker 仍被其他几何引用，已阻止删除")

        paths = [scene.marker_path, scene.element_path]
        if scene.shapeout_path:
            paths.append(scene.shapeout_path)
        if scene.floating_path:
            if self._floating_has_other_references(scene):
                raise GeometryEditError("floating marker 仍被其他节点引用，已阻止删除")
            paths.append(scene.floating_path)
        replacements = [_whole_line_replacement(self._text, self._source_index.require(path)) for path in paths]
        replacements = _merge_empty_replacements(replacements)
        self._commit_patches(replacements, f"删除 {scene.name}")

    def translate_object(
        self,
        object_id: str | int,
        delta_or_dx: Vec3 | Sequence[float] | float = 0.0,
        dy: float | None = None,
        dz: float | None = None,
        *,
        snap_to_grid: bool = False,
    ) -> None:
        scene = self.get_object(object_id)
        self._ensure_editable(scene)
        delta = _coerce_delta(delta_or_dx, dy, dz)
        if snap_to_grid:
            spacing = self.dp
            if spacing is None or spacing <= 0:
                raise GeometryEditError("当前 XML 没有可用的 dp，无法启用网格吸附")
            reference = scene.position or scene.center
            if reference is None:
                raise GeometryEditError("对象没有可吸附的参考点")
            snapped = snap_point(reference + delta, spacing)
            delta = snapped - reference
        if delta.length() == 0:
            return

        changes: list[tuple[str, str, str]] = []
        if scene.geometry_type is GeometryType.DRAWBOX:
            if not scene.point_paths or not scene.vertices:
                raise GeometryEditError("drawbox 缺少 point")
            changes.extend(self._vec_attribute_changes(scene.point_paths[0], scene.vertices[0] + delta))
        else:
            for path, point in zip(scene.point_paths, scene.vertices):
                changes.extend(self._vec_attribute_changes(path, point + delta))
        if scene.floating_center_path and scene.floating_center:
            changes.extend(
                self._vec_attribute_changes(
                    scene.floating_center_path, scene.floating_center + delta
                )
            )
        self._apply_attribute_changes(changes, f"平移 {scene.name}")

    # Concise aliases suit viewport controllers.
    translate = translate_object

    def scale_object(
        self,
        object_id: str | int,
        factors: Vec3 | Sequence[float] | float,
        *,
        pivot: Vec3 | Sequence[float] | None = None,
    ) -> None:
        scene = self.get_object(object_id)
        self._ensure_editable(scene)
        factor = _coerce_factors(factors)
        if factor.x <= 0 or factor.y <= 0 or factor.z <= 0:
            raise GeometryEditError("缩放系数必须全部大于 0")
        center = Vec3.from_value(pivot) if pivot is not None else scene.center
        if center is None:
            raise GeometryEditError("对象没有可用的缩放中心")
        changes: list[tuple[str, str, str]] = []

        if scene.geometry_type is GeometryType.DRAWBOX:
            if not scene.point_paths or not scene.size_path or scene.size is None:
                raise GeometryEditError("drawbox 缺少 point/size")
            origin = scale_point(scene.vertices[0], factor, center)
            new_size = Vec3(
                scene.size.x * factor.x,
                scene.size.y * factor.y,
                scene.size.z * factor.z,
            )
            changes.extend(self._vec_attribute_changes(scene.point_paths[0], origin))
            changes.extend(self._vec_attribute_changes(scene.size_path, new_size))
        else:
            for path, point in zip(scene.point_paths, scene.vertices):
                changes.extend(
                    self._vec_attribute_changes(path, scale_point(point, factor, center))
                )
        if scene.floating_center_path and scene.floating_center:
            changes.extend(
                self._vec_attribute_changes(
                    scene.floating_center_path,
                    scale_point(scene.floating_center, factor, center),
                )
            )
        self._apply_attribute_changes(changes, f"缩放 {scene.name}")

    scale = scale_object

    def rotate_object(
        self,
        object_id: str | int,
        angle_degrees: float,
        *,
        axis: str | Vec3 | Sequence[float] = "z",
        pivot: Vec3 | Sequence[float] | None = None,
    ) -> None:
        """Rotate a prism, or an axis-aligned box by a representable angle.

        GenCase ``drawbox`` has no local orientation attribute.  To avoid
        silently changing particle/fill semantics, a box edit is accepted only
        when the rotated result is still axis aligned (for example 90 degrees).
        Arbitrary orientations should be authored as ``drawprism``.
        """

        scene = self.get_object(object_id)
        self._ensure_editable(scene)
        center = Vec3.from_value(pivot) if pivot is not None else scene.center
        if center is None:
            raise GeometryEditError("对象没有可用的旋转中心")
        rotated = tuple(
            rotate_point(point, float(angle_degrees), axis=axis, pivot=center)
            for point in scene.vertices
        )
        changes: list[tuple[str, str, str]] = []
        if scene.geometry_type is GeometryType.DRAWBOX:
            minimum = Vec3(
                min(point.x for point in rotated),
                min(point.y for point in rotated),
                min(point.z for point in rotated),
            )
            maximum = Vec3(
                max(point.x for point in rotated),
                max(point.y for point in rotated),
                max(point.z for point in rotated),
            )
            tolerance = max(self.dp or 0.0, 1e-9) * 1e-5
            for point in rotated:
                if not all(
                    min(abs(value - low), abs(value - high)) <= tolerance
                    for value, low, high in zip(point, minimum, maximum)
                ):
                    raise GeometryEditError(
                        "drawbox 无法无损表达任意角度；请改用 drawprism，或旋转 90° 的整数倍"
                    )
            if not scene.point_paths or not scene.size_path:
                raise GeometryEditError("drawbox 缺少 point/size")
            changes.extend(self._vec_attribute_changes(scene.point_paths[0], minimum))
            changes.extend(self._vec_attribute_changes(scene.size_path, maximum - minimum))
        else:
            for path, point in zip(scene.point_paths, rotated):
                changes.extend(self._vec_attribute_changes(path, point))
        if scene.floating_center_path and scene.floating_center:
            changes.extend(
                self._vec_attribute_changes(
                    scene.floating_center_path,
                    rotate_point(
                        scene.floating_center,
                        float(angle_degrees),
                        axis=axis,
                        pivot=center,
                    ),
                )
            )
        self._apply_attribute_changes(changes, f"旋转 {scene.name}")

    rotate = rotate_object

    def set_vertex(
        self,
        object_id: str | int,
        vertex_index: int,
        position: Vec3 | Sequence[float],
        *,
        snap_to_grid: bool = False,
    ) -> None:
        scene = self.get_object(object_id)
        self._ensure_editable(scene)
        if not 0 <= vertex_index < len(scene.vertices):
            raise GeometryEditError(f"顶点序号不存在：{vertex_index}")
        old_vertex = scene.vertices[vertex_index]
        target = Vec3.from_value(position)
        if not all(math.isfinite(component) for component in target):
            raise GeometryEditError("顶点坐标必须是有限数值")
        if snap_to_grid:
            if self.dp is None or self.dp <= 0:
                raise GeometryEditError("当前 XML 没有可用的 dp，无法启用网格吸附")
            try:
                target = snap_point(target, self.dp)
            except (OverflowError, ValueError) as exc:
                raise GeometryEditError("顶点坐标超出网格吸附可处理的数值范围") from exc
        if target == old_vertex:
            return
        if any(
            index != vertex_index and target == vertex
            for index, vertex in enumerate(scene.vertices)
        ):
            raise GeometryEditError("顶点坐标不能与同一对象的其他顶点重复")

        vertices = list(scene.vertices)
        vertices[vertex_index] = target
        try:
            _, old_geometry_center = prism_mass_properties(scene.vertices)
            _, new_geometry_center = prism_mass_properties(vertices)
        except ValueError as exc:
            raise GeometryEditError(str(exc)) from exc
        center_shift = new_geometry_center - old_geometry_center

        changes: list[tuple[str, str, str]] = []
        if scene.geometry_type is GeometryType.DRAWPRISM:
            if vertex_index >= len(scene.point_paths):
                raise GeometryEditError("drawprism 顶点信息不完整")
            changes.extend(self._vec_attribute_changes(scene.point_paths[vertex_index], target))
            if scene.floating_center_path and scene.floating_center:
                changes.extend(
                    self._vec_attribute_changes(
                        scene.floating_center_path,
                        scene.floating_center + center_shift,
                    )
                )
            self._apply_attribute_changes(changes, f"修改 {scene.name} 顶点 {vertex_index}")
            return

        if (
            scene.geometry_type is not GeometryType.DRAWBOX
            or len(scene.vertices) != 8
            or not scene.point_paths
            or not scene.size_path
        ):
            raise GeometryEditError("drawbox 顶点信息不完整")

        draw_mode, mask = _boxfill_prism_spec(scene.box_fill)
        restore_mode = self._draw_mode_before(scene)
        replacements = [
            self._drawbox_prism_replacement(
                scene,
                vertices,
                draw_mode=draw_mode,
                mask=mask,
                restore_mode=restore_mode,
            )
        ]
        if scene.floating_center_path and scene.floating_center:
            replacements.extend(
                self._attribute_replacements(
                    self._vec_attribute_changes(
                        scene.floating_center_path,
                        scene.floating_center + center_shift,
                    )
                )
            )
        self._commit_patches(
            replacements,
            f"修改 {scene.name} 顶点 {vertex_index}（drawbox 转 drawprism）",
        )

    set_object_vertex = set_vertex

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        previous, description = self._undo_stack.pop()
        current = self._text
        state = self._build_state(previous)
        self._redo_stack.append((current, description))
        self._restore_prebuilt(previous, state)
        return True

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        following, description = self._redo_stack.pop()
        current = self._text
        state = self._build_state(following)
        self._undo_stack.append((current, description))
        self._restore_prebuilt(following, state)
        return True

    def save(
        self,
        path: str | Path | None = None,
        *,
        create_backup: bool = True,
    ) -> SaveResult:
        """Explicitly save, backing up an existing target before replacement."""

        if self._draft_text is not None:
            raise XmlSyntaxError("源码编辑器中仍有无效 XML；请修正或放弃草稿后再保存")
        target = Path(path).resolve() if path is not None else self.path
        if target is None:
            raise CaseDocumentError("首次保存需要指定文件路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        backup_path: Path | None = None
        if target.exists() and create_backup:
            try:
                backup_path = self._create_backup(target)
            except OSError as exc:
                raise BackupError(f"无法创建安全备份，原文件未覆盖：{exc}") from exc

        data = self.to_bytes()
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        self.path = target
        self._saved_text = self._text
        return SaveResult(target, backup_path, len(data))

    def write_recovery_copy(self) -> Path:
        """Write a non-authoritative crash-recovery copy next to the case."""

        if self.path is None:
            raise CaseDocumentError("未命名文档无法创建恢复副本")
        directory = self.path.parent / ".dualsphysics-studio" / "recovery"
        directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(self.path).encode("utf-8")).hexdigest()[:16]
        recovery = directory / f"{self.path.stem}-{digest}.xml"
        recovery.write_bytes(self.to_bytes())
        return recovery

    def _resolve_new_marker(
        self, marker_kind: MarkerKind, requested: int | None
    ) -> int:
        used = {
            scene.marker
            for scene in self.objects
            if scene.marker_kind is marker_kind and scene.marker is not None
        }
        count: int | None = None
        mkconfig = next(iter(iter_elements(self._root, "mkconfig")), None)
        if mkconfig is not None:
            count = parse_integer(
                mkconfig.get("fluidcount" if marker_kind is MarkerKind.FLUID else "boundcount")
            )
        if requested is not None:
            if not isinstance(requested, int) or isinstance(requested, bool) or requested < 0:
                raise GeometryEditError("marker 必须是非负整数")
            if requested in used:
                raise GeometryEditError(f"本地 marker={requested} 已被使用")
            if count is not None and requested >= count:
                raise GeometryEditError(f"marker={requested} 超出 mkconfig 范围 0..{count - 1}")
            return requested
        candidate = 0
        while candidate in used:
            candidate += 1
        if count is not None and candidate >= count:
            raise GeometryEditError("mkconfig 中没有可用的本地 marker")
        return candidate

    def _resolve_new_shapeout(self, requested: str | None, base: str) -> str:
        used = {scene.shapeout.casefold() for scene in self.objects if scene.shapeout}
        if requested is not None:
            candidate = requested.strip()
            if not candidate:
                raise GeometryEditError("shapeout 文件名不能为空")
            if candidate.casefold() in used:
                raise GeometryEditError(f"shapeout 文件名“{candidate}”已被使用")
            return candidate
        index = 1
        candidate = f"{base}_{index}"
        while candidate.casefold() in used:
            index += 1
            candidate = f"{base}_{index}"
        return candidate

    def _insert_new_object(
        self,
        object_lines: list[str],
        role: ObjectRole,
        marker: int,
        floating_center: Vec3,
        floating_density: float,
        description: str,
    ) -> None:
        if not math.isfinite(floating_density) or floating_density <= 0:
            raise GeometryEditError("浮体密度必须是大于 0 的有限数值")
        mainlist = next(iter(iter_elements(self._root, "mainlist")), None)
        if mainlist is None:
            raise GeometryEditError("缺少 geometry/commands/mainlist，无法图形化新建对象")
        mainlist_path = self._model.path_by_identity[id(mainlist)]
        replacements = [
            _child_block_insertion(
                self._text,
                self._source_index.require(mainlist_path),
                object_lines,
            )
        ]
        if role is ObjectRole.FLOATING:
            floating_lines = [
                f'<floating mkbound="{marker}" rhopbody="{_format_number(floating_density)}">',
                f"    {_vec_tag('center', floating_center)}",
                "</floating>",
            ]
            floatings = next(iter(iter_elements(self._root, "floatings")), None)
            if floatings is not None:
                floating_path = self._model.path_by_identity[id(floatings)]
                replacements.append(
                    _child_block_insertion(
                        self._text,
                        self._source_index.require(floating_path),
                        floating_lines,
                    )
                )
            else:
                casedef = next(iter(iter_elements(self._root, "casedef")), None)
                if casedef is None:
                    raise GeometryEditError("缺少 casedef，无法创建 floating 关联")
                casedef_path = self._model.path_by_identity[id(casedef)]
                replacements.append(
                    _child_block_insertion(
                        self._text,
                        self._source_index.require(casedef_path),
                        ["<floatings>", *[f"    {line}" for line in floating_lines], "</floatings>"],
                    )
                )
        self._commit_patches(replacements, description)

    def _association_has_other_draw_references(self, scene: SceneObject) -> bool:
        if scene.marker_path is None:
            return True
        parent_path = scene.element_path.rsplit("/", 1)[0]
        parent = self._model.element_by_path.get(parent_path)
        marker_element = self._model.element_by_path.get(scene.marker_path)
        draw_element = self._model.element_by_path.get(scene.element_path)
        if parent is None or marker_element is None or draw_element is None:
            return True
        children = [child for child in list(parent) if is_element(child)]
        try:
            marker_index = children.index(marker_element)
            draw_index = children.index(draw_element)
        except ValueError:
            return True
        end_index = len(children)
        for index in range(marker_index + 1, len(children)):
            if local_name(children[index].tag) in ("setmkfluid", "setmkbound"):
                end_index = index
                break
        for index in range(marker_index + 1, end_index):
            child = children[index]
            name = local_name(child.tag)
            is_drawing = name.startswith("draw") or name.startswith("fill")
            if is_drawing and index != draw_index:
                return True

        if scene.shapeout_path:
            shapeout_element = self._model.element_by_path.get(scene.shapeout_path)
            if shapeout_element is None:
                return True
            try:
                shapeout_index = children.index(shapeout_element)
            except ValueError:
                return True
            previous_shapeout = -1
            for index in range(shapeout_index - 1, -1, -1):
                if local_name(children[index].tag) == "shapeout":
                    previous_shapeout = index
                    break
            for index in range(previous_shapeout + 1, shapeout_index):
                name = local_name(children[index].tag)
                is_drawing = name.startswith("draw") or name.startswith("fill")
                if is_drawing and index != draw_index:
                    return True
        return False

    def _floating_has_other_references(self, scene: SceneObject) -> bool:
        if scene.floating_path is None or scene.marker is None:
            return False
        floating_element = self._model.element_by_path.get(scene.floating_path)
        for element in self._root.iter():
            if not is_element(element) or element is floating_element:
                continue
            if parse_integer(element.get("mkbound")) == scene.marker:
                return True
        return False

    def _create_backup(self, target: Path) -> Path:
        directory = (
            target.parent / ".dualsphysics-studio" / "backups" / target.name
        )
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = directory / f"{timestamp}.xml"
        counter = 1
        while backup.exists():
            backup = directory / f"{timestamp}-{counter}.xml"
            counter += 1
        shutil.copy2(target, backup)
        return backup

    def _ensure_editable(self, scene: SceneObject) -> None:
        if not scene.editable:
            raise ReadOnlyGeometryError(
                f"对象“{scene.name}”仅支持源码编辑：{scene.readonly_reason or '未知原因'}"
            )

    def _vec_attribute_changes(
        self, element_path: str, value: Vec3
    ) -> list[tuple[str, str, str]]:
        element = self.element(element_path)
        return [
            (
                element_path,
                axis,
                _format_number(component, element.get(axis)),
            )
            for axis, component in zip(("x", "y", "z"), value)
        ]

    def _draw_mode_before(self, scene: SceneObject) -> str:
        parent_path = scene.element_path.rsplit("/", 1)[0]
        parent = self._model.element_by_path.get(parent_path)
        draw_element = self._model.element_by_path.get(scene.element_path)
        if parent is None or draw_element is None:
            raise GeometryEditError("无法定位 drawbox 所在的 mainlist")

        active_mode: str | None = None
        for child in list(parent):
            if not is_element(child):
                continue
            if child is draw_element:
                break
            if local_name(child.tag) != "setdrawmode":
                continue
            raw_mode = child.get("mode")
            if raw_mode is None:
                raw_mode = child.text
            mode = (raw_mode or "").strip().casefold()
            if mode not in _DRAW_MODES:
                raise GeometryEditError(
                    "drawbox 前方的 setdrawmode 不是可识别的固定模式，无法安全恢复"
                )
            active_mode = mode
        if active_mode is None:
            raise GeometryEditError(
                "drawbox 前方没有显式 setdrawmode，无法在转换后安全恢复绘制模式"
            )
        return active_mode

    def _drawbox_prism_replacement(
        self,
        scene: SceneObject,
        vertices: Sequence[Vec3],
        *,
        draw_mode: str,
        mask: str,
        restore_mode: str,
    ) -> _Replacement:
        token = self._source_index.require(scene.element_path)
        if token.self_closing:
            raise GeometryEditError("自闭合 drawbox 无法转换为 drawprism")
        command = self.element(scene.element_path)
        if command.attrib:
            raise GeometryEditError("带属性的 drawbox 无法保真转换为 drawprism")

        newline = "\r\n" if "\r\n" in self._text else "\n"
        indent = _line_indent(self._text, token.start)
        child_indent = indent + "    "
        original = self._text[token.start : token.end]
        comment = _source_block_comment(original, indent=indent, newline=newline)
        lines = [
            comment,
            f'{indent}<setdrawmode mode="{draw_mode}" />',
            f'{indent}<drawprism mask="{mask}">',
            *[f"{child_indent}{_vec_tag('point', point)}" for point in vertices],
            f"{indent}</drawprism>",
            f'{indent}<setdrawmode mode="{restore_mode}" />',
        ]
        return _Replacement(token.start, token.end, newline.join(lines))

    def _attribute_replacements(
        self,
        changes: Iterable[tuple[str, str, str]],
    ) -> list[_Replacement]:
        replacements: list[_Replacement] = []
        for element_path, attribute, value in changes:
            if not _ATTRIBUTE_NAME.match(attribute):
                raise SourceLocationError(f"无效的 XML 属性名：{attribute}")
            token = self._source_index.require(element_path)
            replacements.append(
                _attribute_replacement(
                    self._text,
                    token.start,
                    token.start_tag_end,
                    attribute,
                    value,
                )
            )
        return replacements

    def _apply_attribute_changes(
        self,
        changes: Iterable[tuple[str, str, str]],
        description: str,
    ) -> None:
        self._commit_patches(self._attribute_replacements(changes), description)

    def _commit_patches(self, replacements: list[_Replacement], description: str) -> None:
        if not replacements:
            return
        replacements.sort(key=lambda item: (item.start, item.end), reverse=True)
        previous_start = len(self._text) + 1
        new_text = self._text
        for replacement in replacements:
            if replacement.end > previous_start:
                raise SourceLocationError("XML 局部修改范围发生重叠")
            new_text = (
                new_text[: replacement.start]
                + replacement.value
                + new_text[replacement.end :]
            )
            previous_start = replacement.start
        state = self._build_state(new_text)
        self._commit_prebuilt(new_text, state, description)

    def _commit_prebuilt(
        self,
        text: str,
        state: tuple[ET.Element, SourceIndex, SemanticModel],
        description: str,
    ) -> None:
        if text == self._text:
            self.discard_draft()
            return
        self._undo_stack.append((self._text, description))
        self._redo_stack.clear()
        self._restore_prebuilt(text, state)

    def _restore_prebuilt(
        self,
        text: str,
        state: tuple[ET.Element, SourceIndex, SemanticModel],
    ) -> None:
        self._text = text
        self._root, self._source_index, self._model = state
        self.discard_draft()


def _attribute_replacement(
    text: str,
    tag_start: int,
    tag_end: int,
    attribute: str,
    value: str,
) -> _Replacement:
    raw_tag = text[tag_start:tag_end]
    pattern = re.compile(
        rf"(?P<prefix>\s+{re.escape(attribute)}\s*=\s*)(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
        re.DOTALL,
    )
    match = pattern.search(raw_tag)
    if match:
        quote = match.group("quote")
        escaped = _escape_attribute(value, quote)
        return _Replacement(
            tag_start + match.start("value"),
            tag_start + match.end("value"),
            escaped,
        )
    closing_offset = raw_tag.rfind("/>")
    if closing_offset < 0:
        closing_offset = raw_tag.rfind(">")
    if closing_offset < 0:
        raise SourceLocationError("无法定位 XML 起始标签结尾")
    escaped = _escape_attribute(value, '"')
    return _Replacement(
        tag_start + closing_offset,
        tag_start + closing_offset,
        f' {attribute}="{escaped}"',
    )


def _escape_attribute(value: str, quote: str) -> str:
    escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if quote == '"':
        return escaped.replace('"', "&quot;")
    return escaped.replace("'", "&apos;")


def _child_block_insertion(text: str, token, lines: Sequence[str]) -> _Replacement:
    if token.self_closing or token.close_start is None:
        raise SourceLocationError(f"{token.name} 是自闭合节点，无法插入子命令")
    newline = "\r\n" if "\r\n" in text else "\n"
    line_start = text.rfind("\n", 0, token.close_start) + 1
    closing_indent = text[line_start:token.close_start]
    if closing_indent.strip():
        # Compact ``<parent></parent>`` form: expand just before the close tag.
        insertion_start = token.close_start
        closing_indent = _line_indent(text, token.start)
        prefix = newline
    else:
        insertion_start = line_start
        prefix = ""
    child_indent = closing_indent + "    "
    block = newline.join(child_indent + line for line in lines)
    return _Replacement(
        insertion_start,
        insertion_start,
        prefix + block + newline + closing_indent,
    )


def _line_indent(text: str, position: int) -> str:
    start = text.rfind("\n", 0, position) + 1
    candidate = text[start:position]
    return candidate if not candidate.strip() else ""


def _source_block_comment(source: str, *, indent: str, newline: str) -> str:
    """Keep replaced XML recoverable while always producing a legal comment."""

    label = "DualSPHysics Studio 原始 drawbox（顶点编辑前）"
    if "--" not in source and not source.endswith("-"):
        return f"<!-- {label}:{newline}{indent}{source}{newline}{indent}-->"
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return f"<!-- {label}（UTF-8 Base64）：{encoded} -->"


def _whole_line_replacement(text: str, token) -> _Replacement:
    start = token.start
    line_start = text.rfind("\n", 0, start) + 1
    if not text[line_start:start].strip():
        start = line_start
    end = token.end
    line_end = text.find("\n", end)
    if line_end >= 0 and not text[end:line_end].strip():
        end = line_end + 1
    return _Replacement(start, end, "")


def _merge_empty_replacements(replacements: list[_Replacement]) -> list[_Replacement]:
    if any(replacement.value for replacement in replacements):
        return replacements
    ordered = sorted(replacements, key=lambda item: (item.start, item.end))
    merged: list[_Replacement] = []
    for replacement in ordered:
        if merged and replacement.start <= merged[-1].end:
            previous = merged[-1]
            merged[-1] = _Replacement(previous.start, max(previous.end, replacement.end), "")
        else:
            merged.append(replacement)
    return merged


def _vec_tag(name: str, value: Vec3) -> str:
    return (
        f'<{name} x="{_format_number(value.x)}" y="{_format_number(value.y)}" '
        f'z="{_format_number(value.z)}" />'
    )


def _coerce_role(role: ObjectRole | str) -> ObjectRole:
    if isinstance(role, ObjectRole):
        return role
    normalized = str(role).strip().casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "fluid": ObjectRole.FLUID,
        "fixed": ObjectRole.FIXED_BOUNDARY,
        "bound": ObjectRole.FIXED_BOUNDARY,
        "boundary": ObjectRole.FIXED_BOUNDARY,
        "fixed_boundary": ObjectRole.FIXED_BOUNDARY,
        "floating": ObjectRole.FLOATING,
        "float": ObjectRole.FLOATING,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise GeometryEditError(f"不支持的对象类型：{role}") from exc


def _coerce_delta(
    delta_or_dx: Vec3 | Sequence[float] | float,
    dy: float | None,
    dz: float | None,
) -> Vec3:
    if isinstance(delta_or_dx, Vec3):
        if dy is not None or dz is not None:
            raise ValueError("使用 Vec3 时不要再传 dy/dz")
        return delta_or_dx
    if isinstance(delta_or_dx, Sequence) and not isinstance(delta_or_dx, (str, bytes)):
        if dy is not None or dz is not None:
            raise ValueError("使用坐标序列时不要再传 dy/dz")
        return Vec3.from_value(delta_or_dx)  # type: ignore[arg-type]
    return Vec3(float(delta_or_dx), float(dy or 0.0), float(dz or 0.0))


def _coerce_factors(factors: Vec3 | Sequence[float] | float) -> Vec3:
    if isinstance(factors, Vec3):
        return factors
    if isinstance(factors, Sequence) and not isinstance(factors, (str, bytes)):
        return Vec3.from_value(factors)  # type: ignore[arg-type]
    value = float(factors)
    return Vec3(value, value, value)


def _format_number(value: float, original: str | None = None) -> str:
    if not math.isfinite(value):
        raise GeometryEditError("坐标必须是有限数值")
    if abs(value) < 5e-15:
        value = 0.0
    original = (original or "").strip()
    match = _NUMBER_TOKEN.match(original)
    if match and match.group(5) is None:
        decimals = len(match.group(3) if match.group(3) is not None else match.group(4) or "")
        if decimals:
            candidate = f"{value:.{decimals}f}"
            if math.isclose(float(candidate), value, rel_tol=0.0, abs_tol=10 ** (-(decimals + 3))):
                return candidate
        elif float(value).is_integer():
            return str(int(round(value)))
    candidate = f"{value:.12g}"
    return "0" if candidate in ("-0", "-0.0") else candidate
