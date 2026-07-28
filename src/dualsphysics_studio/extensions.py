"""Low-cost discovery and launching of optional portable extension tools."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import sys

from PySide6.QtCore import QProcess


class ExtensionToolError(RuntimeError):
    """Raised when an extension directory or executable cannot be used."""


@dataclass(frozen=True, slots=True)
class ExtensionTool:
    label: str
    path: Path


def default_dlc_directory() -> Path:
    """Return the DLC folder beside the frozen app or at the project root."""

    if getattr(sys, "frozen", False):
        application_root = Path(sys.executable).resolve().parent
    else:
        application_root = Path(__file__).resolve().parents[2]
    return (application_root / "DLC").resolve()


def discover_extension_tools(root: str | Path) -> list[ExtensionTool]:
    """Discover EXEs at ``root`` and exactly one directory level below it."""

    root_path = Path(root).expanduser()
    if not root_path.exists():
        return []
    if not root_path.is_dir():
        raise ExtensionToolError(f"扩展目录不是文件夹：{root_path}")

    try:
        resolved_root = root_path.resolve(strict=True)
        root_entries = _sorted_entries(resolved_root)
        candidates = [entry for entry in root_entries if _is_executable_file(entry)]
        for directory in root_entries:
            if not directory.is_dir():
                continue
            candidates.extend(
                entry
                for entry in _sorted_entries(directory)
                if _is_executable_file(entry)
            )
    except ExtensionToolError:
        raise
    except OSError as error:
        raise ExtensionToolError(f"无法扫描扩展目录“{root_path}”：{error}") from error

    # Resolve and de-duplicate before deriving labels.  Case-folding keeps the
    # result deterministic on Windows even if the same target is linked twice.
    unique_paths: dict[str, Path] = {}
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise ExtensionToolError(f"无法解析扩展工具“{candidate}”：{error}") from error
        unique_paths.setdefault(str(resolved).casefold(), resolved)

    paths = sorted(unique_paths.values(), key=_path_sort_key)
    stem_counts = Counter(path.stem.casefold() for path in paths)
    tools = [
        ExtensionTool(
            label=(
                f"{path.stem} ({path.parent.name})"
                if stem_counts[path.stem.casefold()] > 1
                else path.stem
            ),
            path=path,
        )
        for path in paths
    ]
    return sorted(
        tools,
        key=lambda tool: (
            tool.label.casefold(),
            tool.label,
            str(tool.path).casefold(),
            str(tool.path),
        ),
    )


def launch_extension_tool(path: str | Path) -> None:
    """Launch a validated EXE directly, without a command shell."""

    requested = Path(path).expanduser()
    if requested.suffix.casefold() != ".exe":
        raise ExtensionToolError(f"扩展工具必须是 .exe 文件：{requested}")
    try:
        executable = requested.resolve(strict=True)
    except FileNotFoundError as error:
        raise ExtensionToolError(f"扩展工具不存在：{requested}") from error
    except OSError as error:
        raise ExtensionToolError(f"无法访问扩展工具“{requested}”：{error}") from error
    if not executable.is_file():
        raise ExtensionToolError(f"扩展工具不是普通文件：{executable}")

    try:
        result = QProcess.startDetached(
            str(executable),
            [],
            str(executable.parent),
        )
    except Exception as error:
        raise ExtensionToolError(f"无法启动扩展工具“{executable.name}”：{error}") from error

    started = bool(result[0]) if isinstance(result, tuple) else bool(result)
    if not started:
        raise ExtensionToolError(
            f"无法启动扩展工具“{executable.name}”；请检查文件是否完整或被系统阻止。"
        )


def _sorted_entries(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir(), key=lambda path: (path.name.casefold(), path.name))
    except OSError as error:
        raise ExtensionToolError(f"无法读取扩展目录“{directory}”：{error}") from error


def _is_executable_file(path: Path) -> bool:
    return path.suffix.casefold() == ".exe" and path.is_file()


def _path_sort_key(path: Path) -> tuple[str, str]:
    value = str(path)
    return (value.casefold(), value)


__all__ = [
    "ExtensionTool",
    "ExtensionToolError",
    "default_dlc_directory",
    "discover_extension_tools",
    "launch_extension_tool",
]
