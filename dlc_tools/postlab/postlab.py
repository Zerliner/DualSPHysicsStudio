"""Shell-free, auditable recipes for official DualSPHysics post-processors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import locale
import os
from pathlib import Path
from queue import Empty, Queue
import re
import subprocess
from threading import Event, Thread
from typing import Any, Callable, Iterable


RECIPE_SCHEMA_VERSION = 1
RUN_SCHEMA_VERSION = 1
MAX_RECIPE_BYTES = 256 * 1024
MAX_ARGUMENTS = 128
MAX_ARGUMENT_CHARS = 1024
MAX_LOG_BYTES = 64 * 1024 * 1024
MAX_INVENTORY_FILES = 100_000


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    key: str
    label: str
    executable: str


KNOWN_TOOLS = (
    ToolDefinition("partvtk", "PartVTK", "PartVTK_win64.exe"),
    ToolDefinition("partvtkout", "PartVTKOut", "PartVTKOut_win64.exe"),
    ToolDefinition("boundaryvtk", "BoundaryVTK", "BoundaryVTK_win64.exe"),
    ToolDefinition("measuretool", "MeasureTool", "MeasureTool_win64.exe"),
    ToolDefinition("computeforces", "ComputeForces", "ComputeForces_win64.exe"),
    ToolDefinition("floatinginfo", "FloatingInfo", "FloatingInfo_win64.exe"),
    ToolDefinition("isosurface", "IsoSurface", "IsoSurface_win64.exe"),
    ToolDefinition("flowtool", "FlowTool", "FlowTool_win64.exe"),
    ToolDefinition("bifileinfo", "BIFileInfo", "BIFileInfo_win64.exe"),
)
_TOOL_BY_KEY = {tool.key: tool for tool in KNOWN_TOOLS}

DEFAULT_ARGUMENTS = {
    "partvtk": (
        "-dirdata",
        "{input}",
        "-savevtk",
        "{output}\\PartAll",
        "-vars:-all,+vel,+rhop,+press,+idp,+mk,+type",
    ),
}


class PostLabError(RuntimeError):
    """Raised when a recipe cannot be prepared or safely executed."""


@dataclass(frozen=True, slots=True)
class ToolInstallation:
    definition: ToolDefinition
    path: Path
    available: bool


@dataclass(frozen=True, slots=True)
class PostRecipe:
    name: str
    tool: str
    arguments: tuple[str, ...]

    def validate(self) -> None:
        name = self.name.strip()
        if not name or len(name) > 80:
            raise PostLabError("配方名称必须是 1–80 个字符。")
        if self.tool not in _TOOL_BY_KEY:
            raise PostLabError(f"不支持的官方后处理工具：{self.tool}")
        if not self.arguments or len(self.arguments) > MAX_ARGUMENTS:
            raise PostLabError(
                f"参数必须是 1–{MAX_ARGUMENTS} 个独立项目。"
            )
        has_output = False
        for argument in self.arguments:
            if (
                not isinstance(argument, str)
                or not argument
                or len(argument) > MAX_ARGUMENT_CHARS
            ):
                raise PostLabError(
                    f"每个参数必须是 1–{MAX_ARGUMENT_CHARS} 个字符。"
                )
            if "\0" in argument or "\r" in argument or "\n" in argument:
                raise PostLabError("参数不能包含 NUL 或换行。")
            placeholders = re.findall(r"\{[^{}]+\}", argument)
            if any(item not in {"{input}", "{output}"} for item in placeholders):
                raise PostLabError("参数只允许 {input} 和 {output} 占位符。")
            for placeholder in ("{input}", "{output}"):
                for match in re.finditer(re.escape(placeholder), argument):
                    before = argument[match.start() - 1] if match.start() else ""
                    after = (
                        argument[match.end()]
                        if match.end() < len(argument)
                        else ""
                    )
                    if before and before not in "=:":
                        raise PostLabError(
                            f"{placeholder} 前只能是参数开头、= 或 :。"
                        )
                    if after and after not in "\\/":
                        raise PostLabError(
                            f"{placeholder} 后只能是参数结尾或路径分隔符。"
                        )
            if re.search(
                r"(?i)(?:[a-z]:[\\/]|(?:^|[=,])[a-z]:|"
                r"(?:^|[=,:])[\\/]{1,2}(?![{}]))",
                argument,
            ):
                raise PostLabError("绝对路径必须改用 {input} 或 {output} 占位符。")
            if re.search(r"(^|[\\/])\.\.([\\/]|$)", argument):
                raise PostLabError("参数不能包含离开任务目录的 .. 路径。")
            has_output = has_output or "{output}" in argument
        if not has_output:
            raise PostLabError(
                "配方必须至少使用一次 {output}，确保产物进入全新任务目录。"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RECIPE_SCHEMA_VERSION,
            "name": self.name.strip(),
            "tool": self.tool,
            "arguments": list(self.arguments),
        }


@dataclass(frozen=True, slots=True)
class PostLabRun:
    status: str
    return_code: int | None
    output_directory: Path
    record_path: Path
    command: tuple[str, ...]
    generated_files: int
    log_truncated: bool


def discover_tools(root: str | os.PathLike[str]) -> tuple[ToolInstallation, ...]:
    requested = Path(root).expanduser()
    windows_directory = _windows_tools_directory(requested)
    installations: list[ToolInstallation] = []
    for definition in KNOWN_TOOLS:
        path = (windows_directory / definition.executable).resolve()
        installations.append(
            ToolInstallation(
                definition=definition,
                path=path,
                available=path.is_file(),
            )
        )
    return tuple(installations)


def save_recipe(recipe: PostRecipe, destination: str | os.PathLike[str]) -> Path:
    recipe.validate()
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(recipe.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def load_recipe(source: str | os.PathLike[str]) -> PostRecipe:
    path = Path(source)
    if not path.is_file():
        raise PostLabError(f"配方文件不存在：{path}")
    if path.stat().st_size > MAX_RECIPE_BYTES:
        raise PostLabError("配方文件超过 256 KiB 上限。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PostLabError(f"无法读取配方：{error}") from error
    if not isinstance(value, dict):
        raise PostLabError("配方根值必须是对象。")
    if value.get("schema_version") != RECIPE_SCHEMA_VERSION:
        raise PostLabError(
            f"仅支持 schema_version={RECIPE_SCHEMA_VERSION} 的配方。"
        )
    arguments = value.get("arguments")
    if not isinstance(arguments, list) or not all(
        isinstance(item, str) for item in arguments
    ):
        raise PostLabError("arguments 必须是字符串数组。")
    recipe = PostRecipe(
        name=str(value.get("name", "")),
        tool=str(value.get("tool", "")),
        arguments=tuple(arguments),
    )
    recipe.validate()
    return recipe


def preview_command(
    recipe: PostRecipe,
    installation_root: str | os.PathLike[str],
    input_directory: str | os.PathLike[str],
) -> tuple[str, ...]:
    recipe.validate()
    tool = _selected_tool(recipe, installation_root)
    input_path = _input_directory(input_directory)
    return _render_command(
        recipe,
        tool,
        input_path,
        Path("<新建任务目录>"),
    )


def run_recipe(
    recipe: PostRecipe,
    installation_root: str | os.PathLike[str],
    input_directory: str | os.PathLike[str],
    output_parent: str | os.PathLike[str],
    *,
    cancel_event: Event | None = None,
    on_log: Callable[[str], None] | None = None,
    popen_factory: Callable[..., Any] | None = None,
) -> PostLabRun:
    recipe.validate()
    tool = _selected_tool(recipe, installation_root)
    input_path = _input_directory(input_directory)
    parent = Path(output_parent).expanduser()
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as error:
        raise PostLabError(f"输出父目录不可用：{error}") from error
    if not resolved_parent.is_dir():
        raise PostLabError(f"输出父路径不是文件夹：{resolved_parent}")

    output_directory = _new_output_directory(resolved_parent, recipe.name)
    command = _render_command(recipe, tool, input_path, output_directory)
    record_path = output_directory / "postlab-run.json"
    log_path = output_directory / "stdout.log"
    started = _utc_now()
    base_record = {
        "schema_version": RUN_SCHEMA_VERSION,
        "application": {"name": "DSS PostLab", "version": "1.0.0"},
        "recipe": recipe.to_dict(),
        "tool": {
            "name": tool.definition.label,
            "executable": str(tool.path),
            "sha256": _file_sha256(tool.path),
        },
        "input_directory": str(input_path),
        "output_directory": str(output_directory),
        "command": list(command),
        "status": "running",
        "started_at": started,
        "finished_at": None,
        "return_code": None,
        "cancelled": False,
        "log_truncated": False,
        "artifacts": [],
    }
    _write_json_atomic(record_path, base_record)

    popen = popen_factory or subprocess.Popen
    kwargs: dict[str, Any] = {
        "cwd": str(output_directory),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": locale.getpreferredencoding(False),
        "errors": "replace",
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    process: Any | None = None
    return_code: int | None = None
    cancelled = False
    log_bytes = 0
    log_truncated = False
    try:
        process = popen(list(command), **kwargs)
        queue: Queue[str | None] = Queue()
        reader = Thread(
            target=_read_process_output,
            args=(process.stdout, queue),
            daemon=True,
        )
        reader.start()
        with log_path.open("w", encoding="utf-8", newline="\n") as log:
            stream_finished = False
            while process.poll() is None or not stream_finished:
                if cancel_event is not None and cancel_event.is_set() and not cancelled:
                    cancelled = True
                    process.terminate()
                try:
                    line = queue.get(timeout=0.1)
                except Empty:
                    if cancelled and process.poll() is None:
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                    continue
                if line is None:
                    stream_finished = True
                    continue
                encoded = line.encode("utf-8", errors="replace")
                if log_bytes + len(encoded) <= MAX_LOG_BYTES:
                    log.write(line)
                    if not line.endswith("\n"):
                        log.write("\n")
                    log_bytes += len(encoded) + (0 if line.endswith("\n") else 1)
                else:
                    log_truncated = True
                if on_log is not None:
                    on_log(line.rstrip("\r\n"))
        return_code = process.wait()
    except Exception as error:
        if process is not None and process.poll() is None:
            process.kill()
        base_record.update(
            {
                "status": "failed",
                "finished_at": _utc_now(),
                "error": str(error),
                "cancelled": cancelled,
                "log_truncated": log_truncated,
            }
        )
        _write_json_atomic(record_path, base_record)
        raise PostLabError(f"后处理任务无法完成：{error}") from error

    artifacts, inventory_truncated = _inventory(output_directory)
    generated = artifacts
    if cancelled:
        status = "cancelled"
    elif return_code != 0 or not generated:
        status = "failed"
    else:
        status = "succeeded"
    base_record.update(
        {
            "status": status,
            "finished_at": _utc_now(),
            "return_code": return_code,
            "cancelled": cancelled,
            "log_truncated": log_truncated,
            "artifacts": artifacts,
            "artifact_inventory_truncated": inventory_truncated,
        }
    )
    _write_json_atomic(record_path, base_record)
    return PostLabRun(
        status=status,
        return_code=return_code,
        output_directory=output_directory,
        record_path=record_path,
        command=command,
        generated_files=len(generated),
        log_truncated=log_truncated,
    )


def _windows_tools_directory(root: Path) -> Path:
    expanded = root.expanduser()
    if expanded.name.casefold() == "windows" and expanded.parent.name.casefold() == "bin":
        return expanded.resolve()
    return (expanded / "bin" / "windows").resolve()


def _selected_tool(
    recipe: PostRecipe,
    installation_root: str | os.PathLike[str],
) -> ToolInstallation:
    tools = {tool.definition.key: tool for tool in discover_tools(installation_root)}
    selected = tools[recipe.tool]
    if not selected.available:
        raise PostLabError(
            f"未找到 {selected.definition.executable}：{selected.path}"
        )
    return selected


def _input_directory(path: str | os.PathLike[str]) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except OSError as error:
        raise PostLabError(f"输入目录不可用：{error}") from error
    if not resolved.is_dir():
        raise PostLabError(f"输入路径不是文件夹：{resolved}")
    return resolved


def _render_command(
    recipe: PostRecipe,
    tool: ToolInstallation,
    input_directory: Path,
    output_directory: Path,
) -> tuple[str, ...]:
    arguments = tuple(
        argument.replace("{input}", str(input_directory)).replace(
            "{output}",
            str(output_directory),
        )
        for argument in recipe.arguments
    )
    return (str(tool.path), *arguments)


def _new_output_directory(parent: Path, name: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip()).strip("-_")
    slug = slug[:40] or "recipe"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for index in range(100):
        suffix = "" if index == 0 else f"-{index}"
        candidate = parent / f"DSS-PostLab-{stamp}-{slug}{suffix}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate.resolve()
    raise PostLabError("无法创建唯一的后处理任务目录。")


def _read_process_output(stream: Iterable[str] | None, queue: Queue[str | None]) -> None:
    try:
        if stream is not None:
            for line in stream:
                queue.put(line)
    finally:
        queue.put(None)


def _inventory(directory: Path) -> tuple[list[dict[str, Any]], bool]:
    items: list[dict[str, Any]] = []
    truncated = False
    walk_errors: list[OSError] = []
    for current, directories, filenames in os.walk(
        directory,
        topdown=True,
        followlinks=False,
        onerror=walk_errors.append,
    ):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in sorted(directories, key=str.casefold):
            try:
                path = current_path / name
                if not path.is_symlink() and not path.is_junction():
                    safe_directories.append(name)
            except OSError:
                truncated = True
        directories[:] = safe_directories
        for name in sorted(filenames, key=str.casefold):
            if len(items) >= MAX_INVENTORY_FILES:
                truncated = True
                return items, truncated
            path = current_path / name
            relative = path.relative_to(directory).as_posix()
            if relative in {"postlab-run.json", "stdout.log"}:
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                truncated = True
                continue
            items.append(
                {
                    "path": relative,
                    "bytes": stat.st_size,
                }
            )
    return items, truncated or bool(walk_errors)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


__all__ = [
    "DEFAULT_ARGUMENTS",
    "KNOWN_TOOLS",
    "PostLabError",
    "PostLabRun",
    "PostRecipe",
    "ToolDefinition",
    "ToolInstallation",
    "discover_tools",
    "load_recipe",
    "preview_command",
    "run_recipe",
    "save_recipe",
]
