"""Command-line entry point used both from source and by PyInstaller."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import sys
import tempfile

from .version import APP_VERSION


def _smoke_test() -> int:
    """Exercise frozen imports and critical resources without opening a GUI."""

    from .catalog import SemanticCatalog
    from .core import CaseDocument
    from .core.mesh_guard import inspect_mesh_file, validate_loaded_dataset
    from .extensions import default_dlc_directory, discover_extension_tools
    from .pipeline import (
        DualSPHysicsCPUAdapter,
        DualSPHysicsGPUAdapter,
        GenCaseAdapter,
        PartVTKAdapter,
        RunSession,
        estimate_case,
    )
    from .resources import resource_path
    from openai import OpenAI
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    from vtkmodules.vtkIOGeometry import vtkSTLReader
    from vtkmodules.vtkIOLegacy import vtkDataSetReader

    document = CaseDocument.from_text("<case><!-- 中文 --></case>")
    if "中文" not in document.text:
        raise RuntimeError("UTF-8 XML smoke test failed")
    if not resource_path("semantic_catalog.json").is_file():
        raise RuntimeError("semantic catalog is missing")
    SemanticCatalog()
    GenCaseAdapter()
    DualSPHysicsCPUAdapter()
    DualSPHysicsGPUAdapter()
    PartVTKAdapter()
    with tempfile.TemporaryDirectory(prefix="dss-smoke-") as temporary:
        temporary_path = Path(temporary)
        source_path = temporary_path / "Case_Def.xml"
        source_path.write_text(document.text, encoding="utf-8")
        session = RunSession.create(
            source_xml=source_path,
            output_root=temporary_path / "Case_out",
            backend="gpu",
            workflow=("gencase", "gpu", "partvtk"),
        )
        session.add_estimate(estimate_case(document))
        diagnostic_path = session.export_diagnostic(
            temporary_path / "diagnostic.zip"
        )
        if not session.record_path.is_file() or not diagnostic_path.is_file():
            raise RuntimeError("run-session smoke test failed")

        stl_path = temporary_path / "triangle.stl"
        stl_path.write_text(
            "solid test\n"
            "facet normal 0 0 1\n"
            "outer loop\n"
            "vertex 0 0 0\n"
            "vertex 1 0 0\n"
            "vertex 0 1 0\n"
            "endloop\n"
            "endfacet\n"
            "endsolid test\n",
            encoding="ascii",
        )
        stl_reader = vtkSTLReader()
        stl_reader.SetFileName(str(stl_path))
        stl_reader.Update()
        stl_report = inspect_mesh_file(stl_path)
        validate_loaded_dataset(stl_reader.GetOutput())
        if stl_reader.GetOutput().GetNumberOfPoints() != 3:
            raise RuntimeError("STL reader smoke test failed")
        if stl_report.estimated_cells != 1:
            raise RuntimeError("STL resource preflight smoke test failed")

        vtk_path = temporary_path / "triangle.vtk"
        vtk_path.write_text(
            "# vtk DataFile Version 3.0\n"
            "DualSPHysics Studio smoke test\n"
            "ASCII\n"
            "DATASET POLYDATA\n"
            "POINTS 3 float\n"
            "0 0 0  1 0 0  0 1 0\n"
            "POLYGONS 1 4\n"
            "3 0 1 2\n",
            encoding="ascii",
        )
        vtk_reader = vtkDataSetReader()
        vtk_reader.SetFileName(str(vtk_path))
        vtk_reader.Update()
        vtk_report = inspect_mesh_file(vtk_path)
        validate_loaded_dataset(vtk_reader.GetOutput())
        if vtk_reader.GetOutput().GetNumberOfCells() != 1:
            raise RuntimeError("legacy VTK reader smoke test failed")
        if vtk_report.estimated_points != 3:
            raise RuntimeError("VTK resource preflight smoke test failed")

    points = vtkPoints()
    points.InsertNextPoint(0.0, 0.0, 0.0)
    points.InsertNextPoint(1.0, 0.0, 0.0)
    points.InsertNextPoint(0.0, 1.0, 0.0)
    polygon = vtkCellArray()
    polygon.InsertNextCell(3)
    polygon.InsertCellPoint(0)
    polygon.InsertCellPoint(1)
    polygon.InsertCellPoint(2)
    polydata = vtkPolyData()
    polydata.SetPoints(points)
    polydata.SetPolys(polygon)
    if polydata.GetNumberOfPoints() != 3 or polydata.GetNumberOfCells() != 1:
        raise RuntimeError("VTK geometry smoke test failed")
    if getattr(sys, "frozen", False) and "numpy" in sys.modules:
        raise RuntimeError("unused NumPy runtime was loaded")
    excluded_runtime_modules = (
        "_pytest",
        "pydantic.mypy",
        "pydantic.v1._hypothesis_plugin",
        "pydantic.v1.mypy",
        "keyring.backends.SecretService",
        "keyring.backends.kwallet",
        "keyring.backends.libsecret",
        "keyring.backends.macOS",
        "anyio._backends._trio",
        "httpcore._backends.trio",
        "vtkmodules.numpy_interface",
        "vtkmodules.util.numpy_support",
    )
    if getattr(sys, "frozen", False):
        for module_name in excluded_runtime_modules:
            try:
                bundled = importlib.util.find_spec(module_name) is not None
            except ModuleNotFoundError:
                bundled = False
            if bundled:
                raise RuntimeError(f"unused runtime module was bundled: {module_name}")
    OpenAI(api_key="offline-smoke-test-key", timeout=0.1)
    if os.name == "nt":
        from .ai.openai_provider import _windows_credential_backend

        if _windows_credential_backend().priority < 1:
            raise RuntimeError("Windows Credential Manager backend is unavailable")
    dlc_directory = default_dlc_directory()
    discover_extension_tools(dlc_directory)
    if getattr(sys, "frozen", False):
        if not (dlc_directory / "README.txt").is_file():
            raise RuntimeError("portable DLC directory is missing")
    print(f"DualSPHysics Studio {APP_VERSION} smoke test: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="DualSPHysicsStudio")
    parser.add_argument("xml", nargs="?", help="启动后打开的 Case_Def.xml")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="验证打包运行时后立即退出",
    )
    arguments = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if arguments.smoke_test:
        return _smoke_test()

    from .app import run_application

    initial = Path(arguments.xml).resolve() if arguments.xml else None
    return run_application(sys.argv if argv is None else ["DualSPHysicsStudio", *argv], initial_path=initial)


if __name__ == "__main__":
    raise SystemExit(main())
