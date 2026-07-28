"""Application controller for DualSPHysics Studio 1.0.0.

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
from typing import Any

from PySide6.QtCore import QEvent, QObject, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
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
    RunRequest,
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

        self.window.documentChanged.connect(self._document_changed)
        self.window.quickCheckRequested.connect(lambda: self._start_run("quick"))
        self.window.generateRequested.connect(lambda: self._start_run("full"))
        self.window.cpuRequested.connect(self._start_cpu)
        self.window.gpuRequested.connect(self._start_gpu)
        self.window.partVTKRequested.connect(self._start_partvtk)
        self.window.cancelRequested.connect(self.cancel_run)
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
    ) -> None:
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
        request = RunRequest(
            source_xml=source,
            output_prefix=output_prefix,
            mode=mode,
            create_dirs=True,
            use_timestamp_on_conflict=True,
        )

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
        self._launch_pipeline(adapter, request, tool_info, "正在转换 PartVTK…")

    @Slot(str, str)
    def _append_run_log(self, stream: str, line: str) -> None:
        self.window.append_log(line, stream=stream)

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
        ui_result = copy(result)
        # Output has already streamed live; avoid appending it a second time.
        ui_result.stdout = ""
        ui_result.stderr = ""
        self.window.finish_run(ui_result, stage=stage)
        for warning in getattr(result, "warnings", ()):
            self.window.append_log(f"警告：{warning}")
        for error in getattr(result, "errors", ()):
            self.window.append_log(f"错误：{error}", stream="stderr")
        manifest_path = getattr(result, "manifest_path", None)
        if manifest_path:
            self.window.append_log(f"运行清单：{manifest_path}")
        self.window.append_log(f"输出前缀：{getattr(result, 'output_prefix', '')}")

    @Slot(str)
    def _run_failed(self, message: str) -> None:
        self.window.append_log(message, stream="stderr")
        stage = _STAGE_LABELS.get(self._active_stage or "", "当前任务")
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

    @Slot()
    def cancel_run(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        if self._active_adapter is not None:
            self._active_adapter.cancel()
        stage = _STAGE_LABELS.get(self._active_stage or "", "当前任务")
        self.window.status_message.setText(f"正在取消{stage}…")

    @Slot()
    def _mark_stale(self) -> None:
        manifest = getattr(self._last_gencase_run or self._last_run, "manifest", None)
        document = self.window.document
        if manifest is None or document is None:
            return
        current = getattr(document, "source_sha256", "")
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
