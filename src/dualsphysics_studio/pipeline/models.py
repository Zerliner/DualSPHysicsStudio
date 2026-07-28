"""Data contracts shared by executable pipeline adapters.

The module deliberately depends only on the Python standard library.  The UI
can therefore inspect manifests and previous runs without importing Qt or VTK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


MANIFEST_SCHEMA_VERSION = 1


def file_sha256(path: str | os.PathLike[str], chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 digest of *path* without loading it all into memory."""

    digest = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True, frozen=True)
class ToolInfo:
    """Discovery and compatibility information for an external tool."""

    name: str
    root: Path | None = None
    executable: Path | None = None
    config_path: Path | None = None
    version: str | None = None
    available: bool = False
    compatible: bool = False
    diagnostics: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        """Whether the tool can safely be used by this application version."""

        return self.available and self.compatible

    @property
    def config(self) -> Path | None:
        """Compatibility alias used by simple UI bindings."""

        return self.config_path


@dataclass(slots=True)
class RunRequest:
    """A request to run one stage of the local toolchain.

    ``output_prefix`` is a file prefix rather than a directory.  For example,
    ``C:/case/Case_out/Case`` asks GenCase to create ``Case.bi4`` and
    ``Case.xml`` inside ``Case_out``.
    """

    source_xml: Path | str
    output_prefix: Path | str | None = None
    mode: str = "full"
    create_dirs: bool = True
    extra_args: tuple[str, ...] | Sequence[str] = ()
    timeout: float | None = None
    manifest_path: Path | str | None = None
    use_timestamp_on_conflict: bool = True

    def __post_init__(self) -> None:
        self.source_xml = Path(self.source_xml)
        if self.output_prefix is not None:
            self.output_prefix = Path(self.output_prefix)
        if self.manifest_path is not None:
            self.manifest_path = Path(self.manifest_path)
        self.extra_args = tuple(str(arg) for arg in self.extra_args)

    @property
    def case_xml(self) -> Path:
        """Readable alias for callers that refer to a case instead of source."""

        return self.source_xml


@dataclass(slots=True, frozen=True)
class Artifact:
    """One file produced by a pipeline stage."""

    path: Path
    role: str
    size: int
    required: bool = False
    sha256: str | None = None

    @classmethod
    def from_path(
        cls,
        path: str | os.PathLike[str],
        role: str,
        *,
        required: bool = False,
        calculate_hash: bool = False,
    ) -> "Artifact":
        file_path = Path(path).resolve()
        return cls(
            path=file_path,
            role=role,
            size=file_path.stat().st_size,
            required=required,
            sha256=file_sha256(file_path) if calculate_hash else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "role": self.role,
            "size": self.size,
            "required": self.required,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Artifact":
        return cls(
            path=Path(str(value["path"])),
            role=str(value.get("role", "other")),
            size=int(value.get("size", 0)),
            required=bool(value.get("required", False)),
            sha256=str(value["sha256"]) if value.get("sha256") else None,
        )


@dataclass(slots=True)
class ArtifactManifest:
    """Persistent, JSON-serialisable record of a pipeline invocation."""

    stage: str
    tool_name: str
    tool_version: str | None
    source_path: Path
    source_sha256: str
    output_prefix: Path
    command: tuple[str, ...]
    started_at: str
    finished_at: str
    return_code: int | None
    status: str
    parameters: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "stage": self.stage,
            "tool": {
                "name": self.tool_name,
                "version": self.tool_version,
            },
            "source": {
                "path": str(self.source_path),
                "sha256": self.source_sha256,
            },
            "output_prefix": str(self.output_prefix),
            "command": list(self.command),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "return_code": self.return_code,
            "status": self.status,
            "parameters": self.parameters,
            "summary": self.summary,
            "warnings": self.warnings,
            "errors": self.errors,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactManifest":
        tool = value.get("tool") or {}
        source = value.get("source") or {}
        return cls(
            schema_version=int(value.get("schema_version", MANIFEST_SCHEMA_VERSION)),
            stage=str(value.get("stage", "unknown")),
            tool_name=str(tool.get("name", "unknown")),
            tool_version=str(tool["version"]) if tool.get("version") else None,
            source_path=Path(str(source.get("path", ""))),
            source_sha256=str(source.get("sha256", "")),
            output_prefix=Path(str(value.get("output_prefix", ""))),
            command=tuple(str(part) for part in value.get("command", ())),
            started_at=str(value.get("started_at", "")),
            finished_at=str(value.get("finished_at", "")),
            return_code=(
                int(value["return_code"])
                if value.get("return_code") is not None
                else None
            ),
            status=str(value.get("status", "unknown")),
            parameters=dict(value.get("parameters") or {}),
            summary=dict(value.get("summary") or {}),
            warnings=[str(item) for item in value.get("warnings", ())],
            errors=[str(item) for item in value.get("errors", ())],
            artifacts=[
                Artifact.from_dict(item) for item in value.get("artifacts", ())
            ],
        )

    def write(self, path: str | os.PathLike[str]) -> Path:
        """Atomically write this manifest as UTF-8 JSON."""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return target

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "ArtifactManifest":
        content = Path(path).read_text(encoding="utf-8")
        return cls.from_dict(json.loads(content))

    def is_stale(self, source_xml: str | os.PathLike[str] | None = None) -> bool:
        """Return ``True`` when the source no longer matches the recorded hash."""

        source_path = Path(source_xml) if source_xml is not None else self.source_path
        try:
            return file_sha256(source_path) != self.source_sha256
        except OSError:
            return True

    def artifact_by_role(self, role: str) -> Artifact | None:
        return next((item for item in self.artifacts if item.role == role), None)


@dataclass(slots=True)
class RunResult:
    """Completed outcome of a synchronous adapter invocation."""

    status: str
    return_code: int | None
    command: tuple[str, ...]
    working_directory: Path
    output_prefix: Path
    started_at: str
    finished_at: str
    stdout: str = ""
    stderr: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    manifest: ArtifactManifest | None = None
    manifest_path: Path | None = None
    cancelled: bool = False
    timed_out: bool = False

    @property
    def success(self) -> bool:
        return self.status == "succeeded"

    @property
    def duration_seconds(self) -> float | None:
        """Best-effort wall-clock duration from the stored ISO timestamps."""

        from datetime import datetime

        try:
            return (
                datetime.fromisoformat(self.finished_at)
                - datetime.fromisoformat(self.started_at)
            ).total_seconds()
        except (TypeError, ValueError):
            return None

    @property
    def log(self) -> str:
        if self.stdout and self.stderr:
            return f"{self.stdout}\n{self.stderr}"
        return self.stdout or self.stderr


def manifest_is_stale(
    manifest: ArtifactManifest | str | os.PathLike[str],
    source_xml: str | os.PathLike[str] | None = None,
) -> bool:
    """Convenience function for callers holding either a manifest or its path."""

    record = (
        manifest
        if isinstance(manifest, ArtifactManifest)
        else ArtifactManifest.load(manifest)
    )
    return record.is_stale(source_xml)
