from __future__ import annotations

from datetime import datetime
from io import StringIO
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch

from dualsphysics_studio.pipeline import (
    ArtifactManifest,
    DUALSPHYSICS_CPU_EXECUTABLE,
    DUALSPHYSICS_GPU_EXECUTABLE,
    PARTVTK_EXECUTABLE,
    PARTVTK_VARIABLES,
    DualSPHysicsCPUAdapter,
    DualSPHysicsGPUAdapter,
    GenCaseAdapter,
    PartVTKAdapter,
    RunRequest,
    manifest_is_stale,
    parse_case_out,
)


CASE_REPORT = """\
GenCase v5.4.354.01 (07-04-2025)
OmpThreads: 12
Case limits: (-0.05,-0.05,-0.05) - (1.65,0.3,0.9)
Distance between points (Dp): 0.01
Domain points: 170 x 35 x 95 = 565,250
FileShapes> Water_Dp.vtk <---- EMPTY FILE NOT SAVED
Points loaded: 153,888
Points -> Fixed-Boundary particles:
  block[  0] (mk: 10):  27990  -->   27990
Points -> Floating-Boundary particles:
  block[  2] (mk: 20):    396  -->     396
Particle summary:
  Fixed....: 68,427  id:(0-68426)
  Moving...: 0
  Floating.: 2,061
  Fluid....: 83,400
Total particles: 153,888 (bound=70488 (fx=68427 mv=0 ft=2061) fluid=83400)
Total MK blocks: 9 (bound=8 fluid=1)
Maximum depth: 0.29
Particle limits:
  X range: 0 to 1.6 [m]
  Y range: 0 to 0.25 [m]
  Z range: 0 to 0.8 [m]
Time of execution: 0.040 sec. (12 threads)
Finished execution (code=0).
"""


def make_installation(root: Path) -> tuple[Path, Path]:
    windows = root / "bin" / "windows"
    windows.mkdir(parents=True)
    executable = windows / "GenCase_win64.exe"
    executable.write_bytes(b"fake")
    (windows / "DsphConfig.xml").write_text("<config/>", encoding="utf-8")
    return executable, windows


def make_pipeline_installation(root: Path) -> tuple[Path, Path]:
    _, windows = make_installation(root)
    cpu = windows / DUALSPHYSICS_CPU_EXECUTABLE
    gpu = windows / DUALSPHYSICS_GPU_EXECUTABLE
    partvtk = windows / PARTVTK_EXECUTABLE
    cpu.write_bytes(b"fake cpu")
    gpu.write_bytes(b"fake gpu")
    partvtk.write_bytes(b"fake partvtk")
    return gpu, partvtk


def fake_tool_version_runner(
    banner: str, calls: list[tuple[list[str], dict]] | None = None
):
    def run(args, **kwargs):
        if calls is not None:
            calls.append((list(args), kwargs))
        return subprocess.CompletedProcess(args, 1, banner, "")

    return run


def make_case_inputs(source: Path) -> Path:
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("<case/>", encoding="utf-8")
    prefix = source.parent / "Case_out" / "Case"
    prefix.parent.mkdir()
    prefix.with_suffix(".xml").write_text("<case/>", encoding="utf-8")
    prefix.with_suffix(".bi4").write_bytes(b"CASE")
    return prefix


def make_part_inputs(case_prefix: Path, count: int = 2) -> Path:
    data = case_prefix.parent / "data"
    data.mkdir()
    (data / "Part_Head.ibi4").write_bytes(b"HEAD")
    for index in range(count):
        (data / f"Part_{index:04d}.bi4").write_bytes(b"PART")
    return data


def fake_version_runner(calls: list[tuple[list[str], dict]]):
    def run(args, **kwargs):
        calls.append((list(args), kwargs))
        # Official v5.4 builds can return 1 for -ver, so exercise that case.
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="GenCase v5.4.354.01 (07-04-2025)\n",
            stderr="",
        )

    return run


