from __future__ import annotations

import json
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


def test_manifest_describes_nested_executable_and_provenance(tmp_path: Path) -> None:
    directory = tmp_path / "Output Auditor"
    executable = _fake_executable(directory / "bin" / "DSSOutputAuditor.exe")
    license_path = directory / "LICENSE.txt"
    license_path.write_text("MIT", encoding="utf-8")
    manifest = directory / "dss-extension.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "DSS Output Auditor",
                "version": "1.0.0",
                "publisher": "DualSPHysics Studio",
                "description": "Read-only output integrity checks.",
                "executable": "bin/DSSOutputAuditor.exe",
                "license": "LICENSE.txt",
                "homepage": "https://github.com/Zerliner/DualSPHysicsStudio",
                "supported_inputs": [".vtk", ".BI4", ".vtk"],
            }
        ),
        encoding="utf-8",
    )

    tools = discover_extension_tools(tmp_path)

    assert len(tools) == 1
    tool = tools[0]
    assert tool.path == executable.resolve()
    assert tool.label == "DSS Output Auditor"
    assert tool.version == "1.0.0"
    assert tool.publisher == "DualSPHysics Studio"
    assert tool.license_path == license_path.resolve()
    assert tool.supported_inputs == (".vtk", ".bi4")
    assert tool.manifest_path == manifest.resolve()
    assert tool.available
    assert not tool.legacy
    assert "Read-only output integrity checks." in tool.detail_text()


def test_invalid_manifest_is_visible_but_cannot_fall_back_to_legacy(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "untrusted"
    _fake_executable(directory / "Tool.exe")
    (directory / "dss-extension.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "Escaping Tool",
                "executable": "../outside.exe",
            }
        ),
        encoding="utf-8",
    )
    _fake_executable(tmp_path / "outside.exe")

    tools = discover_extension_tools(tmp_path)

    invalid = next(tool for tool in tools if tool.manifest_path is not None)
    assert not invalid.available
    assert "不能离开 manifest 目录" in invalid.error
    assert all(tool.path.name != "Tool.exe" for tool in tools)
    assert any(tool.path.name == "outside.exe" and tool.legacy for tool in tools)


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
