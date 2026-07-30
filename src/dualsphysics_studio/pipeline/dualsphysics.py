"""DualSPHysics GPU execution and PartVTK conversion adapters."""

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
from typing import Callable, TextIO

from .base import (
    IncompatibleToolError,
    LogCallback,
    PipelineAdapter,
    PipelineError,
    ToolNotFoundError,
)
from .gencase import GenCaseAdapter, SUPPORTED_VERSION
from .models import (
    Artifact,
    ArtifactManifest,
    RunRequest,
    RunResult,
    ToolInfo,
    file_sha256,
)


DUALSPHYSICS_CPU_EXECUTABLE = "DualSPHysics5.4CPU_win64.exe"
DUALSPHYSICS_GPU_EXECUTABLE = "DualSPHysics5.4_win64.exe"
PARTVTK_EXECUTABLE = "PartVTK_win64.exe"
PARTVTK_VARIABLES = "-vars:-all,+vel,+rhop,+press,+idp,+mk,+type"

_GPU_VERSION_RE = re.compile(
    r"\bDualSPHysics5\s+v?([0-9]+(?:\.[0-9]+)+)", re.IGNORECASE
)
_PARTVTK_VERSION_RE = re.compile(
    r"\bPartVTK\s+v?([0-9]+(?:\.[0-9]+)+)", re.IGNORECASE
)
_FINISHED_RE = re.compile(r"Finished\s+execution\s*\(\s*code\s*=\s*0\s*\)", re.IGNORECASE)
_PART_BINARY_RE = re.compile(r"^Part_(\d{4})\.bi4$", re.IGNORECASE)
_PART_VTK_RE = re.compile(r"^PartAll_(\d{4})\.vtk$", re.IGNORECASE)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in dict.fromkeys(
        ("utf-8-sig", locale.getpreferredencoding(False), "cp1252")
    ):
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _nonempty_files(directory: Path, pattern: re.Pattern[str]) -> list[Path]:
    if not directory.is_dir():
        return []
    files: list[Path] = []
    for path in directory.iterdir():
        try:
            if path.is_file() and path.stat().st_size > 0 and pattern.fullmatch(path.name):
                files.append(path.resolve())
        except OSError:
            continue
    return sorted(files, key=lambda item: item.name.lower())


def _frame_ids(files: list[Path], pattern: re.Pattern[str]) -> set[str]:
    return {
        match.group(1)
        for path in files
        if (match := pattern.fullmatch(path.name)) is not None
    }