class FakeGenCaseProcess:
    """Popen-compatible process that materialises an isolated fake run."""

    def __init__(
        self,
        args,
        *,
        cwd,
        create_required: bool = True,
        return_code: int = 0,
        calls: list[dict] | None = None,
        **kwargs,
    ) -> None:
        if calls is not None:
            calls.append({"args": list(args), "cwd": cwd, **kwargs})
        self.args = list(args)
        self.cwd = Path(cwd)
        self.returncode = return_code
        self.stdout = StringIO("starting GenCase\nfinished\n")
        self.stderr = StringIO("diagnostic line\n")
        self._terminated = False

        prefix = Path(self.args[2])
        if not prefix.is_absolute():
            prefix = self.cwd / prefix
        prefix.parent.mkdir(parents=True, exist_ok=True)
        prefix.with_suffix(".out").write_text(CASE_REPORT, encoding="utf-8")
        prefix.with_name(f"{prefix.name}_MkCells.vtk").write_text(
            "# vtk DataFile Version 3.0\n", encoding="ascii"
        )
        if create_required:
            prefix.with_suffix(".bi4").write_bytes(b"BI4")
            prefix.with_suffix(".xml").write_text("<case/>", encoding="utf-8")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self._terminated = True
        self.returncode = -15

    def kill(self):
        self._terminated = True
        self.returncode = -9


