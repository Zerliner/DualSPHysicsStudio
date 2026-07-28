from __future__ import annotations

from pathlib import Path

import dualsphysics_studio.app as app_module
from dualsphysics_studio.ai.models import (
    EditSuggestion,
    ExplainResult,
    NodeExplanation,
    RiskItem,
)
from dualsphysics_studio.app import StudioController, format_ai_result
from dualsphysics_studio.pipeline import ToolInfo
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
