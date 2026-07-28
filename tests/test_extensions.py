from __future__ import annotations

from pathlib import Path
import sys

import pytest

import dualsphysics_studio.extensions as extensions_module
from dualsphysics_studio.extensions import (
    ExtensionTool,
    ExtensionToolError,
    default_dlc_directory,
    discover_extension_tools,
    launch_extension_tool,
)


def _fake_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ fake Windows executable")
    return path


def test_default_dlc_directory_uses_project_root_when_running_from_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert default_dlc_directory() == (
        Path(extensions_module.__file__).resolve().parents[2] / "DLC"
    )


def test_default_dlc_directory_is_beside_frozen_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "portable folder" / "DualSPHysicsStudio.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    assert default_dlc_directory() == executable.parent / "DLC"


def test_discovery_finds_root_and_one_level_executables_but_not_deeper(
    tmp_path: Path,
) -> None:
    root_tool = _fake_executable(tmp_path / "Root Mesh Tool.exe")
    child_tool = _fake_executable(tmp_path / "vendor" / "Child Mesh Tool.EXE")
    deep_tool = _fake_executable(
        tmp_path / "vendor" / "nested" / "Deep Mesh Tool.exe"
    )
    (tmp_path / "not-a-tool.txt").write_text("ignored", encoding="utf-8")
    (tmp_path / "directory.exe").mkdir()

    tools = discover_extension_tools(tmp_path)
    paths = {tool.path.resolve() for tool in tools}

    assert all(isinstance(tool, ExtensionTool) for tool in tools)
    assert paths == {root_tool.resolve(), child_tool.resolve()}
    assert deep_tool.resolve() not in paths


def test_discovery_returns_empty_for_a_missing_dlc_directory(tmp_path: Path) -> None:
    assert not discover_extension_tools(tmp_path / "missing")


def test_duplicate_executable_names_receive_unique_labels(tmp_path: Path) -> None:
    first = _fake_executable(tmp_path / "vendor-a" / "Mesh-Tool.exe")
    second = _fake_executable(tmp_path / "vendor-b" / "Mesh-Tool.exe")

    tools = discover_extension_tools(tmp_path)

    assert {tool.path.resolve() for tool in tools} == {first.resolve(), second.resolve()}
    assert len({tool.label.casefold() for tool in tools}) == 2
    assert all(tool.label.strip() for tool in tools)


def test_launch_extension_uses_detached_program_argument_list_and_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_executable(
        tmp_path / "中文 & extension folder" / "Mesh Tool.exe"
    ).resolve()
    calls: list[tuple[str, list[str], str]] = []

    class FakeQProcess:
        @staticmethod
        def startDetached(program: str, arguments: list[str], cwd: str):
            calls.append((program, arguments, cwd))
            return True, 4321

    monkeypatch.setattr(extensions_module, "QProcess", FakeQProcess)

    assert launch_extension_tool(executable) is None
    assert calls == [(str(executable), [], str(executable.parent))]


def test_launch_extension_raises_when_qprocess_fails_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_executable(tmp_path / "Mesh-Tool.exe").resolve()
    calls: list[tuple[str, list[str], str]] = []

    class FailingQProcess:
        @staticmethod
        def startDetached(program: str, arguments: list[str], cwd: str):
            calls.append((program, arguments, cwd))
            return False, -1

    monkeypatch.setattr(extensions_module, "QProcess", FailingQProcess)

    with pytest.raises(ExtensionToolError):
        launch_extension_tool(executable)

    assert calls == [(str(executable), [], str(executable.parent))]


def test_launch_extension_rejects_missing_executable_before_qprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, list[str], str]] = []

    class UnexpectedQProcess:
        @staticmethod
        def startDetached(program: str, arguments: list[str], cwd: str):
            calls.append((program, arguments, cwd))
            return True, 1

    monkeypatch.setattr(extensions_module, "QProcess", UnexpectedQProcess)

    with pytest.raises(ExtensionToolError):
        launch_extension_tool(tmp_path / "missing.exe")

    assert calls == []
