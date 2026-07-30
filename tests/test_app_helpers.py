from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import dualsphysics_studio.app as app_module
from dualsphysics_studio.ai.models import (
    EditSuggestion,
    ExplainResult,
    NodeExplanation,
    RiskItem,
)
from dualsphysics_studio.app import StudioController, format_ai_result
from dualsphysics_studio.pipeline import RunResult, RunSession, ToolInfo
from dualsphysics_studio.resources import resource_path


def test_ai_result_contains_reviewable_attribute_diff() -> None:
    result = ExplainResult(
        summary="案例可以运行，但时间步偏激进。",
        node_explanations=[
            NodeExplanation(
                selector="/case/execution[1]",
                title="执行参数",
                explanation="控制求解阶段。",
            )
        ],
        risks=[
            RiskItem(
                severity="warning",
                selector="/case/execution[1]/parameters[1]/parameter[1]",
                message="需结合声速检查。",
            )
        ],
        suggested_edits=[
            EditSuggestion(
                selector="/case/execution[1]/parameters[1]/parameter[1]",
                attribute="value",
                old_value="0.2",
                new_value="0.1",
                reason="降低风险",
            )
        ],
    )

    text = format_ai_result(result)

    assert "案例可以运行" in text
    assert "--- /case/execution[1]/parameters[1]/parameter[1]" in text
    assert '- value="0.2"' in text
    assert '+ value="0.1"' in text
    assert "尚未应用" in text


def test_packaged_resources_resolve_to_files() -> None:
    assert resource_path("semantic_catalog.json").is_file()
    assert resource_path("theme.qss").is_file()
    assert resource_path("app_icon.svg").is_file()


def test_case_output_prefix_does_not_follow_save_as_to_another_source(tmp_path) -> None:
    original_source = tmp_path / "original" / "Case_Def.xml"
    new_source = tmp_path / "copy" / "Case_Def.xml"
    original_source.parent.mkdir()
    new_source.parent.mkdir()
    original_source.write_text("<case/>", encoding="utf-8")
    new_source.write_text("<case/>", encoding="utf-8")
    original_prefix = original_source.parent / "Case_out" / "Case"
    original_prefix.parent.mkdir()
    original_prefix.with_suffix(".xml").write_text("<case/>", encoding="utf-8")

    controller = StudioController.__new__(StudioController)
    controller._case_output_prefix = original_prefix
    controller._case_output_source = original_source.resolve()

    assert controller._case_prefix_for_source(new_source.resolve()) is None


class _FakeSettings:
    def __init__(self) -> None:
        self.gencase_root = ""
        self.save_count = 0

    def save(self) -> None:
        self.save_count += 1


class _FakeStatusLabel:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:  # noqa: N802 - Qt-compatible fake
        self.text = text


class _FakeWindow:
    document_path = None

    def __init__(self) -> None:
        self.status_message = _FakeStatusLabel()


def _fake_adapter(
    name: str,
    executable_name: str,
    *,
    config: bool = False,
    ready: bool = True,
):
    class FakeAdapter:
        def __init__(self, root: str | Path) -> None:
            self.root = Path(root).resolve()

        def probe(self, _source=None) -> ToolInfo:
            windows = self.root / "bin" / "windows"
            return ToolInfo(
                name=name,
                root=self.root,
                executable=windows / executable_name,
                config_path=windows / "DsphConfig.xml" if config else None,
                version="5.4.1" if ready else "5.3.0",
                available=True,
                compatible=ready,
                diagnostics=() if ready else ("版本不兼容",),
            )

    return FakeAdapter


def _controller_for_path_configuration() -> StudioController:
    controller = StudioController.__new__(StudioController)
    controller.settings = _FakeSettings()
    controller.window = _FakeWindow()
    return controller


