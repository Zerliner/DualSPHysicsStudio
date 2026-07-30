"""Case-local, privacy-aware records for multi-stage run sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import shutil
from typing import Any, Iterable, Mapping
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from ..version import APP_VERSION
from .estimate import ResourceEstimate, available_physical_memory
from .models import RunResult, ToolInfo, file_sha256


SESSION_SCHEMA_VERSION = 1
SESSION_DIRECTORY = ".dss-run"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(slots=True)
class RunSession:
    """Mutable in-memory representation persisted atomically after each event."""

    record_path: Path
    session_id: str
    source_name: str
    source_sha256: str
    backend: str
    workflow: tuple[str, ...]
    status: str = "created"
    created_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    app_version: str = APP_VERSION
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    estimates: list[dict[str, Any]] = field(default_factory=list)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    hardware: dict[str, Any] = field(default_factory=dict)

    @property
    def output_root(self) -> Path:
        return self.record_path.parent.parent

    @property
    def run_directory(self) -> Path:
        return self.record_path.parent

    @property
    def source_snapshot(self) -> Path:
        return self.run_directory / "source" / self.source_name

    @classmethod
    def create(
        cls,
        *,
        source_xml: str | os.PathLike[str],
        output_root: str | os.PathLike[str],
        backend: str,
        workflow: Iterable[str],
        tools: Mapping[str, ToolInfo] | None = None,
    ) -> "RunSession":
        source = Path(source_xml).resolve()
        root = Path(output_root).resolve()
        run_directory = root / SESSION_DIRECTORY
        record_path = run_directory / "run.json"
        if record_path.exists():
            raise FileExistsError(f"运行记录已存在: {record_path}")
        (run_directory / "source").mkdir(parents=True, exist_ok=True)
        (run_directory / "logs").mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, run_directory / "source" / source.name)
        now = datetime.now(timezone.utc)
        session = cls(
            record_path=record_path,
            session_id=f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}",
            source_name=source.name,
            source_sha256=file_sha256(source),
            backend=backend,
            workflow=tuple(workflow),
            stages={
                stage: {
                    "status": "pending",
                    "started_at": None,
                    "finished_at": None,
                    "return_code": None,
                    "manifest": None,
                    "summary": {},
                    "warnings": [],
                    "errors": [],
                }
                for stage in workflow
            },
            tools=_tool_records(tools or {}),
            hardware=_hardware_facts(),
        )
        session.write()
        return session

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "RunSession":
        record_path = Path(path).resolve()
        value = json.loads(record_path.read_text(encoding="utf-8"))
        source = value.get("source") or {}
        return cls(
            record_path=record_path,
            session_id=str(value.get("session_id", "")),
            source_name=str(source.get("snapshot", "Case_Def.xml")),
            source_sha256=str(source.get("sha256", "")),
            backend=str(value.get("backend", "")),
            workflow=tuple(str(item) for item in value.get("workflow", ())),
            status=str(value.get("status", "unknown")),
            created_at=str(value.get("created_at", "")),
            started_at=value.get("started_at"),
            finished_at=value.get("finished_at"),
            app_version=str(value.get("application", {}).get("version", "")),
            stages={
                str(key): dict(item)
                for key, item in (value.get("stages") or {}).items()
            },
            estimates=[dict(item) for item in value.get("estimates", ())],
            tools={
                str(key): dict(item)
                for key, item in (value.get("tools") or {}).items()
            },
            actual=dict(value.get("actual") or {}),
            hardware=dict(value.get("hardware") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": self.session_id,
            "application": {
                "name": "DualSPHysics Studio",
                "version": self.app_version,
            },
            "source": {
                "snapshot": self.source_name,
                "sha256": self.source_sha256,
            },
            "backend": self.backend,
            "workflow": list(self.workflow),
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "hardware": self.hardware,
            "tools": self.tools,
            "estimates": self.estimates,
            "stages": self.stages,
            "actual": self.actual,
            "privacy": {
                "local_record_contains_xml": True,
                "absolute_paths_in_session_record": False,
            },
        }

    def write(self) -> Path:
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.record_path.with_name(
            f".{self.record_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.record_path)
        return self.record_path

    def start_stage(self, stage: str) -> None:
        if stage not in self.stages:
            self.workflow = (*self.workflow, stage)
            self.stages[stage] = {
                "status": "pending",
                "started_at": None,
                "finished_at": None,
                "return_code": None,
                "manifest": None,
                "summary": {},
                "warnings": [],
                "errors": [],
            }
        now = _utc_now()
        if self.started_at is None:
            self.started_at = now
        self.status = "running"
        self.stages[stage]["status"] = "running"
        self.stages[stage]["started_at"] = now
        self.write()

    def append_log(self, stage: str, stream: str, line: str) -> Path:
        safe_stream = "stderr" if stream.casefold() == "stderr" else "stdout"
        target = self.run_directory / "logs" / f"{stage}.{safe_stream}.log"
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            if not line.endswith("\n"):
                handle.write("\n")
        return target

    def add_estimate(self, estimate: ResourceEstimate) -> None:
        item = estimate.to_dict()
        item["recorded_at"] = _utc_now()
        self.estimates.append(item)
        self.write()

    def add_tool(self, stage: str, info: ToolInfo) -> None:
        self.tools.update(_tool_records({stage: info}))
        self.write()

    def finish_stage(self, stage: str, result: RunResult) -> None:
        entry = self.stages.setdefault(stage, {})
        entry.update(
            {
                "status": result.status,
                "finished_at": result.finished_at,
                "return_code": result.return_code,
                "summary": dict(result.summary),
                "warnings": list(result.warnings),
                "errors": list(result.errors),
            }
        )
        if result.manifest_path is not None:
            entry["manifest"] = _relative_or_name(
                Path(result.manifest_path), self.output_root
            )
        self._update_actual(stage, result)
        self.write()

    def fail_stage(self, stage: str, message: str) -> None:
        entry = self.stages.setdefault(stage, {})
        entry.update(
            {
                "status": "failed",
                "finished_at": _utc_now(),
                "errors": [message],
            }
        )
        self.write()

    def skip_remaining(self, *, after: str | None, reason: str) -> None:
        seen = after is None
        for stage in self.workflow:
            if stage == after:
                seen = True
                continue
            if seen and self.stages.get(stage, {}).get("status") == "pending":
                self.stages[stage]["status"] = "skipped"
                self.stages[stage]["errors"] = [reason]
        self.write()

    def finalize(self, status: str) -> None:
        self.status = status
        self.finished_at = _utc_now()
        self.write()

    def mark_interrupted(self) -> bool:
        if self.status != "running":
            return False
        self.status = "interrupted"
        self.finished_at = _utc_now()
        for entry in self.stages.values():
            if entry.get("status") == "running":
                entry["status"] = "interrupted"
                entry["finished_at"] = self.finished_at
        self.write()
        return True

    def export_diagnostic(
        self,
        destination: str | os.PathLike[str],
        *,
        include_xml: bool = True,
        redaction_paths: Iterable[str | os.PathLike[str]] = (),
    ) -> Path:
        """Export a reviewable ZIP; XML is included unless the user opts out."""

        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        redactor = _Redactor(redaction_paths)
        payloads: dict[str, bytes] = {}
        run_json = json.dumps(
            _redact_value(self.to_dict(), redactor),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        payloads["run.json"] = run_json
        payloads["summary.md"] = redactor.text(self.summary_markdown()).encode("utf-8")

        if include_xml and self.source_snapshot.is_file():
            payloads[f"source/{self.source_name}"] = self.source_snapshot.read_bytes()

        logs_directory = self.run_directory / "logs"
        if logs_directory.is_dir():
            for path in sorted(logs_directory.glob("*.log")):
                payloads[f"logs/{path.name}"] = redactor.text(
                    path.read_text(encoding="utf-8", errors="replace")
                ).encode("utf-8")

        for stage, entry in self.stages.items():
            manifest = entry.get("manifest")
            if not manifest:
                continue
            path = (self.output_root / str(manifest)).resolve()
            if not path.is_file():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            payloads[f"manifests/{stage}.json"] = json.dumps(
                _redact_value(value, redactor),
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")

        checksums = [
            f"{sha256(content).hexdigest()}  {name}"
            for name, content in sorted(payloads.items())
        ]
        payloads["checksums.sha256"] = ("\n".join(checksums) + "\n").encode("ascii")
        with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
            for name, content in payloads.items():
                archive.writestr(name, content)
        return target

    def summary_markdown(self) -> str:
        lines = [
            "# DualSPHysics Studio 运行记录",
            "",
            f"- 会话：`{self.session_id}`",
            f"- Studio：`{self.app_version}`",
            f"- 后端：`{self.backend.upper()}`",
            f"- 状态：`{self.status}`",
            f"- XML SHA-256：`{self.source_sha256}`",
            "",
            "## 阶段",
            "",
        ]
        for stage in self.workflow:
            entry = self.stages.get(stage, {})
            lines.append(f"- {stage}: {entry.get('status', 'unknown')}")
        if self.actual:
            lines.extend(["", "## 实际结果", ""])
            for key, value in self.actual.items():
                lines.append(f"- {key}: {value}")
        return "\n".join(lines) + "\n"

    def _update_actual(self, stage: str, result: RunResult) -> None:
        summary = result.summary
        if stage == "gencase":
            counts = summary.get("particle_counts")
            if isinstance(counts, Mapping):
                self.actual["particle_counts"] = dict(counts)
        if stage in {"cpu", "gpu"}:
            for key in (
                "total_runtime_seconds",
                "simulation_runtime_seconds",
                "allocated_cpu_memory_bytes",
                "allocated_gpu_memory_bytes",
                "requested_gpu_memory_bytes",
                "device",
                "steps",
                "reported_part_files",
            ):
                if key in summary:
                    self.actual[key] = summary[key]
        if result.duration_seconds is not None:
            self.actual[f"{stage}_wall_seconds"] = result.duration_seconds


def find_case_sessions(source_xml: str | os.PathLike[str]) -> tuple[RunSession, ...]:
    source = Path(source_xml).resolve()
    stem = source.stem[:-4] if source.stem.casefold().endswith("_def") else source.stem
    sessions: list[RunSession] = []
    output_name = f"{stem}_out"
    try:
        directories = tuple(source.parent.iterdir())
    except OSError:
        directories = ()
    for directory in directories:
        if not directory.is_dir() or not (
            directory.name == output_name
            or directory.name.startswith(f"{output_name}_")
        ):
            continue
        record = directory / SESSION_DIRECTORY / "run.json"
        if not record.is_file():
            continue
        try:
            session = RunSession.load(record)
        except (OSError, ValueError, TypeError):
            continue
        if session.source_name.casefold() != source.name.casefold():
            continue
        sessions.append(session)
    return tuple(sorted(sessions, key=lambda item: item.created_at, reverse=True))


def session_history_values(
    sessions: Iterable[RunSession],
) -> tuple[dict[str, Any], ...]:
    return tuple(session.to_dict() for session in sessions)


def _tool_records(tools: Mapping[str, ToolInfo]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for stage, info in tools.items():
        executable = info.executable
        config = info.config_path
        records[stage] = {
            "name": info.name,
            "version": info.version,
            "executable": executable.name if executable is not None else None,
            "executable_sha256": _safe_hash(executable),
            "config": config.name if config is not None else None,
            "config_sha256": _safe_hash(config),
        }
    return records


def _safe_hash(path: Path | None) -> str | None:
    try:
        return file_sha256(path) if path is not None and path.is_file() else None
    except OSError:
        return None


def _hardware_facts() -> dict[str, Any]:
    total_memory = None
    if hasattr(__import__("ctypes"), "windll"):
        # Available memory is useful preflight evidence; total memory is not
        # required for identity and no hostname or serial number is collected.
        total_memory = available_physical_memory()
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "architecture": platform.machine(),
        "cpu": platform.processor() or None,
        "logical_cpu_count": os.cpu_count(),
        "available_memory_at_start_bytes": total_memory,
    }


def _relative_or_name(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.name


class _Redactor:
    def __init__(self, paths: Iterable[str | os.PathLike[str]]) -> None:
        resolved: list[str] = []
        for item in paths:
            raw = str(Path(item))
            resolved.append(raw)
            try:
                resolved.append(str(Path(item).resolve()))
            except OSError:
                pass
        self.paths = tuple(sorted(set(resolved), key=len, reverse=True))

    def text(self, value: str) -> str:
        result = value
        for index, path in enumerate(self.paths, start=1):
            result = result.replace(path, f"<LOCAL_PATH_{index}>")
            result = result.replace(path.replace("\\", "/"), f"<LOCAL_PATH_{index}>")
        result = re.sub(
            r"(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"']+",
            "<USER_HOME>",
            result,
        )
        return result


def _redact_value(value: Any, redactor: _Redactor) -> Any:
    if isinstance(value, str):
        return redactor.text(value)
    if isinstance(value, list):
        return [_redact_value(item, redactor) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, redactor) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _redact_value(item, redactor)
            for key, item in value.items()
        }
    return value