class BlockingFakeProcess:
    def __init__(self, args, *, cwd, started: Event, **kwargs):
        self.args = list(args)
        self.cwd = cwd
        self.stdout = StringIO("")
        self.stderr = StringIO("")
        self.returncode = None
        started.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class FakePipelineProcess:
    def __init__(
        self,
        args,
        *,
        cwd,
        create_products: bool = True,
        return_code: int = 0,
        calls: list[dict] | None = None,
        **kwargs,
    ) -> None:
        if calls is not None:
            calls.append({"args": list(args), "cwd": cwd, **kwargs})
        self.args = list(args)
        self.cwd = Path(cwd)
        self.returncode = return_code
        self.stdout = StringIO("tool output\n")
        self.stderr = StringIO("tool diagnostic\n")
        if not create_products:
            return

        if "-dirdata" not in self.args:
            output_directory = Path(self.args[3] if "-gpu" in self.args else self.args[2])
            data = output_directory / "data"
            data.mkdir(parents=True)
            device = 'Device default: 0 "Test GPU"\n' if "-gpu" in self.args else ""
            (output_directory / "Run.out").write_text(
                "DualSPHysics5 v5.4.355\n"
                f"{device}"
                "*** WARNING: Test warning.\n"
                "Total Runtime....................: 2.5 sec.\n"
                "Simulation Runtime...............: 2.0 sec.\n"
                "Steps of simulation..............: 1,234\n"
                "PART files.......................: 1\n"
                "Finished execution (code=0).\n",
                encoding="utf-8",
            )
            (output_directory / "Run.csv").write_text("time\n", encoding="utf-8")
            (data / "Part_Head.ibi4").write_bytes(b"HEAD")
            (data / "Part_0000.bi4").write_bytes(b"PART")
        else:
            data = Path(self.args[self.args.index("-dirdata") + 1])
            prefix = Path(self.args[self.args.index("-savevtk") + 1])
            prefix.parent.mkdir(parents=True, exist_ok=True)
            part_count = len(list(data.glob("Part_[0-9][0-9][0-9][0-9].bi4")))
            for index in range(part_count):
                prefix.with_name(f"{prefix.name}_{index:04d}.vtk").write_text(
                    "# vtk DataFile Version 3.0\n",
                    encoding="ascii",
                )

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class PipelineTests(unittest.TestCase):
    def test_report_parser_extracts_particle_summary_and_marker_blocks(self):
        summary = parse_case_out(CASE_REPORT)

        self.assertEqual(summary["gencase_version"], "5.4.354.01")
        self.assertEqual(summary["total_particles"], 153_888)
        self.assertEqual(summary["particle_counts"]["fixed"], 68_427)
        self.assertEqual(summary["particle_counts"]["floating"], 2_061)
        self.assertEqual(summary["domain_points"]["total"], 565_250)
        self.assertEqual(summary["case_limits"]["max"], [1.65, 0.3, 0.9])
        self.assertEqual(summary["particle_limits"]["z"], [0.0, 0.8])
        self.assertEqual(summary["marker_blocks"][1]["mk"], 20)
        self.assertEqual(summary["empty_shape_files"], ["Water_Dp.vtk"])
        self.assertAlmostEqual(summary["execution_seconds"], 0.04)

    def test_ignores_case_ancestors_and_working_directory_when_unconfigured(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics_v5.4"
            make_installation(root)
            case_dir = root / "examples" / "SPH Learning" / "中文案例"
            case_dir.mkdir(parents=True)
            source = case_dir / "Case_Def.xml"
            source.write_text("<case/>", encoding="utf-8")
            calls: list[tuple[list[str], dict]] = []

            with (
                patch.dict(os.environ, {"DUALSPHYSICS_ROOT": ""}),
                patch(
                    "dualsphysics_studio.pipeline.gencase.Path.cwd",
                    return_value=root,
                ),
            ):
                info = GenCaseAdapter(version_runner=fake_version_runner(calls)).probe(source)

            self.assertIsNone(info.root)
            self.assertIsNone(info.executable)
            self.assertFalse(info.ready)
            self.assertEqual(calls, [])

    def test_discovers_root_from_explicit_environment_and_accepts_version_banner(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics_v5.4"
            executable, windows = make_installation(root)
            source = Path(temporary) / "cases" / "Case_Def.xml"
            source.parent.mkdir()
            source.write_text("<case/>", encoding="utf-8")
            calls: list[tuple[list[str], dict]] = []

            with patch.dict(os.environ, {"DUALSPHYSICS_ROOT": str(root)}):
                info = GenCaseAdapter(version_runner=fake_version_runner(calls)).probe(source)

            self.assertEqual(info.root, root.resolve())
            self.assertEqual(info.executable, executable.resolve())
            self.assertEqual(info.config_path, (windows / "DsphConfig.xml").resolve())
            self.assertEqual(info.version, "5.4.354.01")
            self.assertTrue(info.ready)
            self.assertEqual(calls[0][0], [str(executable.resolve()), "-ver"])
            self.assertIs(calls[0][1]["shell"], False)

    def test_solver_stage_probes_ignore_case_ancestors_when_unconfigured(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics_v5.4"
            make_pipeline_installation(root)
            source = root / "examples" / "case" / "Case_Def.xml"
            source.parent.mkdir(parents=True)
            source.write_text("<case/>", encoding="utf-8")

            with (
                patch.dict(os.environ, {"DUALSPHYSICS_ROOT": ""}),
                patch(
                    "dualsphysics_studio.pipeline.gencase.Path.cwd",
                    return_value=root,
                ),
            ):
                for adapter_type in (
                    DualSPHysicsCPUAdapter,
                    DualSPHysicsGPUAdapter,
                    PartVTKAdapter,
                ):
                    calls: list[tuple[list[str], dict]] = []
                    info = adapter_type(
                        version_runner=fake_tool_version_runner("v5.4", calls)
                    ).probe(source)
                    self.assertIsNone(info.root)
                    self.assertIsNone(info.executable)
                    self.assertFalse(info.ready)
                    self.assertEqual(calls, [])


    def test_builds_argument_vector_matching_official_case_convention(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics"
            executable, _ = make_installation(root)
            case_dir = root / "examples" / "中文 空格"
            case_dir.mkdir(parents=True)
            source = case_dir / "Case_Def.xml"
            source.write_text("<case/>", encoding="utf-8")
            adapter = GenCaseAdapter(
                dualsphysics_root=root,
                version_runner=fake_version_runner([]),
            )
            request = RunRequest(
                source,
                output_prefix=Path("结果 目录") / "Case",
                extra_args=("-threads:4",),
            )

            command = adapter.build_command(request)

            self.assertIsInstance(command, tuple)
            self.assertEqual(command[0], str(executable.resolve()))
            self.assertEqual(command[1], "Case_Def")
            self.assertEqual(Path(command[2]), Path("结果 目录") / "Case")
            self.assertIn("-save:all", command)
            self.assertIn("-createdirs:1", command)
            self.assertIn("-threads:4", command)

            quick = adapter.build_command(
                RunRequest(source, output_prefix="Quick/Case", mode="quick")
            )
            self.assertNotIn("-save:all", quick)
            self.assertIn("-createdirs:1", quick)

    def test_nonempty_default_output_gets_timestamped_sibling_suggestion(self):
        with TemporaryDirectory() as temporary:
            case_dir = Path(temporary) / "案例"
            case_dir.mkdir()
            source = case_dir / "Case_Def.xml"
            source.write_text("<case/>", encoding="utf-8")
            default = GenCaseAdapter.default_output_prefix(source)
            default.parent.mkdir()
            (default.parent / "old-result.txt").write_text("old", encoding="utf-8")
            collision = case_dir / "Case_out_20260713-173045"
            collision.mkdir()

            suggested = GenCaseAdapter.suggest_output_prefix(
                source,
                now=datetime(2026, 7, 13, 17, 30, 45),
            )

            self.assertEqual(
                suggested,
                case_dir / "Case_out_20260713-173045-02" / "Case",
            )

    def test_successful_run_streams_logs_validates_outputs_and_writes_manifest(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics"
            _, _ = make_installation(root)
            case_dir = root / "examples" / "中文案例"
            case_dir.mkdir(parents=True)
            source = case_dir / "Case_Def.xml"
            source.write_text("<case><!-- 中文 --></case>", encoding="utf-8")
            popen_calls: list[dict] = []

            def popen(args, **kwargs):
                return FakeGenCaseProcess(args, calls=popen_calls, **kwargs)

            adapter = GenCaseAdapter(
                root,
                version_runner=fake_version_runner([]),
                popen_factory=popen,
            )
            streamed: list[tuple[str, str]] = []
            result = adapter.run(
                RunRequest(source),
                on_log=lambda channel, line: streamed.append((channel, line)),
            )

            self.assertTrue(result.success, result.errors)
            self.assertEqual(result.return_code, 0)
            self.assertEqual(result.working_directory, case_dir.resolve())
            self.assertEqual(result.summary["total_particles"], 153_888)
            self.assertTrue(any("Water_Dp.vtk" in item for item in result.warnings))
            self.assertIn(("stdout", "starting GenCase"), streamed)
            self.assertIn(("stderr", "diagnostic line"), streamed)
            self.assertIs(popen_calls[0]["shell"], False)
            self.assertEqual(Path(popen_calls[0]["cwd"]), case_dir.resolve())
            roles = {artifact.role for artifact in result.artifacts}
            self.assertTrue({"case_binary", "resolved_xml", "gencase_log", "vtk_mkcells"} <= roles)

            self.assertIsNotNone(result.manifest_path)
            self.assertTrue(result.manifest_path.is_file())
            loaded = ArtifactManifest.load(result.manifest_path)
            self.assertEqual(loaded.status, "succeeded")
            self.assertFalse(loaded.is_stale())
            self.assertFalse(manifest_is_stale(result.manifest_path, source))

            source.write_text("<case changed='1'/>", encoding="utf-8")
            self.assertTrue(loaded.is_stale())
            self.assertTrue(manifest_is_stale(result.manifest_path, source))

    def test_zero_exit_without_required_products_is_failure(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics"
            make_installation(root)
            source = root / "Case_Def.xml"
            source.write_text("<case/>", encoding="utf-8")

            def popen(args, **kwargs):
                return FakeGenCaseProcess(args, create_required=False, **kwargs)

            adapter = GenCaseAdapter(
                root,
                version_runner=fake_version_runner([]),
                popen_factory=popen,
            )
            result = adapter.run(RunRequest(source))

            self.assertFalse(result.success)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.return_code, 0)
            self.assertTrue(any("Case.bi4" in item for item in result.errors))
            self.assertTrue(any("Case.xml" in item for item in result.errors))
            # A missing/empty Water shape VTK is a notice, not the cause of failure.
            self.assertTrue(any("Water_Dp.vtk" in item for item in result.warnings))

    def test_pre_cancelled_request_never_starts_process(self):
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "Case_Def.xml"
            source.write_text("<case/>", encoding="utf-8")
            started = []

            def forbidden_popen(*args, **kwargs):
                started.append(True)
                raise AssertionError("process must not start")

            cancel_event = Event()
            cancel_event.set()
            result = GenCaseAdapter(popen_factory=forbidden_popen).run(
                RunRequest(source), cancel_event=cancel_event
            )

            self.assertEqual(result.status, "cancelled")
            self.assertTrue(result.cancelled)
            self.assertFalse(started)

    def test_cancel_marks_an_active_run_cancelled(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics"
            make_installation(root)
            source = root / "Case_Def.xml"
            source.write_text("<case/>", encoding="utf-8")
            started = Event()

            def popen(args, **kwargs):
                return BlockingFakeProcess(args, started=started, **kwargs)

            adapter = GenCaseAdapter(
                root,
                version_runner=fake_version_runner([]),
                popen_factory=popen,
            )
            outcomes = []
            thread = Thread(
                target=lambda: outcomes.append(adapter.run(RunRequest(source))),
                daemon=True,
            )
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            self.assertTrue(adapter.cancel())
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes[0].status, "cancelled")
            self.assertTrue(outcomes[0].cancelled)

    def test_cpu_gpu_and_partvtk_probe_accept_nonzero_version_banner(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics_v5.4"
            gpu, partvtk = make_pipeline_installation(root)
            cpu = root / "bin" / "windows" / DUALSPHYSICS_CPU_EXECUTABLE
            source = root / "examples" / "中文 案例" / "Case_Def.xml"
            source.parent.mkdir(parents=True)
            source.write_text("<case/>", encoding="utf-8")
            cpu_calls: list[tuple[list[str], dict]] = []
            gpu_calls: list[tuple[list[str], dict]] = []
            vtk_calls: list[tuple[list[str], dict]] = []

            cpu_info = DualSPHysicsCPUAdapter(
                root,
                version_runner=fake_tool_version_runner(
                    "DualSPHysics5 v5.4.355 (08-04-2025)\n", cpu_calls
                )
            ).probe(source)
            gpu_info = DualSPHysicsGPUAdapter(
                root,
                version_runner=fake_tool_version_runner(
                    "DualSPHysics5 v5.4.355 (08-04-2025)\n", gpu_calls
                )
            ).probe(source)
            vtk_info = PartVTKAdapter(
                root,
                version_runner=fake_tool_version_runner(
                    "PartVTK v5.4.266.02 (07-05-2025)\n", vtk_calls
                )
            ).probe(source)

            self.assertTrue(cpu_info.ready)
            self.assertEqual(cpu_info.executable, cpu.resolve())
            self.assertEqual(cpu_info.version, "5.4.355")
            self.assertTrue(gpu_info.ready)
            self.assertEqual(gpu_info.executable, gpu.resolve())
            self.assertEqual(gpu_info.version, "5.4.355")
            self.assertTrue(vtk_info.ready)
            self.assertEqual(vtk_info.executable, partvtk.resolve())
            self.assertEqual(vtk_info.version, "5.4.266.02")
            self.assertEqual(cpu_calls[0][0], [str(cpu.resolve()), "-ver"])
            self.assertEqual(gpu_calls[0][0], [str(gpu.resolve()), "-ver"])
            self.assertEqual(vtk_calls[0][0], [str(partvtk.resolve()), "-ver"])
            self.assertIs(cpu_calls[0][1]["shell"], False)
            self.assertIs(gpu_calls[0][1]["shell"], False)
            self.assertIs(vtk_calls[0][1]["shell"], False)

    def test_cpu_run_uses_shell_free_argv_and_validates_solver_products(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics v5.4"
            make_pipeline_installation(root)
            source = root / "examples" / "中文 空格" / "Case_Def.xml"
            case_prefix = make_case_inputs(source)
            popen_calls: list[dict] = []

            def popen(args, **kwargs):
                return FakePipelineProcess(args, calls=popen_calls, **kwargs)

            result = DualSPHysicsCPUAdapter(
                root,
                version_runner=fake_tool_version_runner("DualSPHysics5 v5.4.355\n"),
                popen_factory=popen,
            ).run(RunRequest(source, output_prefix=case_prefix, mode="cpu"))

            self.assertTrue(result.success, result.errors)
            self.assertEqual(
                result.command[1:],
                (str(case_prefix.resolve()), str(case_prefix.parent.resolve())),
            )
            self.assertNotIn("-cpu", result.command)
            self.assertNotIn("-gpu", result.command)
            self.assertEqual(Path(popen_calls[0]["cwd"]), source.parent.resolve())
            self.assertIs(popen_calls[0]["shell"], False)
            self.assertEqual(result.summary["part_count"], 1)
            self.assertEqual(result.summary["steps"], 1_234)
            self.assertEqual(result.manifest.stage, "cpu")
            self.assertEqual(result.manifest.tool_name, "DualSPHysics CPU")
            self.assertTrue(result.manifest_path.name.endswith(".cpu.manifest.json"))

    def test_cpu_blocks_existing_results_and_missing_gencase_inputs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics"
            make_pipeline_installation(root)
            source = root / "Case_Def.xml"
            case_prefix = make_case_inputs(source)
            started: list[bool] = []

            def forbidden_popen(*args, **kwargs):
                started.append(True)
                raise AssertionError("CPU process must not start")

            adapter = DualSPHysicsCPUAdapter(
                root,
                version_runner=fake_tool_version_runner("DualSPHysics5 v5.4.355\n"),
                popen_factory=forbidden_popen,
            )
            (case_prefix.parent / "Run.out").write_text("old", encoding="utf-8")
            conflict = adapter.run(
                RunRequest(source, output_prefix=case_prefix, mode="cpu")
            )
            self.assertFalse(conflict.success)
            self.assertTrue(any("阻止覆盖" in item for item in conflict.errors))
            self.assertFalse((case_prefix.parent / "Case.cpu.manifest.json").exists())
            self.assertFalse(started)

            (case_prefix.parent / "Run.out").unlink()
            case_prefix.with_suffix(".xml").unlink()
            missing = adapter.run(
                RunRequest(source, output_prefix=case_prefix, mode="cpu")
            )
            self.assertFalse(missing.success)
            self.assertTrue(any("Case.xml" in item for item in missing.errors))
            self.assertFalse(started)

    def test_cpu_zero_exit_without_products_is_failure(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics"
            make_pipeline_installation(root)
            source = root / "Case_Def.xml"
            case_prefix = make_case_inputs(source)

            def popen(args, **kwargs):
                return FakePipelineProcess(args, create_products=False, **kwargs)

            result = DualSPHysicsCPUAdapter(
                root,
                version_runner=fake_tool_version_runner("DualSPHysics5 v5.4.355\n"),
                popen_factory=popen,
            ).run(RunRequest(source, output_prefix=case_prefix, mode="cpu"))

            self.assertFalse(result.success)
            self.assertEqual(result.return_code, 0)
            self.assertTrue(any("Run.out" in item for item in result.errors))
            self.assertTrue(any("Part_Head.ibi4" in item for item in result.errors))
            self.assertTrue(any("Part_????.bi4" in item for item in result.errors))

    def test_gpu_run_uses_shell_free_argv_and_validates_solver_products(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics v5.4"
            make_pipeline_installation(root)
            source = root / "examples" / "中文 空格" / "Case_Def.xml"
            case_prefix = make_case_inputs(source)
            popen_calls: list[dict] = []

            def popen(args, **kwargs):
                return FakePipelineProcess(args, calls=popen_calls, **kwargs)

            adapter = DualSPHysicsGPUAdapter(
                root,
                version_runner=fake_tool_version_runner("DualSPHysics5 v5.4.355\n"),
                popen_factory=popen,
            )
            old_manifest = case_prefix.with_name("Case.gpu.manifest.json")
            old_manifest.write_text("old manifest", encoding="utf-8")
            streamed: list[tuple[str, str]] = []
            result = adapter.run(
                RunRequest(source, output_prefix=case_prefix, mode="gpu"),
                on_log=lambda channel, line: streamed.append((channel, line)),
            )

            self.assertTrue(result.success, result.errors)
            self.assertEqual(
                result.command[1:],
                ("-gpu", str(case_prefix.resolve()), str(case_prefix.parent.resolve())),
            )
            self.assertEqual(Path(popen_calls[0]["cwd"]), source.parent.resolve())
            self.assertIs(popen_calls[0]["shell"], False)
            self.assertIn(("stdout", "tool output"), streamed)
            self.assertEqual(result.summary["part_count"], 1)
            self.assertEqual(result.summary["device"], '0 "Test GPU"')
            self.assertEqual(result.summary["steps"], 1_234)
            self.assertEqual(result.summary["total_runtime_seconds"], 2.5)
            self.assertIn("Test warning.", result.warnings)
            self.assertEqual(result.manifest.stage, "gpu")
            self.assertTrue(result.manifest_path.name.endswith(".gpu.manifest.json"))
            self.assertNotEqual(result.manifest_path, old_manifest)
            self.assertEqual(old_manifest.read_text(encoding="utf-8"), "old manifest")
            roles = {artifact.role for artifact in result.artifacts}
            self.assertTrue({"solver_log", "part_head", "part_binary"} <= roles)

    def test_gpu_blocks_existing_results_and_missing_gencase_inputs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics"
            make_pipeline_installation(root)
            source = root / "examples" / "Case_Def.xml"
            case_prefix = make_case_inputs(source)
            started: list[bool] = []

            def forbidden_popen(*args, **kwargs):
                started.append(True)
                raise AssertionError("GPU process must not start")

            adapter = DualSPHysicsGPUAdapter(
                root,
                version_runner=fake_tool_version_runner("DualSPHysics5 v5.4.355\n"),
                popen_factory=forbidden_popen,
            )
            (case_prefix.parent / "RunPARTs.csv").write_text("old", encoding="utf-8")
            conflict = adapter.run(
                RunRequest(source, output_prefix=case_prefix, mode="gpu")
            )
            self.assertFalse(conflict.success)
            self.assertTrue(any("阻止覆盖" in item for item in conflict.errors))
            self.assertFalse((case_prefix.parent / "Case.gpu.manifest.json").exists())
            self.assertFalse(started)

            (case_prefix.parent / "RunPARTs.csv").unlink()
            vtk_directory = case_prefix.parent / "vtk"
            vtk_directory.mkdir()
            old_vtk = vtk_directory / "PartAll_0000.vtk"
            old_vtk.write_text("old vtk", encoding="utf-8")
            vtk_conflict = adapter.run(
                RunRequest(source, output_prefix=case_prefix, mode="gpu")
            )
            self.assertFalse(vtk_conflict.success)
            self.assertTrue(any("PartAll_0000.vtk" in item for item in vtk_conflict.errors))
            self.assertEqual(old_vtk.read_text(encoding="utf-8"), "old vtk")
            self.assertFalse(started)

            old_vtk.unlink()
            case_prefix.with_suffix(".bi4").unlink()
            missing = adapter.run(
                RunRequest(source, output_prefix=case_prefix, mode="gpu")
            )
            self.assertFalse(missing.success)
            self.assertTrue(any("Case.bi4" in item for item in missing.errors))
            self.assertFalse(started)

    def test_gpu_zero_exit_without_products_is_failure(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics"
            make_pipeline_installation(root)
            source = root / "Case_Def.xml"
            case_prefix = make_case_inputs(source)

            def popen(args, **kwargs):
                return FakePipelineProcess(args, create_products=False, **kwargs)

            result = DualSPHysicsGPUAdapter(
                root,
                version_runner=fake_tool_version_runner("DualSPHysics5 v5.4.355\n"),
                popen_factory=popen,
            ).run(RunRequest(source, output_prefix=case_prefix, mode="gpu"))

            self.assertFalse(result.success)
            self.assertEqual(result.return_code, 0)
            self.assertTrue(any("Run.out" in item for item in result.errors))
            self.assertTrue(any("Part_Head.ibi4" in item for item in result.errors))
            self.assertTrue(any("Part_????.bi4" in item for item in result.errors))

    def test_partvtk_run_converts_every_part_and_does_not_filter_particle_type(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics v5.4"
            make_pipeline_installation(root)
            source = root / "examples" / "中文 空格" / "Case_Def.xml"
            case_prefix = make_case_inputs(source)
            data = make_part_inputs(case_prefix, count=2)
            vtk_prefix = case_prefix.parent / "vtk" / "PartAll"
            popen_calls: list[dict] = []

            def popen(args, **kwargs):
                return FakePipelineProcess(args, calls=popen_calls, **kwargs)

            adapter = PartVTKAdapter(
                root,
                version_runner=fake_tool_version_runner("PartVTK v5.4.266.02\n"),
                popen_factory=popen,
            )
            vtk_prefix.parent.mkdir(parents=True)
            old_manifest = vtk_prefix.with_name("PartAll.partvtk.manifest.json")
            old_manifest.write_text("old manifest", encoding="utf-8")
            result = adapter.run(
                RunRequest(
                    source,
                    output_prefix=vtk_prefix,
                    mode="partvtk",
                    create_dirs=True,
                )
            )

            self.assertTrue(result.success, result.errors)
            self.assertEqual(
                result.command[1:],
                (
                    "-dirdata",
                    str(data.resolve()),
                    "-savevtk",
                    str(vtk_prefix.resolve()),
                    PARTVTK_VARIABLES,
                ),
            )
            self.assertFalse(any(arg.startswith("-onlytype") for arg in result.command))
            self.assertIs(popen_calls[0]["shell"], False)
            self.assertEqual(Path(popen_calls[0]["cwd"]), source.parent.resolve())
            self.assertEqual(result.summary["input_part_count"], 2)
            self.assertEqual(result.summary["output_vtk_count"], 2)
            self.assertEqual(result.manifest.stage, "partvtk")
            self.assertTrue(result.manifest_path.name.endswith(".partvtk.manifest.json"))
            self.assertNotEqual(result.manifest_path, old_manifest)
            self.assertEqual(old_manifest.read_text(encoding="utf-8"), "old manifest")
            self.assertEqual(
                [artifact.role for artifact in result.artifacts],
                ["particle_vtk", "particle_vtk"],
            )

    def test_partvtk_blocks_existing_output_and_missing_input(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics"
            make_pipeline_installation(root)
            source = root / "Case_Def.xml"
            case_prefix = make_case_inputs(source)
            make_part_inputs(case_prefix, count=1)
            vtk_prefix = case_prefix.parent / "vtk" / "PartAll"
            vtk_prefix.parent.mkdir()
            (vtk_prefix.parent / "PartAll_0000.vtk").write_text("old", encoding="utf-8")
            started: list[bool] = []

            def forbidden_popen(*args, **kwargs):
                started.append(True)
                raise AssertionError("PartVTK process must not start")

            adapter = PartVTKAdapter(
                root,
                version_runner=fake_tool_version_runner("PartVTK v5.4.266.02\n"),
                popen_factory=forbidden_popen,
            )
            conflict = adapter.run(
                RunRequest(source, output_prefix=vtk_prefix, mode="partvtk")
            )
            self.assertFalse(conflict.success)
            self.assertTrue(any("阻止覆盖" in item for item in conflict.errors))
            self.assertFalse(
                (vtk_prefix.parent / "PartAll.partvtk.manifest.json").exists()
            )
            self.assertFalse(started)

            (vtk_prefix.parent / "PartAll_0000.vtk").unlink()
            (case_prefix.parent / "data" / "Part_0000.bi4").unlink()
            missing = adapter.run(
                RunRequest(source, output_prefix=vtk_prefix, mode="partvtk")
            )
            self.assertFalse(missing.success)
            self.assertTrue(any("Part_????.bi4" in item for item in missing.errors))
            self.assertFalse(started)

    def test_partvtk_zero_exit_with_wrong_frame_id_is_failure(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics"
            make_pipeline_installation(root)
            source = root / "Case_Def.xml"
            case_prefix = make_case_inputs(source)
            make_part_inputs(case_prefix, count=2)
            vtk_prefix = case_prefix.parent / "vtk" / "PartAll"

            def popen(args, **kwargs):
                process = FakePipelineProcess(args, create_products=False, **kwargs)
                vtk_prefix.parent.mkdir(parents=True, exist_ok=True)
                (vtk_prefix.parent / "PartAll_0000.vtk").write_text(
                    "# vtk\n", encoding="ascii"
                )
                (vtk_prefix.parent / "PartAll_0002.vtk").write_text(
                    "# vtk\n", encoding="ascii"
                )
                return process

            result = PartVTKAdapter(
                root,
                version_runner=fake_tool_version_runner("PartVTK v5.4.266.02\n"),
                popen_factory=popen,
            ).run(
                RunRequest(
                    source,
                    output_prefix=vtk_prefix,
                    mode="partvtk",
                    create_dirs=True,
                )
            )

            self.assertFalse(result.success)
            self.assertEqual(result.return_code, 0)
            self.assertEqual(result.summary["input_part_count"], 2)
            self.assertEqual(result.summary["output_vtk_count"], 2)
            self.assertTrue(any("输出帧编号" in item for item in result.errors))

    def test_gpu_cancel_marks_active_solver_run_cancelled(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics"
            make_pipeline_installation(root)
            source = root / "Case_Def.xml"
            case_prefix = make_case_inputs(source)
            started = Event()

            def popen(args, **kwargs):
                return BlockingFakeProcess(args, started=started, **kwargs)

            adapter = DualSPHysicsGPUAdapter(
                root,
                version_runner=fake_tool_version_runner("DualSPHysics5 v5.4.355\n"),
                popen_factory=popen,
            )
            outcomes = []
            thread = Thread(
                target=lambda: outcomes.append(
                    adapter.run(RunRequest(source, output_prefix=case_prefix, mode="gpu"))
                ),
                daemon=True,
            )
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            self.assertTrue(adapter.cancel())
            thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes[0].status, "cancelled")
            self.assertTrue(outcomes[0].cancelled)

    def test_shared_runner_enforces_timeout(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics"
            make_pipeline_installation(root)
            source = root / "Case_Def.xml"
            case_prefix = make_case_inputs(source)
            started = Event()

            def popen(args, **kwargs):
                return BlockingFakeProcess(args, started=started, **kwargs)

            result = DualSPHysicsGPUAdapter(
                root,
                version_runner=fake_tool_version_runner("DualSPHysics5 v5.4.355\n"),
                popen_factory=popen,
            ).run(
                RunRequest(
                    source,
                    output_prefix=case_prefix,
                    mode="gpu",
                    timeout=0.05,
                )
            )

            self.assertTrue(started.is_set())
            self.assertEqual(result.status, "failed")
            self.assertTrue(result.timed_out)
            self.assertTrue(any("超时" in item for item in result.errors))

    def test_incompatible_version_blocks_execution(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "DualSPHysics"
            make_installation(root)
            source = root / "Case_Def.xml"
            source.write_text("<case/>", encoding="utf-8")

            def old_version(args, **kwargs):
                return subprocess.CompletedProcess(args, 0, "GenCase v5.3.1\n", "")

            result = GenCaseAdapter(root, version_runner=old_version).run(RunRequest(source))

            self.assertFalse(result.success)
            self.assertTrue(any("不兼容" in error for error in result.errors))


if __name__ == "__main__":
    unittest.main()
