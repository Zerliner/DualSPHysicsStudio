# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir specification for DualSPHysics Studio 1.0.2.

Run this specification through ``packaging/build_portable.ps1``.  The spec
deliberately packages only the Studio application and its Python runtime; it
does not collect DualSPHysics, GenCase, or files from a user's case directory.
"""

from pathlib import Path

# PyInstaller exposes SPECPATH as the directory containing this spec.
PROJECT_ROOT = Path(SPECPATH).resolve().parent
SOURCE_ROOT = PROJECT_ROOT / "src"
PACKAGE_ROOT = SOURCE_ROOT / "dualsphysics_studio"
RESOURCE_ROOT = PACKAGE_ROOT / "resources"


datas = [
    (str(RESOURCE_ROOT / "semantic_catalog.json"), "dualsphysics_studio/resources"),
    (str(RESOURCE_ROOT / "theme.qss"), "dualsphysics_studio/resources"),
    (str(RESOURCE_ROOT / "app_icon.svg"), "dualsphysics_studio/resources"),
]

hiddenimports = [
    "keyring.backends.Windows",
    "vtkmodules.qt.QVTKRenderWindowInteractor",
    "vtkmodules.vtkIOGeometry",
    "vtkmodules.vtkInteractionStyle",
    "vtkmodules.vtkRenderingOpenGL2",
]


a = Analysis(
    [str(PROJECT_ROOT / "packaging" / "pyinstaller_entry.py")],
    pathex=[str(SOURCE_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "tzdata",
        "IPython",
        "jupyter",
        "notebook",
        "pandas",
        "scipy",
        "lxml",
        "matplotlib",
        "PIL",
        "numpy",
        "pygments",
        "pytest",
        "_pytest",
        "pydantic.mypy",
        "pydantic.v1.mypy",
        "pydantic.v1._hypothesis_plugin",
        "keyring.backends.SecretService",
        "keyring.backends.kwallet",
        "keyring.backends.libsecret",
        "keyring.backends.macOS",
        "keyring.backends.macOS.api",
        "anyio._backends._trio",
        "httpcore._backends.trio",
        "setuptools",
        "vtkmodules.numpy_interface",
        "vtkmodules.numpy_interface.dataset_adapter",
        "vtkmodules.util.numpy_support",
        "vtkmodules.util.pickle_support",
        "vtkmodules.vtkParallelCore",
    ],
    noarchive=False,
    optimize=1,
)

# Some upstream PyInstaller hooks add distribution metadata automatically.
# Studio does not query package versions at run time, Pydantic plugins are not
# supported, and the Windows keyring backend is selected directly in code.
a.datas = [
    entry
    for entry in a.datas
    if ".dist-info" not in entry[0].lower()
    and entry[0].replace("/", "\\").lower() != "certifi\\py.typed"
    and not entry[0].replace("/", "\\").lower().startswith(
        "pyside6\\translations\\"
    )
]


def _exclude_binary(destination: str) -> bool:
    """Remove build-host files and Qt features Studio does not use."""

    normalized = destination.replace("/", "\\").lower()
    name = normalized.rsplit("\\", 1)[-1]

    # Windows 10/11 provide API-set contracts and the Universal CRT. These
    # copies can otherwise leak in from a Windows Performance Toolkit PATH.
    if name.startswith("api-ms-win-") or name == "ucrtbase.dll":
        return True

    unused_qt_libraries = {
        "pyside6\\qt6network.dll",
        "pyside6\\qtnetwork.pyd",
        "pyside6\\qt6opengl.dll",
        "pyside6\\qt6openglwidgets.dll",
        "pyside6\\qtopengl.pyd",
        "pyside6\\qtopenglwidgets.pyd",
        "pyside6\\qt6pdf.dll",
        "pyside6\\qt6qml.dll",
        "pyside6\\qt6qmlmeta.dll",
        "pyside6\\qt6qmlmodels.dll",
        "pyside6\\qt6qmlworkerscript.dll",
        "pyside6\\qt6quick.dll",
        "pyside6\\qt6virtualkeyboard.dll",
        "pyside6\\opengl32sw.dll",
    }
    if normalized in unused_qt_libraries:
        return True

    if normalized.startswith(
        (
            "pyside6\\plugins\\generic\\",
            "pyside6\\plugins\\networkinformation\\",
            "pyside6\\plugins\\platforminputcontexts\\",
            "pyside6\\plugins\\tls\\",
        )
    ):
        return True

    if normalized.startswith("pyside6\\plugins\\platforms\\"):
        return name != "qwindows.dll"

    if normalized.startswith("pyside6\\plugins\\imageformats\\"):
        return name != "qsvg.dll"

    if normalized.startswith(("numpy\\", "numpy.libs\\")):
        return True

    if normalized in {
        "vtkmodules\\vtkparallelcore.cp312-win_amd64.pyd",
        "vtk.libs\\vtkparallelcore-9.6.2.dll",
    }:
        return True

    return False


a.binaries = [entry for entry in a.binaries if not _exclude_binary(entry[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DualSPHysicsStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    # The build script enforces a 64-bit Python runtime.  PyInstaller's
    # target_arch option is macOS-specific and must remain unset on Windows.
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(PROJECT_ROOT / "packaging" / "version_info.txt"),
    icon=str(RESOURCE_ROOT / "app_icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DualSPHysicsStudio",
)
