"""Application controller for DualSPHysics Studio.

Qt widgets live in :mod:`dualsphysics_studio.ui`; this module connects them to
the lossless document, local DualSPHysics pipeline, persistent settings, and
optional OpenAI provider.  External tools and network calls run off the GUI
thread.
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from copy import copy
from datetime import datetime
from pathlib import Path
from threading import Event
import time
from typing import Any

from PySide6.QtCore import QEvent, QObject, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .ai import ExplainRequest, OpenAIProvider, SecretStore
from .core import CaseDocument
from .extensions import ExtensionToolError, launch_extension_tool
from .pipeline import (
    DualSPHysicsCPUAdapter,
    DualSPHysicsGPUAdapter,
    GenCaseAdapter,
    PartVTKAdapter,
    PipelineAdapter,
    ResourceEstimate,
    RunRequest,
    RunSession,
    estimate_case,
    find_case_sessions,
    live_stage_progress,
    session_history_values,
)
from .pipeline.models import file_sha256
from .resources import resource_path
from .settings import AppSettings, cache_dir
from .ui import MainWindow
from .version import APP_NAME, APP_VERSION


_STAGE_LABELS = {
    "gencase": "GenCase",
    "cpu": "CPU 求解",
    "gpu": "GPU 求解",
    "partvtk": "PartVTK 转换",
}

_DUALSPHYSICS_ADAPTER_TYPES: tuple[type[PipelineAdapter], ...] = (
    GenCaseAdapter,
    DualSPHysicsCPUAdapter,
    DualSPHysicsGPUAdapter,
    PartVTKAdapter,
)


class _PipelineWorker(QObject):
    log = Signal(str, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        adapter: PipelineAdapter,
        request: RunRequest,
        cancel_event: Event,
    ) -> None:
        super().__init__()
        self.adapter = adapter
        self.request = request
        self.cancel_event = cancel_event

    @Slot()
    def execute(self) -> None:
        try:
            result = self.adapter.run(
                self.request,
                on_log=lambda stream, line: self.log.emit(stream, line),
                cancel_event=self.cancel_event,
            )
        except Exception as exc:  # defensive boundary around external tooling
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(result)


class _AIWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, provider: OpenAIProvider, request: ExplainRequest) -> None:
        super().__init__()
        self.provider = provider
        self.request = request

    @Slot()
    def execute(self) -> None:
        try:
            self.finished.emit(self.provider.explain(self.request))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def format_ai_result(result: Any) -> str:
    """Format a schema-validated AI response, including a reviewable diff."""

    lines = ["摘要", "────", str(getattr(result, "summary", ""))]
    explanations = list(getattr(result, "node_explanations", ()) or ())
    if explanations:
        lines.extend(["", "节点解释", "────────"])
        for item in explanations:
            lines.append(
                f"[{getattr(item, 'selector', '')}] {getattr(item, 'title', '')}\n"
                f"{getattr(item, 'explanation', '')}"
            )
    risks = list(getattr(result, "risks", ()) or ())
    if risks:
        lines.extend(["", "风险", "────"])
        for item in risks:
            severity = str(getattr(item, "severity", "info")).upper()
            lines.append(
                f"[{severity}] {getattr(item, 'selector', '')}: "
                f"{getattr(item, 'message', '')}"
            )
    suggestions = list(getattr(result, "suggested_edits", ()) or ())
    if suggestions:
        lines.extend(["", "建议修改（尚未应用）", "────────────"])
        for item in suggestions:
            selector = str(getattr(item, "selector", ""))
            attribute = str(getattr(item, "attribute", ""))
            old = str(getattr(item, "old_value", ""))
            new = str(getattr(item, "new_value", ""))
            reason = str(getattr(item, "reason", ""))
            lines.extend(
                [
                    f"--- {selector}",
                    f"+++ {selector}",
                    f'- {attribute}="{old}"',
                    f'+ {attribute}="{new}"',
                    f"  原因：{reason}",
                ]
            )
    return "\n".join(lines).strip()


class _AISettingsDialog(QDialog):
    def __init__(
        self,
        parent: MainWindow,
        *,
        model: str,
        has_key: bool,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("配置 OpenAI")
        self.setModal(True)
        layout = QVBoxLayout(self)
        note = QLabel(
            "密钥只保存到 Windows Credential Manager，不会写入设置、日志或发布包。",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.model_edit = QLineEdit(model, self)
        self.key_edit = QLineEdit(self)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("已配置；留空则保持不变" if has_key else "sk-…")
        form.addRow("模型", self.model_edit)
        form.addRow("API 密钥", self.key_edit)
        layout.addLayout(form)
        self.delete_button = QPushButton("删除已保存密钥", self)
        self.delete_button.setEnabled(has_key)
        layout.addWidget(self.delete_button)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class _FullRunDialog(QDialog):
    """Explicit confirmation for the uninterrupted full pipeline."""

    def __init__(
        self,
        parent: MainWindow,
        *,
        default_backend: str,
        estimate: ResourceEstimate,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认一键完整运行")
        self.setModal(True)
        self.resize(620, 460)
        layout = QVBoxLayout(self)
        warning = QLabel(
            "<b>确认后将直接完成剩余全部步骤：</b><br>"
            "GenCase → 选择的 CPU/GPU 求解 → PartVTK。<br><br>"
            "流程中不会再次逐阶段询问。开始前请确认 XML 参数、几何、边界条件，"
            "以及用于比对或校准的数据均合理。运行可能持续很久并产生大量文件。",
            self,
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)

        form = QFormLayout()
        self.backend_combo = QComboBox(self)
        self.backend_combo.addItem("GPU 求解", "gpu")
        self.backend_combo.addItem("CPU 求解", "cpu")
        index = self.backend_combo.findData(default_backend.casefold())
        self.backend_combo.setCurrentIndex(max(0, index))
        form.addRow("求解后端", self.backend_combo)
        layout.addLayout(form)

        estimate_view = QPlainTextEdit(self)
        estimate_view.setReadOnly(True)
        estimate_view.setMaximumHeight(190)
        estimate_view.setPlainText(
            "\n".join(
                f"{label}：{value}　[{confidence}]\n  {basis}"
                for label, value, confidence, basis in estimate.metric_rows()
            )
        )
        layout.addWidget(estimate_view)

        self.confirm_check = QCheckBox(
            "我已检查 XML 和比对数据，并确认立即执行完整流程（包含 PartVTK）",
            self,
        )
        layout.addWidget(self.confirm_check)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.start_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.start_button.setText("开始完整运行")
        self.start_button.setEnabled(False)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.confirm_check.toggled.connect(self.start_button.setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def backend(self) -> str:
        return str(self.backend_combo.currentData())


class _DiagnosticExportDialog(QDialog):
    """Collect the explicit privacy exception for diagnostic export."""

    def __init__(self, parent: MainWindow) -> None:
        super().__init__(parent)
        self.setWindowTitle("导出可复现记录")
        self.setModal(True)
        layout = QVBoxLayout(self)
        note = QLabel(
            "默认导出 XML 快照、脱敏日志、阶段清单、工具哈希和校验文件。"
            "绝对路径会自动替换为占位符。",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.exclude_xml_check = QCheckBox(
            "因案例隐私需要，不在导出包中包含 XML",
            self,
        )
        self.exclude_xml_check.setChecked(False)
        layout.addWidget(self.exclude_xml_check)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("选择导出位置")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def include_xml(self) -> bool:
        return not self.exclude_xml_check.isChecked()


class StudioController(QObject):
    """Own application-level state and asynchronous integrations."""

    def __init__(
        self,
        application: QApplication,
        *,
        initial_path: str | Path | None = None,
    ) -> None:
        super().__init__()
        self.application = application
        self.settings = AppSettings.load()
        self.secret_store = SecretStore()
        self.window = MainWindow(document_loader=CaseDocument.from_file)
        self.window.installEventFilter(self)
        self.window.ai_model_edit.setText(self.settings.openai_model)
        self.window.action_orthographic.setChecked(self.settings.orthographic_view)
        self._restore_window_state()

        icon_path = resource_path("app_icon.svg")
        if icon_path.is_file():
            icon = QIcon(str(icon_path))
            self.application.setWindowIcon(icon)
            self.window.setWindowIcon(icon)

        self._active_adapter: PipelineAdapter | None = None
        self._active_stage: str | None = None
        self._cancel_event: Event | None = None
        self._run_thread: QThread | None = None
        self._run_worker: _PipelineWorker | None = None
        self._ai_thread: QThread | None = None
        self._ai_worker: _AIWorker | None = None
        self._last_run: Any = None
        self._last_gencase_run: Any = None
        self._case_output_prefix: Path | None = None
        self._case_output_source: Path | None = None
        self._run_session: RunSession | None = None
        self._selected_session: RunSession | None = None
        self._full_run_active = False
        self._manual_session_active = False
        self._full_run_backend: str | None = None
        self._full_run_adapters: dict[str, tuple[PipelineAdapter, Any]] = {}
        self._pending_full_stage: str | None = None
        self._run_estimate: ResourceEstimate | None = None
        self._gencase_summary: dict[str, Any] = {}
        self._active_stage_started_monotonic: float | None = None

        self.window.documentChanged.connect(self._document_changed)
        self.window.fullRunRequested.connect(self._start_full_run)
        self.window.quickCheckRequested.connect(lambda: self._start_run("quick"))
        self.window.generateRequested.connect(lambda: self._start_run("full"))
        self.window.cpuRequested.connect(self._start_cpu)
        self.window.gpuRequested.connect(self._start_gpu)
        self.window.partVTKRequested.connect(self._start_partvtk)
        self.window.cancelRequested.connect(self.cancel_run)
        self.window.openRunOutputRequested.connect(self._open_run_output)
        self.window.exportRunRequested.connect(self._export_run_record)
        self.window.rerunFromRecordRequested.connect(self._rerun_from_record)
        self.window.runSessionSelected.connect(self._select_run_session)
        self.window.configureDualSPHysicsRequested.connect(
            self.configure_dualsphysics
        )
        self.window.configureAIRequested.connect(self.configure_ai)
        self.window.extensionToolRequested.connect(self._launch_extension_tool)
        self.window.aiExplainRequested.connect(self._start_ai)
        self.window.sourceEdited.connect(lambda _text: QTimer.singleShot(0, self._mark_stale))
        self.window.propertyEditRequested.connect(
            lambda *_args: QTimer.singleShot(0, self._mark_stale)
        )
        self.application.aboutToQuit.connect(self._persist_settings)

        if initial_path is not None:
            QTimer.singleShot(0, lambda: self.window.open_path(initial_path))

    def show(self) -> None:
        self.window.show()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt API
        if watched is self.window and event.type() == QEvent.Type.Close:
            if self._run_thread is not None:
                self.cancel_run()
                stage = _STAGE_LABELS.get(self._active_stage or "", "当前任务")
                QMessageBox.information(
                    self.window,
                    f"正在取消{stage}",
                    f"请等待{stage}结束后再关闭程序。",
                )
                return True
            if self._ai_thread is not None:
                QMessageBox.information(
                    self.window,
                    "AI 解读仍在进行",
                    "请等待本次联网解读完成或超时后再关闭程序。离线编辑不受影响。",
                )
                return True
        return super().eventFilter(watched, event)

    @Slot(str)
    def _launch_extension_tool(self, path: str) -> None:
        try:
            launch_extension_tool(path)
        except ExtensionToolError as exc:
            self.window.status_message.setText(str(exc))
            QMessageBox.critical(self.window, "扩展工具启动失败", str(exc))
            return
        self.window.status_message.setText(f"已启动扩展工具：{Path(path).name}")

    def _restore_window_state(self) -> None:
        try:
            if self.settings.window_geometry:
                self.window.restoreGeometry(b64decode(self.settings.window_geometry))
            if self.settings.window_state:
                self.window.restoreState(b64decode(self.settings.window_state))
        except (ValueError, TypeError):
            pass

    @Slot()
    def _persist_settings(self) -> None:
        self.settings.openai_model = (
            self.window.ai_model_edit.text().strip() or "gpt-5.6-terra"
        )
        self.settings.orthographic_view = self.window.action_orthographic.isChecked()
        self.settings.window_geometry = b64encode(self.window.saveGeometry().data()).decode("ascii")
        self.settings.window_state = b64encode(self.window.saveState().data()).decode("ascii")
        try:
            self.settings.save()
        except OSError:
            # Exiting must not be blocked by a non-critical preference failure.
            pass

    @Slot(object)
    def _document_changed(self, document: Any) -> None:
        path = getattr(document, "path", None)
        if path:
            self.settings.remember_file(str(path))
        self._last_run = None
        self._last_gencase_run = None
        self._case_output_prefix = None
        self._case_output_source = None
        self._run_session = None
        self._selected_session = None
        self._full_run_active = False
        self._manual_session_active = False
        self._pending_full_stage = None
        self._gencase_summary = {}
        if path:
            source = Path(path).resolve()
            sessions = self._refresh_run_history(source)
            try:
                estimate = estimate_case(
                    document,
                    output_directory=GenCaseAdapter.default_output_prefix(source).parent,
                    source_sha256=file_sha256(source),
                    backend=self.settings.last_solver_backend,
                    history=session_history_values(sessions),
                )
            except OSError:
                estimate = estimate_case(document)
            self._run_estimate = estimate
            self.window.update_resource_estimate(estimate)
        else:
            self.window.set_run_history(())
            self.window.set_run_record_available(False)

    def _refresh_run_history(self, source: Path) -> tuple[RunSession, ...]:
        sessions = find_case_sessions(source)
        for session in sessions:
            session.mark_interrupted()
        self.window.set_run_history(sessions)
        if sessions:
            self._display_session(sessions[0])
        else:
            self.window.set_run_record_available(False)
        return sessions

    @Slot(str)
    def _select_run_session(self, record_path: str) -> None:
        try:
            session = RunSession.load(record_path)
        except (OSError, ValueError, TypeError) as error:
            QMessageBox.warning(
                self.window,
                "无法读取运行记录",
                str(error),
            )
            return
        if session.mark_interrupted():
            self.window.append_log(
                f"检测到未正常结束的会话，已标记为中断：{session.session_id}"
            )
        self._display_session(session)

    def _display_session(self, session: RunSession) -> None:
        self._selected_session = session
        self.window.begin_run_session(session.session_id, session.backend)
        for stage, entry in session.stages.items():
            detail = ""
            errors = entry.get("errors") or ()
            if errors:
                detail = str(errors[0])
            self.window.update_run_stage(
                stage,
                str(entry.get("status", "unknown")),
                detail,
            )
        if session.estimates:
            try:
                estimate = ResourceEstimate.from_dict(session.estimates[-1])
            except (TypeError, ValueError):
                estimate = None
            if estimate is not None:
                self.window.update_resource_estimate(estimate)
        self.window.set_run_record_available(True)

    @Slot()
    def _start_full_run(self, backend_override: str | None = None) -> None:
        if self._run_thread is not None:
            return
        source = self._prepare_source()
        if source is None:
            return
        output_prefix = GenCaseAdapter.suggest_output_prefix(source)
        sessions = find_case_sessions(source)
        backend = (
            backend_override
            if backend_override in {"cpu", "gpu"}
            else self.settings.last_solver_backend
            if self.settings.last_solver_backend in {"cpu", "gpu"}
            else "gpu"
        )
        source_hash = file_sha256(source)
        estimate = estimate_case(
            self.window.document,
            output_directory=output_prefix.parent,
            source_sha256=source_hash,
            backend=backend,
            history=session_history_values(sessions),
        )
        dialog = _FullRunDialog(
            self.window,
            default_backend=backend,
            estimate=estimate,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.window.set_run_state(False, "已取消完整运行")
            return
        backend = dialog.backend
        if backend != self.settings.last_solver_backend:
            self.settings.last_solver_backend = backend
            try:
                self.settings.save()
            except OSError:
                pass

        located = self._locate_full_run_tools(source, backend)
        if located is None:
            self.window.set_run_state(False, "完整运行所需工具未通过核验")
            return
        # Backend selection can change in the confirmation dialog; refresh the
        # history-calibrated duration using the final explicit choice.
        estimate = estimate_case(
            self.window.document,
            output_directory=output_prefix.parent,
            source_sha256=source_hash,
            backend=backend,
            history=session_history_values(sessions),
        )
        workflow = ("gencase", backend, "partvtk")
        self.window.set_run_state(True, "正在创建记录并计算工具 SHA-256…")
        try:
            session = RunSession.create(
                source_xml=source,
                output_root=output_prefix.parent,
                backend=backend,
                workflow=workflow,
                tools={stage: info for stage, (_adapter, info) in located.items()},
            )
        except (OSError, ValueError) as error:
            QMessageBox.critical(
                self.window,
                "无法创建运行记录",
                f"完整流程尚未启动。\n\n{error}",
            )
            self.window.set_run_state(False, "运行记录创建失败")
            return

        session.add_estimate(estimate)
        self._run_session = session
        self._selected_session = session
        self._full_run_active = True
        self._manual_session_active = False
        self._full_run_backend = backend
        self._full_run_adapters = located
        self._pending_full_stage = None
        self._run_estimate = estimate
        self._gencase_summary = {}
        self._case_output_prefix = output_prefix.resolve()
        self._case_output_source = source.resolve()
        self.window.log_view.clear()
        self.window.begin_run_session(session.session_id, backend)
        self.window.update_resource_estimate(estimate)
        self.window.set_run_record_available(True)
        self.window.set_run_state(True, "完整运行：准备 GenCase…")
        self._refresh_run_history(source)
        self._display_session(session)
        self._start_full_stage("gencase")

    def _locate_full_run_tools(
        self,
        source: Path,
        backend: str,
    ) -> dict[str, tuple[PipelineAdapter, Any]] | None:
        adapter_types: tuple[type[PipelineAdapter], ...] = (
            GenCaseAdapter,
            DualSPHysicsGPUAdapter if backend == "gpu" else DualSPHysicsCPUAdapter,
            PartVTKAdapter,
        )

        def locate() -> dict[str, tuple[PipelineAdapter, Any]]:
            found: dict[str, tuple[PipelineAdapter, Any]] = {}
            for adapter_type in adapter_types:
                adapter = adapter_type(self.settings.gencase_root or None)
                info = adapter.probe(source)
                found[adapter.stage] = (adapter, info)
            return found

        found = locate()
        if all(info.ready for _adapter, info in found.values()):
            return found
        details = "\n".join(
            message
            for _adapter, info in found.values()
            for message in info.diagnostics
        )
        QMessageBox.information(
            self.window,
            "完整运行需要核验本地工具",
            (details or "GenCase、求解器或 PartVTK 尚未配置")
            + "\n\n请选择并核验 DualSPHysics v5.4 根目录。",
        )
        if not self.configure_dualsphysics(source=source):
            return None
        found = locate()
        if not all(info.ready for _adapter, info in found.values()):
            QMessageBox.critical(
                self.window,
                "完整运行工具不可用",
                "\n".join(
                    message
                    for _adapter, info in found.values()
                    for message in info.diagnostics
                )
                or "至少一个完整流程工具不可用。",
            )
            return None
        return found

    def _start_full_stage(self, stage: str) -> None:
        session = self._run_session
        source = self._source_path()
        case_prefix = self._case_output_prefix
        located = self._full_run_adapters.get(stage)
        if (
            not self._full_run_active
            or session is None
            or source is None
            or case_prefix is None
            or located is None
        ):
            return
        adapter, tool_info = located
        if stage == "gencase":
            request = RunRequest(
                source_xml=source,
                output_prefix=case_prefix,
                mode="full",
                create_dirs=True,
                use_timestamp_on_conflict=False,
            )
            message = "完整运行：正在生成初始模型…"
        elif stage in {"cpu", "gpu"}:
            request = RunRequest(
                source_xml=source,
                output_prefix=case_prefix,
                mode=stage,
                create_dirs=False,
                use_timestamp_on_conflict=False,
            )
            message = f"完整运行：正在执行 {stage.upper()} 求解…"
        else:
            request = RunRequest(
                source_xml=source,
                output_prefix=case_prefix.parent / "vtk" / "PartAll",
                mode="partvtk",
                create_dirs=True,
                use_timestamp_on_conflict=False,
            )
            message = "完整运行：正在生成 PartVTK…"
        session.start_stage(stage)
        self.window.update_run_stage(stage, "running")
        self._launch_pipeline(
            adapter,
            request,
            tool_info,
            message,
            preserve_log=True,
        )

    def _prepare_manual_session(
        self,
        *,
        source: Path,
        output_prefix: Path,
        stage: str,
        tool_info: Any,
    ) -> bool:
        record_path = output_prefix.parent / ".dss-run" / "run.json"
        self.window.set_run_state(True, "正在准备可复现记录…")
        try:
            if record_path.is_file():
                session = RunSession.load(record_path)
                if session.source_sha256 != file_sha256(source):
                    raise ValueError(
                        "现有输出目录的运行记录属于不同版本的 XML；"
                        "为保护可复现性，已阻止混用"
                    )
                if session.status == "running":
                    session.mark_interrupted()
                session.add_tool(stage, tool_info)
                if stage in {"cpu", "gpu"}:
                    session.backend = stage
                    session.write()
            else:
                backend = stage if stage in {"cpu", "gpu"} else "manual"
                session = RunSession.create(
                    source_xml=source,
                    output_root=output_prefix.parent,
                    backend=backend,
                    workflow=(stage,),
                    tools={stage: tool_info},
                )
            estimate = estimate_case(
                self.window.document,
                output_directory=output_prefix.parent,
                source_sha256=session.source_sha256,
                backend=session.backend,
                history=session_history_values(find_case_sessions(source)),
            )
            session.add_estimate(estimate)
        except (OSError, ValueError) as error:
            QMessageBox.critical(
                self.window,
                "无法准备可复现记录",
                f"阶段尚未启动。\n\n{error}",
            )
            self.window.set_run_state(False, "运行记录准备失败")
            return False
        self._run_session = session
        self._selected_session = session
        self._manual_session_active = True
        self._full_run_active = False
        self._run_estimate = estimate
        if stage == "gencase":
            self._gencase_summary = {}
        self.window.begin_run_session(session.session_id, session.backend)
        self.window.update_resource_estimate(estimate)
        self.window.set_run_record_available(True)
        session.start_stage(stage)
        self.window.update_run_stage(stage, "running")
        return True

    def _source_path(self) -> Path | None:
        document = self.window.document
        path = self.window.document_path
        if document is None or path is None:
            self.window.set_run_state(False, "请先打开并保存 Case_Def.xml")
            return None
        return Path(path).resolve()

    def _prepare_source(self) -> Path | None:
        source = self._source_path()
        if source is None:
            return None
        document = self.window.document
        assert document is not None
        dirty = bool(getattr(document, "dirty", False)) or self.window.source_editor.document().isModified()
        if dirty:
            answer = QMessageBox.question(
                self.window,
                "运行前保存",
                "外部工具读取磁盘上的 XML。是否先显式保存当前修改并创建备份？",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer != QMessageBox.StandardButton.Save or not self.window.save_document():
                self.window.set_run_state(False, "已取消；XML 未保存")
                return None
        return source

    def _choose_ready_adapter(
        self,
        source: Path,
        adapter_type: type[PipelineAdapter] = GenCaseAdapter,
    ) -> tuple[PipelineAdapter, Any] | None:
        adapter = adapter_type(self.settings.gencase_root or None)
        info = adapter.probe(source)
        if info.ready:
            return adapter, info

        details = "\n".join(info.diagnostics) or "未找到已配置的 DualSPHysics v5.4。"
        QMessageBox.information(
            self.window,
            "选择 DualSPHysics v5.4",
            f"{details}\n\n请选择包含 bin\\windows 的 DualSPHysics v5.4 根目录。",
        )
        if not self.configure_dualsphysics(source=source):
            return None
        adapter = adapter_type(self.settings.gencase_root)
        info = adapter.probe(source)
        if not info.ready:
            QMessageBox.critical(
                self.window,
                f"{info.name} 不可用",
                "\n".join(info.diagnostics)
                or f"所选目录不包含可用的 {info.name} v5.4。",
            )
            return None
        return adapter, info

    @Slot()
    def configure_dualsphysics(self, *, source: Path | None = None) -> bool:
        """Select one official v5.4 root and validate every supported tool."""

        if source is None and self.window.document_path is not None:
            source = Path(self.window.document_path).resolve()
        initial = self.settings.gencase_root
        if not initial:
            initial = str(source.parent if source is not None else Path.home())
        selected = QFileDialog.getExistingDirectory(
            self.window,
            "选择 DualSPHysics v5.4 根目录",
            initial,
            QFileDialog.Option.ShowDirsOnly,
        )
        if not selected:
            return False

        selected_root = Path(selected)
        tool_infos = tuple(
            adapter_type(selected_root).probe(source)
            for adapter_type in _DUALSPHYSICS_ADAPTER_TYPES
        )
        report_lines: list[str] = []
        for index, info in enumerate(tool_infos):
            state = "通过" if info.ready else "失败"
            version = f"，版本 {info.version}" if info.version else ""
            executable = str(info.executable) if info.executable else "未找到"
            report_lines.append(f"[{state}] {info.name}{version}\n    {executable}")
            if index == 0:
                config_path = info.config_path
                config_ready = bool(config_path and config_path.is_file())
                config_state = "通过" if config_ready else "失败"
                report_lines.append(
                    f"[{config_state}] DsphConfig.xml\n"
                    f"    {config_path or '未找到'}"
                )
            if info.diagnostics:
                report_lines.extend(f"    {message}" for message in info.diagnostics)
        report = "\n".join(report_lines)

        if not all(info.ready for info in tool_infos):
            QMessageBox.critical(
                self.window,
                "DualSPHysics 路径核验失败",
                "所选目录未通过完整核验，路径未保存。\n\n" + report,
            )
            return False

        root = Path(tool_infos[0].root or selected_root).resolve()
        previous_root = self.settings.gencase_root
        self.settings.gencase_root = str(root)
        try:
            self.settings.save()
        except OSError as error:
            self.settings.gencase_root = previous_root
            QMessageBox.critical(
                self.window,
                "无法保存 DualSPHysics 路径",
                f"设置文件写入失败，原设置保持不变。\n\n{error}",
            )
            return False

        self.window.status_message.setText(f"DualSPHysics 路径：{root}")
        QMessageBox.information(
            self.window,
            "DualSPHysics 路径已保存",
            f"根目录：{root}\n\n{report}",
        )
        return True

    def _case_prefix_for_source(self, source: Path) -> Path | None:
        active_prefix = (
            self._case_output_prefix
            if self._case_output_source == source.resolve()
            else None
        )
        candidates = [
            active_prefix,
            GenCaseAdapter.default_output_prefix(source).resolve(),
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            prefix = Path(candidate).resolve()
            if (
                prefix.with_suffix(".xml").is_file()
                or prefix.with_suffix(".bi4").is_file()
                or (prefix.parent / "data").is_dir()
            ):
                return prefix
        return None

    def _launch_pipeline(
        self,
        adapter: PipelineAdapter,
        request: RunRequest,
        tool_info: Any,
        message: str,
        *,
        preserve_log: bool = False,
    ) -> None:
        if not preserve_log:
            self.window.log_view.clear()
        self.window.set_run_state(True, message)
        output = request.output_prefix or "自动选择"
        self.window.append_log(
            f"{tool_info.name} {tool_info.version or '未知版本'}\n"
            f"源文件：{request.source_xml}\n"
            f"源 SHA-256：{file_sha256(request.source_xml)}\n"
            f"输出：{output}"
        )

        self._active_adapter = adapter
        self._active_stage = adapter.stage
        self._active_stage_started_monotonic = time.monotonic()
        self._cancel_event = Event()
        thread = QThread(self)
        worker = _PipelineWorker(adapter, request, self._cancel_event)
        worker.moveToThread(thread)
        thread.started.connect(worker.execute)
        worker.log.connect(self._append_run_log)
        worker.finished.connect(self._run_finished)
        worker.failed.connect(self._run_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._run_thread_finished)
        self._run_thread = thread
        self._run_worker = worker
        thread.start()

    @Slot(str)
    def _start_run(self, mode: str) -> None:
        if self._run_thread is not None:
            return
        source = self._prepare_source()
        if source is None:
            return
        located = self._choose_ready_adapter(source)
        if located is None:
            self.window.set_run_state(False, "GenCase 未配置")
            return
        adapter, tool_info = located

        output_prefix: Path | None = None
        if mode == "quick":
            digest = file_sha256(source)[:16]
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            output_prefix = cache_dir() / "quick" / digest / stamp / "Case"
        else:
            output_prefix = GenCaseAdapter.suggest_output_prefix(source)
        request = RunRequest(
            source_xml=source,
            output_prefix=output_prefix,
            mode=mode,
            create_dirs=True,
            use_timestamp_on_conflict=True,
        )
        if mode == "full":
            assert output_prefix is not None
            if not self._prepare_manual_session(
                source=source,
                output_prefix=output_prefix,
                stage="gencase",
                tool_info=tool_info,
            ):
                return

        self._launch_pipeline(
            adapter,
            request,
            tool_info,
            "正在快速检查…" if mode == "quick" else "正在生成初始模型…",
        )

    def _start_solver(
        self,
        mode: str,
        adapter_type: type[PipelineAdapter],
        stage_label: str,
    ) -> None:
        if self._run_thread is not None:
            return
        source = self._prepare_source()
        if source is None:
            return
        manifest = getattr(self._last_gencase_run, "manifest", None)
        if manifest is not None and manifest.is_stale(source):
            self.window.set_run_state(False, "XML 已修改，请重新生成初始模型")
            QMessageBox.warning(
                self.window,
                "初始模型已过期",
                "当前 XML 与最近一次 GenCase 结果不一致，请先重新生成初始模型。",
            )
            return
        case_prefix = self._case_prefix_for_source(source)
        if case_prefix is None:
            self.window.set_run_state(False, "未找到 Case.xml 和 Case.bi4")
            QMessageBox.warning(
                self.window,
                "缺少初始模型",
                "请先运行“生成初始模型”，确保 Case.xml 和 Case.bi4 已生成。",
            )
            return
        located = self._choose_ready_adapter(source, adapter_type)
        if located is None:
            self.window.set_run_state(False, f"DualSPHysics {stage_label} 未配置")
            return
        adapter, tool_info = located
        request = RunRequest(
            source_xml=source,
            output_prefix=case_prefix,
            mode=mode,
            create_dirs=False,
            use_timestamp_on_conflict=False,
        )
        self._case_output_prefix = case_prefix
        self._case_output_source = source.resolve()
        if not self._prepare_manual_session(
            source=source,
            output_prefix=case_prefix,
            stage=mode,
            tool_info=tool_info,
        ):
            return
        self._launch_pipeline(
            adapter,
            request,
            tool_info,
            f"正在进行 {stage_label} 求解…",
        )

    @Slot()
    def _start_cpu(self) -> None:
        self._start_solver("cpu", DualSPHysicsCPUAdapter, "CPU")

    @Slot()
    def _start_gpu(self) -> None:
        self._start_solver("gpu", DualSPHysicsGPUAdapter, "GPU")

    @Slot()
    def _start_partvtk(self) -> None:
        if self._run_thread is not None:
            return
        source = self._source_path()
        if source is None:
            return
        case_prefix = self._case_prefix_for_source(source)
        if case_prefix is None:
            self.window.set_run_state(False, "未找到 Case_out/data")
            QMessageBox.warning(
                self.window,
                "缺少求解数据",
                "请先完成 CPU 或 GPU 求解，确保 Case_out/data 中存在 Part_????.bi4。",
            )
            return
        located = self._choose_ready_adapter(source, PartVTKAdapter)
        if located is None:
            self.window.set_run_state(False, "PartVTK 未配置")
            return
        adapter, tool_info = located
        output_prefix = case_prefix.parent / "vtk" / "PartAll"
        request = RunRequest(
            source_xml=source,
            output_prefix=output_prefix,
            mode="partvtk",
            create_dirs=True,
            use_timestamp_on_conflict=False,
        )
        self._case_output_prefix = case_prefix
        self._case_output_source = source.resolve()
        if not self._prepare_manual_session(
            source=source,
            output_prefix=case_prefix,
            stage="partvtk",
            tool_info=tool_info,
        ):
            return
        self._launch_pipeline(adapter, request, tool_info, "正在转换 PartVTK…")

    @Slot(str, str)
    def _append_run_log(self, stream: str, line: str) -> None:
        self.window.append_log(line, stream=stream)
        session = self._run_session
        stage = self._active_stage
        if session is not None and stage is not None:
            try:
                session.append_log(stage, stream, line)
            except OSError as error:
                self.window.append_log(
                    f"警告：无法写入会话日志：{error}",
                    stream="stderr",
                )
            expected = (
                self._run_estimate.output_frames
                if self._run_estimate is not None
                else None
            )
            progress = live_stage_progress(
                stage,
                line,
                expected_frames=expected,
            )
            if progress is not None:
                completed, total = progress
                eta_seconds = None
                if (
                    completed > 0
                    and self._active_stage_started_monotonic is not None
                    and completed < total
                ):
                    elapsed = time.monotonic() - self._active_stage_started_monotonic
                    eta_seconds = elapsed * (total - completed) / completed
                self.window.update_stage_progress(
                    stage,
                    completed,
                    total,
                    eta_seconds=eta_seconds,
                )

    @Slot(object)
    def _run_finished(self, result: Any) -> None:
        self._last_run = result
        manifest = getattr(result, "manifest", None)
        stage = getattr(manifest, "stage", None) or self._active_stage
        parameters = getattr(manifest, "parameters", {}) if manifest is not None else {}
        if (
            stage == "gencase"
            and getattr(result, "success", False)
            and parameters.get("mode") == "full"
        ):
            self._last_gencase_run = result
            self._case_output_prefix = Path(result.output_prefix).resolve()
            self._case_output_source = Path(manifest.source_path).resolve()

        workflow_continues = False
        session = self._run_session
        if (
            self._full_run_active
            and session is not None
            and stage in session.workflow
        ):
            session.finish_stage(str(stage), result)
            self.window.update_run_stage(
                str(stage),
                str(getattr(result, "status", "unknown")),
                (
                    f"退出码 {result.return_code}"
                    if result.return_code is not None
                    else ""
                ),
            )
            if getattr(result, "success", False):
                if stage == "gencase":
                    self._gencase_summary = dict(getattr(result, "summary", {}) or {})
                    self._refresh_session_estimate(self._gencase_summary)
                    self._pending_full_stage = self._full_run_backend
                elif stage in {"cpu", "gpu"}:
                    combined = dict(self._gencase_summary)
                    combined.update(dict(getattr(result, "summary", {}) or {}))
                    self._refresh_session_estimate(combined)
                    self._pending_full_stage = "partvtk"
                else:
                    session.finalize("succeeded")
                    self._selected_session = session
                    self._full_run_active = False
                    self._pending_full_stage = None
                workflow_continues = self._pending_full_stage is not None
            else:
                status = str(getattr(result, "status", "failed"))
                session.skip_remaining(
                    after=str(stage),
                    reason=f"{stage} 未成功，后续阶段未执行",
                )
                session.finalize(status)
                for remaining in session.workflow:
                    entry = session.stages.get(remaining, {})
                    if entry.get("status") == "skipped":
                        self.window.update_run_stage(
                            remaining,
                            "skipped",
                            f"因 {stage} 未成功而跳过",
                        )
                self._selected_session = session
                self._full_run_active = False
                self._pending_full_stage = None
        elif (
            self._manual_session_active
            and session is not None
            and stage is not None
        ):
            session.finish_stage(str(stage), result)
            if stage == "gencase" and getattr(result, "success", False):
                self._gencase_summary = dict(getattr(result, "summary", {}) or {})
                self._refresh_session_estimate(self._gencase_summary)
            elif stage in {"cpu", "gpu"}:
                combined = dict(self._gencase_summary)
                combined.update(dict(getattr(result, "summary", {}) or {}))
                self._refresh_session_estimate(combined)
            session.finalize(str(getattr(result, "status", "failed")))
            self._selected_session = session
            self.window.update_run_stage(
                str(stage),
                str(getattr(result, "status", "unknown")),
                (
                    f"退出码 {result.return_code}"
                    if result.return_code is not None
                    else ""
                ),
            )
            self._manual_session_active = False
        ui_result = copy(result)
        # Output has already streamed live; avoid appending it a second time.
        ui_result.stdout = ""
        ui_result.stderr = ""
        self.window.finish_run(
            ui_result,
            stage=stage,
            workflow_continues=workflow_continues,
        )
        for warning in getattr(result, "warnings", ()):
            self.window.append_log(f"警告：{warning}")
        for error in getattr(result, "errors", ()):
            self.window.append_log(f"错误：{error}", stream="stderr")
        manifest_path = getattr(result, "manifest_path", None)
        if manifest_path:
            self.window.append_log(f"运行清单：{manifest_path}")
        self.window.append_log(f"输出前缀：{getattr(result, 'output_prefix', '')}")
        if session is not None:
            self.window.append_log(f"会话记录：{session.record_path}")
            self.window.set_run_record_available(True)

    @Slot(str)
    def _run_failed(self, message: str) -> None:
        self.window.append_log(message, stream="stderr")
        stage_key = self._active_stage or "unknown"
        stage = _STAGE_LABELS.get(stage_key, "当前任务")
        session = self._run_session
        if self._full_run_active and session is not None:
            session.fail_stage(stage_key, message)
            session.skip_remaining(
                after=stage_key,
                reason=f"{stage}异常，后续阶段未执行",
            )
            session.finalize("failed")
            self._selected_session = session
            self._full_run_active = False
            self._pending_full_stage = None
            self.window.update_run_stage(stage_key, "failed", message)
            for remaining in session.workflow:
                if session.stages.get(remaining, {}).get("status") == "skipped":
                    self.window.update_run_stage(remaining, "skipped", "前序阶段异常")
            self.window.set_run_record_available(True)
        elif self._manual_session_active and session is not None:
            session.fail_stage(stage_key, message)
            session.finalize("failed")
            self._selected_session = session
            self._manual_session_active = False
            self.window.update_run_stage(stage_key, "failed", message)
            self.window.set_run_record_available(True)
        self.window.set_run_state(False, f"{stage}失败")
        QMessageBox.critical(self.window, f"{stage}失败", message)

    @Slot()
    def _run_thread_finished(self) -> None:
        if self._run_thread is not None:
            self._run_thread.deleteLater()
        self._run_thread = None
        self._run_worker = None
        self._active_adapter = None
        self._active_stage = None
        self._cancel_event = None
        self._active_stage_started_monotonic = None
        next_stage = self._pending_full_stage
        self._pending_full_stage = None
        if next_stage is not None and self._full_run_active:
            QTimer.singleShot(0, lambda stage=next_stage: self._start_full_stage(stage))
            return
        if not self._full_run_active:
            self._run_session = None
            self._full_run_backend = None
            self._full_run_adapters = {}
            self._manual_session_active = False

    def _refresh_session_estimate(self, summary: dict[str, Any]) -> None:
        session = self._run_session
        document = self.window.document
        if session is None or document is None:
            return
        source = Path(self.window.document_path).resolve()
        estimate = estimate_case(
            document,
            output_directory=session.output_root,
            particle_summary=summary,
            source_sha256=session.source_sha256,
            backend=session.backend,
            history=session_history_values(find_case_sessions(source)),
        )
        session.add_estimate(estimate)
        self._run_estimate = estimate
        self.window.update_resource_estimate(estimate)

    @Slot()
    def cancel_run(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        if self._active_adapter is not None:
            self._active_adapter.cancel()
        stage = _STAGE_LABELS.get(self._active_stage or "", "当前任务")
        self.window.status_message.setText(f"正在取消{stage}…")

    @Slot()
    def _open_run_output(self) -> None:
        session = self._selected_session or self._run_session
        if session is None or not session.output_root.is_dir():
            QMessageBox.warning(self.window, "输出目录不可用", "当前没有可打开的运行输出目录。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(session.output_root))):
            QMessageBox.warning(
                self.window,
                "无法打开输出目录",
                str(session.output_root),
            )

    @Slot()
    def _export_run_record(self) -> None:
        session = self._selected_session or self._run_session
        if session is None:
            QMessageBox.warning(self.window, "没有运行记录", "请先选择一个运行会话。")
            return
        privacy = _DiagnosticExportDialog(self.window)
        if privacy.exec() != QDialog.DialogCode.Accepted:
            return
        source_stem = Path(session.source_name).stem
        default_name = (
            session.output_root
            / f"{source_stem}-{session.session_id}-diagnostic.zip"
        )
        selected, _filter = QFileDialog.getSaveFileName(
            self.window,
            "导出可复现记录",
            str(default_name),
            "ZIP 压缩包 (*.zip)",
        )
        if not selected:
            return
        target = Path(selected)
        if target.suffix.casefold() != ".zip":
            target = target.with_suffix(".zip")
        source_path = (
            Path(self.window.document_path).resolve()
            if self.window.document_path is not None
            else session.source_snapshot
        )
        try:
            exported = session.export_diagnostic(
                target,
                include_xml=privacy.include_xml,
                redaction_paths=(
                    source_path.parent,
                    session.output_root,
                    self.settings.gencase_root,
                ),
            )
        except OSError as error:
            QMessageBox.critical(self.window, "导出失败", str(error))
            return
        QMessageBox.information(
            self.window,
            "导出完成",
            f"记录已导出：\n{exported}\n\n"
            f"XML：{'已包含' if privacy.include_xml else '已按隐私选项排除'}",
        )

    @Slot()
    def _rerun_from_record(self) -> None:
        session = self._selected_session
        if session is None:
            QMessageBox.warning(self.window, "没有运行记录", "请先选择一个运行会话。")
            return
        source = self._source_path()
        if source is None:
            return
        if file_sha256(source) != session.source_sha256:
            answer = QMessageBox.warning(
                self.window,
                "当前 XML 与记录不同",
                "当前 XML 的 SHA-256 与所选记录不一致。新运行会使用当前 XML，"
                "不会覆盖旧结果。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._start_full_run(session.backend)

    @Slot()
    def _mark_stale(self) -> None:
        manifest = getattr(self._last_gencase_run or self._last_run, "manifest", None)
        document = self.window.document
        if document is None:
            return
        current = getattr(document, "source_sha256", "")
        if self.window.document_path is not None:
            source = Path(self.window.document_path).resolve()
            estimate = estimate_case(
                document,
                output_directory=GenCaseAdapter.default_output_prefix(source).parent,
                source_sha256=current or None,
                backend=self.settings.last_solver_backend,
                history=session_history_values(find_case_sessions(source)),
            )
            self._run_estimate = estimate
            self.window.update_resource_estimate(estimate)
        if manifest is None:
            return
        if current and current != manifest.source_sha256:
            self.window.run_state_label.setText("当前初始模型结果已过期")

    @Slot()
    def configure_ai(self) -> None:
        existing = self.secret_store.get()
        dialog = _AISettingsDialog(
            self.window,
            model=self.window.ai_model_edit.text().strip() or self.settings.openai_model,
            has_key=bool(existing),
        )
        deleted = {"value": False}

        def delete_key() -> None:
            self.secret_store.delete()
            deleted["value"] = True
            dialog.delete_button.setEnabled(False)
            dialog.key_edit.clear()
            dialog.key_edit.setPlaceholderText("sk-…")

        dialog.delete_button.clicked.connect(delete_key)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        model = dialog.model_edit.text().strip() or "gpt-5.6-terra"
        key = dialog.key_edit.text().strip()
        try:
            if key:
                self.secret_store.set(key)
        except Exception as exc:
            QMessageBox.critical(self.window, "无法保存密钥", str(exc))
            return
        self.window.ai_model_edit.setText(model)
        self.settings.openai_model = model
        try:
            self.settings.save()
        except OSError:
            pass
        status = "密钥已删除" if deleted["value"] and not key else "OpenAI 配置已保存"
        self.window.status_message.setText(status)

    @Slot(str, str)
    def _start_ai(self, xml_text: str, model: str) -> None:
        if self._ai_thread is not None:
            return
        if not self.secret_store.get():
            QMessageBox.warning(
                self.window,
                "尚未配置密钥",
                "请先将 OpenAI API 密钥保存到 Windows Credential Manager。",
            )
            self.window.ai_result_view.setPlainText("AI 解读未启动。")
            return
        selected = getattr(self.window, "_selected_entry", None)
        selected_source = getattr(selected, "source", None)
        selector = str(getattr(selected_source, "element_path", "/case"))
        version = "v5.4"
        if self.settings.gencase_root:
            info = GenCaseAdapter(self.settings.gencase_root).probe(self.window.document_path)
            if info.version:
                version = info.version
        request = ExplainRequest(
            xml_text=xml_text,
            selected_selector=selector,
            gencase_version=version,
            offline_diagnostics=self.window.diagnostics_view.toPlainText(),
            log_excerpt=self.window.log_view.toPlainText(),
        )
        provider = OpenAIProvider(model=model, secret_store=self.secret_store)
        thread = QThread(self)
        worker = _AIWorker(provider, request)
        worker.moveToThread(thread)
        thread.started.connect(worker.execute)
        worker.finished.connect(self._ai_finished)
        worker.failed.connect(self._ai_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._ai_thread_finished)
        self._ai_thread = thread
        self._ai_worker = worker
        self.window.ai_send_button.setEnabled(False)
        thread.start()

    @Slot(object)
    def _ai_finished(self, result: Any) -> None:
        formatted = format_ai_result(result)
        self.window.set_ai_result(formatted)
        suggestions = list(getattr(result, "suggested_edits", ()) or ())
        if not suggestions:
            return
        diff = "\n".join(
            f"--- {item.selector}\n+++ {item.selector}\n"
            f'- {item.attribute}="{item.old_value}"\n'
            f'+ {item.attribute}="{item.new_value}"\n  {item.reason}'
            for item in suggestions
        )
        dialog = QMessageBox(self.window)
        dialog.setWindowTitle("审阅 AI 修改建议")
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setText(
            f"AI 提供了 {len(suggestions)} 项属性修改建议。是否将它们应用到编辑器？\n"
            "应用后仍需由你显式保存。"
        )
        dialog.setDetailedText(diff)
        dialog.setStandardButtons(
            QMessageBox.StandardButton.Apply | QMessageBox.StandardButton.Cancel
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if dialog.exec() == QMessageBox.StandardButton.Apply:
            self._apply_ai_suggestions(suggestions)

    def _apply_ai_suggestions(self, suggestions: list[Any]) -> None:
        document = self.window.document
        if document is None or not hasattr(document, "update_attribute"):
            QMessageBox.warning(self.window, "无法应用", "当前文档不支持安全属性修改。")
            return
        # Validate every precondition before making the first source patch.
        try:
            for item in suggestions:
                element = document.element(str(item.selector))
                actual = element.get(str(item.attribute))
                if (actual or "") != str(item.old_value):
                    raise ValueError(
                        f"{item.selector}/@{item.attribute} 当前值已变化（{actual!r}），建议未应用"
                    )
        except Exception as exc:
            QMessageBox.warning(self.window, "建议已失效", str(exc))
            return

        applied = 0
        try:
            for item in suggestions:
                document.update_attribute(
                    str(item.selector),
                    str(item.attribute),
                    str(item.new_value),
                    description=f"AI 建议：{item.reason}",
                )
                applied += 1
        except Exception as exc:
            for _ in range(applied):
                document.undo()
            QMessageBox.critical(self.window, "无法应用建议", str(exc))
            return
        self.window._dirty = True
        self.window._update_source_from_document()
        self.window.refresh_document()
        self.window._update_title()
        self.window.status_message.setText("AI 建议已进入编辑器，尚未保存")
        self._mark_stale()

    @Slot(str)
    def _ai_failed(self, message: str) -> None:
        self.window.set_ai_result(f"AI 解读失败\n\n{message}\n\n离线功能不受影响。")

    @Slot()
    def _ai_thread_finished(self) -> None:
        if self._ai_thread is not None:
            self._ai_thread.deleteLater()
        self._ai_thread = None
        self._ai_worker = None
        self.window.ai_send_button.setEnabled(self.window.ai_consent_check.isChecked())


def run_application(
    argv: list[str] | None = None,
    *,
    initial_path: str | Path | None = None,
) -> int:
    application = QApplication(argv or [])
    application.setApplicationName(APP_NAME)
    application.setApplicationVersion(APP_VERSION)
    application.setOrganizationName("DualSPHysics Studio")
    controller = StudioController(application, initial_path=initial_path)
    # Keep the controller alive for the complete Qt event loop.
    application._studio_controller = controller  # type: ignore[attr-defined]
    controller.show()
    return application.exec()


__all__ = ["StudioController", "format_ai_result", "run_application"]
