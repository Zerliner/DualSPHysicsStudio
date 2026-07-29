from __future__ import annotations

import os
from pathlib import Path
import re
import struct
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QPlainTextEdit, QPushButton, QToolBar
from shiboken6 import delete

import dualsphysics_studio.ui.dialogs as dialogs_module
import dualsphysics_studio.ui.main_window as main_window_module
import dualsphysics_studio.ui.viewport as viewport_module
from dualsphysics_studio.core import CaseDocument
from dualsphysics_studio.ui.viewport import VTK_AVAILABLE, VTKViewport

if VTK_AVAILABLE:
    from vtkmodules.vtkRenderingCore import (
        VTKIS_DOLLY,
        VTKIS_NONE,
        VTKIS_PAN,
        VTKIS_ROTATE,
    )


BOX_VERTICES = (
    (-0.5, -0.5, -0.5),
    (0.5, -0.5, -0.5),
    (0.5, 0.5, -0.5),
    (-0.5, 0.5, -0.5),
    (-0.5, -0.5, 0.5),
    (0.5, -0.5, 0.5),
    (0.5, 0.5, 0.5),
    (-0.5, 0.5, 0.5),
)


def editable_box() -> dict[str, object]:
    return {
        "key": "box-1",
        "name": "Box 1",
        "kind": "box",
        "category": "fixed",
        "center": (0.0, 0.0, 0.0),
        "size": (1.0, 1.0, 1.0),
        "vertices": BOX_VERTICES,
        "editable": True,
    }


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def viewport(qapp: QApplication) -> VTKViewport:
    if not VTK_AVAILABLE:
        pytest.skip("VTK runtime is unavailable")
    widget = VTKViewport()
    widget._safe_render = lambda: None
    yield widget
    qapp.processEvents()
    widget._clear_vertex_widgets(restore=False)
    widget.close()
    widget._vtk_widget.close()
    delete(widget)
    qapp.processEvents()


def test_left_button_is_selection_only_and_middle_button_starts_pan(
    viewport: VTKViewport,
) -> None:
    interactor = viewport._vtk_widget.GetRenderWindow().GetInteractor()
    style = interactor.GetInteractorStyle()

    assert style.GetClassName() == "vtkInteractorStyleTrackballCamera"
    assert not hasattr(viewport, "_handle_widget")

    interactor.SetEventInformation(20, 20)
    interactor.InvokeEvent("LeftButtonPressEvent")
    assert style.GetState() == VTKIS_NONE
    interactor.InvokeEvent("LeftButtonReleaseEvent")

    interactor.InvokeEvent("MiddleButtonPressEvent")
    assert style.GetState() == VTKIS_PAN
    interactor.InvokeEvent("MiddleButtonReleaseEvent")
    assert style.GetState() == VTKIS_NONE


def test_ctrl_left_button_rotates_and_temporarily_overrides_handle(
    viewport: VTKViewport,
) -> None:
    viewport.set_scene_objects([editable_box()])
    viewport.select_object("box-1")
    interactor = viewport._vtk_widget.GetRenderWindow().GetInteractor()
    style = interactor.GetInteractorStyle()
    camera = viewport._renderer.GetActiveCamera()
    camera.ParallelProjectionOn()

    interactor.SetEventInformation(20, 20, 1, 0)
    interactor.InvokeEvent("LeftButtonPressEvent")

    assert style.GetState() == VTKIS_ROTATE
    assert viewport._ctrl_rotating
    assert len(viewport._vertex_widgets) == 8
    assert all(widget.GetProcessEvents() == 0 for widget in viewport._vertex_widgets)
    assert camera.GetParallelProjection() == 1

    # The gesture remains active even if Ctrl is released before the mouse.
    interactor.SetControlKey(0)
    interactor.InvokeEvent("LeftButtonReleaseEvent")

    assert style.GetState() == VTKIS_NONE
    assert not viewport._ctrl_rotating
    assert all(widget.GetProcessEvents() == 1 for widget in viewport._vertex_widgets)
    assert camera.GetParallelProjection() == 1