def _parse_gpu_report(text: str) -> tuple[dict[str, object], list[str]]:
    summary: dict[str, object] = {}
    warnings: list[str] = []
    in_warnings = False
    patterns = (
        ("total_runtime_seconds", r"^Total Runtime\.*:\s*([0-9.,]+)\s*sec"),
        ("simulation_runtime_seconds", r"^Simulation Runtime\.*:\s*([0-9.,]+)\s*sec"),
        ("steps", r"^Steps of simulation\.*:\s*([0-9,]+)"),
        ("reported_part_files", r"^PART files\.*:\s*([0-9,]+)"),
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "[WARNINGS]":
            in_warnings = True
            continue
        if in_warnings and not line:
            in_warnings = False
            continue
        warning_match = re.match(r"^\*\*\* WARNING:\s*(.+)$", line)
        if warning_match is None and in_warnings:
            warning_match = re.match(r"^\d+\.\s*(.+)$", line)
        if warning_match is not None:
            warning = warning_match.group(1).strip()
            if warning and warning not in warnings:
                warnings.append(warning)

        device_match = re.match(r"^Device default:\s*(.+)$", line)
        if device_match is not None:
            summary["device"] = device_match.group(1).strip()
        particle_match = re.match(r"^CaseNp\s*=\s*([\d,]+)", line, re.IGNORECASE)
        if particle_match is not None:
            summary["particles"] = int(particle_match.group(1).replace(",", ""))
        requested_memory = re.search(
            r"Requested\s+gpu\s+memory\s+for\s+[\d,]+\s+particles:\s*"
            r"([0-9.,]+)\s*(B|KB|MB|GB)\b",
            line,
            re.IGNORECASE,
        )
        if requested_memory is not None:
            summary["requested_gpu_memory_bytes"] = _memory_bytes(
                requested_memory.group(1), requested_memory.group(2)
            )
        allocated_memory = re.match(
            r"^Allocated\s+memory\s+in\s+(CPU|GPU):\s*([\d,]+)"
            r"(?:\s*\(\s*([0-9.,]+)\s*(B|KB|MB|GB)\s*\))?",
            line,
            re.IGNORECASE,
        )
        if allocated_memory is not None:
            raw_bytes = int(allocated_memory.group(2).replace(",", ""))
            value = (
                _memory_bytes(allocated_memory.group(3), allocated_memory.group(4))
                if allocated_memory.group(3) and allocated_memory.group(4)
                else raw_bytes
            )
            key = f"allocated_{allocated_memory.group(1).casefold()}_memory_bytes"
            summary[key] = value
        for key, pattern in patterns:
            match = re.match(pattern, line, re.IGNORECASE)
            if match is None:
                continue
            value = match.group(1).replace(",", "")
            summary[key] = float(value) if key.endswith("_seconds") else int(value)
            break
    return summary, warnings


def _memory_bytes(value: str, unit: str) -> int:
    number = float(value.replace(",", ""))
    factor = {
        "b": 1,
        "kb": 1024,
        "mb": 1024 * 1024,
        "gb": 1024 * 1024 * 1024,
    }[unit.casefold()]
    return int(round(number * factor))


class _DualSPHysicsExecutableAdapter(PipelineAdapter):
    """Shared shell-free runner for one official v5.4 executable."""

    stage = ""
    tool_name = ""
    executable_name = ""
    version_pattern: re.Pattern[str]
    manifest_suffix = ""

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
    def extract_version(cls, output: str) -> str | None:
        match = cls.version_pattern.search(output)
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
            root = GenCaseAdapter.discover_root(source_xml, self.dualsphysics_root)
            if root is None and self.dualsphysics_root is not None:
                root = self.dualsphysics_root.resolve()
            executable = root / "bin" / "windows" / self.executable_name if root else None

        available = bool(executable and executable.is_file())
        if not available:
            diagnostics.append(f"未找到 {self.executable_name}")

        version: str | None = None
        if available and executable is not None:
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
                stdout = completed.stdout or ""
                stderr = completed.stderr or ""
                if isinstance(stdout, bytes):
                    stdout = stdout.decode(errors="replace")
                if isinstance(stderr, bytes):
                    stderr = stderr.decode(errors="replace")
                version = self.extract_version(f"{stdout}\n{stderr}")
                # Official v5.4 tools commonly return 1 for ``-ver``.
                if version is None:
                    diagnostics.append(f"{self.tool_name} 未返回可识别的版本信息")
            except (OSError, subprocess.SubprocessError) as error:
                diagnostics.append(f"无法读取 {self.tool_name} 版本: {error}")

        compatible = bool(
            version
            and (
                version == self.expected_version
                or version.startswith(f"{self.expected_version}.")
            )
        )
        if version and not compatible:
            diagnostics.append(
                f"{self.tool_name} {version} 与本程序要求的 {self.expected_version} 不兼容"
            )
        return ToolInfo(
            name=self.tool_name,
            root=root,
            executable=executable,
            version=version,
            available=available,
            compatible=compatible,
            diagnostics=tuple(diagnostics),
        )

    locate_tool = probe

    def _default_output_prefix(self, source_xml: Path) -> Path:
        raise NotImplementedError

    def _resolve_request(self, request: RunRequest) -> RunRequest:
        source = request.source_xml.expanduser().resolve()
        output = request.output_prefix
        if output is None:
            resolved_output = self._default_output_prefix(source)
        else:
            resolved_output = output.expanduser()
            if not resolved_output.is_absolute():
                resolved_output = source.parent / resolved_output
        resolved_output = resolved_output.resolve()

        manifest_path = request.manifest_path
        if manifest_path is not None:
            manifest_path = manifest_path.expanduser()
            if not manifest_path.is_absolute():
                manifest_path = resolved_output.parent / manifest_path
            manifest_path = manifest_path.resolve()
        return replace(
            request,
            source_xml=source,
            output_prefix=resolved_output,
            manifest_path=manifest_path,
        )

    def _require_tool(self, tool_info: ToolInfo) -> None:
        if not tool_info.available or tool_info.executable is None:
            detail = "; ".join(tool_info.diagnostics) or f"{self.tool_name} 不可用"
            raise ToolNotFoundError(detail)
        if not tool_info.compatible:
            detail = "; ".join(tool_info.diagnostics) or f"{self.tool_name} 版本不兼容"
            raise IncompatibleToolError(detail)

    def _validate_common(self, request: RunRequest, *, require_source: bool) -> None:
        if request.source_xml.suffix.lower() != ".xml":
            raise PipelineError("源文件必须是 XML 文件")
        if require_source and not request.source_xml.is_file():
            raise PipelineError(f"输入 XML 不存在: {request.source_xml}")
        if request.timeout is not None and request.timeout <= 0:
            raise PipelineError("timeout 必须大于 0")

    def _validate_request(self, request: RunRequest, *, preflight: bool) -> None:
        raise NotImplementedError

    def _command_arguments(self, request: RunRequest) -> list[str]:
        raise NotImplementedError

    def build_command(
        self, request: RunRequest, tool_info: ToolInfo | None = None
    ) -> tuple[str, ...]:
        resolved = self._resolve_request(request)
        self._validate_common(resolved, require_source=False)
        self._validate_request(resolved, preflight=False)
        tool = tool_info or self.probe(resolved.source_xml)
        self._require_tool(tool)
        assert tool.executable is not None
        return tuple([str(tool.executable), *self._command_arguments(resolved)])

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

    def _collect_and_validate(
        self, request: RunRequest, return_code: int | None
    ) -> tuple[list[Artifact], dict[str, object], list[str], list[str]]:
        raise NotImplementedError

    def _attach_manifest(
        self,
        result: RunResult,
        request: RunRequest,
        tool_info: ToolInfo | None,
    ) -> None:
        try:
            source_hash = file_sha256(request.source_xml)
        except OSError:
            source_hash = ""
        manifest = ArtifactManifest(
            stage=self.stage,
            tool_name=self.tool_name,
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
            f"{result.output_prefix.name}.{self.manifest_suffix}.manifest.json"
        )
        if target.exists():
            if request.manifest_path is not None:
                result.warnings.append(f"运行清单已存在，未覆盖: {target}")
                result.manifest = manifest
                return
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            base_name = f"{result.output_prefix.name}.{stamp}"
            target = target.with_name(
                f"{base_name}.{self.manifest_suffix}.manifest.json"
            )
            counter = 2
            while target.exists():
                target = target.with_name(
                    f"{base_name}-{counter:02d}.{self.manifest_suffix}.manifest.json"
                )
                counter += 1
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
        """Run one official tool directly, with no batch file or shell."""

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

        tool_info: ToolInfo | None = None
        command: tuple[str, ...] = ()
        try:
            self._validate_common(resolved, require_source=True)
            self._validate_request(resolved, preflight=True)
            tool_info = self.probe(resolved.source_xml)
            command = self.build_command(resolved, tool_info)
            if resolved.create_dirs:
                resolved.output_prefix.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, PipelineError) as error:
            return self._early_result(
                resolved,
                started_at=started_at,
                status="failed",
                errors=[str(error)],
                tool_info=tool_info,
                command=command,
                # A safety preflight must leave an occupied output tree fully
                # untouched, including any manifest from its successful run.
                write_manifest=False,
            )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
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
            return self._early_result(
                resolved,
                started_at=started_at,
                status="failed",
                errors=[f"无法启动 {self.tool_name}: {error}"],
                tool_info=tool_info,
                command=command,
                write_manifest=True,
            )

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
                    if on_log is not None:
                        try:
                            on_log(channel, line)
                        except Exception as error:
                            with output_lock:
                                callback_errors.append(str(error))
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        readers = [
            Thread(target=pump, args=(process.stdout, "stdout", stdout_lines), daemon=True),
            Thread(target=pump, args=(process.stderr, "stderr", stderr_lines), daemon=True),
        ]
        for reader in readers:
            reader.start()

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
            for reader in readers:
                reader.join(timeout=2)

        artifacts, summary, warnings, product_errors = self._collect_and_validate(
            resolved, return_code
        )
        errors: list[str] = []
        if timed_out:
            errors.append(f"{self.tool_name} 超时（{resolved.timeout:g} 秒）")
        elif not cancelled and return_code != 0:
            errors.append(f"{self.tool_name} 退出码为 {return_code}")
        if not cancelled:
            errors.extend(product_errors)
        if callback_errors:
            warnings.append(f"日志回调发生异常；{self.tool_name} 运行未受影响")

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
            stdout="\n".join(stdout_lines),
            stderr="\n".join(stderr_lines),
            warnings=warnings,
            errors=errors,
            summary=summary,
            artifacts=artifacts,
            cancelled=cancelled,
            timed_out=timed_out,
        )
        self._attach_manifest(result, resolved, tool_info)
        return result


