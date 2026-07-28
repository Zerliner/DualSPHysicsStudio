"""Focused, non-technical geometry dialogs used by the main window."""

from __future__ import annotations

from math import isfinite
from typing import Any

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .models import vertices_of


def _spin(value: float, parent: QWidget, *, positive: bool = False) -> QDoubleSpinBox:
    editor = QDoubleSpinBox(parent)
    editor.setDecimals(8)
    editor.setRange(0.00000001 if positive else -1_000_000_000.0, 1_000_000_000.0)
    editor.setSingleStep(0.01)
    editor.setValue(float(value))
    editor.setAccelerated(True)
    return editor


class VectorDialog(QDialog):
    """Edit translation or scale triples."""

    def __init__(
        self,
        title: str,
        labels: tuple[str, str, str],
        values: tuple[float, float, float],
        *,
        positive: bool = False,
        allow_snap: bool = False,
        dp: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(330)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.editors = tuple(_spin(value, self, positive=positive) for value in values)
        for label, editor in zip(labels, self.editors):
            form.addRow(label, editor)
        layout.addLayout(form)
        self.snap_check = QCheckBox("吸附到 dp 网格", self)
        self.snap_check.setVisible(allow_snap)
        self.snap_check.setEnabled(bool(dp and dp > 0))
        if dp and dp > 0:
            self.snap_check.setToolTip(f"当前 dp = {dp:.8g} m")
        else:
            self.snap_check.setToolTip("当前 XML 没有可用的 dp")
        layout.addWidget(self.snap_check)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def vector(self) -> tuple[float, float, float]:
        return tuple(editor.value() for editor in self.editors)  # type: ignore[return-value]

    def snap_to_grid(self) -> bool:
        return self.snap_check.isVisible() and self.snap_check.isChecked()


class RotationDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("旋转对象")
        self.setModal(True)
        self.setMinimumWidth(330)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.axis_combo = QComboBox(self)
        self.axis_combo.setObjectName("rotation_axis")
        self.axis_combo.addItem("X 轴", "x")
        self.axis_combo.addItem("Y 轴", "y")
        self.axis_combo.addItem("Z 轴", "z")
        self.axis_combo.setCurrentIndex(2)
        self.angle_edit = _spin(0.0, self)
        self.angle_edit.setObjectName("rotation_angle")
        self.angle_edit.setRange(-36000.0, 36000.0)
        self.angle_edit.setSuffix(" °")
        self.angle_edit.setSingleStep(5.0)
        form.addRow("旋转轴", self.axis_combo)
        form.addRow("角度", self.angle_edit)
        layout.addLayout(form)
        note = QLabel("drawbox 仅能无损保存仍保持轴对齐的角度（例如 90°）。", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def axis(self) -> str:
        return str(self.axis_combo.currentData())

    def angle(self) -> float:
        return self.angle_edit.value()


class VertexDialog(QDialog):
    """Select and edit exactly one vertex so the operation stays undoable."""

    def __init__(
        self,
        obj: Any,
        *,
        dp: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑顶点")
        self.setModal(True)
        self.setMinimumWidth(360)
        self._vertices = vertices_of(obj)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.index_combo = QComboBox(self)
        self.index_combo.setObjectName("vertex_index")
        for index, point in enumerate(self._vertices):
            self.index_combo.addItem(
                f"顶点 {index + 1}  ({point[0]:.6g}, {point[1]:.6g}, {point[2]:.6g})",
                index,
            )
        form.addRow("选择顶点", self.index_combo)
        initial = self._vertices[0] if self._vertices else (0.0, 0.0, 0.0)
        self.editors = tuple(_spin(value, self) for value in initial)
        for label, editor in zip(("X", "Y", "Z"), self.editors):
            form.addRow(label, editor)
        layout.addLayout(form)
        self.snap_check = QCheckBox("吸附到 dp 网格", self)
        self.snap_check.setEnabled(bool(dp and dp > 0))
        self.snap_check.setToolTip(f"当前 dp = {dp:.8g} m" if dp else "当前 XML 没有可用的 dp")
        layout.addWidget(self.snap_check)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(self._vertices))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.index_combo.currentIndexChanged.connect(self._load_vertex)

    def _load_vertex(self, combo_index: int) -> None:
        if not 0 <= combo_index < len(self._vertices):
            return
        for editor, value in zip(self.editors, self._vertices[combo_index]):
            editor.setValue(value)

    def vertex_index(self) -> int:
        return int(self.index_combo.currentData())

    def position(self) -> tuple[float, float, float]:
        return tuple(editor.value() for editor in self.editors)  # type: ignore[return-value]

    def snap_to_grid(self) -> bool:
        return self.snap_check.isChecked() and self.snap_check.isEnabled()


class VertexCoordinateDialog(QDialog):
    """Compact exact-coordinate editor opened beside a selected vertex."""

    def __init__(
        self,
        position: tuple[float, float, float],
        *,
        global_position: QPoint | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("精确设置顶点坐标")
        self.setModal(True)
        self._position = tuple(float(value) for value in position)
        self.setMinimumWidth(270)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.editors = tuple(QLineEdit(f"{value:.12g}", self) for value in position)
        for label, editor in zip(("X", "Y", "Z"), self.editors):
            editor.setObjectName(f"vertex_coordinate_{label.lower()}")
            editor.setPlaceholderText("有限数值")
            form.addRow(label, editor)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        save_button.setText("保存")
        save_button.setDefault(True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if global_position is not None:
            self.move(global_position + QPoint(14, 10))

    def _validate_and_accept(self) -> None:
        values: list[float] = []
        for label, editor in zip(("X", "Y", "Z"), self.editors):
            text = editor.text().strip()
            try:
                value = float(text)
            except ValueError:
                QMessageBox.warning(self, "坐标无效", f"{label} 坐标必须是数值。")
                editor.setFocus()
                editor.selectAll()
                return
            if not isfinite(value):
                QMessageBox.warning(self, "坐标无效", f"{label} 坐标必须是有限数值。")
                editor.setFocus()
                editor.selectAll()
                return
            values.append(value)
        self._position = tuple(values)
        self.accept()

    def position(self) -> tuple[float, float, float]:
        return self._position


class NewObjectDialog(QDialog):
    """Collect a conservative axis-aligned starter shape for a controller."""

    def __init__(
        self,
        kind: str,
        *,
        used_markers: dict[str, set[int]] | None = None,
        used_names: set[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.kind = kind
        self._used_markers = used_markers or {}
        self.setWindowTitle("新建方箱" if kind == "box" else "新建棱柱")
        self.setModal(True)
        self.setMinimumWidth(390)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.category_combo = QComboBox(self)
        self.category_combo.addItem("流体", "fluid")
        self.category_combo.addItem("固定边界", "fixed")
        self.category_combo.addItem("浮动刚体", "floating")
        self.marker_edit = QSpinBox(self)
        self.marker_edit.setRange(0, 999_999)
        self.marker_edit.setValue(0)
        base_name = "NewBox" if kind == "box" else "NewPrism"
        existing = {name.casefold() for name in used_names or set()}
        suggested_name = base_name
        suffix = 2
        while suggested_name.casefold() in existing:
            suggested_name = f"{base_name}{suffix}"
            suffix += 1
        self.name_edit = QLineEdit(suggested_name, self)
        form.addRow("对象类别", self.category_combo)
        form.addRow("本地 Marker", self.marker_edit)
        form.addRow("ShapeOut 名称", self.name_edit)
        self.point_editors = tuple(_spin(0.0, self) for _ in range(3))
        self.size_editors = tuple(_spin(0.1, self, positive=True) for _ in range(3))
        for label, editor in zip(("起点 X", "起点 Y", "起点 Z"), self.point_editors):
            form.addRow(label, editor)
        for label, editor in zip(("尺寸 X", "尺寸 Y", "尺寸 Z"), self.size_editors):
            form.addRow(label, editor)
        layout.addLayout(form)
        note = QLabel(
            "棱柱将先创建为两个对应矩形截面；创建后可逐顶点调整。"
            if kind == "prism"
            else "方箱使用起点与三轴尺寸创建。",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("创建")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.category_combo.currentIndexChanged.connect(self._suggest_marker)
        self._suggest_marker()

    def _suggest_marker(self, _index: int = 0) -> None:
        category = str(self.category_combo.currentData())
        namespace = "fluid" if category == "fluid" else "bound"
        used = self._used_markers.get(namespace, set())
        marker = 0
        while marker in used:
            marker += 1
        self.marker_edit.setValue(marker)

    def payload(self) -> dict[str, Any]:
        point = tuple(editor.value() for editor in self.point_editors)
        size = tuple(editor.value() for editor in self.size_editors)
        payload: dict[str, Any] = {
            "kind": self.kind,
            "category": self.category_combo.currentData(),
            "marker": self.marker_edit.value(),
            "shapeout": self.name_edit.text().strip(),
            "point": point,
            "size": size,
        }
        if self.kind == "prism":
            x, y, z = point
            sx, sy, sz = size
            # First four points and the corresponding second cross-section use
            # the same convention as DualSPHysics drawprism.
            payload["vertices"] = (
                (x, y, z),
                (x, y, z + sz),
                (x + sx, y, z + sz),
                (x + sx, y, z),
                (x, y + sy, z),
                (x, y + sy, z + sz),
                (x + sx, y + sy, z + sz),
                (x + sx, y + sy, z),
            )
        return payload


__all__ = [
    "NewObjectDialog",
    "RotationDialog",
    "VectorDialog",
    "VertexCoordinateDialog",
    "VertexDialog",
]