def test_configure_dualsphysics_saves_root_only_after_all_tools_pass(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "DualSPHysics_v5.4"
    windows = root / "bin" / "windows"
    windows.mkdir(parents=True)
    (windows / "DsphConfig.xml").write_text("<config/>", encoding="utf-8")
    controller = _controller_for_path_configuration()
    adapters = (
        _fake_adapter("GenCase", "GenCase_win64.exe", config=True),
        _fake_adapter("DualSPHysics CPU", "DualSPHysics5.4CPU_win64.exe"),
        _fake_adapter("DualSPHysics GPU", "DualSPHysics5.4_win64.exe"),
        _fake_adapter("PartVTK", "PartVTK_win64.exe"),
    )
    messages: list[str] = []
    monkeypatch.setattr(app_module, "_DUALSPHYSICS_ADAPTER_TYPES", adapters)
    monkeypatch.setattr(
        app_module.QFileDialog, "getExistingDirectory", lambda *_args: str(root)
    )
    monkeypatch.setattr(
        app_module.QMessageBox,
        "information",
        lambda _parent, _title, message: messages.append(message),
    )

    assert controller.configure_dualsphysics()
    assert controller.settings.gencase_root == str(root.resolve())
    assert controller.settings.save_count == 1
    assert "DsphConfig.xml" in messages[-1]
    assert messages[-1].count("[通过]") == 5


def test_configure_dualsphysics_rejects_incomplete_or_wrong_version_root(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "DualSPHysics_v5.4"
    windows = root / "bin" / "windows"
    windows.mkdir(parents=True)
    (windows / "DsphConfig.xml").write_text("<config/>", encoding="utf-8")
    controller = _controller_for_path_configuration()
    adapters = (
        _fake_adapter("GenCase", "GenCase_win64.exe", config=True),
        _fake_adapter("DualSPHysics CPU", "DualSPHysics5.4CPU_win64.exe"),
        _fake_adapter("DualSPHysics GPU", "DualSPHysics5.4_win64.exe"),
        _fake_adapter("PartVTK", "PartVTK_win64.exe", ready=False),
    )
    errors: list[str] = []
    monkeypatch.setattr(app_module, "_DUALSPHYSICS_ADAPTER_TYPES", adapters)
    monkeypatch.setattr(
        app_module.QFileDialog, "getExistingDirectory", lambda *_args: str(root)
    )
    monkeypatch.setattr(
        app_module.QMessageBox,
        "critical",
        lambda _parent, _title, message: errors.append(message),
    )

    assert not controller.configure_dualsphysics()
    assert controller.settings.gencase_root == ""
    assert controller.settings.save_count == 0
    assert "[失败] PartVTK" in errors[-1]


class _FakeRunWindow:
    def __init__(self, source: Path) -> None:
        self.document_path = source
        self.document = object()
        self.finished: list[tuple[str | None, bool]] = []
        self.stages: list[tuple[str, str, str]] = []
        self.logs: list[tuple[str, str]] = []
        self.record_available = False

    def finish_run(
        self,
        _result,
        *,
        stage=None,
        workflow_continues=False,
    ) -> None:
        self.finished.append((stage, workflow_continues))

    def update_run_stage(self, stage: str, status: str, detail: str = "") -> None:
        self.stages.append((stage, status, detail))

    def update_resource_estimate(self, _estimate) -> None:
        pass

    def append_log(self, text: str, *, stream: str = "stdout") -> None:
        self.logs.append((stream, text))

    def set_run_record_available(self, available: bool) -> None:
        self.record_available = available


def _run_result(
    source: Path,
    output_prefix: Path,
    stage: str,
    *,
    status: str = "succeeded",
) -> RunResult:
    result = RunResult(
        status=status,
        return_code=0 if status == "succeeded" else 1,
        command=(stage,),
        working_directory=source.parent,
        output_prefix=output_prefix,
        started_at="2026-07-29T00:00:00+00:00",
        finished_at="2026-07-29T00:00:01+00:00",
        summary={},
        cancelled=status == "cancelled",
    )
    result.manifest = SimpleNamespace(
        stage=stage,
        parameters={"mode": "full" if stage == "gencase" else stage},
        source_path=source,
    )
    return result


def test_full_run_state_machine_always_reaches_partvtk(tmp_path) -> None:
    source = tmp_path / "Case_Def.xml"
    source.write_text("<case/>", encoding="utf-8")
    output_prefix = tmp_path / "Case_out" / "Case"
    session = RunSession.create(
        source_xml=source,
        output_root=output_prefix.parent,
        backend="gpu",
        workflow=("gencase", "gpu", "partvtk"),
    )
    controller = StudioController.__new__(StudioController)
    controller.window = _FakeRunWindow(source)
    controller._run_session = session
    controller._selected_session = session
    controller._full_run_active = True
    controller._full_run_backend = "gpu"
    controller._pending_full_stage = None
    controller._active_stage = "gencase"
    controller._last_run = None
    controller._last_gencase_run = None
    controller._case_output_prefix = output_prefix
    controller._case_output_source = source
    controller._gencase_summary = {}
    estimates: list[dict] = []
    controller._refresh_session_estimate = lambda summary: estimates.append(summary)

    controller._run_finished(_run_result(source, output_prefix, "gencase"))
    assert controller._pending_full_stage == "gpu"
    assert controller.window.finished[-1] == ("gencase", True)

    controller._active_stage = "gpu"
    controller._pending_full_stage = None
    controller._run_finished(_run_result(source, output_prefix, "gpu"))
    assert controller._pending_full_stage == "partvtk"
    assert controller.window.finished[-1] == ("gpu", True)

    controller._active_stage = "partvtk"
    controller._pending_full_stage = None
    controller._run_finished(_run_result(source, output_prefix, "partvtk"))
    assert not controller._full_run_active
    assert controller._pending_full_stage is None
    assert controller.window.finished[-1] == ("partvtk", False)
    assert session.status == "succeeded"
    assert [session.stages[stage]["status"] for stage in session.workflow] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert len(estimates) == 2
