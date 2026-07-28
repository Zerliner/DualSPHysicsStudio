"""Native VTK viewport embedded in a PySide6 widget.

Importing this module does not require VTK.  When the optional runtime cannot
be imported, :class:`VTKViewport` keeps the same public API and presents an
actionable placeholder instead of preventing the XML editor from starting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from .models import (
    CATEGORY_COLORS,
    box_spec,
    build_scene_entries,
    faces_of,
    scene_object_kind,
    scene_object_editable,
    vertices_of,
)


VTK_AVAILABLE = False
VTK_IMPORT_ERROR = ""
_VTK_OUTPUT_CAPTURE: Any | None = None

try:  # pragma: no branch - only one branch is available in an installed app.
    from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
    from vtkmodules.vtkCommonCore import vtkOutputWindow, vtkPoints, vtkStringOutputWindow
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    from vtkmodules.vtkFiltersSources import vtkCubeSource, vtkOutlineSource
    from vtkmodules.vtkIOGeometry import vtkSTLReader
    from vtkmodules.vtkIOLegacy import vtkDataSetReader
    from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
    from vtkmodules.vtkInteractionWidgets import (
        vtkHandleWidget,
        vtkOrientationMarkerWidget,
        vtkSphereHandleRepresentation,
    )
    from vtkmodules.vtkRenderingAnnotation import vtkAxesActor
    from vtkmodules.vtkRenderingCore import (
        vtkActor,
        vtkCellPicker,
        vtkDataSetMapper,
        vtkPolyDataMapper,
        vtkRenderer,
    )

    # These imports register interaction and OpenGL factories with VTK.
    import vtkmodules.vtkRenderingOpenGL2  # noqa: F401

    # VTK's Windows default is a separate native ``vtkOutputWindow``.  It can
    # appear as a blank orphan beside an otherwise healthy Qt application.
    # Retain diagnostics in memory instead; user-facing load errors are routed
    # through ``vtkLoadFailed`` below.
    _VTK_OUTPUT_CAPTURE = vtkStringOutputWindow()
    vtkOutputWindow.SetInstance(_VTK_OUTPUT_CAPTURE)
    VTK_AVAILABLE = True
except Exception as exc:  # pragma: no cover - exercised in packages without VTK.
    VTK_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def _lighter(color: tuple[float, float, float], factor: float = 1.32) -> tuple[float, float, float]:
    return tuple(min(1.0, channel * factor + 0.08) for channel in color)


_VERTEX_BLUE = (0.12, 0.48, 0.96)
_VERTEX_YELLOW = (1.0, 0.95, 0.0)
_VERTEX_HANDLE_SIZE = 14.0
_SELECTED_VERTEX_HANDLE_SIZE = 22.0
_RIGHT_CLICK_TOLERANCE = 5


class VTKViewport(QWidget):
    """Display editable previews and authoritative legacy VTK artifacts."""

    selectionChanged = Signal(str)
    translationRequested = Signal(str, float, float, float)
    vertexCommitRequested = Signal(str, int, object)
    vertexContextMenuRequested = Signal(str, int, object, QPoint)
    vtkLoaded = Signal(str)
    vtkLoadFailed = Signal(str, str)
    stlLoaded = Signal(str)
    stlLoadFailed = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("viewport")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(360, 260)

        self._actor_by_key: dict[str, Any] = {}
        self._key_by_actor_id: dict[int, str] = {}
        self._base_color: dict[str, tuple[float, float, float]] = {}
        self._scene_keys: set[str] = set()
        self._editable_keys: set[str] = set()
        self._vertices_by_key: dict[str, tuple[tuple[float, float, float], ...]] = {}
        self._vtk_keys: set[str] = set()
        self._domain_actor: Any | None = None
        self._selected_key: str | None = None
        self._initialized = False
        self._orthographic = False
        self._ctrl_rotating = False
        self._vertex_representations: list[Any] = []
        self._vertex_widgets: list[Any] = []
        self._selected_vertex_index: int | None = None
        self._vertex_interacting = False
        self._right_press_position: tuple[int, int] | None = None
        self._axes_visible = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if not VTK_AVAILABLE:
            self._placeholder = QLabel(
                "3D 视图暂不可用\n\n未能载入 VTK 运行库。XML 查看、编辑和 GenCase 运行仍可正常使用。",
                self,
            )
            self._placeholder.setObjectName("vtk_unavailable_placeholder")
            self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._placeholder.setWordWrap(True)
            self._placeholder.setToolTip(VTK_IMPORT_ERROR)
            palette = self._placeholder.palette()
            palette.setColor(QPalette.ColorRole.Window, QColor("#17202b"))
            palette.setColor(QPalette.ColorRole.WindowText, QColor("#c7d1dc"))
            self._placeholder.setAutoFillBackground(True)
            self._placeholder.setPalette(palette)
            layout.addWidget(self._placeholder)
            self._vtk_widget = None
            self._renderer = None
            self._axes_widget = None
            return

        self._placeholder = None
        self._vtk_widget = QVTKRenderWindowInteractor(self)
        self._vtk_widget.setObjectName("vtk_render_widget")
        layout.addWidget(self._vtk_widget)

        self._renderer = vtkRenderer()
        self._renderer.SetBackground(0.055, 0.075, 0.105)
        self._renderer.SetBackground2(0.12, 0.16, 0.21)
        self._renderer.GradientBackgroundOn()
        self._vtk_widget.GetRenderWindow().AddRenderer(self._renderer)

        self._picker = vtkCellPicker()
        self._picker.SetTolerance(0.004)
        interactor = self._vtk_widget.GetRenderWindow().GetInteractor()
        self._interaction_style = vtkInteractorStyleTrackballCamera()
        self._interaction_style.AddObserver(
            "LeftButtonPressEvent", self._on_camera_left_button
        )
        self._interaction_style.AddObserver(
            "LeftButtonReleaseEvent", self._on_camera_left_button
        )
        self._interaction_style.AddObserver(
            "RightButtonPressEvent", self._on_camera_right_button
        )
        self._interaction_style.AddObserver(
            "RightButtonReleaseEvent", self._on_camera_right_button
        )
        interactor.SetInteractorStyle(self._interaction_style)
        # Interactive vertex widgets must see presses before the scene picker.
        # A lower picker priority keeps geometry behind a vertex from stealing
        # selection when a vertex drag begins.
        interactor.AddObserver("LeftButtonPressEvent", self._on_left_button, -0.1)
        interactor.AddObserver("KeyPressEvent", self._on_key_press, 1.0)

        axes = vtkAxesActor()
        axes.SetTotalLength(0.75, 0.75, 0.75)
        axes.SetShaftTypeToCylinder()
        self._axes_widget = vtkOrientationMarkerWidget()
        self._axes_widget.SetOrientationMarker(axes)
        self._axes_widget.SetInteractor(interactor)
        self._axes_widget.SetViewport(0.0, 0.0, 0.18, 0.24)
        self._axes_widget.InteractiveOff()

        # Ctrl+left rotation takes precedence over the handle (priority 0.5).
        interactor.AddObserver("LeftButtonPressEvent", self._prepare_ctrl_rotation, 1.0)

        self._coordinate_label = QLabel(self._vtk_widget)
        self._coordinate_label.setObjectName("vertex_coordinate_label")
        self._coordinate_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._coordinate_label.setStyleSheet(
            "QLabel { color: #eef6ff; background: rgba(16, 34, 55, 220); "
            "border: 1px solid #4f9ee8; border-radius: 4px; padding: 4px 7px; }"
        )
        self._coordinate_label.hide()

    @property
    def vtk_available(self) -> bool:
        return VTK_AVAILABLE

    @property
    def selected_key(self) -> str | None:
        return self._selected_key

    @property
    def scene_object_count(self) -> int:
        return len(self._scene_keys)

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        if not VTK_AVAILABLE or self._initialized:
            return
        try:
            interactor = self._vtk_widget.GetRenderWindow().GetInteractor()
            interactor.Initialize()
            self._axes_widget.SetEnabled(1 if self._axes_visible else 0)
            self._initialized = True
            self._safe_render()
        except Exception:
            # Some remote desktop/OpenGL setups initialize only after the next
            # resize.  Rendering is retried on subsequent user operations.
            self._initialized = False

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if VTK_AVAILABLE:
            self._clear_vertex_widgets(restore=False)
        super().closeEvent(event)

    def set_scene_objects(self, objects: Any) -> None:
        """Replace the approximate XML geometry while retaining loaded VTK."""

        if not VTK_AVAILABLE:
            return
        self._clear_vertex_widgets(restore=False)
        for key in tuple(self._scene_keys):
            self._remove_actor(key)
        self._scene_keys.clear()
        self._editable_keys.clear()
        self._vertices_by_key.clear()

        for entry in build_scene_entries(objects or ()):
            actor = self._actor_for_object(entry.source)
            if actor is None:
                continue
            color = CATEGORY_COLORS[entry.category]
            self._configure_actor(actor, entry.key, color, entry.category)
            self._scene_keys.add(entry.key)
            if scene_object_editable(entry.source):
                self._editable_keys.add(entry.key)
                self._vertices_by_key[entry.key] = tuple(
                    self._vertices_for_object(entry.source)
                )
            self._renderer.AddActor(actor)

        if self._scene_keys:
            self.reset_camera()
        else:
            self._safe_render()

    def set_definition_bounds(self, bounds: Any | None) -> None:
        """Show the XML definition domain as a non-pickable wireframe."""

        if not VTK_AVAILABLE:
            return
        if self._domain_actor is not None:
            self._renderer.RemoveActor(self._domain_actor)
            self._domain_actor = None
        if bounds is None:
            self._safe_render()
            return
        minimum = getattr(bounds, "minimum", None)
        maximum = getattr(bounds, "maximum", None)
        if isinstance(bounds, dict):
            minimum = bounds.get("minimum") or bounds.get("min")
            maximum = bounds.get("maximum") or bounds.get("max")

        def xyz(value: Any) -> tuple[float, float, float] | None:
            try:
                if all(hasattr(value, axis) for axis in "xyz"):
                    return (float(value.x), float(value.y), float(value.z))
                return (float(value[0]), float(value[1]), float(value[2]))
            except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                return None

        low = xyz(minimum)
        high = xyz(maximum)
        if low is None or high is None or any(high[i] <= low[i] for i in range(3)):
            self._safe_render()
            return
        source = vtkOutlineSource()
        source.SetBounds(low[0], high[0], low[1], high[1], low[2], high[2])
        mapper = vtkPolyDataMapper()
        mapper.SetInputConnection(source.GetOutputPort())
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.PickableOff()
        actor.GetProperty().SetColor(0.35, 0.78, 0.96)
        actor.GetProperty().SetOpacity(0.55)
        actor.GetProperty().SetLineWidth(1.5)
        self._domain_actor = actor
        self._renderer.AddActor(actor)
        self._safe_render()

    def _actor_for_object(self, obj: Any) -> Any | None:
        kind = scene_object_kind(obj)
        if kind == "box":
            spec = box_spec(obj)
            if spec is None:
                return None
            source = vtkCubeSource()
            source.SetCenter(*spec.center)
            source.SetXLength(spec.size[0])
            source.SetYLength(spec.size[1])
            source.SetZLength(spec.size[2])
            mapper = vtkPolyDataMapper()
            mapper.SetInputConnection(source.GetOutputPort())
            actor = vtkActor()
            actor.SetMapper(mapper)
            actor.RotateX(spec.rotation[0])
            actor.RotateY(spec.rotation[1])
            actor.RotateZ(spec.rotation[2])
            return actor

        if kind == "prism":
            vertices = vertices_of(obj)
            faces = faces_of(obj, len(vertices))
            if len(vertices) < 3 or not faces:
                return None
            points = vtkPoints()
            for point in vertices:
                points.InsertNextPoint(*point)
            polygons = vtkCellArray()
            for face in faces:
                polygons.InsertNextCell(len(face))
                for point_id in face:
                    polygons.InsertCellPoint(point_id)
            polydata = vtkPolyData()
            polydata.SetPoints(points)
            polydata.SetPolys(polygons)
            mapper = vtkPolyDataMapper()
            mapper.SetInputData(polydata)
            actor = vtkActor()
            actor.SetMapper(mapper)
            return actor
        return None

    def _vertices_for_object(self, obj: Any) -> list[tuple[float, float, float]]:
        vertices = vertices_of(obj)
        if vertices or scene_object_kind(obj) != "box":
            return vertices
        spec = box_spec(obj)
        if spec is None:
            return []
        half = tuple(value * 0.5 for value in spec.size)
        low = tuple(spec.center[index] - half[index] for index in range(3))
        high = tuple(spec.center[index] + half[index] for index in range(3))
        return [
            (low[0], low[1], low[2]),
            (high[0], low[1], low[2]),
            (high[0], high[1], low[2]),
            (low[0], high[1], low[2]),
            (low[0], low[1], high[2]),
            (high[0], low[1], high[2]),
            (high[0], high[1], high[2]),
            (low[0], high[1], high[2]),
        ]

    def _configure_actor(
        self,
        actor: Any,
        key: str,
        color: tuple[float, float, float],
        category: str,
    ) -> None:
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetEdgeColor(*(0.03, 0.05, 0.07))
        prop.EdgeVisibilityOn()
        prop.SetLineWidth(1.0)
        prop.SetOpacity(0.46 if category == "fluid" else 0.70)
        prop.SetInterpolationToPhong()
        actor.PickableOn()
        self._actor_by_key[key] = actor
        self._key_by_actor_id[id(actor)] = key
        self._base_color[key] = color

    def _remove_actor(self, key: str) -> None:
        if self._selected_key == key:
            self._clear_vertex_widgets(restore=False)
        actor = self._actor_by_key.pop(key, None)
        self._base_color.pop(key, None)
        if actor is not None:
            self._key_by_actor_id.pop(id(actor), None)
            self._renderer.RemoveActor(actor)
        if self._selected_key == key:
            self._selected_key = None

    def _on_left_button(self, interactor: Any, _event: str) -> None:
        if (
            not VTK_AVAILABLE
            or self._ctrl_rotating
            or self._vertex_interacting
            or interactor.GetControlKey()
        ):
            return
        x, y = interactor.GetEventPosition()
        self._picker.Pick(x, y, 0, self._renderer)
        actor = self._picker.GetActor()
        key = self._key_by_actor_id.get(id(actor)) if actor is not None else None
        if key is not None:
            self.select_object(key, emit=True)

    def _prepare_ctrl_rotation(self, interactor: Any, _event: str) -> None:
        if not interactor.GetControlKey():
            return
        self._ctrl_rotating = True
        for widget in self._vertex_widgets:
            widget.ProcessEventsOff()

    def _on_camera_left_button(self, style: Any, event: str) -> None:
        if event == "LeftButtonPressEvent":
            if not self._ctrl_rotating:
                return
            interactor = style.GetInteractor()
            control_key = interactor.GetControlKey()
            shift_key = interactor.GetShiftKey()
            try:
                # VTK maps Ctrl+left to Spin.  Temporarily clear modifiers so
                # its standard entry path starts a free Trackball rotation.
                interactor.SetControlKey(0)
                interactor.SetShiftKey(0)
                style.OnLeftButtonDown()
            finally:
                interactor.SetControlKey(control_key)
                interactor.SetShiftKey(shift_key)
            return

        if not self._ctrl_rotating:
            return
        try:
            style.OnLeftButtonUp()
        finally:
            self._ctrl_rotating = False
            for widget in self._vertex_widgets:
                widget.ProcessEventsOn()

    def _on_camera_right_button(self, style: Any, event: str) -> None:
        interactor = style.GetInteractor()
        if event == "RightButtonPressEvent":
            self._right_press_position = tuple(interactor.GetEventPosition())
            style.OnRightButtonDown()
            return

        try:
            style.OnRightButtonUp()
        finally:
            release = tuple(interactor.GetEventPosition())
            press = self._right_press_position
            should_open = (
                press is not None
                and max(abs(release[index] - press[index]) for index in range(2))
                <= _RIGHT_CLICK_TOLERANCE
            )
            self._right_press_position = None
        if should_open:
            key = self._selected_key
            index = self._selected_vertex_index
            if key is None or index is None:
                return
            position = self._vertex_position(index)
            qt_position = QPoint(
                int(release[0]),
                max(0, self._vtk_widget.height() - 1 - int(release[1])),
            )
            self.vertexContextMenuRequested.emit(
                key,
                index,
                position,
                self._vtk_widget.mapToGlobal(qt_position),
            )

    def _on_key_press(self, interactor: Any, _event: str) -> None:
        key = str(interactor.GetKeySym() or "").casefold()
        if key in {"return", "enter", "kp_enter"}:
            self.commit_selected_vertex()
        elif key in {"escape", "esc"}:
            self.cancel_vertex_edit()

    def _on_vertex_drag_start(self, index: int) -> None:
        self._vertex_interacting = True
        self._select_vertex(index)

    def _on_vertex_drag(self, index: int) -> None:
        if index != self._selected_vertex_index or self._selected_key is None:
            return
        position = self._vertex_position(index)
        self._show_coordinate_label(index, position)
        self._safe_render()

    def _on_vertex_drag_end(self, index: int) -> None:
        try:
            self._on_vertex_drag(index)
        finally:
            self._vertex_interacting = False

    def _select_vertex(self, index: int) -> None:
        key = self._selected_key
        if key not in self._editable_keys or not 0 <= index < len(self._vertex_widgets):
            return
        if self._selected_vertex_index is not None and self._selected_vertex_index != index:
            self._restore_selected_vertex()
        self._selected_vertex_index = index
        self._update_vertex_colors()
        position = self._vertex_position(index)
        self._show_coordinate_label(index, position)
        self._safe_render()

    def _vertex_position(self, index: int) -> tuple[float, float, float]:
        position = [0.0, 0.0, 0.0]
        self._vertex_representations[index].GetWorldPosition(position)
        return tuple(float(value) for value in position)

    def _restore_selected_vertex(self) -> None:
        key = self._selected_key
        index = self._selected_vertex_index
        vertices = self._vertices_by_key.get(key or "", ())
        if index is not None and 0 <= index < len(vertices) and index < len(
            self._vertex_representations
        ):
            self._vertex_representations[index].SetWorldPosition(vertices[index])

    def _update_vertex_colors(self) -> None:
        for index, representation in enumerate(self._vertex_representations):
            selected = index == self._selected_vertex_index
            color = _VERTEX_YELLOW if selected else _VERTEX_BLUE
            representation.SetHandleSize(
                _SELECTED_VERTEX_HANDLE_SIZE if selected else _VERTEX_HANDLE_SIZE
            )
            for prop in (
                representation.GetProperty(),
                representation.GetSelectedProperty(),
            ):
                prop.SetColor(*color)
                prop.SetOpacity(1.0)
                if selected:
                    prop.LightingOff()
                    prop.EdgeVisibilityOn()
                    prop.SetEdgeColor(0.10, 0.08, 0.02)
                    prop.SetEdgeWidth(2.0)
                else:
                    prop.LightingOn()
                    prop.EdgeVisibilityOff()

    def commit_selected_vertex(self) -> bool:
        key = self._selected_key
        index = self._selected_vertex_index
        if key is None or index is None:
            return False
        position = self._vertex_position(index)
        self.vertexCommitRequested.emit(key, index, position)
        return True

    def cancel_vertex_edit(self) -> bool:
        if self._selected_vertex_index is None:
            return False
        self._clear_vertex_selection(restore=True)
        return True

    def _clear_vertex_selection(self, *, restore: bool) -> None:
        if restore:
            self._restore_selected_vertex()
        self._selected_vertex_index = None
        self._vertex_interacting = False
        self._update_vertex_colors()
        if hasattr(self, "_coordinate_label"):
            self._coordinate_label.hide()
        self._safe_render()

    def _show_coordinate_label(
        self, index: int, position: tuple[float, float, float]
    ) -> None:
        if not hasattr(self, "_coordinate_label"):
            return
        self._coordinate_label.setText(
            f"顶点 {index + 1}  "
            f"X {position[0]:.8g}  Y {position[1]:.8g}  Z {position[2]:.8g}"
        )
        self._coordinate_label.adjustSize()
        interactor = self._vtk_widget.GetRenderWindow().GetInteractor()
        mouse_x, mouse_y = interactor.GetEventPosition()
        x = min(
            max(0, int(mouse_x) + 16),
            max(0, self._vtk_widget.width() - self._coordinate_label.width()),
        )
        qt_y = self._vtk_widget.height() - 1 - int(mouse_y)
        y = min(
            max(0, qt_y - self._coordinate_label.height() // 2),
            max(0, self._vtk_widget.height() - self._coordinate_label.height()),
        )
        self._coordinate_label.move(x, y)
        self._coordinate_label.show()
        self._coordinate_label.raise_()

    def select_object(self, key: str | None, *, emit: bool = False) -> None:
        if not VTK_AVAILABLE:
            self._selected_key = key
            if emit and key:
                self.selectionChanged.emit(key)
            return
        self._clear_vertex_widgets(restore=True)
        if self._selected_key in self._actor_by_key:
            previous = self._actor_by_key[self._selected_key]
            color = self._base_color[self._selected_key]
            previous.GetProperty().SetColor(*color)
            previous.GetProperty().SetLineWidth(1.0)
        self._selected_key = key if key in self._actor_by_key else None
        if self._selected_key:
            selected = self._actor_by_key[self._selected_key]
            selected.GetProperty().SetColor(*_lighter(self._base_color[self._selected_key]))
            selected.GetProperty().SetLineWidth(3.0)
            if emit:
                self.selectionChanged.emit(self._selected_key)
        self._create_vertex_widgets()
        self._safe_render()

    def _create_vertex_widgets(self) -> None:
        if not VTK_AVAILABLE:
            return
        key = self._selected_key
        actor = self._actor_by_key.get(key or "")
        if (
            actor is None
            or key not in self._editable_keys
            or key not in self._scene_keys
            or not actor.GetVisibility()
        ):
            return
        interactor = self._vtk_widget.GetRenderWindow().GetInteractor()
        for index, position in enumerate(self._vertices_by_key.get(key, ())):
            representation = vtkSphereHandleRepresentation()
            representation.GetProperty().SetColor(*_VERTEX_BLUE)
            representation.GetSelectedProperty().SetColor(*_VERTEX_BLUE)
            representation.SetHandleSize(_VERTEX_HANDLE_SIZE)
            representation.SetWorldPosition(position)
            widget = vtkHandleWidget()
            widget.SetInteractor(interactor)
            widget.SetRepresentation(representation)
            widget.AddObserver(
                "StartInteractionEvent",
                lambda _caller, _event, vertex_index=index: self._on_vertex_drag_start(
                    vertex_index
                ),
            )
            widget.AddObserver(
                "InteractionEvent",
                lambda _caller, _event, vertex_index=index: self._on_vertex_drag(
                    vertex_index
                ),
            )
            widget.AddObserver(
                "EndInteractionEvent",
                lambda _caller, _event, vertex_index=index: self._on_vertex_drag_end(
                    vertex_index
                ),
            )
            self._vertex_representations.append(representation)
            self._vertex_widgets.append(widget)
            widget.SetEnabled(1)

    def _clear_vertex_widgets(self, *, restore: bool) -> None:
        if not VTK_AVAILABLE:
            return
        self._clear_vertex_selection(restore=restore)
        for widget in self._vertex_widgets:
            try:
                widget.SetEnabled(0)
                widget.ProcessEventsOff()
                widget.SetInteractor(None)
            except Exception:
                pass
        self._vertex_widgets.clear()
        self._vertex_representations.clear()

    def set_object_visible(self, key: str, visible: bool) -> None:
        actor = self._actor_by_key.get(key)
        if actor is None:
            return
        actor.SetVisibility(bool(visible))
        if key == self._selected_key:
            self._clear_vertex_widgets(restore=True)
            if visible:
                self._create_vertex_widgets()
        self._safe_render()

    def show_all_objects(self) -> None:
        for actor in self._actor_by_key.values():
            actor.SetVisibility(True)
        if self._selected_key and not self._vertex_widgets:
            self._create_vertex_widgets()
        self._safe_render()

    def reset_camera(self) -> None:
        if not VTK_AVAILABLE:
            return
        self._renderer.ResetCamera()
        self._renderer.ResetCameraClippingRange()
        self._safe_render()

    def set_orthographic(self, enabled: bool) -> None:
        self._orthographic = bool(enabled)
        if not VTK_AVAILABLE:
            return
        camera = self._renderer.GetActiveCamera()
        camera.SetParallelProjection(1 if enabled else 0)
        self._safe_render()

    def is_orthographic(self) -> bool:
        return self._orthographic

    def set_standard_view(self, direction: str) -> None:
        views = {
            "front": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
            "back": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            "left": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
            "right": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
            "top": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
            "bottom": ((0.0, 0.0, -1.0), (0.0, -1.0, 0.0)),
        }
        try:
            camera_offset, view_up = views[direction]
        except KeyError as exc:
            raise ValueError(f"未知标准视角：{direction}") from exc

        self._orthographic = True
        if not VTK_AVAILABLE:
            return
        camera = self._renderer.GetActiveCamera()
        focal_point = camera.GetFocalPoint()
        distance = max(float(camera.GetDistance()), 1e-9)
        camera.SetPosition(
            focal_point[0] + camera_offset[0] * distance,
            focal_point[1] + camera_offset[1] * distance,
            focal_point[2] + camera_offset[2] * distance,
        )
        camera.SetViewUp(*view_up)
        camera.OrthogonalizeViewUp()
        camera.ParallelProjectionOn()
        self._renderer.ResetCameraClippingRange()
        self._safe_render()

    def set_axes_visible(self, visible: bool) -> None:
        self._axes_visible = bool(visible)
        if not VTK_AVAILABLE or self._axes_widget is None:
            return
        try:
            self._axes_widget.SetEnabled(1 if visible else 0)
            self._safe_render()
        except Exception:
            pass

    def load_legacy_vtk(self, path: str | Path) -> bool:
        """Load a legacy VTK dataset and render it as authoritative particles."""

        path = Path(path)
        if not VTK_AVAILABLE:
            message = "当前安装未包含 VTK 运行库。"
            self.vtkLoadFailed.emit(str(path), message)
            return False
        if not path.is_file():
            message = "文件不存在。"
            self.vtkLoadFailed.emit(str(path), message)
            return False

        try:
            reader = vtkDataSetReader()
            reader.SetFileName(str(path))
            reader.ReadAllScalarsOn()
            reader.ReadAllVectorsOn()
            reader.Update()
            dataset = reader.GetOutput()
            if dataset is None or dataset.GetNumberOfPoints() <= 0:
                raise ValueError("文件中没有可显示的点")

            mapper = vtkDataSetMapper()
            mapper.SetInputConnection(reader.GetOutputPort())
            mapper.ScalarVisibilityOff()
            actor = vtkActor()
            actor.SetMapper(mapper)
            prop = actor.GetProperty()
            prop.SetColor(*CATEGORY_COLORS["vtk"])
            prop.SetPointSize(3.0)
            # GenCase artifacts are primarily particle datasets.  Showing
            # vertices avoids opaque surfaces hiding the interior distribution.
            prop.SetRepresentationToPoints()
            prop.SetOpacity(0.95)
            key = f"vtk:{path.resolve()}"
            self._remove_actor(key)
            self._actor_by_key[key] = actor
            self._key_by_actor_id[id(actor)] = key
            self._base_color[key] = CATEGORY_COLORS["vtk"]
            self._vtk_keys.add(key)
            self._renderer.AddActor(actor)
            self.reset_camera()
            self.vtkLoaded.emit(str(path))
            return True
        except Exception as exc:
            self.vtkLoadFailed.emit(str(path), str(exc))
            return False

    def load_stl(self, path: str | Path) -> bool:
        """Load an ASCII or binary STL surface at its original coordinates."""

        path = Path(path)
        if not VTK_AVAILABLE:
            message = "当前安装未包含 VTK 运行库。"
            self.stlLoadFailed.emit(str(path), message)
            return False
        if not path.is_file():
            message = "文件不存在。"
            self.stlLoadFailed.emit(str(path), message)
            return False

        try:
            reader = vtkSTLReader()
            reader.SetFileName(str(path))
            reader.Update()
            polydata = reader.GetOutput()
            if (
                polydata is None
                or polydata.GetNumberOfPoints() <= 0
                or polydata.GetNumberOfCells() <= 0
            ):
                raise ValueError("文件中没有可显示的三角网格")

            mapper = vtkPolyDataMapper()
            mapper.SetInputConnection(reader.GetOutputPort())
            mapper.ScalarVisibilityOff()
            actor = vtkActor()
            actor.SetMapper(mapper)
            actor.PickableOff()
            prop = actor.GetProperty()
            prop.SetColor(*CATEGORY_COLORS["vtk"])
            prop.SetRepresentationToSurface()
            prop.SetInterpolationToPhong()
            prop.SetOpacity(0.82)

            key = f"stl:{path.resolve()}"
            self._remove_actor(key)
            self._actor_by_key[key] = actor
            self._key_by_actor_id[id(actor)] = key
            self._base_color[key] = CATEGORY_COLORS["vtk"]
            self._vtk_keys.add(key)
            self._renderer.AddActor(actor)
            self.reset_camera()
            self.stlLoaded.emit(str(path))
            return True
        except Exception as exc:
            self.stlLoadFailed.emit(str(path), str(exc))
            return False

    def clear_loaded_vtk(self) -> None:
        if not VTK_AVAILABLE:
            return
        for key in tuple(self._vtk_keys):
            self._remove_actor(key)
        self._vtk_keys.clear()
        self._safe_render()

    def _safe_render(self) -> None:
        if not VTK_AVAILABLE or self._vtk_widget is None:
            return
        try:
            self._vtk_widget.GetRenderWindow().Render()
        except Exception:
            # OpenGL can be temporarily unavailable while a dock/splitter is
            # moving or before a remote desktop surface has been allocated.
            pass


__all__ = ["VTKViewport", "VTK_AVAILABLE", "VTK_IMPORT_ERROR"]
