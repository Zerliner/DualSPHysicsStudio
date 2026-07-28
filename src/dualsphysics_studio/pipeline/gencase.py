"""GenCase v5.4 discovery, execution, validation, and result parsing."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import locale
import os
from pathlib import Path
import re
import subprocess
from threading import Event, Lock, Thread
import time
from typing import Any, Callable, TextIO

from .base import (
    IncompatibleToolError,
    LogCallback,
    PipelineAdapter,
    PipelineError,
    ToolNotFoundError,
)
from .models import (
    Artifact,
    ArtifactManifest,
    RunRequest,
    RunResult,
    ToolInfo,
    file_sha256,
)


GENCASE_EXECUTABLE = "GenCase_win64.exe"
GENCASE_CONFIG = "DsphConfig.xml"
SUPPORTED_VERSION = "5.4"
_VERSION_RE = re.compile(r"\bGenCase\s+v?([0-9]+(?:\.[0-9]+)+)", re.IGNORECASE)
_FLOAT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _number(value: str) -> int:
    return int(value.replace(",", ""))


def _read_output_text(path: Path) -> str:
    data = path.read_bytes()
    encodings = ("utf-8-sig", locale.getpreferredencoding(False), "cp1252")
    for encoding in dict.fromkeys(encodings):
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def parse_case_out(source: str | os.PathLike[str]) -> dict[str, Any]:
    """Parse useful, stable facts from a GenCase ``Case.out`` report.

    ``source`` may be a path or the report text itself.  Unrecognised lines are
    intentionally ignored so this parser remains forward-compatible.
    """

    if isinstance(source, os.PathLike):
        text = _read_output_text(Path(source))
    else:
        candidate = str(source)
        if "\n" not in candidate and "\r" not in candidate:
            try:
                path = Path(candidate)
                text = _read_output_text(path) if path.is_file() else candidate
            except OSError:
                text = candidate
        else:
            text = candidate

    summary: dict[str, Any] = {
        "particle_counts": {},
        "particle_limits": {},
        "marker_blocks": [],
        "empty_shape_files": [],
        "warnings": [],
    }

    def first(pattern: str, flags: int = re.IGNORECASE | re.MULTILINE):
        return re.search(pattern, text, flags)

    version_match = _VERSION_RE.search(text)
    if version_match:
        summary["gencase_version"] = version_match.group(1)

    scalar_patterns: tuple[tuple[str, str, Callable[[str], Any]], ...] = (
        ("omp_threads", r"^OmpThreads:\s*(\d+)", int),
        ("dp", rf"Distance between points \(Dp\):\s*({_FLOAT})", float),
        ("points_loaded", r"Points loaded:\s*([\d,]+)", _number),
        ("total_particles", r"Total particles:\s*([\d,]+)", _number),
        ("total_mk_blocks", r"Total MK blocks:\s*([\d,]+)", _number),
        ("maximum_depth", rf"Maximum depth:\s*({_FLOAT})", float),
        ("execution_seconds", rf"Time of execution:\s*({_FLOAT})\s*sec", float),
    )
    for key, pattern, converter in scalar_patterns:
        match = first(pattern)
        if match:
            summary[key] = converter(match.group(1))

    limits_match = first(
        rf"Case limits:\s*\(\s*({_FLOAT})\s*,\s*({_FLOAT})\s*,\s*({_FLOAT})\s*\)"
        rf"\s*-\s*\(\s*({_FLOAT})\s*,\s*({_FLOAT})\s*,\s*({_FLOAT})\s*\)"
    )
    if limits_match:
        values = [float(item) for item in limits_match.groups()]
        summary["case_limits"] = {"min": values[:3], "max": values[3:]}

    domain_match = first(
        r"Domain points:\s*([\d,]+)\s*x\s*([\d,]+)\s*x\s*([\d,]+)\s*=\s*([\d,]+)"
    )
    if domain_match:
        values = [_number(item) for item in domain_match.groups()]
        summary["domain_points"] = {
            "x": values[0],
            "y": values[1],
            "z": values[2],
            "total": values[3],
        }

    for match in re.finditer(
        r"^\s*(Fixed|Moving|Floating|Fluid)\.*:\s*([\d,]+)",
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        summary["particle_counts"][match.group(1).lower()] = _number(match.group(2))

    total_detail = first(
        r"Total particles:\s*[\d,]+\s*\(bound=([\d,]+)\s*"
        r"\(fx=([\d,]+)\s+mv=([\d,]+)\s+ft=([\d,]+)\)\s+fluid=([\d,]+)\)"
    )
    if total_detail:
        values = [_number(item) for item in total_detail.groups()]
        summary["particle_counts"].update(
            {
                "boundary": values[0],
                "fixed": values[1],
                "moving": values[2],
                "floating": values[3],
                "fluid": values[4],
            }
        )

    for axis, low, high in re.findall(
        rf"^\s*([XYZ]) range:\s*({_FLOAT})\s+to\s+({_FLOAT})\s*\[m\]",
        text,
        re.IGNORECASE | re.MULTILINE,
    ):
        summary["particle_limits"][axis.lower()] = [float(low), float(high)]

    category: str | None = None
    category_re = re.compile(r"^Points ->\s*([A-Za-z-]+)\s+particles:", re.IGNORECASE)
    block_re = re.compile(
        r"^\s*block\[\s*(\d+)\]\s*\(mk:\s*(\d+)\):\s*"
        r"([\d,]+)\s*-->\s*([\d,]+)",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        category_match = category_re.search(line)
        if category_match:
            category = category_match.group(1).lower()
            continue
        block_match = block_re.search(line)
        if block_match:
            summary["marker_blocks"].append(
                {
                    "category": category,
                    "block": int(block_match.group(1)),
                    "mk": int(block_match.group(2)),
                    "points_before": _number(block_match.group(3)),
                    "points_after": _number(block_match.group(4)),
                }
            )

    empty_re = re.compile(
        r"FileShapes>\s*(\S+)\s*<----\s*EMPTY FILE NOT SAVED", re.IGNORECASE
    )
    summary["empty_shape_files"] = empty_re.findall(text)

    warnings: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and re.search(r"\bWARN(?:ING)?\b", stripped, re.IGNORECASE):
            warnings.append(stripped)
    summary["warnings"] = warnings

    if not summary["particle_counts"]:
        summary.pop("particle_counts")
    if not summary["particle_limits"]:
        summary.pop("particle_limits")
    if not summary["marker_blocks"]:
        summary.pop("marker_blocks")
    if not summary["empty_shape_files"]:
        summary.pop("empty_shape_files")
    if not summary["warnings"]:
        summary.pop("warnings")
    return summary


# Backwards-readable name for callers that do not know the report filename.
parse_gencase_output = parse_case_out


class GenCaseAdapter(PipelineAdapter):
    """Standard-library adapter for the GenCase stage of DualSPHysics."""

    stage = "gencase"

    def __init__(
        self,
        dualsphysics_root: str | os.PathLike[str] | None = None,
        *,
        executable: str | os.PathLike[str] | None = None,
        expected_version: str = SUPPORTED_VERSION,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        version_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.dualsphysics_root = (
            Path(dualsphysics_root).expanduser() if dualsphysics_root else None
        )
        self.executable = Path(executable).expanduser() if executable else None
        self.expected_version = expected_version
        self._popen_factory = popen_factory or subprocess.Popen
        self._version_runner = version_runner or subprocess.run
        self._current_process: subprocess.Popen[str] | None = None
        self._process_lock = Lock()
        self._cancel_event = Event()

    @staticmethod
    def _root_for_executable(executable: Path) -> Path | None:
        parent = executable.parent
        if parent.name.lower() == "windows" and parent.parent.name.lower() == "bin":
            return parent.parent.parent
        return None

    @classmethod
    def _normalise_root_candidate(cls, candidate: Path) -> Path | None:
        path = candidate.expanduser()
        if path.name.lower() == GENCASE_EXECUTABLE.lower():
            return cls._root_for_executable(path)
        if (path / "bin" / "windows" / GENCASE_EXECUTABLE).is_file():
            return path
        if path.name.lower() == "windows" and (path / GENCASE_EXECUTABLE).is_file():
            if path.parent.name.lower() == "bin":
                return path.parent.parent
        return None

    @classmethod
    def discover_root(
        cls,
        source_xml: str | os.PathLike[str] | None = None,
        configured_root: str | os.PathLike[str] | None = None,
    ) -> Path | None:
        """Locate a DualSPHysics root from operator-controlled configuration.

        ``source_xml`` remains in the public signature for compatibility, but
        case paths and the process working directory are intentionally never
        searched for executables.
        """

        direct_candidates: list[Path] = []
        if configured_root:
            direct_candidates.append(Path(configured_root))
        environment_root = os.environ.get("DUALSPHYSICS_ROOT")
        if environment_root:
            direct_candidates.append(Path(environment_root))

        for candidate in direct_candidates:
            root = cls._normalise_root_candidate(candidate)
            if root is not None:
                return root.resolve()
        return None

    @staticmethod
    def extract_version(output: str) -> str | None:
        match = _VERSION_RE.search(output)
        return match.group(1) if match else None

    def probe(self, source_xml: str | Path | None = None) -> ToolInfo:
        diagnostics: list[str] = []
        root: Path | None
        executable: Path | None

        if self.executable is not None:
            executable = self.executable.resolve()
            root = self._root_for_executable(executable)
            if root is None and self.dualsphysics_root is not None:
                root = self.dualsphysics_root.resolve()
        else:
            root = self.discover_root(source_xml, self.dualsphysics_root)
            if root is None and self.dualsphysics_root is not None:
                root = self.dualsphysics_root.resolve()
            executable = root / "bin" / "windows" / GENCASE_EXECUTABLE if root else None

        config_path = executable.parent / GENCASE_CONFIG if executable else None
        executable_exists = bool(executable and executable.is_file())
        config_exists = bool(config_path and config_path.is_file())

        if not executable_exists:
            diagnostics.append(f"未找到 {GENCASE_EXECUTABLE}")
        if executable_exists and not config_exists:
            diagnostics.append(f"未找到与 GenCase 相邻的 {GENCASE_CONFIG}")

        version: str | None = None
        if executable_exists and executable is not None:
            try:
                completed = self._version_runner(
                    [str(executable), "-ver"],
                    cwd=str(executable.parent),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding=locale.getpreferredencoding(False),
                    errors="replace",
                    timeout=10,
                    shell=False,
                    check=False,
                )
                standard_output = completed.stdout or ""
                standard_error = completed.stderr or ""
                if isinstance(standard_output, bytes):
                    standard_output = standard_output.decode(errors="replace")
                if isinstance(standard_error, bytes):
                    standard_error = standard_error.decode(errors="replace")
                version = self.extract_version(f"{standard_output}\n{standard_error}")
                # Some official builds return 1 for -ver despite printing a
                # valid version, so the parsed banner is authoritative here.
                if version is None:
                    diagnostics.append("GenCase 未返回可识别的版本信息")
            except (OSError, subprocess.SubprocessError) as error:
                diagnostics.append(f"无法读取 GenCase 版本: {error}")

        compatible = bool(
            version
            and (version == self.expected_version or version.startswith(f"{self.expected_version}."))
        )
        if version and not compatible:
            diagnostics.append(
                f"GenCase {version} 与本程序要求的 {self.expected_version} 不兼容"
            )

        return ToolInfo(
            name="GenCase",
            root=root,
            executable=executable,
            config_path=config_path,
            version=version,
            available=executable_exists and config_exists,
            compatible=compatible,
            diagnostics=tuple(diagnostics),
        )

    # Friendly alias for setup dialogs.
    locate_tool = probe

    @staticmethod
    def case_name(source_xml: str | os.PathLike[str]) -> str:
        stem = Path(source_xml).stem
        return stem[:-4] if stem.lower().endswith("_def") else stem

    @classmethod
    def default_output_prefix(cls, source_xml: str | os.PathLike[str]) -> Path:
        source = Path(source_xml)
        name = cls.case_name(source)
        return source.parent / f"{name}_out" / name

    @staticmethod
    def output_directory_nonempty(output_prefix: str | os.PathLike[str]) -> bool:
        directory = Path(output_prefix).parent
        try:
            return directory.is_dir() and next(directory.iterdir(), None) is not None
        except OSError:
            # Treat unreadable directories as occupied; never suggest that the
            # UI overwrite a location it could not inspect.
            return directory.exists()

    @classmethod
    def suggest_output_prefix(
        cls,
        source_xml: str | os.PathLike[str],
        requested: str | os.PathLike[str] | None = None,
        *,
        now: datetime | None = None,
    ) -> Path:
        """Return a timestamped sibling directory when output is non-empty."""

        source = Path(source_xml)
        output = Path(requested) if requested is not None else cls.default_output_prefix(source)
        if not output.is_absolute():
            output = source.parent / output
        if not cls.output_directory_nonempty(output):
            return output

        timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        directory = output.parent.with_name(f"{output.parent.name}_{timestamp}")
        candidate = directory / output.name
        suffix = 2
        while candidate.parent.exists():
            candidate = directory.with_name(f"{directory.name}-{suffix:02d}") / output.name
            suffix += 1
        return candidate

    def _resolve_request(self, request: RunRequest) -> RunRequest:
        source = request.source_xml.expanduser().resolve()
        if request.output_prefix is None:
            output = self.default_output_prefix(source)
            if request.use_timestamp_on_conflict:
                output = self.suggest_output_prefix(source, output)
        else:
            output = request.output_prefix.expanduser()
            if not output.is_absolute():
                output = source.parent / output
        output = output.resolve()

        manifest_path = request.manifest_path
        if manifest_path is not None:
            manifest_path = manifest_path.expanduser()
            if not manifest_path.is_absolute():
                manifest_path = output.parent / manifest_path
            manifest_path = manifest_path.resolve()
        return replace(
            request,
            source_xml=source,
            output_prefix=output,
            manifest_path=manifest_path,
        )

    def _require_tool(self, tool_info: ToolInfo) -> None:
        if not tool_info.available or tool_info.executable is None:
            detail = "; ".join(tool_info.diagnostics) or "GenCase 不可用"
            raise ToolNotFoundError(detail)
        if not tool_info.compatible:
            detail = "; ".join(tool_info.diagnostics) or "GenCase 版本不兼容"
            raise IncompatibleToolError(detail)

    @staticmethod
    def _validate_request(request: RunRequest, *, require_source: bool) -> None:
        if request.mode not in {"full", "quick"}:
            raise PipelineError("mode 必须是 'full' 或 'quick'")
        if request.source_xml.suffix.lower() != ".xml":
            raise PipelineError("GenCase 输入必须是 XML 文件")
        if require_source and not request.source_xml.is_file():
            raise PipelineError(f"输入 XML 不存在: {request.source_xml}")
        if request.timeout is not None and request.timeout <= 0:
            raise PipelineError("timeout 必须大于 0")

    def build_command(
        self, request: RunRequest, tool_info: ToolInfo | None = None
    ) -> tuple[str, ...]:
        resolved = self._resolve_request(request)
        self._validate_request(resolved, require_source=False)
        tool = tool_info or self.probe(resolved.source_xml)
        self._require_tool(tool)
        assert tool.executable is not None
        assert resolved.output_prefix is not None

        working_directory = resolved.source_xml.parent
        try:
            relative_output = resolved.output_prefix.relative_to(working_directory)
            output_argument = str(relative_output)
        except ValueError:
            output_argument = str(resolved.output_prefix)

        command = [
            str(tool.executable),
            resolved.source_xml.stem,
            output_argument,
        ]
        if resolved.mode == "full":
            command.append("-save:all")
        # GenCase v5.4's documented defaults are bi, vtkcells and vtkfreept.
        # Leaving -save unspecified is therefore the safest lightweight check:
        # it produces Case_MkCells.vtk plus the required Case.bi4/Case.xml and
        # does not rely on an undocumented combination syntax.
        if resolved.create_dirs:
            command.append("-createdirs:1")
        command.extend(resolved.extra_args)
        return tuple(command)

    @staticmethod
    def _artifact_role(path: Path, prefix: Path) -> tuple[str, bool]:
        lower_name = path.name.lower()
        if path == prefix.with_suffix(".bi4"):
            return "case_binary", True
        if path == prefix.with_suffix(".xml"):
            return "resolved_xml", True
        if path == prefix.with_suffix(".out"):
            return "gencase_log", False
        if path.suffix.lower() == ".vtk":
            if lower_name.endswith("_all.vtk"):
                return "vtk_all", False
            if lower_name.endswith("_bound.vtk"):
                return "vtk_bound", False
            if lower_name.endswith("_fluid.vtk"):
                return "vtk_fluid", False
            if lower_name.endswith("_mkcells.vtk"):
                return "vtk_mkcells", False
            if lower_name.endswith("_dp.vtk"):
                return "shape_vtk", False
            return "vtk", False
        return "other", False

    @classmethod
    def collect_artifacts(cls, output_prefix: str | os.PathLike[str]) -> list[Artifact]:
        prefix = Path(output_prefix).resolve()
        candidates: set[Path] = set()
        if prefix.parent.is_dir():
            candidates.update(path for path in prefix.parent.glob(f"{prefix.name}*") if path.is_file())
            candidates.update(path for path in prefix.parent.glob("CfgInit_*.vtk") if path.is_file())
        artifacts: list[Artifact] = []
        for path in sorted(candidates, key=lambda item: item.name.lower()):
            if path.name.lower().endswith(".manifest.json"):
                continue
            role, required = cls._artifact_role(path.resolve(), prefix)
            artifacts.append(
                Artifact.from_path(
                    path,
                    role,
                    required=required,
                    calculate_hash=required,
                )
            )
        return artifacts

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            pass

    def cancel(self) -> bool:
        with self._process_lock:
            process = self._current_process
        if process is None or process.poll() is not None:
            return False
        self._cancel_event.set()
        try:
            process.terminate()
            return True
        except OSError:
            return False

    def _attach_manifest(
        self,
        result: RunResult,
        request: RunRequest,
        tool_info: ToolInfo | None,
    ) -> None:
        source_hash = ""
        try:
            source_hash = file_sha256(request.source_xml)
        except OSError:
            pass
        manifest = ArtifactManifest(
            stage=self.stage,
            tool_name="GenCase",
            tool_version=tool_info.version if tool_info else None,
            source_path=request.source_xml,
            source_sha256=source_hash,
            output_prefix=result.output_prefix,
            command=result.command,
            started_at=result.started_at,
            finished_at=result.finished_at,
            return_code=result.return_code,
            status=result.status,
            parameters={
                "mode": request.mode,
                "create_dirs": request.create_dirs,
                "extra_args": list(request.extra_args),
            },
            summary=result.summary,
            warnings=list(result.warnings),
            errors=list(result.errors),
            artifacts=list(result.artifacts),
        )
        target = request.manifest_path or result.output_prefix.with_name(
            f"{result.output_prefix.name}.manifest.json"
        )
        try:
            result.manifest_path = manifest.write(target)
        except OSError as error:
            result.warnings.append(f"无法写入运行清单: {error}")
        result.manifest = manifest

    def _early_result(
        self,
        request: RunRequest,
        *,
        started_at: str,
        status: str,
        errors: list[str] | None = None,
        cancelled: bool = False,
        tool_info: ToolInfo | None = None,
        command: tuple[str, ...] = (),
        write_manifest: bool = False,
    ) -> RunResult:
        assert request.output_prefix is not None
        result = RunResult(
            status=status,
            return_code=None,
            command=command,
            working_directory=request.source_xml.parent,
            output_prefix=request.output_prefix,
            started_at=started_at,
            finished_at=_utc_now(),
            errors=errors or [],
            cancelled=cancelled,
        )
        if write_manifest:
            self._attach_manifest(result, request, tool_info)
        return result

    def run(
        self,
        request: RunRequest,
        *,
        on_log: LogCallback | None = None,
        cancel_event: Event | None = None,
    ) -> RunResult:
        """Run GenCase synchronously without invoking a command shell."""

        started_at = _utc_now()
        resolved = self._resolve_request(request)
        assert resolved.output_prefix is not None
        external_cancel = cancel_event or Event()
        self._cancel_event.clear()

        if external_cancel.is_set():
            return self._early_result(
                resolved,
                started_at=started_at,
                status="cancelled",
                cancelled=True,
            )

        try:
            self._validate_request(resolved, require_source=True)
            tool_info = self.probe(resolved.source_xml)
            command = self.build_command(resolved, tool_info)
        except PipelineError as error:
            return self._early_result(
                resolved,
                started_at=started_at,
                status="failed",
                errors=[str(error)],
            )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        combined_lines: list[str] = []
        callback_errors: list[str] = []
        output_lock = Lock()
        encoding = locale.getpreferredencoding(False)

        try:
            process = self._popen_factory(
                list(command),
                cwd=str(resolved.source_xml.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding=encoding,
                errors="replace",
                bufsize=1,
                shell=False,
            )
        except OSError as error:
            result = self._early_result(
                resolved,
                started_at=started_at,
                status="failed",
                errors=[f"无法启动 GenCase: {error}"],
                tool_info=tool_info,
                command=command,
                write_manifest=True,
            )
            return result

        with self._process_lock:
            self._current_process = process

        def pump(stream: TextIO | None, channel: str, destination: list[str]) -> None:
            if stream is None:
                return
            try:
                for raw_line in iter(stream.readline, ""):
                    line = raw_line.rstrip("\r\n")
                    with output_lock:
                        destination.append(line)
                        combined_lines.append(line)
                    if on_log is not None:
                        try:
                            on_log(channel, line)
                        except Exception as error:  # UI callbacks must not kill GenCase.
                            with output_lock:
                                callback_errors.append(str(error))
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        reader_threads = [
            Thread(target=pump, args=(process.stdout, "stdout", stdout_lines), daemon=True),
            Thread(target=pump, args=(process.stderr, "stderr", stderr_lines), daemon=True),
        ]
        for thread in reader_threads:
            thread.start()

        cancelled = False
        timed_out = False
        monotonic_start = time.monotonic()
        try:
            while process.poll() is None:
                if external_cancel.is_set() or self._cancel_event.is_set():
                    cancelled = True
                    self._terminate(process)
                    break
                if (
                    resolved.timeout is not None
                    and time.monotonic() - monotonic_start >= resolved.timeout
                ):
                    timed_out = True
                    self._terminate(process)
                    break
                time.sleep(0.03)
            # ``cancel()`` terminates from another thread.  The process may
            # therefore leave the loop through poll() before the body observes
            # the token; retain cancellation as the user-visible outcome.
            if external_cancel.is_set() or self._cancel_event.is_set():
                cancelled = True
            try:
                return_code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._terminate(process)
                return_code = process.poll()
        finally:
            with self._process_lock:
                self._current_process = None
            for thread in reader_threads:
                thread.join(timeout=2)

        stdout = "\n".join(stdout_lines)
        stderr = "\n".join(stderr_lines)
        artifacts = self.collect_artifacts(resolved.output_prefix)
        required_paths = (
            resolved.output_prefix.with_suffix(".bi4"),
            resolved.output_prefix.with_suffix(".xml"),
        )

        errors: list[str] = []
        warnings: list[str] = []
        if timed_out:
            errors.append(f"GenCase 超时（{resolved.timeout:g} 秒）")
        elif not cancelled and return_code != 0:
            errors.append(f"GenCase 退出码为 {return_code}")
        if not cancelled:
            for required_path in required_paths:
                if not required_path.is_file():
                    errors.append(f"缺少必需产物: {required_path.name}")

        report_path = resolved.output_prefix.with_suffix(".out")
        if report_path.is_file():
            summary = parse_case_out(report_path)
        else:
            summary = parse_case_out("\n".join(combined_lines))
        warnings.extend(str(item) for item in summary.get("warnings", ()))
        for filename in summary.get("empty_shape_files", ()):
            warnings.append(f"shapeout 为空，未保存文件: {filename}")
        if callback_errors:
            warnings.append("日志回调发生异常；GenCase 运行未受影响")

        if cancelled:
            status = "cancelled"
        elif return_code == 0 and not errors:
            status = "succeeded"
        else:
            status = "failed"

        result = RunResult(
            status=status,
            return_code=return_code,
            command=command,
            working_directory=resolved.source_xml.parent,
            output_prefix=resolved.output_prefix,
            started_at=started_at,
            finished_at=_utc_now(),
            stdout=stdout,
            stderr=stderr,
            warnings=warnings,
            errors=errors,
            summary=summary,
            artifacts=artifacts,
            cancelled=cancelled,
            timed_out=timed_out,
        )
        self._attach_manifest(result, resolved, tool_info)
        return result
