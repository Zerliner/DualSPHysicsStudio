"""Main PySide6 window for DualSPHysics Studio."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QPoint, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QFontDatabase, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStatusBar,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.mesh_guard import MeshPreflightError, inspect_mesh_file
from ..extensions import (
    ExtensionTool,
    ExtensionToolError,
    default_dlc_directory,
    discover_extension_tools,
)
from ..version import APP_VERSION
from .models import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    DiagnosticRow,
    SceneTreeEntry,
    build_scene_entries,
    diagnostic_counts,
    diagnostic_rows,
    diagnostics_to_text,
    object_explanation,
    parse_editor_value,
    property_rows,
    value_of,
)
from .dialogs import (
    NewObjectDialog,
    RotationDialog,
    VectorDialog,
    VertexCoordinateDialog,
    VertexDialog,
)
from .viewport import VTK_AVAILABLE, VTKViewport
from .xml_editor import XmlCodeEditor
from .run_comparison import RunComparisonDialog


PRODUCT_NAME = "DualSPHysics Studio"
VERSION = APP_VERSION

TREE_KEY_ROLE = int(Qt.ItemDataRole.UserRole)
PROPERTY_FIELD_ROLE = int(Qt.ItemDataRole.UserRole)
PROPERTY_RAW_ROLE = int(Qt.ItemDataRole.UserRole) + 1
DIAGNOSTIC_ROW_ROLE = int(Qt.ItemDataRole.UserRole) + 10
PARAMETER_FIELD_ROLE = int(Qt.ItemDataRole.UserRole) + 20


def _document_attribute(document: Any, *names: str, default: Any = None) -> Any:
    value = value_of(document, *names, default=default)
    if callable(value):
        try:
            return value()
        except TypeError:
            return default
    return value


class MainWindow(QMainWindow):
    """Four-area XML, scene, property, and run workspace.

    The window can use the built-in ``CaseDocument`` directly, while signals
    allow an application controller to replace loading, AI, and pipeline work.
    Long-running tools are never launched through a shell from this class.
    """

    openRequested = Signal(str)
    documentChanged = Signal(object)
    saveRequested = Signal()
    sourceEdited = Signal(str)
    objectSelected = Signal(object)
    propertyEditRequested = Signal(object, str, object)
    quickCheckRequested = Signal()
    generateRequested = Signal()
    cpuRequested = Signal()
    gpuRequested = Signal()
    partVTKRequested = Signal()
    fullRunRequested = Signal()
    cancelRequested = Signal()
    openRunOutputRequested = Signal()
    exportRunRequested = Signal()
    rerunFromRecordRequested = Signal()
    runSessionSelected = Signal(str)
    configureDualSPHysicsRequested = Signal()
    aiPayloadPreviewRequested = Signal(str, str)
    aiExplainRequested = Signal(str, str)
    configureAIRequested = Signal()
    extensionToolRequested = Signal(str)
    createObjectRequested = Signal(str, object)
    deleteObjectRequested = Signal(object)
    geometryEditRequested = Signal(object, str, object)

    def __init__(
        self,
        document: Any = None,
        *,
        document_loader: Callable[[str], Any] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("main_window")
        self.resize(1440, 900)
        self.setMinimumSize(980, 650)

        self._document = None
        self._document_loader = document_loader
        self._document_path: Path | None = None
        self._entries: list[SceneTreeEntry] = []
        self._entry_by_key: dict[str, SceneTreeEntry] = {}
        self._tree_item_by_key: dict[str, QTreeWidgetItem] = {}
        self._visibility_by_key: dict[str, bool] = {}
        self._selected_entry: SceneTreeEntry | None = None
        self._loading_source = False
        self._loading_properties = False
        self._loading_parameters = False
        self._diagnostic_rows: list[DiagnosticRow] = []
        self._diagnostic_detail_dialog: QDialog | None = None
        self._dirty = False
        self._run_active = False
        self._run_sessions: tuple[Any, ...] = ()
        self._extension_directory = default_dlc_directory()
        self._extension_actions: list[QAction] = []
        self._trusted_extensions: set[str] = set()

        self._source_timer = QTimer(self)
        self._source_timer.setSingleShot(True)
        self._source_timer.setInterval(450)
        self._source_timer.timeout.connect(self._flush_source_edit)

        self._create_actions()
        self._create_menu_bar()
        self._create_toolbars()
        self._create_workspace()
        self._create_status_bar()
        self._connect_signals()
        self._apply_style()
        self._update_title()

        if document is not None:
            self.set_document(document)
        else:
            self._show_welcome_state()

    # ------------------------------------------------------------------ setup
    def _create_actions(self) -> None:
        style = self.style()

        self.action_open = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton), "打开 XML…", self
        )
        self.action_open.setObjectName("action_open")
        self.action_open.setShortcut(QKeySequence.StandardKey.Open)
        self.action_open.setStatusTip("打开 DualSPHysics Case_Def.xml")

        self.action_save = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton), "保存", self
        )
        self.action_save.setObjectName("action_save")
        self.action_save.setShortcut(QKeySequence.StandardKey.Save)

        self.action_save_as = QAction("另存为…", self)
        self.action_save_as.setObjectName("action_save_as")
        self.action_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)

        self.action_exit = QAction("退出", self)
        self.action_exit.setShortcut(QKeySequence.StandardKey.Quit)

        self.action_undo = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_ArrowBack), "撤销", self
        )
        self.action_undo.setObjectName("action_undo")
        self.action_undo.setShortcut(QKeySequence.StandardKey.Undo)

        self.action_redo = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward), "重做", self
        )
        self.action_redo.setObjectName("action_redo")
        self.action_redo.setShortcut(QKeySequence.StandardKey.Redo)

        self.action_new_box = QAction("新建方箱", self)
        self.action_new_box.setObjectName("action_new_box")
        self.action_new_box.setToolTip("创建 drawbox")
        self.action_new_prism = QAction("新建棱柱", self)
        self.action_new_prism.setObjectName("action_new_prism")
        self.action_new_prism.setToolTip("创建 drawprism")
        self.action_delete_object = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon), "删除对象", self
        )
        self.action_delete_object.setObjectName("action_delete_object")
        self.action_translate = QAction("平移", self)
        self.action_translate.setObjectName("action_translate")
        self.action_rotate = QAction("旋转", self)
        self.action_rotate.setObjectName("action_rotate")
        self.action_scale = QAction("缩放", self)
        self.action_scale.setObjectName("action_scale")
        self.action_edit_vertex = QAction("编辑顶点", self)
        self.action_edit_vertex.setObjectName("action_edit_vertex")
        for action in (
            self.action_new_box,
            self.action_new_prism,
            self.action_delete_object,
            self.action_translate,
            self.action_rotate,
            self.action_scale,
            self.action_edit_vertex,
        ):
            action.setEnabled(False)

        self.action_reset_camera = QAction("重置视角", self)
        self.action_reset_camera.setObjectName("action_reset_camera")
        self.action_view_front = QAction("前", self)
        self.action_view_front.setObjectName("action_view_front")
        self.action_view_front.setToolTip("前视图（从 -Y 看向 +Y）")
        self.action_view_back = QAction("后", self)
        self.action_view_back.setObjectName("action_view_back")
        self.action_view_back.setToolTip("后视图（从 +Y 看向 -Y）")
        self.action_view_left = QAction("左", self)
        self.action_view_left.setObjectName("action_view_left")
        self.action_view_left.setToolTip("左视图（从 -X 看向 +X）")
        self.action_view_right = QAction("右", self)
        self.action_view_right.setObjectName("action_view_right")
        self.action_view_right.setToolTip("右视图（从 +X 看向 -X）")
        self.action_view_top = QAction("顶", self)
        self.action_view_top.setObjectName("action_view_top")
        self.action_view_top.setToolTip("顶视图（从 +Z 看向 -Z）")
        self.action_view_bottom = QAction("底", self)
        self.action_view_bottom.setObjectName("action_view_bottom")
        self.action_view_bottom.setToolTip("底视图（从 -Z 看向 +Z）")
        self.action_orthographic = QAction("正交投影", self)
        self.action_orthographic.setObjectName("action_orthographic")
        self.action_orthographic.setCheckable(True)
        self.action_axes = QAction("显示坐标轴", self)
        self.action_axes.setObjectName("action_axes")
        self.action_axes.setCheckable(True)
        self.action_axes.setChecked(True)
        self.action_snap_grid = QAction("dp 吸附", self)
        self.action_snap_grid.setObjectName("action_snap_grid")
        self.action_snap_grid.setCheckable(True)
        self.action_snap_grid.setChecked(True)
        self.action_snap_grid.setToolTip("3D 拖动时将对象参考点吸附到当前 dp 网格")
        self.action_show_all = QAction("全部显示", self)
        self.action_show_all.setObjectName("action_show_all")
        self.action_load_vtk = QAction("加载 3D 文件…", self)
        self.action_load_vtk.setObjectName("action_load_vtk")
        self.action_load_vtk.setToolTip("加载 legacy VTK 或 STL 表面模型")

        self.action_quick_check = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton), "快速检查", self
        )
        self.action_quick_check.setObjectName("action_quick_check")
        self.action_quick_check.setToolTip("在缓存目录执行轻量 GenCase 检查")

        self.action_generate = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay), "生成初始模型", self
        )
        self.action_generate.setObjectName("action_generate")
        self.action_generate.setToolTip("运行 GenCase 并生成 Case.xml、Case.bi4 和初始 VTK")

        self.action_full_run = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay), "一键完整运行", self
        )
        self.action_full_run.setObjectName("action_full_run")
        self.action_full_run.setToolTip(
            "依次执行 GenCase、明确选择的 CPU/GPU 求解和 PartVTK 转换"
        )

        self.action_cancel = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_MediaStop), "取消运行", self
        )
        self.action_cancel.setObjectName("action_cancel")
        self.action_cancel.setEnabled(False)

        self.action_cpu = QAction("CPU 求解", self)
        self.action_cpu.setObjectName("action_cpu")
        self.action_cpu.setToolTip("运行 DualSPHysics CPU 求解并生成粒子 BI4 数据")
        self.action_gpu = QAction("GPU 求解", self)
        self.action_gpu.setObjectName("action_gpu")
        self.action_gpu.setToolTip("运行 DualSPHysics GPU 求解并生成粒子 BI4 数据")
        self.action_partvtk = QAction("PartVTK 转换", self)
        self.action_partvtk.setObjectName("action_partvtk")
        self.action_partvtk.setToolTip("将 data 中的粒子 BI4 数据转换为 PartAll VTK")
        self.action_open_run_output = QAction("打开运行输出目录", self)
        self.action_open_run_output.setObjectName("action_open_run_output")
        self.action_open_run_output.setEnabled(False)
        self.action_export_run = QAction("导出可复现记录…", self)
        self.action_export_run.setObjectName("action_export_run")
        self.action_export_run.setEnabled(False)
        self.action_rerun_record = QAction("从记录创建新运行", self)
        self.action_rerun_record.setObjectName("action_rerun_record")
        self.action_rerun_record.setEnabled(False)
        self.action_compare_runs = QAction("对比运行记录…", self)
        self.action_compare_runs.setObjectName("action_compare_runs")
        self.action_compare_runs.setEnabled(False)
        self.action_configure_dualsphysics = QAction("配置 DualSPHysics 路径…", self)
        self.action_configure_dualsphysics.setObjectName(
            "action_configure_dualsphysics"
        )
        self.action_configure_dualsphysics.setToolTip(
            "选择并核验 DualSPHysics v5.4 根目录中的全部本地工具"
        )

        self.action_about = QAction(f"关于 {PRODUCT_NAME}", self)
        self.action_scan_extensions = QAction("扫描可用功能…", self)
        self.action_scan_extensions.setObjectName("action_scan_extensions")

    def _create_menu_bar(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")
        file_menu.addAction(self.action_open)
        file_menu.addAction(self.action_save)
        file_menu.addAction(self.action_save_as)
        file_menu.addSeparator()
        file_menu.addAction(self.action_exit)

        edit_menu = self.menuBar().addMenu("编辑(&E)")
        edit_menu.addAction(self.action_undo)
        edit_menu.addAction(self.action_redo)
        edit_menu.addSeparator()
        edit_menu.addAction(self.action_new_box)
        edit_menu.addAction(self.action_new_prism)
        edit_menu.addAction(self.action_delete_object)
        edit_menu.addSeparator()
        edit_menu.addAction(self.action_translate)
        edit_menu.addAction(self.action_rotate)
        edit_menu.addAction(self.action_scale)
        edit_menu.addAction(self.action_edit_vertex)

        view_menu = self.menuBar().addMenu("视图(&V)")
        view_menu.addAction(self.action_reset_camera)
        standard_view_menu = view_menu.addMenu("标准视角")
        standard_view_menu.addAction(self.action_view_front)
        standard_view_menu.addAction(self.action_view_back)
        standard_view_menu.addAction(self.action_view_left)
        standard_view_menu.addAction(self.action_view_right)
        standard_view_menu.addAction(self.action_view_top)
        standard_view_menu.addAction(self.action_view_bottom)
        view_menu.addAction(self.action_orthographic)
        view_menu.addAction(self.action_axes)
        view_menu.addAction(self.action_show_all)
        view_menu.addSeparator()
        view_menu.addAction(self.action_load_vtk)

        run_menu = self.menuBar().addMenu("运行(&R)")
        run_menu.addAction(self.action_configure_dualsphysics)
        run_menu.addSeparator()
        run_menu.addAction(self.action_full_run)
        run_menu.addAction(self.action_quick_check)
        run_menu.addAction(self.action_generate)
        run_menu.addAction(self.action_cancel)
        run_menu.addSeparator()
        run_menu.addAction(self.action_cpu)
        run_menu.addAction(self.action_gpu)
        run_menu.addAction(self.action_partvtk)
        run_menu.addSeparator()
        run_menu.addAction(self.action_open_run_output)
        run_menu.addAction(self.action_export_run)
        run_menu.addAction(self.action_rerun_record)
        run_menu.addAction(self.action_compare_runs)

        self.extensions_menu = self.menuBar().addMenu("拓展功能(&X)")
        self.extensions_menu.setObjectName("extensions_menu")
        self._refresh_extension_menu()

        help_menu = self.menuBar().addMenu("帮助(&H)")
        help_menu.addAction(self.action_about)

    def _create_toolbars(self) -> None:
        file_toolbar = QToolBar("文件与编辑", self)
        file_toolbar.setObjectName("file_toolbar")
        file_toolbar.setMovable(False)
        file_toolbar.addAction(self.action_open)
        file_toolbar.addAction(self.action_save)
        file_toolbar.addSeparator()
        file_toolbar.addAction(self.action_undo)
        file_toolbar.addAction(self.action_redo)
        self.addToolBar(file_toolbar)

        run_toolbar = QToolBar("运行", self)
        run_toolbar.setObjectName("run_toolbar")
        run_toolbar.setMovable(False)
        run_toolbar.addAction(self.action_full_run)
        run_toolbar.addAction(self.action_quick_check)
        run_toolbar.addAction(self.action_generate)
        run_toolbar.addAction(self.action_cpu)
        run_toolbar.addAction(self.action_gpu)
        run_toolbar.addAction(self.action_partvtk)
        run_toolbar.addAction(self.action_cancel)
        self.addToolBar(run_toolbar)

    def _create_workspace(self) -> None:
        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.workspace_splitter.setObjectName("workspace_splitter")
        self.workspace_splitter.setChildrenCollapsible(False)
        self.setCentralWidget(self.workspace_splitter)

        self._create_tree_panel()
        self._create_center_panel()
        self._create_inspector_panel()

        self.workspace_splitter.addWidget(self.tree_panel)
        self.workspace_splitter.addWidget(self.center_splitter)
        self.workspace_splitter.addWidget(self.inspector_tabs)
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setStretchFactor(2, 0)
        self.workspace_splitter.setSizes([250, 900, 320])

    def _create_tree_panel(self) -> None:
        self.tree_panel = QGroupBox("模型树", self)
        self.tree_panel.setObjectName("tree_panel")
        layout = QVBoxLayout(self.tree_panel)
        layout.setContentsMargins(7, 12, 7, 7)
        object_toolbar = QToolBar(self.tree_panel)
        object_toolbar.setObjectName("object_toolbar")
        object_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        object_toolbar.addAction(self.action_new_box)
        object_toolbar.addAction(self.action_new_prism)
        object_toolbar.addAction(self.action_delete_object)
        layout.addWidget(object_toolbar)
        self.model_tree = QTreeWidget(self.tree_panel)
        self.model_tree.setObjectName("model_tree")
        self.model_tree.setHeaderLabels(["对象", "类型", "Marker"])
        self.model_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.model_tree.setAlternatingRowColors(True)
        self.model_tree.setUniformRowHeights(True)
        self.model_tree.setColumnWidth(0, 138)
        self.model_tree.setColumnWidth(1, 58)
        layout.addWidget(self.model_tree)

        hint = QLabel("取消勾选可隐藏对象；单击对象可联动 3D 与属性。", self.tree_panel)
        hint.setWordWrap(True)
        hint.setObjectName("tree_hint")
        layout.addWidget(hint)

    def _create_center_panel(self) -> None:
        self.center_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.center_splitter.setObjectName("center_splitter")
        self.center_splitter.setChildrenCollapsible(False)

        viewport_group = QGroupBox("3D 视图 · 实时表面为近似预览", self.center_splitter)
        viewport_group.setObjectName("viewport_panel")
        viewport_layout = QVBoxLayout(viewport_group)
        viewport_layout.setContentsMargins(5, 12, 5, 5)

        viewport_toolbar = QToolBar(viewport_group)
        viewport_toolbar.setObjectName("viewport_toolbar")
        viewport_toolbar.setIconSize(viewport_toolbar.iconSize())
        viewport_toolbar.addAction(self.action_reset_camera)
        viewport_toolbar.addSeparator()
        viewport_toolbar.addAction(self.action_view_front)
        viewport_toolbar.addAction(self.action_view_back)
        viewport_toolbar.addAction(self.action_view_left)
        viewport_toolbar.addAction(self.action_view_right)
        viewport_toolbar.addAction(self.action_view_top)
        viewport_toolbar.addAction(self.action_view_bottom)
        viewport_toolbar.addSeparator()
        viewport_toolbar.addAction(self.action_orthographic)
        viewport_toolbar.addAction(self.action_axes)
        viewport_toolbar.addAction(self.action_snap_grid)
        viewport_toolbar.addAction(self.action_show_all)
        viewport_toolbar.addSeparator()
        viewport_toolbar.addAction(self.action_load_vtk)
        viewport_layout.addWidget(viewport_toolbar)

        self.viewport = VTKViewport(viewport_group)
        viewport_layout.addWidget(self.viewport, 1)

        self.bottom_tabs = QTabWidget(self.center_splitter)
        self.bottom_tabs.setObjectName("source_log_tabs")
        self._create_source_tab()
        self._create_log_tab()
        self.bottom_tabs.addTab(self.source_tab, "XML 源码")
        self.bottom_tabs.addTab(self.log_tab, "运行中心")

        self.center_splitter.addWidget(viewport_group)
        self.center_splitter.addWidget(self.bottom_tabs)
        self.center_splitter.setStretchFactor(0, 3)
        self.center_splitter.setStretchFactor(1, 2)
        self.center_splitter.setSizes([560, 280])

    def _create_source_tab(self) -> None:
        self.source_tab = QWidget(self)
        layout = QVBoxLayout(self.source_tab)
        layout.setContentsMargins(4, 4, 4, 4)

        source_bar = QHBoxLayout()
        self.source_state_label = QLabel("尚未打开 XML", self.source_tab)
        self.source_state_label.setObjectName("source_state_label")
        source_bar.addWidget(self.source_state_label)
        source_bar.addStretch(1)
        self.source_mode_label = QLabel("UTF-8 · 延迟解析", self.source_tab)
        self.source_mode_label.setObjectName("source_mode_label")
        source_bar.addWidget(self.source_mode_label)
        layout.addLayout(source_bar)

        self.source_editor = XmlCodeEditor(self.source_tab)
        self.source_editor.setObjectName("source_editor")
        self.source_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.source_editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.source_editor.setTabStopDistance(
            self.source_editor.fontMetrics().horizontalAdvance(" ") * 4
        )
        self.source_editor.setPlaceholderText("打开 Case_Def.xml 后在此显示无损源码。")
        layout.addWidget(self.source_editor, 1)

    def _create_log_tab(self) -> None:
        self.log_tab = QWidget(self)
        layout = QVBoxLayout(self.log_tab)
        layout.setContentsMargins(4, 4, 4, 4)
        run_bar = QHBoxLayout()
        self.run_state_label = QLabel("尚未运行任务", self.log_tab)
        self.run_state_label.setObjectName("run_state_label")
        run_bar.addWidget(self.run_state_label)
        self.run_progress = QProgressBar(self.log_tab)
        self.run_progress.setObjectName("run_progress")
        self.run_progress.setRange(0, 100)
        self.run_progress.setValue(0)
        self.run_progress.setTextVisible(False)
        self.run_progress.setMaximumWidth(180)
        run_bar.addWidget(self.run_progress)
        layout.addLayout(run_bar)

        self.run_center_tabs = QTabWidget(self.log_tab)
        self.run_center_tabs.setObjectName("run_center_tabs")

        self.run_overview_page = QWidget(self.run_center_tabs)
        overview_layout = QVBoxLayout(self.run_overview_page)
        overview_layout.setContentsMargins(4, 4, 4, 4)
        session_row = QHBoxLayout()
        self.run_session_label = QLabel("会话：尚未创建", self.run_overview_page)
        self.run_session_label.setObjectName("run_session_label")
        session_row.addWidget(self.run_session_label)
        session_row.addStretch(1)
        session_row.addWidget(QLabel("历史记录", self.run_overview_page))
        self.run_history_combo = QComboBox(self.run_overview_page)
        self.run_history_combo.setObjectName("run_history_combo")
        self.run_history_combo.setMinimumWidth(260)
        self.run_history_combo.addItem("暂无记录", "")
        session_row.addWidget(self.run_history_combo)
        overview_layout.addLayout(session_row)

        self.run_stage_table = QTableWidget(4, 3, self.run_overview_page)
        self.run_stage_table.setObjectName("run_stage_table")
        self.run_stage_table.setHorizontalHeaderLabels(["阶段", "状态", "详情"])
        self.run_stage_table.verticalHeader().setVisible(False)
        self.run_stage_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.run_stage_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.run_stage_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.run_stage_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.run_stage_table.horizontalHeader().setStretchLastSection(True)
        self.run_stage_table.setMaximumHeight(150)
        for row, (stage, label) in enumerate(
            (
                ("preflight", "资源预检"),
                ("gencase", "GenCase"),
                ("solver", "CPU/GPU 求解"),
                ("partvtk", "PartVTK"),
            )
        ):
            item = QTableWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, stage)
            self.run_stage_table.setItem(row, 0, item)
            self.run_stage_table.setItem(row, 1, QTableWidgetItem("等待"))
            self.run_stage_table.setItem(row, 2, QTableWidgetItem(""))
        overview_layout.addWidget(self.run_stage_table)

        self.resource_table = QTableWidget(0, 4, self.run_overview_page)
        self.resource_table.setObjectName("resource_estimate_table")
        self.resource_table.setHorizontalHeaderLabels(
            ["资源项目", "估算/实测", "置信度", "依据"]
        )
        self.resource_table.verticalHeader().setVisible(False)
        self.resource_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.resource_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.resource_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.resource_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.resource_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.resource_table.horizontalHeader().setStretchLastSection(True)
        overview_layout.addWidget(self.resource_table, 1)

        record_buttons = QHBoxLayout()
        self.open_run_output_button = QPushButton("打开输出目录", self.run_overview_page)
        self.open_run_output_button.setEnabled(False)
        record_buttons.addWidget(self.open_run_output_button)
        self.export_run_button = QPushButton("导出记录…", self.run_overview_page)
        self.export_run_button.setEnabled(False)
        record_buttons.addWidget(self.export_run_button)
        self.rerun_record_button = QPushButton(
            "从记录创建新运行", self.run_overview_page
        )
        self.rerun_record_button.setEnabled(False)
        record_buttons.addWidget(self.rerun_record_button)
        self.compare_runs_button = QPushButton("对比记录…", self.run_overview_page)
        self.compare_runs_button.setObjectName("compare_runs_button")
        self.compare_runs_button.setEnabled(False)
        record_buttons.addWidget(self.compare_runs_button)
        record_buttons.addStretch(1)
        overview_layout.addLayout(record_buttons)

        self.run_log_page = QWidget(self.run_center_tabs)
        log_layout = QVBoxLayout(self.run_log_page)
        log_layout.setContentsMargins(4, 4, 4, 4)
        self.log_view = QPlainTextEdit(self.run_log_page)
        self.log_view.setObjectName("log_view")
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log_view.setPlaceholderText("运行工具的标准输出、错误和产物摘要将在这里显示。")
        log_layout.addWidget(self.log_view, 1)

        self.run_center_tabs.addTab(self.run_overview_page, "会话概览")
        self.run_center_tabs.addTab(self.run_log_page, "实时日志")
        layout.addWidget(self.run_center_tabs, 1)

    def _create_inspector_panel(self) -> None:
        self.inspector_tabs = QTabWidget(self)
        self.inspector_tabs.setObjectName("inspector_tabs")
        self.inspector_tabs.setMinimumWidth(285)
        self.inspector_tabs.setMaximumWidth(580)

        property_page = QWidget(self.inspector_tabs)
        property_layout = QVBoxLayout(property_page)
        property_layout.setContentsMargins(5, 5, 5, 5)
        self.selection_label = QLabel("未选择对象", property_page)
        self.selection_label.setObjectName("selection_label")
        self.selection_label.setWordWrap(True)
        property_layout.addWidget(self.selection_label)
        transform_toolbar = QToolBar(property_page)
        transform_toolbar.setObjectName("transform_toolbar")
        transform_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        transform_toolbar.addAction(self.action_translate)
        transform_toolbar.addAction(self.action_rotate)
        transform_toolbar.addAction(self.action_scale)
        transform_toolbar.addAction(self.action_edit_vertex)
        property_layout.addWidget(transform_toolbar)
        self.property_table = QTableWidget(0, 2, property_page)
        self.property_table.setObjectName("property_table")
        self.property_table.setHorizontalHeaderLabels(["属性", "值"])
        self.property_table.horizontalHeader().setStretchLastSection(True)
        self.property_table.verticalHeader().setVisible(False)
        self.property_table.setAlternatingRowColors(True)
        self.property_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        property_layout.addWidget(self.property_table, 1)
        self.property_hint = QLabel("复杂表达式和结构化坐标请在 XML 源码中编辑。", property_page)
        self.property_hint.setObjectName("property_hint")
        self.property_hint.setWordWrap(True)
        property_layout.addWidget(self.property_hint)

        explanation_page = QWidget(self.inspector_tabs)
        explanation_layout = QVBoxLayout(explanation_page)
        explanation_layout.setContentsMargins(5, 5, 5, 5)
        self.explanation_view = QPlainTextEdit(explanation_page)
        self.explanation_view.setObjectName("explanation_view")
        self.explanation_view.setReadOnly(True)
        self.explanation_view.setPlaceholderText("选择模型或 XML 节点后显示离线解释。")
        explanation_layout.addWidget(self.explanation_view, 3)
        diagnostics_label = QLabel("诊断摘要（完整列表见“算例医生”）", explanation_page)
        diagnostics_label.setObjectName("diagnostics_label")
        explanation_layout.addWidget(diagnostics_label)
        self.diagnostics_view = QPlainTextEdit(explanation_page)
        self.diagnostics_view.setObjectName("diagnostics_view")
        self.diagnostics_view.setReadOnly(True)
        explanation_layout.addWidget(self.diagnostics_view, 2)

        parameter_page = QWidget(self.inspector_tabs)
        parameter_layout = QVBoxLayout(parameter_page)
        parameter_layout.setContentsMargins(5, 5, 5, 5)
        parameter_filter_row = QHBoxLayout()
        self.parameter_search = QLineEdit(parameter_page)
        self.parameter_search.setObjectName("parameter_search")
        self.parameter_search.setPlaceholderText("搜索参数、说明或当前值")
        parameter_filter_row.addWidget(self.parameter_search, 1)
        self.parameter_category = QComboBox(parameter_page)
        self.parameter_category.setObjectName("parameter_category")
        for label, value in (
            ("全部", "all"),
            ("几何", "geometry"),
            ("物理常量", "constants"),
            ("执行参数", "execution"),
        ):
            self.parameter_category.addItem(label, value)
        parameter_filter_row.addWidget(self.parameter_category)
        parameter_layout.addLayout(parameter_filter_row)
        self.parameter_table = QTableWidget(0, 5, parameter_page)
        self.parameter_table.setObjectName("parameter_table")
        self.parameter_table.setHorizontalHeaderLabels(
            ["分类", "参数", "值", "单位", "状态"]
        )
        parameter_header = self.parameter_table.horizontalHeader()
        for column in (0, 1, 3, 4):
            parameter_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        parameter_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.parameter_table.verticalHeader().setVisible(False)
        self.parameter_table.setAlternatingRowColors(True)
        self.parameter_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.parameter_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        parameter_layout.addWidget(self.parameter_table, 3)
        self.parameter_explanation = QPlainTextEdit(parameter_page)
        self.parameter_explanation.setObjectName("parameter_explanation")
        self.parameter_explanation.setReadOnly(True)
        self.parameter_explanation.setMaximumHeight(155)
        self.parameter_explanation.setPlaceholderText(
            "选择参数后显示 v5.4 离线说明、单位和生效状态。"
        )
        parameter_layout.addWidget(self.parameter_explanation, 1)

        doctor_page = QWidget(self.inspector_tabs)
        doctor_layout = QVBoxLayout(doctor_page)
        doctor_layout.setContentsMargins(5, 5, 5, 5)
        self.doctor_summary = QLabel("尚未打开 XML", doctor_page)
        self.doctor_summary.setObjectName("doctor_summary")
        self.doctor_summary.setWordWrap(True)
        doctor_layout.addWidget(self.doctor_summary)
        doctor_toolbar = QHBoxLayout()
        self.doctor_filter = QComboBox(doctor_page)
        self.doctor_filter.setObjectName("doctor_filter")
        for label, value in (
            ("全部问题", "all"),
            ("仅错误", "error"),
            ("仅警告", "warning"),
            ("仅建议", "info"),
        ):
            self.doctor_filter.addItem(label, value)
        doctor_toolbar.addWidget(self.doctor_filter)
        self.doctor_jump_button = QPushButton("跳转到 XML", doctor_page)
        self.doctor_jump_button.setObjectName("doctor_jump_button")
        doctor_toolbar.addWidget(self.doctor_jump_button)
        doctor_layout.addLayout(doctor_toolbar)
        self.doctor_quick_check_button = QPushButton("GenCase 快速检查", doctor_page)
        self.doctor_quick_check_button.setObjectName("doctor_quick_check_button")
        doctor_layout.addWidget(self.doctor_quick_check_button)
        self.doctor_table = QTreeWidget(doctor_page)
        self.doctor_table.setObjectName("doctor_table")
        self.doctor_table.setHeaderLabels(["级别", "问题", "位置"])
        self.doctor_table.setRootIsDecorated(False)
        self.doctor_table.setAlternatingRowColors(True)
        doctor_header = self.doctor_table.header()
        doctor_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        doctor_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        doctor_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        doctor_header.resizeSection(2, 120)
        self.doctor_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.doctor_table.setUniformRowHeights(True)
        doctor_layout.addWidget(self.doctor_table, 1)
        doctor_hint = QLabel(
            "双击级别或问题查看详情；双击位置或点击“跳转到 XML”可定位。",
            doctor_page,
        )
        doctor_hint.setWordWrap(True)
        doctor_layout.addWidget(doctor_hint)

        ai_page = QWidget(self.inspector_tabs)
        ai_layout = QVBoxLayout(ai_page)
        ai_layout.setContentsMargins(8, 8, 8, 8)
        ai_intro = QLabel(
            "AI 解读是可选联网功能。发送前可预览载荷；AI 不能运行程序或直接修改 XML。",
            ai_page,
        )
        ai_intro.setWordWrap(True)
        ai_layout.addWidget(ai_intro)
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("模型：", ai_page))
        self.ai_model_edit = QLineEdit("gpt-5.6-terra", ai_page)
        self.ai_model_edit.setObjectName("ai_model_edit")
        model_row.addWidget(self.ai_model_edit, 1)
        ai_layout.addLayout(model_row)
        self.ai_consent_check = QCheckBox("我同意将当前 XML 发送给 OpenAI", ai_page)
        self.ai_consent_check.setObjectName("ai_consent_check")
        ai_layout.addWidget(self.ai_consent_check)
        ai_buttons = QHBoxLayout()
        self.ai_preview_button = QPushButton("预览载荷", ai_page)
        self.ai_preview_button.setObjectName("ai_preview_button")
        self.ai_send_button = QPushButton("开始深度解读", ai_page)
        self.ai_send_button.setObjectName("ai_send_button")
        self.ai_send_button.setEnabled(False)
        ai_buttons.addWidget(self.ai_preview_button)
        ai_buttons.addWidget(self.ai_send_button)
        ai_layout.addLayout(ai_buttons)
        self.ai_config_button = QPushButton("配置 API 密钥…", ai_page)
        self.ai_config_button.setObjectName("ai_config_button")
        ai_layout.addWidget(self.ai_config_button)
        self.ai_result_view = QPlainTextEdit(ai_page)
        self.ai_result_view.setObjectName("ai_result_view")
        self.ai_result_view.setReadOnly(True)
        self.ai_result_view.setPlaceholderText("经过结构校验的 AI 摘要、风险与修改建议将显示在此处。")
        ai_layout.addWidget(self.ai_result_view, 1)

        self.inspector_tabs.addTab(property_page, "属性")
        self.inspector_tabs.addTab(parameter_page, "参数")
        self.inspector_tabs.addTab(doctor_page, "算例医生")
        self.inspector_tabs.addTab(explanation_page, "离线解读")
        self.inspector_tabs.addTab(ai_page, "AI 解读")

    def _create_status_bar(self) -> None:
        status = QStatusBar(self)
        status.setObjectName("status_bar")
        self.setStatusBar(status)
        self.status_message = QLabel("就绪", status)
        self.status_message.setObjectName("status_message")
        status.addWidget(self.status_message, 1)
        self.backend_label = QLabel(
            "3D: VTK" if VTK_AVAILABLE else "3D: 不可用（不影响 XML 与运行工具）", status
        )
        self.backend_label.setObjectName("backend_label")
        status.addPermanentWidget(self.backend_label)

    def _connect_signals(self) -> None:
        self.action_open.triggered.connect(self._open_dialog)
        self.action_save.triggered.connect(self.save_document)
        self.action_save_as.triggered.connect(self._save_as_dialog)
        self.action_exit.triggered.connect(self.close)
        self.action_undo.triggered.connect(self.undo)
        self.action_redo.triggered.connect(self.redo)
        self.action_new_box.triggered.connect(lambda _checked=False: self._request_create_object("box"))
        self.action_new_prism.triggered.connect(
            lambda _checked=False: self._request_create_object("prism")
        )
        self.action_delete_object.triggered.connect(self._request_delete_object)
        self.action_translate.triggered.connect(self._request_translate)
        self.action_rotate.triggered.connect(self._request_rotate)
        self.action_scale.triggered.connect(self._request_scale)
        self.action_edit_vertex.triggered.connect(self._request_vertex_edit)
        self.action_reset_camera.triggered.connect(self.viewport.reset_camera)
        self.action_view_front.triggered.connect(
            lambda _checked=False: self._set_standard_view("front")
        )
        self.action_view_back.triggered.connect(
            lambda _checked=False: self._set_standard_view("back")
        )
        self.action_view_left.triggered.connect(
            lambda _checked=False: self._set_standard_view("left")
        )
        self.action_view_right.triggered.connect(
            lambda _checked=False: self._set_standard_view("right")
        )
        self.action_view_top.triggered.connect(
            lambda _checked=False: self._set_standard_view("top")
        )
        self.action_view_bottom.triggered.connect(
            lambda _checked=False: self._set_standard_view("bottom")
        )
        self.action_orthographic.toggled.connect(self.viewport.set_orthographic)
        self.action_axes.toggled.connect(self.viewport.set_axes_visible)
        self.action_show_all.triggered.connect(self._show_all_objects)
        self.action_load_vtk.triggered.connect(self._load_vtk_dialog)
        self.action_scan_extensions.triggered.connect(self._scan_extension_directory)
        self.extensions_menu.aboutToShow.connect(self._refresh_extension_menu)
        self.action_configure_dualsphysics.triggered.connect(
            lambda _checked=False: self.configureDualSPHysicsRequested.emit()
        )
        self.action_quick_check.triggered.connect(self._request_quick_check)
        self.action_full_run.triggered.connect(self._request_full_run)
        self.action_generate.triggered.connect(self._request_generate)
        self.action_cpu.triggered.connect(self._request_cpu)
        self.action_gpu.triggered.connect(self._request_gpu)
        self.action_partvtk.triggered.connect(self._request_partvtk)
        self.action_cancel.triggered.connect(lambda _checked=False: self.cancelRequested.emit())
        self.action_open_run_output.triggered.connect(
            lambda _checked=False: self.openRunOutputRequested.emit()
        )
        self.action_export_run.triggered.connect(
            lambda _checked=False: self.exportRunRequested.emit()
        )
        self.action_rerun_record.triggered.connect(
            lambda _checked=False: self.rerunFromRecordRequested.emit()
        )
        self.action_compare_runs.triggered.connect(self._show_run_comparison)
        self.open_run_output_button.clicked.connect(self.openRunOutputRequested.emit)
        self.export_run_button.clicked.connect(self.exportRunRequested.emit)
        self.rerun_record_button.clicked.connect(self.rerunFromRecordRequested.emit)
        self.compare_runs_button.clicked.connect(self._show_run_comparison)
        self.run_history_combo.currentIndexChanged.connect(
            self._run_history_selection_changed
        )
        self.action_about.triggered.connect(self._show_about)

        self.model_tree.currentItemChanged.connect(self._tree_selection_changed)
        self.model_tree.itemChanged.connect(self._tree_item_changed)
        self.viewport.selectionChanged.connect(self._viewport_selection_changed)
        self.viewport.translationRequested.connect(self._viewport_translation_requested)
        self.viewport.vertexCommitRequested.connect(
            self._viewport_vertex_commit_requested
        )
        self.viewport.vertexContextMenuRequested.connect(
            self._viewport_vertex_context_menu_requested
        )
        self.viewport.vtkLoaded.connect(self._vtk_loaded)
        self.viewport.vtkLoadFailed.connect(self._vtk_failed)
        self.viewport.stlLoaded.connect(self._stl_loaded)
        self.viewport.stlLoadFailed.connect(self._stl_failed)
        self.source_editor.textChanged.connect(self._source_text_changed)
        self.property_table.itemChanged.connect(self._property_item_changed)
        self.parameter_search.textChanged.connect(self._populate_parameters)
        self.parameter_category.currentIndexChanged.connect(self._populate_parameters)
        self.parameter_table.itemSelectionChanged.connect(
            self._parameter_selection_changed
        )
        self.parameter_table.itemChanged.connect(self._parameter_item_changed)
        self.doctor_filter.currentIndexChanged.connect(self._populate_doctor)
        self.doctor_table.itemDoubleClicked.connect(
            self._doctor_item_double_clicked
        )
        self.doctor_jump_button.clicked.connect(self._jump_to_selected_diagnostic)
        self.doctor_quick_check_button.clicked.connect(self._request_quick_check)
        self.ai_consent_check.toggled.connect(self.ai_send_button.setEnabled)
        self.ai_preview_button.clicked.connect(self._preview_ai_payload)
        self.ai_send_button.clicked.connect(self._request_ai_explanation)
        self.ai_config_button.clicked.connect(lambda _checked=False: self.configureAIRequested.emit())

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #eef1f5; }
            QGroupBox { font-weight: 600; border: 1px solid #cbd3dc; border-radius: 5px;
                        margin-top: 7px; background: #f8fafc; }
            QGroupBox::title { subcontrol-origin: margin; left: 9px; padding: 0 4px; }
            QTreeWidget, QTableWidget, QPlainTextEdit, QLineEdit {
                border: 1px solid #cbd3dc; border-radius: 3px; background: #ffffff;
                selection-background-color: #1976d2; selection-color: #ffffff;
            }
            QTreeWidget::item { min-height: 23px; }
            QTreeWidget#model_tree::indicator {
                width: 14px; height: 14px; border: 1px solid #455a64;
                border-radius: 2px;
            }
            QTreeWidget#model_tree::indicator:checked {
                background-color: #ffd54f;
            }
            QTreeWidget#model_tree::indicator:unchecked {
                background-color: #81d4fa;
            }
            QTabWidget::pane { border: 1px solid #cbd3dc; background: #f8fafc; }
            QTabBar::tab { padding: 6px 11px; }
            QPushButton { padding: 5px 9px; }
            QLabel#tree_hint, QLabel#property_hint, QLabel#source_mode_label {
                color: #687585; font-size: 11px;
            }
            QLabel#selection_label { font-weight: 600; color: #263746; }
            QToolBar { spacing: 4px; border: 0; }
            """
        )

    # ------------------------------------------------------------ document API
    @property
    def document(self) -> Any:
        return self._document

    @property
    def document_path(self) -> Path | None:
        return self._document_path

    def set_document(self, document: Any, path: str | Path | None = None) -> None:
        self._document = document
        detected_path = path or _document_attribute(
            document, "path", "source_path", "file_path", "filename", default=None
        )
        self._document_path = Path(detected_path) if detected_path else None
        source = _document_attribute(
            document, "editor_text", "source", "text", "xml_source", "xml_text", default=""
        )
        if isinstance(source, bytes):
            source = source.decode("utf-8", errors="replace")

        self._loading_source = True
        try:
            self.source_editor.setPlainText(str(source or ""))
            self.source_editor.document().setModified(False)
        finally:
            self._loading_source = False
        self._dirty = False
        self._visibility_by_key.clear()
        self.source_state_label.setText("XML 已解析")
        self.source_state_label.setStyleSheet("color: #247a3d;")
        self.refresh_document()
        self.documentChanged.emit(document)
        self._update_title()
        self.status_message.setText("案例已载入")

    def refresh_document(self) -> None:
        if self._document is None:
            return
        selected_key = self._selected_entry.key if self._selected_entry else None
        previous_identity = {
            entry.key: (entry.name, entry.kind, entry.category, entry.marker)
            for entry in self._entries
        }
        objects = _document_attribute(
            self._document, "scene_objects", "objects", "geometry_objects", default=()
        ) or ()
        self._entries = build_scene_entries(objects)
        self._entry_by_key = {entry.key: entry for entry in self._entries}
        self._visibility_by_key = {
            entry.key: (
                self._visibility_by_key.get(entry.key, True)
                if previous_identity.get(entry.key)
                == (entry.name, entry.kind, entry.category, entry.marker)
                else True
            )
            for entry in self._entries
        }
        self._populate_tree()
        self.viewport.set_scene_objects([entry.source for entry in self._entries])
        for key, visible in self._visibility_by_key.items():
            if not visible:
                self.viewport.set_object_visible(key, False)
        facts = _document_attribute(self._document, "facts", default=None)
        definition_bounds = value_of(
            facts, "definition_bounds", "bounds", default=None
        ) if facts is not None else None
        self.viewport.set_definition_bounds(definition_bounds)

        diagnostics = _document_attribute(self._document, "diagnostics", "issues", default=())
        self._set_diagnostics(diagnostics)
        self._populate_parameters()

        if selected_key and selected_key in self._entry_by_key:
            self.select_object(selected_key)
        elif self._entries:
            self.select_object(self._entries[0].key)
        else:
            self._selected_entry = None
            self._populate_properties(None)
            self.explanation_view.setPlainText("此 XML 中没有可直接预览的 drawbox/drawprism 对象。")

    def open_path(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.is_file():
            self._report_error("打开失败", f"找不到文件：\n{path}")
            return False
        try:
            loader = self._document_loader or self._default_document_loader
            document = loader(str(path))
        except (ImportError, AttributeError) as exc:
            # An external controller can still satisfy the request.
            self.openRequested.emit(str(path))
            if self._document is None:
                self._report_error("无法载入 XML", str(exc))
                return False
            return True
        except Exception as exc:
            self._report_error("XML 解析失败", str(exc))
            self.source_state_label.setText("XML 无效，保留上一个有效 3D 场景")
            self.source_state_label.setStyleSheet("color: #b42318;")
            return False
        self.set_document(document, path)
        return True

    @staticmethod
    def _default_document_loader(path: str) -> Any:
        candidates = (
            "dualsphysics_studio.core.document",
            "dualsphysics_studio.core.case_document",
            "dualsphysics_studio.core",
        )
        case_document = None
        last_error: Exception | None = None
        for module_name in candidates:
            try:
                module = importlib.import_module(module_name)
                case_document = getattr(module, "CaseDocument", None)
                if case_document is not None:
                    break
            except ImportError as exc:
                last_error = exc
        if case_document is None:
            raise ImportError("未找到 CaseDocument 载入器") from last_error

        for factory_name in ("from_file", "load", "open"):
            factory = getattr(case_document, factory_name, None)
            if callable(factory):
                return factory(path)
        return case_document(path)

    def save_document(self, path: str | Path | None = None) -> bool:
        if self._document is None:
            self.status_message.setText("尚未打开 XML")
            return False
        if not self._flush_source_edit(strict=True):
            return False

        target = Path(path) if path else self._document_path
        if target is None:
            return self._save_as_dialog()

        try:
            result = self._call_document_save(target)
        except AttributeError:
            self.saveRequested.emit()
            self.status_message.setText("保存请求已交给应用控制器")
            return False
        except Exception as exc:
            self._report_error("保存失败", str(exc))
            return False

        self._document_path = Path(value_of(result, "path", default=target))
        self._dirty = False
        self.source_editor.document().setModified(False)
        backup = value_of(result, "backup_path", "backup", default=None)
        if backup:
            self.status_message.setText(f"已保存；备份：{backup}")
        else:
            self.status_message.setText("XML 已保存")
        self.source_state_label.setText("XML 已保存并解析")
        self.source_state_label.setStyleSheet("color: #247a3d;")
        self._update_title()
        return True

    def _call_document_save(self, target: Path) -> Any:
        for method_name in ("save", "save_as", "write"):
            method = getattr(self._document, method_name, None)
            if not callable(method):
                continue
            try:
                parameters = list(inspect.signature(method).parameters.values())
            except (TypeError, ValueError):
                parameters = [object()]
            if not parameters and self._document_path == target:
                return method()
            return method(target)
        raise AttributeError("CaseDocument 未提供保存方法")

    def _flush_source_edit(self, strict: bool = False) -> bool:
        self._source_timer.stop()
        if self._document is None or not self._dirty:
            return True
        text = self.source_editor.toPlainText()
        self.sourceEdited.emit(text)

        methods = ("stage_source", "replace_source", "set_source", "update_source", "parse_source")
        applied = False
        try:
            for method_name in methods:
                method = getattr(self._document, method_name, None)
                if not callable(method):
                    continue
                result = method(text)
                if result is not None and result is not True and result is not False:
                    self._document = result
                applied = result is not False
                if method_name == "stage_source" and result is False:
                    self.source_state_label.setText("XML 暂时无效；3D 保留上一个有效状态")
                    self.source_state_label.setStyleSheet("color: #b42318;")
                    diagnostics = _document_attribute(
                        self._document, "diagnostics", "issues", default=()
                    )
                    self._set_diagnostics(diagnostics)
                    self._populate_parameters()
                    if strict:
                        self._report_error("无法保存无效 XML", "请先修正源码中的 XML 语法错误。")
                    return False
                break
            else:
                # Some simple document implementations expose a mutable source.
                descriptor = getattr(type(self._document), "source", None)
                if descriptor is None or getattr(descriptor, "fset", None) is not None:
                    setattr(self._document, "source", text)
                    applied = True
        except Exception as exc:
            self.source_state_label.setText("XML 暂时无效；3D 保留上一个有效状态")
            self.source_state_label.setStyleSheet("color: #b42318;")
            self.status_message.setText(str(exc))
            if strict:
                self._report_error("无法保存无效 XML", str(exc))
            return False

        if applied:
            self.source_state_label.setText("XML 已解析 · 尚未保存")
            self.source_state_label.setStyleSheet("color: #9a6700;")
            self.refresh_document()
        else:
            self.source_state_label.setText("等待文档控制器解析")
            self.source_state_label.setStyleSheet("color: #9a6700;")
        return True

    # --------------------------------------------------------------- tree/view
    def _populate_tree(self) -> None:
        blocker = QSignalBlocker(self.model_tree)
        self.model_tree.clear()
        self._tree_item_by_key.clear()
        grouped = {category: [] for category in CATEGORY_ORDER}
        for entry in self._entries:
            grouped[entry.category].append(entry)

        kind_labels = {"box": "方箱", "prism": "棱柱"}
        for category in CATEGORY_ORDER:
            entries = grouped[category]
            if not entries:
                continue
            group = QTreeWidgetItem(
                self.model_tree, [f"{CATEGORY_LABELS[category]}（{len(entries)}）", "", ""]
            )
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            group.setExpanded(True)
            for entry in entries:
                item = QTreeWidgetItem(
                    group,
                    [entry.name, kind_labels.get(entry.kind, entry.kind), entry.marker or "—"],
                )
                item.setData(0, TREE_KEY_ROLE, entry.key)
                item.setFlags(
                    item.flags()
                    | Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsSelectable
                )
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked
                    if self._visibility_by_key.get(entry.key, True)
                    else Qt.CheckState.Unchecked,
                )
                if not entry.editable:
                    item.setToolTip(0, "仅源码编辑")
                self._tree_item_by_key[entry.key] = item
        del blocker

    def select_object(self, key: str) -> bool:
        entry = self._entry_by_key.get(key)
        if entry is None:
            return False
        self._selected_entry = entry
        item = self._tree_item_by_key.get(key)
        if item is not None and self.model_tree.currentItem() is not item:
            blocker = QSignalBlocker(self.model_tree)
            self.model_tree.setCurrentItem(item)
            del blocker
        self.viewport.select_object(key)
        self._populate_properties(entry)
        self.explanation_view.setPlainText(object_explanation(entry.source, self._document))
        self.objectSelected.emit(entry.source)
        return True

    def _populate_properties(self, entry: SceneTreeEntry | None) -> None:
        self._loading_properties = True
        try:
            self.property_table.setRowCount(0)
            if entry is None:
                self.selection_label.setText("未选择对象")
                self._set_geometry_actions_enabled(False)
                return
            status = "可编辑" if entry.editable else "仅源码编辑"
            self.selection_label.setText(f"{entry.name}\n{CATEGORY_LABELS[entry.category]} · {status}")
            self._set_geometry_actions_enabled(entry.editable)
            rows = property_rows(entry.source)
            self.property_table.setRowCount(len(rows))
            read_only_fields = {
                "id",
                "key",
                "uid",
                "kind",
                "geometry_type",
                "role",
                "category",
                "bounds",
                "bounding_box",
                "source_order",
                "element_path",
                "marker_path",
                "point_paths",
                "size_path",
                "shapeout_path",
                "floating_path",
                "floating_center_path",
                "editable",
                "readonly_reason",
            }
            for row, (field, label, display, raw) in enumerate(rows):
                name_item = QTableWidgetItem(label)
                name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                value_item = QTableWidgetItem(display)
                value_item.setData(PROPERTY_FIELD_ROLE, field)
                value_item.setData(PROPERTY_RAW_ROLE, raw)
                structured = isinstance(raw, (dict, list, tuple, set)) or all(
                    hasattr(raw, axis) for axis in "xyz"
                )
                if not entry.editable or field in read_only_fields or structured:
                    value_item.setFlags(value_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.property_table.setItem(row, 0, name_item)
                self.property_table.setItem(row, 1, value_item)
            self.property_table.resizeColumnToContents(0)
        finally:
            self._loading_properties = False

    def _set_diagnostics(self, diagnostics: Any) -> None:
        values = tuple(diagnostics or ())
        self.diagnostics_view.setPlainText(diagnostics_to_text(values))
        self._diagnostic_rows = diagnostic_rows(values)
        self._populate_doctor()

    def _populate_doctor(self, *_args: Any) -> None:
        if not hasattr(self, "doctor_table"):
            return
        self.doctor_table.clear()
        counts = diagnostic_counts(self._diagnostic_rows)
        if self._document is None:
            self.doctor_summary.setText("尚未打开 XML")
        elif not self._diagnostic_rows:
            self.doctor_summary.setText("未发现需要注意的问题，可以继续执行 GenCase 快速检查。")
        else:
            self.doctor_summary.setText(
                f"错误 {counts['error']} · 警告 {counts['warning']} · "
                f"建议 {counts['info']}"
            )
        selected_filter = self.doctor_filter.currentData() or "all"
        for row in self._diagnostic_rows:
            if selected_filter != "all" and row.severity != selected_filter:
                continue
            if row.path and row.line is not None:
                location = f"{row.path} · 第 {row.line} 行"
            elif row.path:
                location = row.path
            elif row.line is not None:
                location = f"第 {row.line} 行"
            else:
                location = "—"
            item = QTreeWidgetItem(
                self.doctor_table,
                [row.severity_label, row.message, location],
            )
            item.setData(0, DIAGNOSTIC_ROW_ROLE, row)
            item.setToolTip(
                1,
                f"{row.code}\n{row.message}" if row.code else row.message,
            )
            item.setToolTip(2, location)
        has_rows = self.doctor_table.topLevelItemCount() > 0
        self.doctor_jump_button.setEnabled(has_rows)
        self.doctor_quick_check_button.setEnabled(
            self._document is not None and not self._run_active
        )
        if has_rows:
            self.doctor_table.setCurrentItem(self.doctor_table.topLevelItem(0))

    def _doctor_item_double_clicked(
        self, item: QTreeWidgetItem, column: int
    ) -> None:
        self.doctor_table.setCurrentItem(item)
        if column == 2:
            self._jump_to_selected_diagnostic()
            return
        self._open_selected_diagnostic_detail()

    def _open_selected_diagnostic_detail(self) -> None:
        item = self.doctor_table.currentItem()
        row = item.data(0, DIAGNOSTIC_ROW_ROLE) if item is not None else None
        if not isinstance(row, DiagnosticRow):
            self.status_message.setText("请先选择一个诊断问题")
            return

        if self._diagnostic_detail_dialog is not None:
            self._diagnostic_detail_dialog.close()

        dialog = QDialog(self)
        dialog.setObjectName("diagnostic_detail_dialog")
        dialog.setWindowTitle("算例医生 · 诊断详情")
        dialog.setModal(False)
        dialog.resize(700, 540)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(14, 14, 14, 14)

        heading = QLabel(
            f"{row.severity_label} · {row.code or '未提供诊断代码'}",
            dialog,
        )
        heading.setObjectName("diagnostic_detail_heading")
        heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(heading)

        layout.addWidget(QLabel("具体问题", dialog))
        message_view = QPlainTextEdit(dialog)
        message_view.setObjectName("diagnostic_detail_message")
        message_view.setReadOnly(True)
        message_view.setPlainText(row.message)
        message_view.setMinimumHeight(105)
        layout.addWidget(message_view, 1)

        layout.addWidget(QLabel("XML 位置", dialog))
        location_view = QPlainTextEdit(dialog)
        location_view.setObjectName("diagnostic_detail_location")
        location_view.setReadOnly(True)
        location_view.setPlainText(self._diagnostic_location_text(row))
        location_view.setMaximumHeight(90)
        layout.addWidget(location_view)

        layout.addWidget(QLabel("源码上下文", dialog))
        source_view = QPlainTextEdit(dialog)
        source_view.setObjectName("diagnostic_detail_source")
        source_view.setReadOnly(True)
        source_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        source_view.setPlainText(
            self._diagnostic_source_context(row)
            or "该诊断没有可显示的源码上下文。"
        )
        layout.addWidget(source_view, 2)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        jump_button = QPushButton("跳转到 XML", dialog)
        jump_button.setObjectName("diagnostic_detail_jump_button")
        jump_button.setEnabled(self._diagnostic_line(row) is not None)
        jump_button.clicked.connect(
            lambda _checked=False: self._jump_to_diagnostic(row)
        )
        button_row.addWidget(jump_button)
        close_button = QPushButton("关闭", dialog)
        close_button.setObjectName("diagnostic_detail_close_button")
        close_button.clicked.connect(dialog.close)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        self._diagnostic_detail_dialog = dialog
        dialog.finished.connect(
            lambda _result, current=dialog: self._diagnostic_detail_closed(current)
        )
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _diagnostic_detail_closed(self, dialog: QDialog) -> None:
        if self._diagnostic_detail_dialog is dialog:
            self._diagnostic_detail_dialog = None
        dialog.deleteLater()

    def _diagnostic_line(self, row: DiagnosticRow) -> int | None:
        line = row.line
        if line is None and row.path and self._document is not None:
            method = getattr(self._document, "source_line", None)
            if callable(method):
                try:
                    line = int(method(row.path))
                except Exception:
                    line = None
        return line if line is not None and line > 0 else None

    def _diagnostic_location_text(self, row: DiagnosticRow) -> str:
        line = self._diagnostic_line(row)
        parts = []
        if row.path:
            parts.append(f"XML 路径：{row.path}")
        if line is not None:
            parts.append(f"源码行号：第 {line} 行")
        return "\n".join(parts) or "该诊断没有可定位的 XML 位置。"

    def _diagnostic_source_context(
        self, row: DiagnosticRow, radius: int = 2
    ) -> str:
        line = self._diagnostic_line(row)
        source_lines = self.source_editor.toPlainText().splitlines()
        if line is None or not source_lines or line > len(source_lines):
            return ""
        start = max(1, line - radius)
        end = min(len(source_lines), line + radius)
        width = len(str(end))
        return "\n".join(
            f"{'→' if number == line else ' '} {number:>{width}} │ "
            f"{source_lines[number - 1]}"
            for number in range(start, end + 1)
        )

    def _jump_to_selected_diagnostic(self) -> None:
        item = self.doctor_table.currentItem()
        row = item.data(0, DIAGNOSTIC_ROW_ROLE) if item is not None else None
        if not isinstance(row, DiagnosticRow):
            self.status_message.setText("请先选择一个诊断问题")
            return
        self._jump_to_diagnostic(row)

    def _jump_to_diagnostic(self, row: DiagnosticRow) -> None:
        line = self._diagnostic_line(row)
        if line is None:
            self.status_message.setText("该问题没有可定位的源码行")
            return
        block = self.source_editor.document().findBlockByNumber(line - 1)
        if not block.isValid():
            self.status_message.setText(f"XML 中不存在第 {line} 行")
            return
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.SelectionType.LineUnderCursor)
        self.source_editor.setTextCursor(cursor)
        self.source_editor.centerCursor()
        self.bottom_tabs.setCurrentWidget(self.source_tab)
        self.source_editor.setFocus()
        if row.path:
            self._select_object_for_source_path(row.path)
        self.status_message.setText(f"已定位到 XML 第 {line} 行")

    def _select_object_for_source_path(self, path: str) -> None:
        for entry in self._entries:
            source = entry.source
            candidates = (
                value_of(source, "element_path", default=""),
                value_of(source, "marker_path", default=""),
                value_of(source, "floating_path", default=""),
                value_of(source, "shapeout_path", default=""),
            )
            if any(
                candidate
                and (path == str(candidate) or path.startswith(f"{candidate}/"))
                for candidate in candidates
            ):
                self.select_object(entry.key)
                return

    def _populate_parameters(self, *_args: Any) -> None:
        if not hasattr(self, "parameter_table"):
            return
        current_item = self.parameter_table.item(
            self.parameter_table.currentRow(), 2
        )
        current_field = (
            current_item.data(PARAMETER_FIELD_ROLE)
            if current_item is not None
            else None
        )
        selected_id = getattr(current_field, "id", "")
        self._loading_parameters = True
        try:
            self.parameter_table.setRowCount(0)
            if self._document is None:
                self.parameter_explanation.setPlainText(
                    "打开 Case_Def.xml 后显示现有标量参数。"
                )
                return
            fields = tuple(
                _document_attribute(
                    self._document, "parameter_fields", default=()
                )
                or ()
            )
            query = self.parameter_search.text().strip().casefold()
            category = self.parameter_category.currentData() or "all"
            diagnostics_by_path: dict[str, list[DiagnosticRow]] = {}
            for diagnostic in self._diagnostic_rows:
                if diagnostic.path:
                    diagnostics_by_path.setdefault(diagnostic.path, []).append(
                        diagnostic
                    )
            filtered: list[tuple[Any, Any, str]] = []
            for field in fields:
                field_category = str(
                    value_of(field, "category", default="execution")
                )
                if category != "all" and field_category != category:
                    continue
                explanation = self._parameter_explanation_for(field)
                haystack = " ".join(
                    (
                        str(value_of(field, "key", default="")),
                        str(value_of(field, "value", default="")),
                        explanation,
                    )
                ).casefold()
                if query and query not in haystack:
                    continue
                path = str(value_of(field, "element_path", default=""))
                status = self._parameter_status(diagnostics_by_path.get(path, []))
                filtered.append((field, explanation, status))

            self.parameter_table.setRowCount(len(filtered))
            draft_diagnostics = _document_attribute(
                self._document, "draft_diagnostics", default=()
            )
            editable = not bool(draft_diagnostics) and not self._run_active
            category_labels = {
                "geometry": "几何",
                "constants": "物理常量",
                "execution": "执行参数",
            }
            for row_index, (field, explanation, status) in enumerate(filtered):
                field_category = str(
                    value_of(field, "category", default="execution")
                )
                key = str(value_of(field, "key", default=""))
                value = str(value_of(field, "value", default=""))
                node_explanation = self._node_explanation(
                    str(value_of(field, "element_path", default=""))
                )
                unit = str(value_of(node_explanation, "units", default="") or "")
                cells = (
                    QTableWidgetItem(
                        category_labels.get(field_category, field_category)
                    ),
                    QTableWidgetItem(key),
                    QTableWidgetItem(value),
                    QTableWidgetItem(unit or "—"),
                    QTableWidgetItem(status),
                )
                for column, cell in enumerate(cells):
                    if column != 2 or not editable:
                        cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    cell.setData(PARAMETER_FIELD_ROLE, field)
                    cell.setToolTip(explanation)
                    self.parameter_table.setItem(row_index, column, cell)
                if getattr(field, "id", "") == selected_id:
                    self.parameter_table.selectRow(row_index)
            if self.parameter_table.rowCount() and self.parameter_table.currentRow() < 0:
                self.parameter_table.selectRow(0)
        finally:
            self._loading_parameters = False
        self._parameter_selection_changed()

    def _node_explanation(self, path: str) -> Any:
        if self._document is None or not path:
            return None
        method = getattr(self._document, "explain_node", None)
        if not callable(method):
            return None
        try:
            return method(path)
        except Exception:
            return None

    def _parameter_explanation_for(self, field: Any) -> str:
        path = str(value_of(field, "element_path", default=""))
        explanation = self._node_explanation(path)
        key = str(value_of(field, "key", default="参数"))
        if explanation is None:
            return f"{key}\n暂无可靠的 DualSPHysics v5.4 离线解释。"
        title = str(value_of(explanation, "title", default=key))
        summary = str(value_of(explanation, "summary", default=""))
        units = value_of(explanation, "units", default=None)
        notes = tuple(value_of(explanation, "notes", default=()) or ())
        effective = value_of(explanation, "effective", default=None)
        heading = title if title.casefold() == key.casefold() else f"{title}（{key}）"
        lines = [heading, summary]
        if units:
            lines.append(f"单位：{units}")
        if effective is False:
            lines.append("状态：当前参数不生效")
        elif effective is True:
            lines.append("状态：当前参数生效")
        lines.extend(f"提示：{note}" for note in notes)
        lines.append(f"XML：{path}")
        return "\n".join(line for line in lines if line)

    @staticmethod
    def _parameter_status(rows: list[DiagnosticRow]) -> str:
        severities = {row.severity for row in rows}
        if "error" in severities:
            return "错误"
        if "warning" in severities:
            return "警告"
        if "info" in severities:
            return "提示"
        return "正常"

    def _parameter_selection_changed(self) -> None:
        if self._loading_parameters:
            return
        item = self.parameter_table.item(self.parameter_table.currentRow(), 2)
        field = item.data(PARAMETER_FIELD_ROLE) if item is not None else None
        if field is None:
            self.parameter_explanation.setPlainText("没有符合筛选条件的参数。")
            return
        self.parameter_explanation.setPlainText(
            self._parameter_explanation_for(field)
        )

    def _parameter_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_parameters or item.column() != 2 or self._document is None:
            return
        field = item.data(PARAMETER_FIELD_ROLE)
        if field is None:
            return
        original = str(value_of(field, "value", default=""))
        if item.text() == original:
            return
        if _document_attribute(self._document, "draft_diagnostics", default=()):
            self.status_message.setText("请先修正 XML 源码错误，再通过参数工作台编辑")
            self._populate_parameters()
            return
        method = getattr(self._document, "update_parameter_value", None)
        if not callable(method):
            self.status_message.setText("当前文档模型不支持参数工作台编辑")
            self._populate_parameters()
            return
        try:
            method(field, item.text())
        except Exception as exc:
            blocker = QSignalBlocker(self.parameter_table)
            item.setText(original)
            del blocker
            self.status_message.setText(f"参数修改失败：{exc}")
            return
        self._dirty = True
        self.refresh_document()
        self._update_source_from_document()
        self._update_title()
        self.status_message.setText(
            f"参数 {value_of(field, 'key', default='')} 已修改，尚未保存"
        )

    def _set_geometry_actions_enabled(self, enabled: bool) -> None:
        for action in (
            self.action_delete_object,
            self.action_translate,
            self.action_rotate,
            self.action_scale,
            self.action_edit_vertex,
        ):
            action.setEnabled(bool(enabled) and not self._run_active)

    def _tree_selection_changed(
        self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None
    ) -> None:
        if current is None:
            return
        key = current.data(0, TREE_KEY_ROLE)
        if key:
            self.select_object(str(key))

    def _tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        key = item.data(0, TREE_KEY_ROLE)
        if key:
            key = str(key)
            visible = item.checkState(0) == Qt.CheckState.Checked
            self._visibility_by_key[key] = visible
            self.viewport.set_object_visible(key, visible)

    def _viewport_selection_changed(self, key: str) -> None:
        self.select_object(key)

    def _viewport_translation_requested(
        self, key: str, dx: float, dy: float, dz: float
    ) -> None:
        if not self.select_object(key) or self._selected_entry is None:
            return
        method = getattr(self._document, "translate_object", None)
        if not callable(method):
            method = getattr(self._document, "translate", None)
        if not callable(method):
            self.status_message.setText("3D 拖动请求已交给应用控制器")
            return
        payload = {
            "delta": (dx, dy, dz),
            "snap_to_grid": self.action_snap_grid.isChecked(),
        }
        self.geometryEditRequested.emit(self._selected_entry.source, "translate", payload)
        try:
            method(
                self._selected_object_id(),
                dx,
                dy,
                dz,
                snap_to_grid=self.action_snap_grid.isChecked(),
            )
        except Exception as exc:
            self._report_error("3D 拖动失败", str(exc))
            self.refresh_document()
            return
        self._after_geometry_edit("对象已通过 3D 手柄拖动")

    def _viewport_vertex_commit_requested(
        self, key: str, vertex_index: int, position: object
    ) -> None:
        self._apply_vertex_position(
            key,
            vertex_index,
            position,
            snap_to_grid=self.action_snap_grid.isChecked(),
        )

    def _viewport_vertex_context_menu_requested(
        self,
        key: str,
        vertex_index: int,
        position: object,
        global_position: QPoint,
    ) -> None:
        try:
            candidate = tuple(float(value) for value in position)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            QMessageBox.warning(self, "坐标无效", "无法读取当前顶点坐标。")
            return
        while True:
            dialog = VertexCoordinateDialog(
                candidate,
                global_position=global_position,
                parent=self,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            candidate = dialog.position()
            if self._apply_vertex_position(
                key,
                vertex_index,
                candidate,
                snap_to_grid=False,
            ):
                return

    def _set_standard_view(self, direction: str) -> None:
        blocker = QSignalBlocker(self.action_orthographic)
        self.action_orthographic.setChecked(True)
        del blocker
        self.viewport.set_standard_view(direction)

    def _show_all_objects(self) -> None:
        blocker = QSignalBlocker(self.model_tree)
        for item in self._tree_item_by_key.values():
            item.setCheckState(0, Qt.CheckState.Checked)
        del blocker
        for key in self._tree_item_by_key:
            self._visibility_by_key[key] = True
        self.viewport.show_all_objects()

    def _property_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_properties or item.column() != 1 or self._selected_entry is None:
            return
        field = item.data(PROPERTY_FIELD_ROLE)
        original = item.data(PROPERTY_RAW_ROLE)
        if not field:
            return
        try:
            value = parse_editor_value(item.text(), original)
        except (TypeError, ValueError) as exc:
            blocker = QSignalBlocker(self.property_table)
            item.setText(str(original))
            del blocker
            self.status_message.setText(str(exc))
            return

        source = self._selected_entry.source
        self.propertyEditRequested.emit(source, str(field), value)
        try:
            applied = self._apply_property_edit(source, str(field), value)
        except Exception as exc:
            self.status_message.setText(f"属性修改失败：{exc}")
            return
        if applied:
            self._dirty = True
            self.refresh_document()
            self._update_source_from_document()
            self._update_title()
            self.status_message.setText("属性已修改，尚未保存")
        else:
            self.status_message.setText("属性修改请求已交给应用控制器")

    def _apply_property_edit(self, source: Any, field: str, value: Any) -> bool:
        if self._document is None:
            return False
        object_id = value_of(source, "id", "key", "uid", default=source)
        candidates = (
            ("update_object_property", (object_id, "boxfill" if field == "box_fill" else field, value)),
            ("set_scene_property", (object_id, field, value)),
            ("update_scene_object_property", (object_id, field, value)),
            ("set_object_property", (object_id, field, value)),
            ("update_object", (object_id, {field: value})),
        )
        for method_name, arguments in candidates:
            method = getattr(self._document, method_name, None)
            if callable(method):
                result = method(*arguments)
                if result is not None and result is not True and result is not False:
                    self._document = result
                return result is not False
        return False

    # ----------------------------------------------------------- geometry tools
    def _selected_object_id(self) -> Any:
        if self._selected_entry is None:
            return None
        return value_of(
            self._selected_entry.source,
            "id",
            "key",
            "uid",
            default=self._selected_entry.source,
        )

    def _request_create_object(self, kind: str) -> None:
        if self._document is None:
            return
        used_markers = {
            "fluid": {
                int(entry.marker)
                for entry in self._entries
                if entry.category == "fluid" and entry.marker.lstrip("-").isdigit()
            },
            "bound": {
                int(entry.marker)
                for entry in self._entries
                if entry.category in {"fixed", "floating"}
                and entry.marker.lstrip("-").isdigit()
            },
        }
        dialog = NewObjectDialog(
            kind,
            used_markers=used_markers,
            used_names={entry.name for entry in self._entries},
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.payload()
        if not payload["shapeout"]:
            self._report_error("无法创建对象", "请输入 ShapeOut 名称。")
            return
        self.createObjectRequested.emit(kind, payload)
        try:
            applied = self._apply_create_object(kind, payload)
        except Exception as exc:
            self._report_error("创建对象失败", str(exc))
            return
        if applied:
            self._after_geometry_edit("对象已创建")
        else:
            self.status_message.setText("创建请求已交给应用控制器")

    def _apply_create_object(self, kind: str, payload: dict[str, Any]) -> bool:
        if self._document is None:
            return False
        specific_names = (
            ("create_box", "add_box") if kind == "box" else ("create_prism", "add_prism")
        )
        for method_name in specific_names:
            method = getattr(self._document, method_name, None)
            if callable(method):
                method(payload)
                return True
        method = getattr(self._document, "create_object", None)
        if callable(method):
            method(kind, payload)
            return True
        return False

    def _request_delete_object(self) -> None:
        if self._selected_entry is None:
            return
        answer = QMessageBox.question(
            self,
            "删除对象",
            f"确定删除“{self._selected_entry.name}”吗？\n\n"
            "若 marker、shapeout 或 floating 仍被其他节点引用，文档模型会阻止删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        source = self._selected_entry.source
        object_id = self._selected_object_id()
        self.deleteObjectRequested.emit(source)
        try:
            applied = False
            for method_name in ("delete_object", "remove_object"):
                method = getattr(self._document, method_name, None)
                if callable(method):
                    method(object_id)
                    applied = True
                    break
        except Exception as exc:
            self._report_error("删除对象失败", str(exc))
            return
        if applied:
            self._selected_entry = None
            self._after_geometry_edit("对象已删除")
        else:
            self.status_message.setText("删除请求已交给应用控制器")

    def _request_translate(self) -> None:
        if self._selected_entry is None:
            return
        dp = _document_attribute(self._document, "dp", default=None)
        dialog = VectorDialog(
            "平移对象",
            ("X 位移", "Y 位移", "Z 位移"),
            (0.0, 0.0, 0.0),
            allow_snap=True,
            dp=dp,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        delta = dialog.vector()
        payload = {"delta": delta, "snap_to_grid": dialog.snap_to_grid()}
        self.geometryEditRequested.emit(self._selected_entry.source, "translate", payload)
        method = getattr(self._document, "translate_object", None)
        if not callable(method):
            method = getattr(self._document, "translate", None)
        if not callable(method):
            self.status_message.setText("平移请求已交给应用控制器")
            return
        try:
            method(
                self._selected_object_id(),
                delta[0],
                delta[1],
                delta[2],
                snap_to_grid=dialog.snap_to_grid(),
            )
        except Exception as exc:
            self._report_error("平移失败", str(exc))
            return
        self._after_geometry_edit("对象已平移")

    def _request_scale(self) -> None:
        if self._selected_entry is None:
            return
        dialog = VectorDialog(
            "缩放对象",
            ("X 比例", "Y 比例", "Z 比例"),
            (1.0, 1.0, 1.0),
            positive=True,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        factors = dialog.vector()
        self.geometryEditRequested.emit(self._selected_entry.source, "scale", factors)
        method = getattr(self._document, "scale_object", None)
        if not callable(method):
            method = getattr(self._document, "scale", None)
        if not callable(method):
            self.status_message.setText("缩放请求已交给应用控制器")
            return
        try:
            parameter_names = list(inspect.signature(method).parameters)
            if len(parameter_names) >= 4 and "factors" not in parameter_names:
                method(self._selected_object_id(), factors[0], factors[1], factors[2])
            else:
                method(self._selected_object_id(), factors)
        except Exception as exc:
            self._report_error("缩放失败", str(exc))
            return
        self._after_geometry_edit("对象已缩放")

    def _request_rotate(self) -> None:
        if self._selected_entry is None:
            return
        dialog = RotationDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        axis = dialog.axis()
        angle = dialog.angle()
        payload = {"axis": axis, "angle_degrees": angle}
        self.geometryEditRequested.emit(self._selected_entry.source, "rotate", payload)
        method = getattr(self._document, "rotate_object", None)
        if not callable(method):
            method = getattr(self._document, "rotate", None)
        if not callable(method):
            self.status_message.setText("旋转请求已交给应用控制器")
            return
        try:
            parameter_names = list(inspect.signature(method).parameters)
            second = parameter_names[1].lower() if len(parameter_names) > 1 else "angle"
            if "angle" in second:
                method(self._selected_object_id(), angle, axis=axis)
            else:
                method(self._selected_object_id(), axis, angle)
        except Exception as exc:
            self._report_error("旋转失败", str(exc))
            return
        self._after_geometry_edit("对象已旋转")

    def _request_vertex_edit(self) -> None:
        if self._selected_entry is None:
            return
        dp = _document_attribute(self._document, "dp", default=None)
        dialog = VertexDialog(self._selected_entry.source, dp=dp, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        index = dialog.vertex_index()
        position = dialog.position()
        self._apply_vertex_position(
            self._selected_entry.key,
            index,
            position,
            snap_to_grid=dialog.snap_to_grid(),
        )

    def _apply_vertex_position(
        self,
        key: str,
        vertex_index: int,
        position: object,
        *,
        snap_to_grid: bool,
    ) -> bool:
        entry = self._entry_by_key.get(key)
        if entry is None or not entry.editable:
            QMessageBox.warning(self, "顶点修改失败", "当前对象不可编辑或已不存在。")
            return False
        payload = {
            "vertex_index": vertex_index,
            "position": position,
            "snap_to_grid": snap_to_grid,
        }
        self.geometryEditRequested.emit(entry.source, "set_vertex", payload)
        method = getattr(self._document, "set_vertex", None)
        if not callable(method):
            method = getattr(self._document, "set_object_vertex", None)
        if not callable(method):
            self.status_message.setText("顶点修改请求已交给应用控制器")
            return False
        object_id = value_of(
            entry.source,
            "id",
            "key",
            "uid",
            default=entry.source,
        )
        try:
            parameter_names = list(inspect.signature(method).parameters)
            third = parameter_names[2].lower() if len(parameter_names) > 2 else "position"
            if third in {"position", "point", "value"}:
                method(
                    object_id,
                    vertex_index,
                    position,
                    snap_to_grid=snap_to_grid,
                )
            else:
                coordinates = tuple(float(value) for value in position)  # type: ignore[arg-type]
                method(
                    object_id,
                    vertex_index,
                    coordinates[0],
                    coordinates[1],
                    coordinates[2],
                )
        except Exception as exc:
            self.status_message.setText(f"顶点修改失败：{exc}")
            QMessageBox.warning(self, "顶点修改失败", str(exc))
            return False
        self._after_geometry_edit("顶点已修改")
        return True

    def _after_geometry_edit(self, message: str) -> None:
        self._dirty = True
        self.refresh_document()
        self._update_source_from_document()
        self.source_editor.document().setModified(True)
        self.source_state_label.setText("XML 已修改 · 尚未保存")
        self.source_state_label.setStyleSheet("color: #9a6700;")
        self.status_message.setText(message)
        self._update_title()

    def _update_source_from_document(self) -> None:
        source = _document_attribute(
            self._document,
            "editor_text",
            "source",
            "text",
            "xml_source",
            "xml_text",
            default=None,
        )
        if source is None or str(source) == self.source_editor.toPlainText():
            return
        self._loading_source = True
        try:
            self.source_editor.setPlainText(str(source))
        finally:
            self._loading_source = False

    # -------------------------------------------------------------- source edit
    def _source_text_changed(self) -> None:
        if self._loading_source:
            return
        self._dirty = True
        self.source_state_label.setText("正在编辑…")
        self.source_state_label.setStyleSheet("color: #9a6700;")
        self._source_timer.start()
        self._update_title()

    def undo(self) -> None:
        if self.source_editor.hasFocus() and self.source_editor.document().isUndoAvailable():
            self.source_editor.undo()
            return
        if self._call_history_method("undo"):
            self.refresh_document()
            self._update_source_from_document()
            self._dirty = True
            self._update_title()
        elif self.source_editor.document().isUndoAvailable():
            self.source_editor.undo()

    def redo(self) -> None:
        if self.source_editor.hasFocus() and self.source_editor.document().isRedoAvailable():
            self.source_editor.redo()
            return
        if self._call_history_method("redo"):
            self.refresh_document()
            self._update_source_from_document()
            self._dirty = True
            self._update_title()
        elif self.source_editor.document().isRedoAvailable():
            self.source_editor.redo()

    def _call_history_method(self, method_name: str) -> bool:
        if self._document is None:
            return False
        method = getattr(self._document, method_name, None)
        if not callable(method):
            return False
        try:
            result = method()
            if result is not None and result is not True and result is not False:
                self._document = result
            return result is not False
        except Exception as exc:
            self.status_message.setText(f"{method_name} 失败：{exc}")
            return False

    # --------------------------------------------------------------- pipeline UI
    def _request_full_run(self) -> None:
        if self._document is None:
            self.status_message.setText("请先打开 XML")
            return
        if not self._flush_source_edit(strict=True):
            return
        self.fullRunRequested.emit()

    def _request_quick_check(self) -> None:
        if self._document is None:
            self.status_message.setText("请先打开 XML")
            return
        if not self._flush_source_edit(strict=True):
            return
        self.set_run_state(True, "正在快速检查…")
        self.quickCheckRequested.emit()

    def _request_generate(self) -> None:
        if self._document is None:
            self.status_message.setText("请先打开 XML")
            return
        if not self._flush_source_edit(strict=True):
            return
        self.set_run_state(True, "正在生成初始模型…")
        self.generateRequested.emit()

    def _request_cpu(self) -> None:
        if self._document is None:
            self.status_message.setText("请先打开 XML")
            return
        if not self._flush_source_edit(strict=True):
            return
        self.set_run_state(True, "正在执行 CPU 求解…")
        self.cpuRequested.emit()

    def _request_gpu(self) -> None:
        if self._document is None:
            self.status_message.setText("请先打开 XML")
            return
        if not self._flush_source_edit(strict=True):
            return
        self.set_run_state(True, "正在执行 GPU 求解…")
        self.gpuRequested.emit()

    def _request_partvtk(self) -> None:
        if self._document is None:
            self.status_message.setText("请先打开 XML")
            return
        if not self._flush_source_edit(strict=True):
            return
        self.set_run_state(True, "正在执行 PartVTK 转换…")
        self.partVTKRequested.emit()

    def begin_run_session(self, session_id: str, backend: str) -> None:
        self.run_session_label.setText(
            f"会话：{session_id}　后端：{backend.upper()}"
        )
        for row in range(self.run_stage_table.rowCount()):
            self.run_stage_table.item(row, 1).setText("等待")
            self.run_stage_table.item(row, 2).setText("")
        self.update_run_stage("preflight", "succeeded", "资源估算已完成")
        self.run_center_tabs.setCurrentWidget(self.run_overview_page)

    def update_run_stage(self, stage: str, status: str, detail: str = "") -> None:
        stage_key = "solver" if stage in {"cpu", "gpu"} else stage
        labels = {
            "pending": "等待",
            "running": "运行中",
            "succeeded": "成功",
            "failed": "失败",
            "cancelled": "已取消",
            "skipped": "已跳过",
            "interrupted": "已中断",
        }
        for row in range(self.run_stage_table.rowCount()):
            item = self.run_stage_table.item(row, 0)
            if item.data(Qt.ItemDataRole.UserRole) != stage_key:
                continue
            self.run_stage_table.item(row, 1).setText(
                labels.get(status, status)
            )
            self.run_stage_table.item(row, 2).setText(detail)
            break

    def update_resource_estimate(self, estimate: Any) -> None:
        rows = getattr(estimate, "metric_rows", lambda: ())()
        self.resource_table.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self.resource_table.setItem(
                    row, column, QTableWidgetItem(str(value))
                )

    def update_stage_progress(
        self,
        stage: str,
        completed: int,
        total: int,
        *,
        eta_seconds: float | None = None,
    ) -> None:
        if total <= 0:
            return
        percent = max(0, min(100, round(completed * 100 / total)))
        self.run_progress.setRange(0, 100)
        self.run_progress.setValue(percent)
        detail = f"{completed}/{total}（{percent}%）"
        if eta_seconds is not None:
            eta = max(0, int(round(eta_seconds)))
            hours, remainder = divmod(eta, 3600)
            minutes, seconds = divmod(remainder, 60)
            if hours:
                eta_text = f"{hours} 小时 {minutes} 分"
            elif minutes:
                eta_text = f"{minutes} 分 {seconds} 秒"
            else:
                eta_text = f"{seconds} 秒"
            detail += f"　预计剩余 {eta_text}"
        self.update_run_stage(stage, "running", detail)

    def set_run_history(self, sessions: Any) -> None:
        self._run_sessions = tuple(sessions)
        blocker = QSignalBlocker(self.run_history_combo)
        self.run_history_combo.clear()
        for session in self._run_sessions:
            status = str(getattr(session, "status", "unknown"))
            backend = str(getattr(session, "backend", "")).upper()
            created = str(getattr(session, "created_at", ""))
            session_id = str(getattr(session, "session_id", ""))
            record_path = str(getattr(session, "record_path", ""))
            self.run_history_combo.addItem(
                f"{created[:19]}　{backend}　{status}　{session_id[-8:]}",
                record_path,
            )
        if self.run_history_combo.count() == 0:
            self.run_history_combo.addItem("暂无记录", "")
        comparison_available = len(self._run_sessions) >= 2 and not self._run_active
        self.action_compare_runs.setEnabled(comparison_available)
        self.compare_runs_button.setEnabled(comparison_available)
        del blocker

    def set_run_record_available(self, available: bool) -> None:
        self.action_open_run_output.setEnabled(available)
        self.action_export_run.setEnabled(available)
        self.action_rerun_record.setEnabled(available and not self._run_active)
        self.open_run_output_button.setEnabled(available)
        self.export_run_button.setEnabled(available)
        self.rerun_record_button.setEnabled(available and not self._run_active)

    def _run_history_selection_changed(self, index: int) -> None:
        path = str(self.run_history_combo.itemData(index) or "")
        if path:
            self.runSessionSelected.emit(path)

    def _show_run_comparison(self) -> None:
        if len(self._run_sessions) < 2:
            QMessageBox.information(
                self,
                "运行记录不足",
                "至少需要两条当前案例的运行记录才能进行对比。",
            )
            return
        selected_record = str(
            self.run_history_combo.currentData(Qt.ItemDataRole.UserRole) or ""
        )
        dialog = RunComparisonDialog(
            self._run_sessions,
            selected_record=selected_record,
            parent=self,
        )
        dialog.exec()

    def set_run_state(self, running: bool, message: str | None = None) -> None:
        self._run_active = bool(running)
        run_enabled = not running and self._document is not None
        self.action_configure_dualsphysics.setEnabled(not running)
        self.action_full_run.setEnabled(run_enabled)
        self.action_quick_check.setEnabled(run_enabled)
        self.action_generate.setEnabled(run_enabled)
        self.action_cpu.setEnabled(run_enabled)
        self.action_gpu.setEnabled(run_enabled)
        self.action_partvtk.setEnabled(run_enabled)
        self.action_cancel.setEnabled(running)
        self.run_history_combo.setEnabled(not running)
        comparison_available = len(self._run_sessions) >= 2 and not running
        self.action_compare_runs.setEnabled(comparison_available)
        self.compare_runs_button.setEnabled(comparison_available)
        record_available = self.action_export_run.isEnabled()
        self.action_rerun_record.setEnabled(record_available and not running)
        self.rerun_record_button.setEnabled(record_available and not running)
        self.action_new_box.setEnabled(not running and self._document is not None)
        self.action_new_prism.setEnabled(not running and self._document is not None)
        self._set_geometry_actions_enabled(
            not running
            and self._selected_entry is not None
            and self._selected_entry.editable
        )
        self.doctor_quick_check_button.setEnabled(run_enabled)
        self._populate_parameters()
        if running:
            self.run_progress.setRange(0, 0)
            self.bottom_tabs.setCurrentWidget(self.log_tab)
            self.run_center_tabs.setCurrentWidget(self.run_log_page)
        else:
            self.run_progress.setRange(0, 100)
            self.run_progress.setValue(100 if message and "成功" in message else 0)
        self.run_state_label.setText(message or ("运行中" if running else "尚未运行任务"))
        self.status_message.setText(message or ("运行中" if running else "就绪"))

    def append_log(self, text: str, *, stream: str = "stdout") -> None:
        if not text:
            return
        cursor = self.log_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        if stream.lower() == "stderr":
            cursor.insertText("[错误] ")
        cursor.insertText(text)
        if not text.endswith("\n"):
            cursor.insertText("\n")
        self.log_view.setTextCursor(cursor)
        self.log_view.ensureCursorVisible()

    def finish_run(
        self,
        result: Any,
        *,
        stage: str | None = None,
        workflow_continues: bool = False,
    ) -> None:
        success = bool(value_of(result, "success", default=False))
        status = value_of(result, "status", default="")
        if str(status).lower() in {"success", "succeeded", "complete", "completed"}:
            success = True
        manifest = value_of(result, "manifest", default=None)
        manifest_stage = value_of(manifest, "stage", default="")
        stage = str(stage or manifest_stage or "gencase").strip().lower()
        stage_label = {
            "gencase": "GenCase",
            "cpu": "CPU 求解",
            "gpu": "GPU 求解",
            "partvtk": "PartVTK 转换",
        }.get(stage, "当前任务")
        message = str(value_of(result, "message", default="")).strip()
        if not message and str(status).lower() == "cancelled":
            message = f"{stage_label}已取消"
        if workflow_continues and success:
            self.set_run_state(True, message or f"{stage_label}成功，准备下一阶段…")
        else:
            self.set_run_state(
                False,
                message or f"{stage_label}{'成功完成' if success else '运行失败'}",
            )
        log = value_of(result, "stdout", default="")
        if log:
            self.append_log(str(log))
        error = value_of(result, "stderr", "error", default="")
        if error:
            self.append_log(str(error), stream="stderr")
        summary = value_of(result, "summary", default={})
        if isinstance(summary, dict) and summary:
            self.append_log("\n结果摘要")
            for key, value in summary.items():
                self.append_log(f"  {key}: {value}")

        if stage in {"cpu", "gpu"} or (stage == "partvtk" and not success):
            return

        artifacts = value_of(result, "artifacts", "files", "outputs", default=()) or ()
        candidates: list[Path] = []
        for artifact in artifacts:
            artifact_path = value_of(artifact, "path", "file", default=artifact)
            if artifact_path and str(artifact_path).lower().endswith(".vtk"):
                candidate = Path(str(artifact_path))
                if (
                    candidate.is_file()
                    and candidate.stat().st_size > 0
                    and (stage != "partvtk" or candidate.name.lower().startswith("partall_"))
                ):
                    candidates.append(candidate)
        if candidates:
            if stage == "partvtk":
                preferred = sorted(candidates, key=lambda candidate: candidate.name.lower())[0]
            else:
                preferred = sorted(
                    candidates,
                    key=lambda candidate: (
                        "mkcells" not in candidate.name.lower(),
                        "all" not in candidate.name.lower(),
                        candidate.name.lower(),
                    ),
                )[0]
            self.viewport.load_legacy_vtk(preferred)

    # ---------------------------------------------------------------- AI panel
    def _preview_ai_payload(self) -> None:
        source = self.source_editor.toPlainText()
        model = self.ai_model_edit.text().strip() or "gpt-5.6-terra"
        self.aiPayloadPreviewRequested.emit(source, model)
        preview = source if len(source) <= 10_000 else source[:10_000] + "\n\n…（预览已截断）"
        dialog = QMessageBox(self)
        dialog.setWindowTitle("AI 发送载荷预览")
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setText(f"模型：{model}\nXML 字符数：{len(source):,}")
        dialog.setDetailedText(preview)
        dialog.exec()

    def _request_ai_explanation(self) -> None:
        if not self.ai_consent_check.isChecked():
            return
        source = self.source_editor.toPlainText()
        model = self.ai_model_edit.text().strip() or "gpt-5.6-terra"
        self.ai_result_view.setPlainText("正在等待 AI 解读…")
        self.aiExplainRequested.emit(source, model)

    def set_ai_result(self, result: Any) -> None:
        if isinstance(result, str):
            text = result
        else:
            sections = []
            for field, title in (
                ("summary", "摘要"),
                ("node_explanations", "节点解释"),
                ("risks", "风险"),
                ("suggested_edits", "建议修改（需人工确认）"),
            ):
                value = value_of(result, field, default=None)
                if value:
                    sections.append(f"{title}\n{'─' * len(title)}\n{value}")
            text = "\n\n".join(sections) or str(result)
        self.ai_result_view.setPlainText(text)

    # --------------------------------------------------------------- dialogs/io
    def _open_dialog(self) -> None:
        initial = str(self._document_path.parent) if self._document_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "打开 DualSPHysics XML", initial, "DualSPHysics XML (*.xml);;所有文件 (*)"
        )
        if path:
            self.open_path(path)

    def _save_as_dialog(self) -> bool:
        initial = str(self._document_path or Path.cwd() / "Case_Def.xml")
        path, _ = QFileDialog.getSaveFileName(
            self, "另存 DualSPHysics XML", initial, "DualSPHysics XML (*.xml)"
        )
        if not path:
            return False
        if not Path(path).suffix:
            path += ".xml"
        return self.save_document(path)

    def _load_vtk_dialog(self) -> None:
        initial = str(self._document_path.parent) if self._document_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "加载 3D 文件",
            initial,
            "3D 文件 (*.vtk *.stl);;Legacy VTK (*.vtk);;STL 模型 (*.stl)",
        )
        if not path:
            return
        self._load_mesh_path(path)

    def _load_mesh_path(self, path: str | Path) -> bool:
        path = Path(path)
        suffix = Path(path).suffix.lower()
        if suffix not in {".vtk", ".stl"}:
            self._report_error("不支持的 3D 文件", "仅支持 .vtk 和 .stl 文件。")
            return False
        try:
            report = inspect_mesh_file(path)
        except MeshPreflightError as exc:
            self._report_error(
                f"{suffix[1:].upper()} 安全预检失败",
                f"{path.name}\n\n{exc}",
            )
            return False
        if report.blocked:
            self._report_error(
                "模型超过安全加载上限",
                f"{path.name}\n\n{report.summary()}",
            )
            return False
        allow_large = False
        if report.requires_confirmation:
            answer = QMessageBox.question(
                self,
                "大型模型资源警告",
                f"{path.name}\n\n{report.summary()}\n\n仍要加载本次模型吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.status_message.setText("已取消大型模型加载")
                return False
            allow_large = True
        loader = (
            self.viewport.load_legacy_vtk
            if suffix == ".vtk"
            else self.viewport.load_stl
        )
        if allow_large:
            return loader(str(path), allow_large=True)
        return loader(str(path))

    def _vtk_loaded(self, path: str) -> None:
        stats = self.viewport.last_mesh_stats
        suffix = f" · {stats.points:,} 点" if stats is not None else ""
        self.status_message.setText(f"已加载权威 VTK：{Path(path).name}{suffix}")

    def _vtk_failed(self, path: str, error: str) -> None:
        self._report_error("VTK 加载失败", f"{Path(path).name}\n\n{error}")

    def _stl_loaded(self, path: str) -> None:
        stats = self.viewport.last_mesh_stats
        suffix = (
            f" · {stats.points:,} 点/{stats.cells:,} 三角形"
            if stats is not None
            else ""
        )
        self.status_message.setText(
            f"已加载 STL 表面模型（保留原始坐标与尺度）：{Path(path).name}{suffix}"
        )

    def _stl_failed(self, path: str, error: str) -> None:
        self._report_error("STL 加载失败", f"{Path(path).name}\n\n{error}")

    def _refresh_extension_menu(self) -> None:
        self.extensions_menu.clear()
        self._extension_actions.clear()
        try:
            tools = discover_extension_tools(self._extension_directory)
        except (OSError, ExtensionToolError) as exc:
            unavailable = QAction("无法访问当前扩展目录", self.extensions_menu)
            unavailable.setEnabled(False)
            unavailable.setToolTip(str(exc))
            self.extensions_menu.addAction(unavailable)
        else:
            if not tools:
                empty = QAction("未发现可用扩展工具", self.extensions_menu)
                empty.setEnabled(False)
                empty.setToolTip(str(self._extension_directory))
                self.extensions_menu.addAction(empty)
            for index, tool in enumerate(tools):
                label = tool.label if tool.available else f"不可用 · {tool.label}"
                action = QAction(label, self.extensions_menu)
                action.setObjectName(f"extension_tool_{index}")
                action.setData(str(tool.path))
                action.setToolTip(tool.detail_text())
                action.setEnabled(tool.available)
                action.triggered.connect(
                    lambda _checked=False, selected=tool: self._request_extension_tool(
                        selected
                    )
                )
                self.extensions_menu.addAction(action)
                self._extension_actions.append(action)

        self.extensions_menu.addSeparator()
        self.extensions_menu.addAction(self.action_scan_extensions)

    def _request_extension_tool(self, tool: ExtensionTool) -> None:
        if not tool.available:
            return
        try:
            executable_stat = tool.path.stat()
            manifest_stamp = ""
            if tool.manifest_path is not None:
                manifest_stat = tool.manifest_path.stat()
                manifest_stamp = (
                    f"|{manifest_stat.st_size}|{manifest_stat.st_mtime_ns}"
                )
            trust_key = (
                f"{str(tool.path).casefold()}|{executable_stat.st_size}|"
                f"{executable_stat.st_mtime_ns}{manifest_stamp}"
            )
        except OSError as error:
            QMessageBox.critical(self, "扩展工具不可用", str(error))
            return
        if trust_key not in self._trusted_extensions:
            answer = QMessageBox.question(
                self,
                "确认启动外部扩展程序",
                f"{tool.detail_text()}\n\n"
                "Studio 将直接启动该 EXE，不会通过 Shell，也不会自动传入当前案例。"
                "外部程序拥有与当前用户相同的系统权限，请仅运行可信来源的软件。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._trusted_extensions.add(trust_key)
        self.extensionToolRequested.emit(str(tool.path))

    def _scan_extension_directory(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "扫描可用扩展功能",
            str(self._extension_directory),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not selected:
            return
        self._extension_directory = Path(selected).resolve()
        self._refresh_extension_menu()
        self.status_message.setText(f"扩展功能目录：{self._extension_directory}")

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            f"关于 {PRODUCT_NAME}",
            f"<b>{PRODUCT_NAME} {VERSION}</b><br><br>"
            "DualSPHysics v5.4 XML 可视化、离线解读与本地计算工作台。<br>"
            "3D 实时表面是近似预览，官方工具生成的粒子结果为权威输出。<br><br>"
            "Copyright © 2026 Fang Li<br>"
            "本程序采用 MIT License 发布。<br><br>"
            "本程序是独立项目，与 DualSPHysics 项目及其开发团队不存在隶属、"
            "赞助或官方认可关系。",
        )

    def _report_error(self, title: str, message: str) -> None:
        self.status_message.setText(message.splitlines()[0] if message else title)
        QMessageBox.critical(self, title, message)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._run_active:
            self.cancelRequested.emit()
            QMessageBox.information(
                self,
                "正在取消当前任务",
                "已请求取消当前运行。请等待日志显示运行结束后再关闭程序。",
            )
            event.ignore()
            return
        document_dirty = bool(
            _document_attribute(self._document, "dirty", "is_dirty", default=False)
        ) if self._document is not None else False
        if not self._dirty and not document_dirty:
            event.accept()
            return
        choice = QMessageBox.warning(
            self,
            "尚未保存",
            "当前 XML 有尚未保存的修改。关闭前要保存吗？",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Save:
            if self.save_document():
                event.accept()
            else:
                event.ignore()
        elif choice == QMessageBox.StandardButton.Discard:
            event.accept()
        else:
            event.ignore()

    # --------------------------------------------------------------- misc state
    def _show_welcome_state(self) -> None:
        self.diagnostics_view.setPlainText("打开 Case_Def.xml 后显示参数和关联诊断。")
        self._diagnostic_rows = []
        self._populate_doctor()
        self._populate_parameters()
        self.explanation_view.setPlainText(
            f"欢迎使用 DualSPHysics Studio {VERSION}。\n\n"
            "请打开 Case_Def.xml。模型树、3D 预览、属性表与源码将保持联动。"
        )
        self.action_save.setEnabled(False)
        self.action_save_as.setEnabled(False)
        self.action_full_run.setEnabled(False)
        self.action_quick_check.setEnabled(False)
        self.action_generate.setEnabled(False)
        self.action_cpu.setEnabled(False)
        self.action_gpu.setEnabled(False)
        self.action_partvtk.setEnabled(False)

    def _update_title(self) -> None:
        name = self._document_path.name if self._document_path else "未命名案例"
        dirty = " *" if self._dirty else ""
        self.setWindowTitle(f"{name}{dirty} — {PRODUCT_NAME} {VERSION}")
        enabled = self._document is not None
        self.action_save.setEnabled(enabled)
        self.action_save_as.setEnabled(enabled)
        self.action_new_box.setEnabled(enabled and not self._run_active)
        self.action_new_prism.setEnabled(enabled and not self._run_active)
        self._set_geometry_actions_enabled(
            enabled and self._selected_entry is not None and self._selected_entry.editable
        )
        if not self._run_active:
            self.action_full_run.setEnabled(enabled)
            self.action_quick_check.setEnabled(enabled)
            self.action_generate.setEnabled(enabled)
            self.action_cpu.setEnabled(enabled)
            self.action_gpu.setEnabled(enabled)
            self.action_partvtk.setEnabled(enabled)


__all__ = ["MainWindow", "PRODUCT_NAME", "VERSION"]
