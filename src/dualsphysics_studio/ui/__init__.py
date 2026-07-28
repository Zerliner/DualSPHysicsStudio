"""Desktop interface for DualSPHysics Studio.

The heavy Qt/VTK modules are imported lazily so command-line tooling and core
unit tests can use the package without initializing a graphics backend.
"""

from __future__ import annotations

from typing import Any

__all__ = ["MainWindow", "VTKViewport", "VTK_AVAILABLE"]


def __getattr__(name: str) -> Any:
    if name == "MainWindow":
        from .main_window import MainWindow

        return MainWindow
    if name in {"VTKViewport", "VTK_AVAILABLE"}:
        from .viewport import VTK_AVAILABLE, VTKViewport

        return {"VTKViewport": VTKViewport, "VTK_AVAILABLE": VTK_AVAILABLE}[name]
    raise AttributeError(name)
