"""Bundled UI and paraphrased DualSPHysics v5.4 semantic resources."""

from __future__ import annotations

from importlib.resources import as_file, files
from pathlib import Path


def resource_path(name: str) -> Path:
    """Return a usable path for a bundled resource.

    PyInstaller's onedir layout exposes package resources as ordinary files.
    The fallback context is retained for compatibility with zipped imports.
    """

    resource = files(__name__).joinpath(name)
    try:
        return Path(resource)  # type: ignore[arg-type]
    except TypeError:
        with as_file(resource) as path:
            return Path(path)


__all__ = ["resource_path"]