class DualSPHysicsGPUAdapter(_DualSPHysicsExecutableAdapter):
    """Run the v5.4 GPU solver against an existing GenCase prefix."""

    stage = "gpu"
    tool_name = "DualSPHysics GPU"
    executable_name = DUALSPHYSICS_GPU_EXECUTABLE
    version_pattern = _GPU_VERSION_RE
    manifest_suffix = "gpu"
    run_mode = "gpu"
    solver_label = "GPU"

    def _default_output_prefix(self, source_xml: Path) -> Path:
        return GenCaseAdapter.default_output_prefix(source_xml)

    def _validate_request(self, request: RunRequest, *, preflight: bool) -> None:
        if request.mode not in {"full", self.run_mode}:
            raise PipelineError(f"{self.solver_label} 求解 mode 必须是 '{self.run_mode}'")
        assert request.output_prefix is not None
        if not preflight:
            return
        missing = [
            path
            for path in (
                request.output_prefix.with_suffix(".xml"),
                request.output_prefix.with_suffix(".bi4"),
            )
            if not path.is_file() or path.stat().st_size == 0
        ]
        if missing:
            raise PipelineError(
                f"{self.solver_label} 求解缺少 GenCase 输入: "
                + ", ".join(path.name for path in missing)
            )

        output_directory = request.output_prefix.parent
        data_directory = output_directory / "data"
        conflicts: list[Path] = []
        if data_directory.exists():
            conflicts.append(data_directory)
        if output_directory.is_dir():
            conflicts.extend(
                path
                for path in output_directory.iterdir()
                if path.is_file()
                and (
                    path.name.lower() == "run.out"
                    or (
                        path.name.lower().startswith("run")
                        and path.suffix.lower() == ".csv"
                    )
                )
            )
        vtk_directory = output_directory / "vtk"
        if vtk_directory.is_dir():
            conflicts.extend(
                path
                for path in vtk_directory.iterdir()
                if path.is_file()
                and path.name.lower().startswith("partall_")
                and path.suffix.lower() == ".vtk"
            )
        if conflicts:
            names = ", ".join(sorted({path.name for path in conflicts}))
            raise PipelineError(
                f"发现已有 {self.solver_label} 求解结果，已阻止覆盖: {names}"
            )

    def _command_arguments(self, request: RunRequest) -> list[str]:
        assert request.output_prefix is not None
        return [
            "-gpu",
            str(request.output_prefix),
            str(request.output_prefix.parent),
            *request.extra_args,
        ]

    def _collect_and_validate(
        self, request: RunRequest, return_code: int | None
    ) -> tuple[list[Artifact], dict[str, object], list[str], list[str]]:
        assert request.output_prefix is not None
        output_directory = request.output_prefix.parent
        data_directory = output_directory / "data"
        run_report = output_directory / "Run.out"
        part_head = data_directory / "Part_Head.ibi4"
        part_files = _nonempty_files(data_directory, _PART_BINARY_RE)

        artifacts: list[Artifact] = []
        if run_report.is_file():
            artifacts.append(Artifact.from_path(run_report, "solver_log", required=True))
        if part_head.is_file():
            artifacts.append(Artifact.from_path(part_head, "part_head", required=True))
        artifacts.extend(
            Artifact.from_path(path, "part_binary", required=True) for path in part_files
        )
        if output_directory.is_dir():
            for path in sorted(output_directory.glob("Run*.csv")):
                if path.is_file():
                    artifacts.append(Artifact.from_path(path, "solver_report"))

        errors: list[str] = []
        report_text = ""
        try:
            if run_report.is_file() and run_report.stat().st_size > 0:
                report_text = _read_text(run_report)
            else:
                errors.append("缺少非空求解日志: Run.out")
        except OSError as error:
            errors.append(f"无法读取 Run.out: {error}")
        if report_text and not _FINISHED_RE.search(report_text):
            errors.append("Run.out 未包含 Finished execution (code=0)")
        try:
            if not part_head.is_file() or part_head.stat().st_size == 0:
                errors.append("缺少非空求解产物: data/Part_Head.ibi4")
        except OSError:
            errors.append("缺少非空求解产物: data/Part_Head.ibi4")
        if not part_files:
            errors.append("缺少非空求解产物: data/Part_????.bi4")
        summary, warnings = _parse_gpu_report(report_text)
        summary["part_count"] = len(part_files)
        return artifacts, summary, warnings, errors