@pytest.mark.parametrize(
    ("direction", "camera_offset", "view_up"),
    [
        ("front", (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
        ("back", (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        ("left", (-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
        ("right", (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
        ("top", (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
        ("bottom", (0.0, 0.0, -1.0), (0.0, -1.0, 0.0)),
    ],
)
def test_standard_views_preserve_target_and_distance_and_enable_orthographic(
    viewport: VTKViewport,
    direction: str,
    camera_offset: tuple[float, float, float],
    view_up: tuple[float, float, float],
) -> None:
    camera = viewport._renderer.GetActiveCamera()
    camera.SetFocalPoint(1.0, 2.0, 3.0)
    camera.SetPosition(4.0, 6.0, 15.0)
    original_distance = camera.GetDistance()
    camera.ParallelProjectionOff()

    viewport.set_standard_view(direction)

    focal_point = camera.GetFocalPoint()
    position = camera.GetPosition()
    actual_offset = tuple(
        (position[index] - focal_point[index]) / original_distance for index in range(3)
    )
    assert focal_point == pytest.approx((1.0, 2.0, 3.0))
    assert camera.GetDistance() == pytest.approx(original_distance)
    assert actual_offset == pytest.approx(camera_offset)
    assert camera.GetViewUp() == pytest.approx(view_up)
    assert camera.GetParallelProjection() == 1
    assert viewport.is_orthographic()


def test_viewport_toolbar_exposes_six_standard_view_buttons(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(viewport_module, "VTK_AVAILABLE", False)
    monkeypatch.setattr(main_window_module, "VTK_AVAILABLE", False)
    window = main_window_module.MainWindow()
    toolbar = window.findChild(QToolBar, "viewport_toolbar")
    actions = [
        window.action_view_front,
        window.action_view_back,
        window.action_view_left,
        window.action_view_right,
        window.action_view_top,
        window.action_view_bottom,
    ]

    assert toolbar is not None
    assert all(action in toolbar.actions() for action in actions)
    assert [action.text() for action in actions] == ["前", "后", "左", "右", "顶", "底"]

    requested: list[str] = []
    window.viewport.set_standard_view = requested.append
    for action in actions:
        action.trigger()

    assert requested == ["front", "back", "left", "right", "top", "bottom"]
    assert window.action_orthographic.isChecked()
    delete(window)
    qapp.processEvents()


def test_model_tree_visibility_survives_document_refresh(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(viewport_module, "VTK_AVAILABLE", False)
    monkeypatch.setattr(main_window_module, "VTK_AVAILABLE", False)
    document = {
        "source": "<case/>",
        "scene_objects": [
            {
                "key": "box-1",
                "name": "Box 1",
                "kind": "box",
                "category": "fixed",
                "center": (0.0, 0.0, 0.0),
                "size": (1.0, 1.0, 1.0),
                "editable": True,
            }
        ],
    }
    window = main_window_module.MainWindow()
    visibility_calls: list[tuple[str, bool]] = []
    show_all_calls: list[bool] = []
    window.viewport.set_object_visible = (
        lambda key, visible: visibility_calls.append((key, visible))
    )
    window.viewport.show_all_objects = lambda: show_all_calls.append(True)
    window.set_document(document)

    item = window._tree_item_by_key["box-1"]
    item.setCheckState(0, Qt.CheckState.Unchecked)
    qapp.processEvents()

    assert visibility_calls[-1] == ("box-1", False)

    window.refresh_document()
    refreshed_item = window._tree_item_by_key["box-1"]

    assert refreshed_item.checkState(0) == Qt.CheckState.Unchecked
    assert visibility_calls[-1] == ("box-1", False)

    window._show_all_objects()
    assert refreshed_item.checkState(0) == Qt.CheckState.Checked
    assert window._visibility_by_key["box-1"] is True
    assert show_all_calls == [True]

    delete(window)
    qapp.processEvents()


def test_model_tree_visibility_indicators_use_requested_state_colors(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(viewport_module, "VTK_AVAILABLE", False)
    monkeypatch.setattr(main_window_module, "VTK_AVAILABLE", False)
    window = main_window_module.MainWindow()
    style = window.styleSheet().lower()

    assert "qtreewidget#model_tree::indicator:checked" in style
    assert "background-color: #ffd54f" in style
    assert "qtreewidget#model_tree::indicator:unchecked" in style
    assert "background-color: #81d4fa" in style

    delete(window)
    qapp.processEvents()


def test_hidden_selected_object_cleans_up_vertex_handles(
    viewport: VTKViewport,
) -> None:
    viewport.set_scene_objects([editable_box()])
    viewport.select_object("box-1")
    actor = viewport._actor_by_key["box-1"]

    assert actor.GetVisibility() == 1
    assert len(viewport._vertex_widgets) == 8
    assert all(widget.GetEnabled() == 1 for widget in viewport._vertex_widgets)
    old_widgets = tuple(viewport._vertex_widgets)

    viewport.set_object_visible("box-1", False)
    assert actor.GetVisibility() == 0
    assert not viewport._vertex_widgets
    assert all(widget.GetEnabled() == 0 for widget in old_widgets)

    viewport.set_object_visible("box-1", True)
    assert actor.GetVisibility() == 1
    assert len(viewport._vertex_widgets) == 8
    assert all(widget.GetEnabled() == 1 for widget in viewport._vertex_widgets)


@pytest.mark.parametrize(
    ("scene_object", "vertex_count"),
    [
        (editable_box(), 8),
        (
            {
                "key": "prism-1",
                "name": "Prism 1",
                "kind": "prism",
                "category": "floating",
                "vertices": (
                    (0.0, 0.0, 0.0),
                    (1.0, 0.0, 0.0),
                    (0.0, 0.0, 1.0),
                    (0.0, 1.0, 0.0),
                    (1.0, 1.0, 0.0),
                    (0.0, 1.0, 1.0),
                ),
                "editable": True,
            },
            6,
        ),
    ],
)
def test_selecting_editable_object_shows_one_blue_handle_per_vertex(
    viewport: VTKViewport,
    scene_object: dict[str, object],
    vertex_count: int,
) -> None:
    viewport.set_scene_objects([scene_object])
    key = str(scene_object["key"])
    viewport.select_object(key)

    assert len(viewport._vertex_widgets) == vertex_count
    assert len(viewport._vertex_representations) == vertex_count
    assert all(widget.GetEnabled() == 1 for widget in viewport._vertex_widgets)
    assert all(
        representation.GetProperty().GetColor() == pytest.approx((0.12, 0.48, 0.96))
        for representation in viewport._vertex_representations
    )


def test_vertex_drag_is_preview_only_and_enter_requests_one_commit(
    viewport: VTKViewport,
) -> None:
    viewport.set_scene_objects([editable_box()])
    viewport.select_object("box-1")
    commits: list[tuple[str, int, object]] = []
    translations: list[tuple[str, float, float, float]] = []
    on_commit = lambda key, index, position: commits.append((key, index, position))
    on_translate = lambda key, x, y, z: translations.append((key, x, y, z))
    viewport.vertexCommitRequested.connect(on_commit)
    viewport.translationRequested.connect(on_translate)
    try:
        viewport._on_vertex_drag_start(0)
        target = (-0.25, -0.4, -0.35)
        viewport._vertex_representations[0].SetWorldPosition(target)
        viewport._on_vertex_drag(0)
        viewport._on_vertex_drag_end(0)

        assert viewport._selected_vertex_index == 0
        assert viewport._vertex_representations[0].GetProperty().GetColor() == pytest.approx(
            (1.0, 0.95, 0.0)
        )
        assert viewport._vertices_by_key["box-1"][0] == BOX_VERTICES[0]
        assert viewport._vertex_position(0) == pytest.approx(target)
        assert not commits
        assert not translations
        assert not viewport._coordinate_label.isHidden()

        interactor = viewport._vtk_widget.GetRenderWindow().GetInteractor()
        interactor.SetKeyEventInformation(0, 0, "\r", 0, "Return")
        interactor.InvokeEvent("KeyPressEvent")

        assert commits == [("box-1", 0, target)]
        # Emitting is not proof that the document accepted the coordinate.
        # Keep the preview selected so a failed validation can be corrected.
        assert viewport._selected_vertex_index == 0
        assert viewport._vertex_representations[0].GetProperty().GetColor() == pytest.approx(
            (1.0, 0.95, 0.0)
        )
        assert not viewport._coordinate_label.isHidden()

        # A successful document write refreshes the scene and naturally returns
        # to the object-selected, no-vertex-selected state.
        viewport.set_scene_objects([editable_box()])
        viewport.select_object("box-1")
        assert viewport._selected_vertex_index is None
        assert all(
            representation.GetProperty().GetColor()
            == pytest.approx((0.12, 0.48, 0.96))
            for representation in viewport._vertex_representations
        )
        assert viewport._coordinate_label.isHidden()
    finally:
        viewport.vertexCommitRequested.disconnect(on_commit)
        viewport.translationRequested.disconnect(on_translate)


def test_selected_vertex_is_larger_bright_yellow_and_previous_vertex_restores(
    viewport: VTKViewport,
) -> None:
    viewport.set_scene_objects([editable_box()])
    viewport.select_object("box-1")

    viewport._select_vertex(6)
    selected = viewport._vertex_representations[6]
    assert selected.GetProperty().GetColor() == pytest.approx((1.0, 0.95, 0.0))
    assert selected.GetHandleSize() == pytest.approx(22.0)
    assert selected.GetProperty().GetLighting() == 0
    assert selected.GetSelectedProperty().GetLighting() == 0
    assert selected.GetProperty().GetEdgeVisibility() == 1
    assert selected.GetSelectedProperty().GetEdgeVisibility() == 1
    assert selected.GetSelectedProperty().GetColor() == pytest.approx((1.0, 0.95, 0.0))
    assert all(
        representation.GetProperty().GetColor() == pytest.approx((0.12, 0.48, 0.96))
        and representation.GetHandleSize() == pytest.approx(14.0)
        and representation.GetProperty().GetLighting() == 1
        and representation.GetProperty().GetEdgeVisibility() == 0
        for index, representation in enumerate(viewport._vertex_representations)
        if index != 6
    )

    viewport._select_vertex(2)
    previous = viewport._vertex_representations[6]
    assert previous.GetProperty().GetColor() == pytest.approx((0.12, 0.48, 0.96))
    assert previous.GetHandleSize() == pytest.approx(14.0)
    assert previous.GetProperty().GetLighting() == 1
    assert previous.GetProperty().GetEdgeVisibility() == 0
    assert viewport._vertex_representations[2].GetHandleSize() == pytest.approx(22.0)

    viewport.cancel_vertex_edit()
    assert all(
        representation.GetProperty().GetColor() == pytest.approx((0.12, 0.48, 0.96))
        and representation.GetHandleSize() == pytest.approx(14.0)
        and representation.GetProperty().GetLighting() == 1
        and representation.GetProperty().GetEdgeVisibility() == 0
        for representation in viewport._vertex_representations
    )


def test_escape_restores_preview_and_clears_vertex_selection(
    viewport: VTKViewport,
) -> None:
    viewport.set_scene_objects([editable_box()])
    viewport.select_object("box-1")
    viewport._on_vertex_drag_start(3)
    viewport._vertex_representations[3].SetWorldPosition((-0.2, 0.4, -0.3))
    viewport._on_vertex_drag_end(3)

    interactor = viewport._vtk_widget.GetRenderWindow().GetInteractor()
    interactor.SetKeyEventInformation(0, 0, "\x1b", 0, "Escape")
    interactor.InvokeEvent("KeyPressEvent")

    assert viewport._selected_vertex_index is None
    assert viewport._vertex_position(3) == pytest.approx(BOX_VERTICES[3])
    assert viewport._coordinate_label.isHidden()


def test_short_right_click_anywhere_requests_vertex_menu_but_drag_remains_dolly(
    viewport: VTKViewport,
) -> None:
    viewport.set_scene_objects([editable_box()])
    viewport.select_object("box-1")
    viewport._select_vertex(2)
    requests: list[tuple[str, int, object, object]] = []
    on_request = lambda key, index, position, point: requests.append(
        (key, index, position, point)
    )
    viewport.vertexContextMenuRequested.connect(on_request)
    try:
        interactor = viewport._vtk_widget.GetRenderWindow().GetInteractor()
        style = interactor.GetInteractorStyle()
        interactor.SetEventInformation(30, 30)
        viewport._on_camera_right_button(style, "RightButtonPressEvent")
        assert style.GetState() == VTKIS_DOLLY
        interactor.SetEventInformation(32, 33)
        viewport._on_camera_right_button(style, "RightButtonReleaseEvent")
        assert style.GetState() == VTKIS_NONE
        assert len(requests) == 1
        assert requests[0][:3] == ("box-1", 2, BOX_VERTICES[2])
        highlighted = viewport._vertex_representations[2]
        assert viewport._selected_vertex_index == 2
        assert highlighted.GetProperty().GetColor() == pytest.approx((1.0, 0.95, 0.0))
        assert highlighted.GetHandleSize() == pytest.approx(22.0)
        assert highlighted.GetProperty().GetLighting() == 0
        assert highlighted.GetProperty().GetEdgeVisibility() == 1

        interactor.SetEventInformation(30, 30)
        viewport._on_camera_right_button(style, "RightButtonPressEvent")
        assert style.GetState() == VTKIS_DOLLY
        interactor.SetEventInformation(55, 55)
        viewport._on_camera_right_button(style, "RightButtonReleaseEvent")
        assert style.GetState() == VTKIS_NONE
        assert len(requests) == 1

        viewport.cancel_vertex_edit()
        interactor.SetEventInformation(5, 5)
        viewport._on_camera_right_button(style, "RightButtonPressEvent")
        viewport._on_camera_right_button(style, "RightButtonReleaseEvent")
        assert len(requests) == 1
    finally:
        viewport.vertexContextMenuRequested.disconnect(on_request)


def test_exact_vertex_dialog_rejects_invalid_values_and_accepts_finite_values(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        dialogs_module.QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    dialog = dialogs_module.VertexCoordinateDialog((1.0, 2.0, 3.0))

    dialog.editors[0].setText("not-a-number")
    dialog._validate_and_accept()
    assert dialog.result() != QDialog.DialogCode.Accepted

    dialog.editors[0].setText("nan")
    dialog._validate_and_accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert len(warnings) == 2

    dialog.editors[0].setText("1.25")
    dialog.editors[1].setText("-2.5")
    dialog.editors[2].setText("3e-2")
    dialog._validate_and_accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.position() == pytest.approx((1.25, -2.5, 0.03))

    delete(dialog)
    qapp.processEvents()


def test_main_window_vertex_writeback_warns_on_invalid_geometry_and_honors_snap(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(viewport_module, "VTK_AVAILABLE", False)
    monkeypatch.setattr(main_window_module, "VTK_AVAILABLE", False)
    warnings: list[str] = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "sample_case.xml")
    document = CaseDocument.from_file(fixture)
    window = main_window_module.MainWindow()
    window.set_document(document)
    entry = next(item for item in window._entries if item.kind == "box" and item.editable)
    assert window.select_object(entry.key)
    object_id = entry.source.id
    original = entry.source.vertices[0]
    opposite = max(
        entry.source.vertices,
        key=lambda point: sum(
            (point[axis] - original[axis]) ** 2 for axis in range(3)
        ),
    )
    original_text = document.text

    assert not window._apply_vertex_position(
        entry.key,
        0,
        opposite.as_tuple(),
        snap_to_grid=False,
    )
    assert document.text == original_text
    assert warnings

    target = tuple(
        original[axis] + (0.006 if opposite[axis] > original[axis] else -0.006)
        for axis in range(3)
    )
    expected = tuple(round(value / document.dp) * document.dp for value in target)
    assert window._apply_vertex_position(
        entry.key,
        0,
        target,
        snap_to_grid=True,
    )
    assert document.get_object(object_id).vertices[0].as_tuple() == pytest.approx(expected)
    assert window._selected_entry is not None
    assert window._selected_entry.key == entry.key

    exact = tuple(
        expected[axis] + (0.003 if opposite[axis] > expected[axis] else -0.003)
        for axis in range(3)
    )
    assert window._apply_vertex_position(
        entry.key,
        0,
        exact,
        snap_to_grid=False,
    )
    assert document.get_object(object_id).vertices[0].as_tuple() == pytest.approx(exact)

    delete(window)
    qapp.processEvents()


def test_cpu_action_is_enabled_for_a_document_and_emits_request(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(viewport_module, "VTK_AVAILABLE", False)
    monkeypatch.setattr(main_window_module, "VTK_AVAILABLE", False)
    window = main_window_module.MainWindow()
    window.set_document({"source": "<case/>", "scene_objects": []})
    requests: list[bool] = []
    window.cpuRequested.connect(lambda: requests.append(True))

    assert window.action_cpu.isEnabled()
    window.action_cpu.trigger()

    assert requests == [True]
    assert window.action_cancel.isEnabled()
    assert not window.action_gpu.isEnabled()

    delete(window)
    qapp.processEvents()


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


def _write_binary_stl(path: Path) -> None:
    header = b"DualSPHysics Studio binary STL".ljust(80, b"\0")
    triangle = struct.pack(
        "<12fH",
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0,
    )
    path.write_bytes(header + struct.pack("<I", 1) + triangle)


def _write_legacy_vtk(path: Path) -> None:
    path.write_text(
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


@pytest.mark.parametrize(
    ("filename", "writer"),
    (
        ("中文 空格 ASCII.stl", _write_ascii_stl),
        ("binary-model.stl", _write_binary_stl),
    ),
)
def test_load_stl_displays_ascii_and_binary_as_non_pickable_surface(
    viewport: VTKViewport,
    tmp_path: Path,
    filename: str,
    writer,
) -> None:
    path = tmp_path / filename
    writer(path)
    before = set(viewport._actor_by_key)

    assert viewport.load_stl(path)

    added = set(viewport._actor_by_key) - before
    assert len(added) == 1
    key = added.pop()
    actor = viewport._actor_by_key[key]
    mapper_input = actor.GetMapper().GetInput()
    assert mapper_input.GetNumberOfPoints() == 3
    assert mapper_input.GetNumberOfPolys() == 1
    assert actor.GetProperty().GetRepresentationAsString() == "Surface"
    assert actor.GetPickable() == 0


def test_load_stl_replaces_the_same_path_instead_of_duplicating_actor(
    viewport: VTKViewport, tmp_path: Path
) -> None:
    path = tmp_path / "repeat.stl"
    _write_ascii_stl(path)

    assert viewport.load_stl(path)
    keys_after_first = set(viewport._actor_by_key)
    actor_count_after_first = viewport._renderer.GetActors().GetNumberOfItems()

    assert viewport.load_stl(path)

    assert set(viewport._actor_by_key) == keys_after_first
    assert viewport._renderer.GetActors().GetNumberOfItems() == actor_count_after_first


@pytest.mark.parametrize(
    ("filename", "contents"),
    (
        ("empty.stl", b"solid empty\nendsolid empty\n"),
        ("damaged.stl", b"this is not an STL file"),
    ),
)
def test_load_stl_rejects_empty_or_damaged_files_without_adding_actor(
    viewport: VTKViewport,
    tmp_path: Path,
    filename: str,
    contents: bytes,
) -> None:
    path = tmp_path / filename
    path.write_bytes(contents)
    before = set(viewport._actor_by_key)

    assert not viewport.load_stl(path)
    assert set(viewport._actor_by_key) == before


def test_load_stl_rejects_missing_file_without_adding_actor(
    viewport: VTKViewport, tmp_path: Path
) -> None:
    path = tmp_path / "missing.stl"
    before = set(viewport._actor_by_key)

    assert not viewport.load_stl(path)
    assert set(viewport._actor_by_key) == before


def _main_window_without_vtk(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(viewport_module, "VTK_AVAILABLE", False)
    monkeypatch.setattr(main_window_module, "VTK_AVAILABLE", False)
    return main_window_module.MainWindow()


def _write_fake_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ fake extension")
    return path.resolve()


def test_case_doctor_filters_and_jumps_to_the_diagnostic_source_line(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = """<case>
  <casedef>
    <geometry>
      <definition dp="0">
        <pointmin x="2" y="0" z="0"/>
        <pointmax x="1" y="1" z="1"/>
      </definition>
      <commands><mainlist/></commands>
    </geometry>
  </casedef>
</case>"""
    document = CaseDocument.from_text(source)
    window = _main_window_without_vtk(monkeypatch)
    window.set_document(document)

    assert "错误" in window.doctor_summary.text()
    target = next(
        window.doctor_table.topLevelItem(index)
        for index in range(window.doctor_table.topLevelItemCount())
        if "pointmin" in window.doctor_table.topLevelItem(index).text(1)
    )
    row = target.data(0, main_window_module.DIAGNOSTIC_ROW_ROLE)
    window.doctor_table.setCurrentItem(target)

    window.doctor_table.itemDoubleClicked.emit(target, 1)
    qapp.processEvents()
    dialog = window._diagnostic_detail_dialog

    assert isinstance(dialog, QDialog)
    assert dialog.isVisible()
    assert row.code in dialog.windowTitle() or row.code in (
        dialog.findChild(main_window_module.QLabel, "diagnostic_detail_heading").text()
    )
    assert (
        dialog.findChild(QPlainTextEdit, "diagnostic_detail_message").toPlainText()
        == row.message
    )
    location_text = dialog.findChild(
        QPlainTextEdit, "diagnostic_detail_location"
    ).toPlainText()
    assert row.path in location_text
    assert f"第 {document.source_line(row.path)} 行" in location_text
    source_context = dialog.findChild(
        QPlainTextEdit, "diagnostic_detail_source"
    ).toPlainText()
    assert "→" in source_context
    assert "<pointmin" in source_context

    window.doctor_table.itemDoubleClicked.emit(target, 2)
    qapp.processEvents()

    assert row.path
    assert (
        window.source_editor.textCursor().blockNumber()
        == document.source_line(row.path) - 1
    )
    assert window.bottom_tabs.currentWidget() is window.source_tab

    detail_jump = dialog.findChild(
        QPushButton, "diagnostic_detail_jump_button"
    )
    assert detail_jump.isEnabled()
    detail_jump.click()
    qapp.processEvents()
    assert "已定位到 XML" in window.status_message.text()

    warning_index = window.doctor_filter.findData("warning")
    window.doctor_filter.setCurrentIndex(warning_index)
    qapp.processEvents()
    assert all(
        window.doctor_table.topLevelItem(index).text(0) == "警告"
        for index in range(window.doctor_table.topLevelItemCount())
    )

    requested: list[bool] = []
    window.quickCheckRequested.connect(lambda: requested.append(True))
    window.doctor_quick_check_button.click()
    qapp.processEvents()
    assert requested == [True]

    dialog.close()
    delete(window)
    qapp.processEvents()


def test_parameter_workbench_searches_edits_and_undoes_existing_values(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "sample_case.xml"
    document = CaseDocument.from_file(fixture)
    original = document.text
    window = _main_window_without_vtk(monkeypatch)
    window.set_document(document)

    window.parameter_search.setText("TimeMax")
    qapp.processEvents()
    assert window.parameter_table.rowCount() == 1
    assert window.parameter_table.item(0, 1).text() == "TimeMax"
    assert window.parameter_table.item(0, 3).text() == "s"

    value_item = window.parameter_table.item(0, 2)
    old_value = value_item.text()
    value_item.setText("0")
    qapp.processEvents()
    assert value_item.text() == old_value
    assert "必须大于 0" in window.status_message.text()

    value_item.setText("12.5")
    qapp.processEvents()
    assert 'key="TimeMax" value="12.5"' in document.text
    assert "TimeMax" in window.parameter_explanation.toPlainText()

    window.undo()
    assert document.text == original

    delete(window)
    qapp.processEvents()


def test_parameter_workbench_disables_edits_while_source_draft_is_invalid(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "sample_case.xml"
    document = CaseDocument.from_file(fixture)
    assert not document.stage_source("<case>")
    window = _main_window_without_vtk(monkeypatch)
    window.set_document(document)

    window.parameter_search.setText("TimeMax")
    qapp.processEvents()

    assert window.parameter_table.rowCount() == 1
    value_item = window.parameter_table.item(0, 2)
    assert not (value_item.flags() & Qt.ItemFlag.ItemIsEditable)

    delete(window)
    qapp.processEvents()


def test_large_mesh_warning_requires_confirmation_before_viewport_load(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _main_window_without_vtk(monkeypatch)
    target = tmp_path / "large.stl"
    _write_ascii_stl(target)
    report = SimpleNamespace(
        blocked=False,
        requires_confirmation=True,
        summary=lambda: "文件大小：600 MiB\n保守内存估算：约 2 GiB",
    )
    monkeypatch.setattr(
        main_window_module, "inspect_mesh_file", lambda _path: report
    )
    questions: list[str] = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "question",
        lambda _parent, _title, message, _buttons, _default: (
            questions.append(message) or main_window_module.QMessageBox.StandardButton.Yes
        ),
    )
    loaded: list[tuple[str, bool]] = []
    window.viewport.load_stl = lambda path, *, allow_large=False: (
        loaded.append((path, allow_large)) or True
    )

    assert window._load_mesh_path(target)
    assert questions
    assert loaded == [(str(target), True)]

    delete(window)
    qapp.processEvents()


def test_blocked_mesh_never_reaches_the_viewport_loader(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _main_window_without_vtk(monkeypatch)
    target = tmp_path / "oversized.stl"
    _write_ascii_stl(target)
    report = SimpleNamespace(
        blocked=True,
        requires_confirmation=True,
        summary=lambda: "超过安全上限",
    )
    monkeypatch.setattr(
        main_window_module, "inspect_mesh_file", lambda _path: report
    )
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window,
        "_report_error",
        lambda title, message: errors.append((title, message)),
    )
    loaded: list[str] = []
    window.viewport.load_stl = lambda path, **_kwargs: loaded.append(path) or True

    assert not window._load_mesh_path(target)
    assert errors and "安全加载上限" in errors[0][0]
    assert loaded == []

    delete(window)
    qapp.processEvents()


def test_extensions_menu_is_between_run_and_help_and_ends_with_scan(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _main_window_without_vtk(monkeypatch)
    top_level = [action.text() for action in window.menuBar().actions()]
    run_index = next(index for index, text in enumerate(top_level) if text.startswith("运行"))
    extension_index = next(
        index for index, text in enumerate(top_level) if text.startswith("拓展功能")
    )
    help_index = next(index for index, text in enumerate(top_level) if text.startswith("帮助"))

    assert run_index < extension_index < help_index
    extension_actions = window.extensions_menu.actions()
    assert extension_actions[-1] is window.action_scan_extensions
    assert extension_actions[-2].isSeparator()

    delete(window)
    qapp.processEvents()


def test_run_menu_exposes_dualsphysics_path_configuration(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _main_window_without_vtk(monkeypatch)
    run_action = next(
        action
        for action in window.menuBar().actions()
        if action.text().startswith("运行")
    )
    run_menu = run_action.menu()

    assert run_menu is not None
    assert run_menu.actions()[0] is window.action_configure_dualsphysics
    emitted: list[bool] = []
    window.configureDualSPHysicsRequested.connect(lambda: emitted.append(True))
    window.action_configure_dualsphysics.trigger()
    assert emitted == [True]

    delete(window)
    qapp.processEvents()


def test_extension_menu_refresh_discovers_only_root_and_one_level_tools(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = _main_window_without_vtk(monkeypatch)
    window._extension_directory = tmp_path.resolve()
    window._refresh_extension_menu()
    assert window._extension_actions == []

    root_tool = _write_fake_executable(tmp_path / "Root Mesh Tool.exe")
    child_tool = _write_fake_executable(
        tmp_path / "vendor" / "Child Mesh Tool.exe"
    )
    deep_tool = _write_fake_executable(
        tmp_path / "vendor" / "nested" / "Deep Mesh Tool.exe"
    )
    window._refresh_extension_menu()

    requested: list[str] = []
    window.extensionToolRequested.connect(requested.append)
    assert len(window._extension_actions) == 2
    assert {Path(action.toolTip()).resolve() for action in window._extension_actions} == {
        root_tool,
        child_tool,
    }
    assert deep_tool not in {
        Path(action.toolTip()).resolve() for action in window._extension_actions
    }

    for action in window._extension_actions:
        action.trigger()

    assert {Path(path).resolve() for path in requested} == {root_tool, child_tool}

    delete(window)
    qapp.processEvents()


@pytest.mark.parametrize(
    ("suffix", "expected_loader"),
    ((".vtk", "vtk"), (".stl", "stl")),
)
def test_3d_file_dialog_uses_strict_filter_and_dispatches_by_suffix(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    expected_loader: str,
) -> None:
    window = _main_window_without_vtk(monkeypatch)
    selected = tmp_path / f"中文 模型{suffix}"
    (_write_legacy_vtk if suffix == ".vtk" else _write_ascii_stl)(selected)
    dialog_calls: list[tuple[str, str, str]] = []

    def fake_open_file_name(_parent, title: str, initial: str, filters: str):
        dialog_calls.append((title, initial, filters))
        return str(selected), ""

    monkeypatch.setattr(
        main_window_module.QFileDialog,
        "getOpenFileName",
        fake_open_file_name,
    )
    dispatched: list[tuple[str, str]] = []
    window.viewport.load_legacy_vtk = lambda path: dispatched.append(("vtk", path))
    window.viewport.load_stl = lambda path: dispatched.append(("stl", path))

    window.action_load_vtk.trigger()

    assert len(dialog_calls) == 1
    filters = dialog_calls[0][2]
    assert set(re.findall(r"\*\.([a-z0-9]+)", filters.casefold())) == {"vtk", "stl"}
    assert "所有文件" not in filters
    assert dispatched == [(expected_loader, str(selected))]

    delete(window)
    qapp.processEvents()


def test_scan_extension_action_switches_directory_and_refreshes_menu(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = tmp_path / "current"
    selected = tmp_path / "selected folder"
    current.mkdir()
    tool = _write_fake_executable(selected / "Mesh Tool.exe")
    window = _main_window_without_vtk(monkeypatch)
    window._extension_directory = current.resolve()
    window._refresh_extension_menu()
    dialog_calls: list[tuple[str, str, object]] = []

    def fake_existing_directory(_parent, title: str, initial: str, options):
        dialog_calls.append((title, initial, options))
        return str(selected)

    monkeypatch.setattr(
        main_window_module.QFileDialog,
        "getExistingDirectory",
        fake_existing_directory,
    )

    window.action_scan_extensions.trigger()

    assert len(dialog_calls) == 1
    assert dialog_calls[0][1] == str(current.resolve())
    assert window._extension_directory == selected.resolve()
    assert len(window._extension_actions) == 1
    assert Path(window._extension_actions[0].toolTip()).resolve() == tool
    assert str(selected.resolve()) in window.status_message.text()
    assert window.extensions_menu.actions()[-1] is window.action_scan_extensions
    assert window.extensions_menu.actions()[-2].isSeparator()

    delete(window)
    qapp.processEvents()
