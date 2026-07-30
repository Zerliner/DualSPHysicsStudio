"""Bounded, read-only inventory checks for DualSPHysics result directories."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any


DEFAULT_MAX_FILES = 250_000
MAX_SESSION_BYTES = 2 * 1024 * 1024
MAX_REPORTED_PATHS = 1_000
MAX_REPORTED_GAPS = 1_000
RECOGNIZED_EXTENSIONS = {
    ".bi4",
    ".csv",
    ".json",
    ".log",
    ".out",
    ".txt",
    ".vtk",
    ".vtu",
    ".xml",
}
_FRAME_PATTERN = re.compile(
    r"^(Part(?:All|Fluid|Bound|Moving|Floating)?)[_-]?(\d+)\.(bi4|vtk|vtu)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FileCategory:
    extension: str
    count: int
    bytes: int


@dataclass(frozen=True, slots=True)
class FrameSequence:
    directory: str
    prefix: str
    extension: str
    first_index: int
    last_index: int
    frame_count: int
    missing_count: int
    missing_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AuditReport:
    schema_version: int
    generated_at: str
    root: str
    total_files: int
    total_bytes: int
    recognized_output_files: int
    truncated: bool
    categories: tuple[FileCategory, ...]
    sequences: tuple[FrameSequence, ...]
    zero_byte_files: tuple[str, ...]
    zero_byte_file_count: int
    session: dict[str, Any] | None
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def audit_directory(
    root: str | os.PathLike[str],
    *,
    max_files: int = DEFAULT_MAX_FILES,
) -> AuditReport:
    """Scan without following directory links or changing any source file."""

    if max_files <= 0:
        raise ValueError("max_files must be positive")
    requested = Path(root).expanduser()
    try:
        directory = requested.resolve(strict=True)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"输出目录不存在：{requested}") from error
    if not directory.is_dir():
        raise NotADirectoryError(f"输出路径不是文件夹：{directory}")

    category_values: dict[str, list[int]] = {}
    sequence_values: dict[tuple[str, str, str], set[int]] = {}
    zero_paths: list[str] = []
    warnings: list[str] = []
    total_files = 0
    total_bytes = 0
    recognized = 0
    zero_count = 0
    truncated = False

    def walk_error(error: OSError) -> None:
        warnings.append(f"无法读取目录：{error.filename or directory}（{error}）")

    for current, directories, filenames in os.walk(
        directory,
        topdown=True,
        onerror=walk_error,
        followlinks=False,
    ):
        current_path = Path(current)
        directories[:] = sorted(
            (
                name
                for name in directories
                if not _is_directory_link(current_path / name)
            ),
            key=str.casefold,
        )
        for filename in sorted(filenames, key=str.casefold):
            if total_files >= max_files:
                truncated = True
                break
            path = current_path / filename
            if path.is_symlink():
                continue
            try:
                stat = path.stat()
            except OSError as error:
                warnings.append(f"无法读取文件：{_relative(path, directory)}（{error}）")
                continue
            if not path.is_file():
                continue
            total_files += 1
            total_bytes += stat.st_size
            relative = _relative(path, directory)
            extension = path.suffix.casefold() or "(无扩展名)"
            category = category_values.setdefault(extension, [0, 0])
            category[0] += 1
            category[1] += stat.st_size
            if extension in RECOGNIZED_EXTENSIONS:
                recognized += 1
            if stat.st_size == 0:
                zero_count += 1
                if len(zero_paths) < MAX_REPORTED_PATHS:
                    zero_paths.append(relative)

            match = _FRAME_PATTERN.fullmatch(path.name)
            if match:
                parent = _relative(path.parent, directory)
                key = (parent, match.group(1), f".{match.group(3).casefold()}")
                sequence_values.setdefault(key, set()).add(int(match.group(2)))
        if truncated:
            break

    session, session_warnings = _read_session(directory)
    warnings.extend(session_warnings)
    sequences = tuple(
        _sequence_report(key, indices)
        for key, indices in sorted(
            sequence_values.items(),
            key=lambda item: (
                item[0][0].casefold(),
                item[0][1].casefold(),
                item[0][2],
            ),
        )
    )
    for sequence in sequences:
        if sequence.missing_count:
            warnings.append(
                f"{sequence.directory or '.'}/{sequence.prefix}*"
                f"{sequence.extension} 缺少 {sequence.missing_count} 个帧编号"
            )
    if zero_count:
        warnings.append(f"发现 {zero_count} 个零字节文件")
    if not recognized:
        warnings.append("未发现 VTK、BI4、CSV 或其他已识别的结果文件")
    if truncated:
        warnings.append(
            f"文件数达到安全上限 {max_files:,}，报告为截断的部分清单"
        )

    categories = tuple(
        FileCategory(extension=extension, count=values[0], bytes=values[1])
        for extension, values in sorted(
            category_values.items(),
            key=lambda item: (-item[1][1], item[0]),
        )
    )
    return AuditReport(
        schema_version=1,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        root=str(directory),
        total_files=total_files,
        total_bytes=total_bytes,
        recognized_output_files=recognized,
        truncated=truncated,
        categories=categories,
        sequences=sequences,
        zero_byte_files=tuple(zero_paths),
        zero_byte_file_count=zero_count,
        session=session,
        warnings=tuple(warnings),
    )


def write_report(
    report: AuditReport,
    destination: str | os.PathLike[str],
) -> Path:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def _sequence_report(
    key: tuple[str, str, str],
    indices: set[int],
) -> FrameSequence:
    ordered = sorted(indices)
    first = ordered[0]
    last = ordered[-1]
    missing_count = max(0, last - first + 1 - len(ordered))
    missing: list[int] = []
    for left, right in zip(ordered, ordered[1:]):
        if right <= left + 1:
            continue
        remaining = MAX_REPORTED_GAPS - len(missing)
        if remaining <= 0:
            break
        missing.extend(range(left + 1, min(right, left + 1 + remaining)))
    return FrameSequence(
        directory=key[0],
        prefix=key[1],
        extension=key[2],
        first_index=first,
        last_index=last,
        frame_count=len(ordered),
        missing_count=missing_count,
        missing_indices=tuple(missing),
    )


def _read_session(directory: Path) -> tuple[dict[str, Any] | None, list[str]]:
    path = directory / ".dss-run" / "run.json"
    if not path.is_file():
        return None, []
    warnings: list[str] = []
    try:
        if path.stat().st_size > MAX_SESSION_BYTES:
            raise ValueError("run.json 超过 2 MiB 安全上限")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("run.json 的根值不是对象")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        return None, [f"无法读取 .dss-run/run.json：{error}"]

    stages = value.get("stages")
    stage_statuses = {
        str(name): str(entry.get("status", "unknown"))
        for name, entry in stages.items()
        if isinstance(entry, dict)
    } if isinstance(stages, dict) else {}
    status = str(value.get("status", "unknown"))
    if status not in {"succeeded", "created"}:
        warnings.append(f"运行记录状态为 {status}")
    for name, stage_status in stage_statuses.items():
        if stage_status not in {"succeeded", "skipped", "pending"}:
            warnings.append(f"运行阶段 {name} 状态为 {stage_status}")
    return (
        {
            "session_id": str(value.get("session_id", "")),
            "status": status,
            "backend": str(value.get("backend", "")),
            "source_sha256": str((value.get("source") or {}).get("sha256", ""))
            if isinstance(value.get("source"), dict)
            else "",
            "stages": stage_statuses,
        },
        warnings,
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _is_directory_link(path: Path) -> bool:
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return True


__all__ = [
    "AuditReport",
    "DEFAULT_MAX_FILES",
    "FileCategory",
    "FrameSequence",
    "audit_directory",
    "write_report",
]