class DualSPHysicsCPUAdapter(DualSPHysicsGPUAdapter):
    """Run the v5.4 CPU solver against an existing GenCase prefix."""

    stage = "cpu"
    tool_name = "DualSPHysics CPU"
    executable_name = DUALSPHYSICS_CPU_EXECUTABLE
    manifest_suffix = "cpu"
    run_mode = "cpu"
    solver_label = "CPU"

    def _command_arguments(self, request: RunRequest) -> list[str]:
        assert request.output_prefix is not None
        return [
            str(request.output_prefix),
            str(request.output_prefix.parent),
            *request.extra_args,
        ]


class PartVTKAdapter(_DualSPHysicsExecutableAdapter):
    """Convert every solver Part BI4 frame to a PartAll VTK frame."""

    stage = "partvtk"
    tool_name = "PartVTK"
    executable_name = PARTVTK_EXECUTABLE
    version_pattern = _PARTVTK_VERSION_RE
    manifest_suffix = "partvtk"

    def _default_output_prefix(self, source_xml: Path) -> Path:
        case_prefix = GenCaseAdapter.default_output_prefix(source_xml)
        return case_prefix.parent / "vtk" / "PartAll"

    @staticmethod
    def _data_directory(output_prefix: Path) -> Path:
        return output_prefix.parent.parent / "data"

    def _validate_request(self, request: RunRequest, *, preflight: bool) -> None:
        if request.mode not in {"full", "partvtk"}:
            raise PipelineError("PartVTK mode 必须是 'partvtk'")
        assert request.output_prefix is not None
        if not preflight:
            return
        data_directory = self._data_directory(request.output_prefix)
        if not data_directory.is_dir():
            raise PipelineError(f"PartVTK 数据目录不存在: {data_directory}")
        part_head = data_directory / "Part_Head.ibi4"
        if not part_head.is_file() or part_head.stat().st_size == 0:
            raise PipelineError("PartVTK 缺少非空输入: data/Part_Head.ibi4")
        part_files = _nonempty_files(data_directory, _PART_BINARY_RE)
        if not part_files:
            raise PipelineError("PartVTK 缺少非空输入: data/Part_????.bi4")
        output_directory = request.output_prefix.parent
        conflicts = []
        if output_directory.is_dir():
            conflicts = [
                path
                for path in output_directory.iterdir()
                if path.is_file()
                and path.name.lower().startswith(request.output_prefix.name.lower())
                and path.suffix.lower() == ".vtk"
            ]
        if conflicts:
            names = ", ".join(sorted(path.name for path in conflicts))
            raise PipelineError(f"发现已有 PartVTK 文件，已阻止覆盖: {names}")

    def _command_arguments(self, request: RunRequest) -> list[str]:
        assert request.output_prefix is not None
        return [
            "-dirdata",
            str(self._data_directory(request.output_prefix)),
            "-savevtk",
            str(request.output_prefix),
            PARTVTK_VARIABLES,
            *request.extra_args,
        ]

    def _collect_and_validate(
        self, request: RunRequest, return_code: int | None
    ) -> tuple[list[Artifact], dict[str, object], list[str], list[str]]:
        assert request.output_prefix is not None
        data_directory = self._data_directory(request.output_prefix)
        input_parts = _nonempty_files(data_directory, _PART_BINARY_RE)
        output_vtks = _nonempty_files(request.output_prefix.parent, _PART_VTK_RE)
        input_frame_ids = _frame_ids(input_parts, _PART_BINARY_RE)
        output_frame_ids = _frame_ids(output_vtks, _PART_VTK_RE)
        artifacts = [
            Artifact.from_path(path, "particle_vtk", required=True)
            for path in output_vtks
        ]
        errors: list[str] = []
        if not output_vtks:
            errors.append("缺少非空 PartVTK 产物: PartAll_????.vtk")
        if len(output_vtks) != len(input_parts):
            errors.append(
                "PartVTK 输出帧数与输入不一致: "
                f"输入 {len(input_parts)}，输出 {len(output_vtks)}"
            )
        if output_frame_ids != input_frame_ids:
            missing = ", ".join(sorted(input_frame_ids - output_frame_ids)) or "无"
            unexpected = ", ".join(sorted(output_frame_ids - input_frame_ids)) or "无"
            errors.append(
                "PartVTK 输出帧编号与输入不一致: "
                f"缺少 {missing}；多出 {unexpected}"
            )
        return (
            artifacts,
            {
                "input_part_count": len(input_parts),
                "output_vtk_count": len(output_vtks),
            },
            [],
            errors,
        )
