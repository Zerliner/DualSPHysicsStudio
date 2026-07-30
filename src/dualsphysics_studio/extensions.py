"""Low-cost discovery and launching of optional portable extension tools."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sys
from urllib.parse import urlparse

from PySide6.QtCore import QProcess


MANIFEST_FILENAME = "dss-extension.json"
MANIFEST_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 64 * 1024


class ExtensionToolError(RuntimeError):
    """Raised when an extension directory or executable cannot be used."""


@dataclass(frozen=True, slots=True)
class ExtensionTool:
    label: str
    path: Path
    version: str = ""
    publisher: str = ""
    description: str = ""
    license_path: Path | None = None
    homepage: str = ""
    supported_inputs: tuple[str, ...] = ()
    manifest_path: Path | None = None
    available: bool = True
    error: str = ""

    @property
    def legacy(self) -> bool:
        return self.manifest_path is None

    def detail_text(self) -> str:
        lines = [f"名称：{self.label}"]
        if self.version:
            lines.append(f"版本：{self.version}")
        if self.publisher:
            lines.append(f"发布者：{self.publisher}")
        lines.append(f"程序：{self.path}")
        lines.append(
            f"元数据：{self.manifest_path}"
            if self.manifest_path is not None
            else "元数据：未提供 manifest（legacy 自动发现）"
        )
        if self.license_path is not None:
            lines.append(f"许可文件：{self.license_path}")
        if self.supported_inputs:
            lines.append(f"支持输入：{', '.join(self.supported_inputs)}")
        if self.homepage:
            lines.append(f"主页：{self.homepage}")
        if self.description:
            lines.extend(("", self.description))
        if self.error:
            lines.extend(("", f"不可用：{self.error}"))
        return "\n".join(lines)


def default_dlc_directory() -> Path:
    """Return the DLC folder beside the frozen app or at the project root."""

    if getattr(sys, "frozen", False):
        application_root = Path(sys.executable).resolve().parent
    else:
        application_root = Path(__file__).resolve().parents[2]
    return (application_root / "DLC").resolve()


def discover_extension_tools(root: str | Path) -> list[ExtensionTool]:
    """Discover legacy EXEs and optional manifest-described portable tools."""

    root_path = Path(root).expanduser()
    if not root_path.exists():
        return []
    if not root_path.is_dir():
        raise ExtensionToolError(f"扩展目录不是文件夹：{root_path}")

    try:
        resolved_root = root_path.resolve(strict=True)
        root_entries = _sorted_entries(resolved_root)
        tools: list[ExtensionTool] = []
        candidates: list[Path] = []
        root_manifest = resolved_root / MANIFEST_FILENAME
        if root_manifest.is_file():
            tools.append(_tool_from_manifest_or_error(root_manifest, resolved_root))
        else:
            candidates.extend(
                entry for entry in root_entries if _is_executable_file(entry)
            )
        for directory in root_entries:
            if not directory.is_dir():
                continue
            manifest = directory / MANIFEST_FILENAME
            if manifest.is_file():
                tools.append(_tool_from_manifest_or_error(manifest, resolved_root))
            else:
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
    tools.extend(ExtensionTool(label=path.stem, path=path) for path in paths)
    label_counts = Counter(tool.label.casefold() for tool in tools)
    tools = [
        replace(
            tool,
            label=f"{tool.label} ({tool.path.parent.name})",
        )
        if label_counts[tool.label.casefold()] > 1
        else tool
        for tool in tools
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


def _tool_from_manifest_or_error(manifest: Path, root: Path) -> ExtensionTool:
    try:
        return _tool_from_manifest(manifest, root)
    except (ExtensionToolError, OSError, UnicodeError, ValueError) as error:
        return ExtensionTool(
            label=f"{manifest.parent.name}（manifest 无效）",
            path=manifest,
            manifest_path=manifest,
            available=False,
            error=str(error),
        )


def _tool_from_manifest(manifest: Path, root: Path) -> ExtensionTool:
    if manifest.stat().st_size > MAX_MANIFEST_BYTES:
        raise ExtensionToolError(
            f"{manifest.name} 超过 {MAX_MANIFEST_BYTES // 1024} KiB 上限"
        )
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ExtensionToolError(
            f"{manifest.name} 不是有效 JSON：第 {error.lineno} 行"
        ) from error
    if not isinstance(value, dict):
        raise ExtensionToolError(f"{manifest.name} 的根值必须是对象")
    if value.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ExtensionToolError(
            f"{manifest.name} 仅支持 schema_version={MANIFEST_SCHEMA_VERSION}"
        )

    name = _manifest_text(value, "name", required=True, maximum=80)
    executable_value = _manifest_text(
        value,
        "executable",
        required=True,
        maximum=260,
    )
    executable = _manifest_member(
        manifest.parent,
        executable_value,
        description="扩展程序",
        suffix=".exe",
    )
    if not executable.is_relative_to(root):
        raise ExtensionToolError("扩展程序必须位于当前 DLC 目录内")

    license_value = _manifest_text(value, "license", maximum=260)
    license_path = (
        _manifest_member(
            manifest.parent,
            license_value,
            description="许可文件",
        )
        if license_value
        else None
    )
    homepage = _manifest_text(value, "homepage", maximum=500)
    if homepage:
        parsed = urlparse(homepage)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ExtensionToolError("homepage 必须是 http 或 https URL")

    supported = value.get("supported_inputs", ())
    if not isinstance(supported, list) or len(supported) > 32:
        raise ExtensionToolError("supported_inputs 必须是不超过 32 项的数组")
    normalized_inputs: list[str] = []
    seen_inputs: set[str] = set()
    for item in supported:
        if (
            not isinstance(item, str)
            or not item.startswith(".")
            or len(item) > 20
            or "/" in item
            or "\\" in item
        ):
            raise ExtensionToolError(
                "supported_inputs 只能包含类似 .osgb、.stl 的扩展名"
            )
        normalized = item.casefold()
        if normalized not in seen_inputs:
            seen_inputs.add(normalized)
            normalized_inputs.append(normalized)

    return ExtensionTool(
        label=name,
        path=executable,
        version=_manifest_text(value, "version", maximum=40),
        publisher=_manifest_text(value, "publisher", maximum=80),
        description=_manifest_text(value, "description", maximum=500),
        license_path=license_path,
        homepage=homepage,
        supported_inputs=tuple(normalized_inputs),
        manifest_path=manifest.resolve(strict=True),
    )


def _manifest_text(
    value: dict[str, object],
    key: str,
    *,
    required: bool = False,
    maximum: int,
) -> str:
    item = value.get(key, "")
    if not isinstance(item, str):
        raise ExtensionToolError(f"{key} 必须是字符串")
    item = item.strip()
    if required and not item:
        raise ExtensionToolError(f"{key} 不能为空")
    if len(item) > maximum:
        raise ExtensionToolError(f"{key} 超过 {maximum} 个字符")
    return item


def _manifest_member(
    directory: Path,
    value: str,
    *,
    description: str,
    suffix: str | None = None,
) -> Path:
    requested = Path(value)
    if requested.is_absolute():
        raise ExtensionToolError(f"{description}必须使用相对路径")
    base = directory.resolve(strict=True)
    try:
        candidate = (base / requested).resolve(strict=True)
    except FileNotFoundError as error:
        raise ExtensionToolError(f"{description}不存在：{value}") from error
    try:
        candidate.relative_to(base)
    except ValueError as error:
        raise ExtensionToolError(f"{description}不能离开 manifest 目录") from error
    if not candidate.is_file():
        raise ExtensionToolError(f"{description}不是普通文件：{value}")
    if suffix is not None and candidate.suffix.casefold() != suffix:
        raise ExtensionToolError(f"{description}必须是 {suffix} 文件")
    return candidate


def _path_sort_key(path: Path) -> tuple[str, str]:
    value = str(path)
    return (value.casefold(), value)


__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "ExtensionTool",
    "ExtensionToolError",
    "default_dlc_directory",
    "discover_extension_tools",
    "launch_extension_tool",
]
