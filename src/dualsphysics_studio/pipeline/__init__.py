"""Executable pipeline API for DualSPHysics Studio 1.0.1."""

from .base import (
    IncompatibleToolError,
    LogCallback,
    PipelineAdapter,
    PipelineError,
    ToolNotFoundError,
)
from .gencase import (
    GENCASE_CONFIG,
    GENCASE_EXECUTABLE,
    SUPPORTED_VERSION,
    GenCaseAdapter,
    parse_case_out,
    parse_gencase_output,
)
from .dualsphysics import (
    DUALSPHYSICS_CPU_EXECUTABLE,
    DUALSPHYSICS_GPU_EXECUTABLE,
    PARTVTK_EXECUTABLE,
    PARTVTK_VARIABLES,
    DualSPHysicsCPUAdapter,
    DualSPHysicsGPUAdapter,
    PartVTKAdapter,
)
from .models import (
    Artifact,
    ArtifactManifest,
    RunRequest,
    RunResult,
    ToolInfo,
    file_sha256,
    manifest_is_stale,
)

__all__ = [
    "Artifact",
    "ArtifactManifest",
    "DUALSPHYSICS_CPU_EXECUTABLE",
    "DUALSPHYSICS_GPU_EXECUTABLE",
    "GENCASE_CONFIG",
    "GENCASE_EXECUTABLE",
    "GenCaseAdapter",
    "IncompatibleToolError",
    "LogCallback",
    "PipelineAdapter",
    "PipelineError",
    "PARTVTK_EXECUTABLE",
    "PARTVTK_VARIABLES",
    "PartVTKAdapter",
    "RunRequest",
    "RunResult",
    "SUPPORTED_VERSION",
    "ToolInfo",
    "ToolNotFoundError",
    "DualSPHysicsCPUAdapter",
    "DualSPHysicsGPUAdapter",
    "file_sha256",
    "manifest_is_stale",
    "parse_case_out",
    "parse_gencase_output",
]
