"""Executable pipeline API for DualSPHysics Studio."""

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
from .estimate import (
    ResourceEstimate,
    estimate_case,
    format_bytes,
    live_stage_progress,
)
from .session import (
    SESSION_DIRECTORY,
    SESSION_SCHEMA_VERSION,
    RunSession,
    find_case_sessions,
    session_history_values,
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
    "RunSession",
    "ResourceEstimate",
    "SESSION_DIRECTORY",
    "SESSION_SCHEMA_VERSION",
    "SUPPORTED_VERSION",
    "ToolInfo",
    "ToolNotFoundError",
    "DualSPHysicsCPUAdapter",
    "DualSPHysicsGPUAdapter",
    "file_sha256",
    "find_case_sessions",
    "estimate_case",
    "format_bytes",
    "live_stage_progress",
    "manifest_is_stale",
    "parse_case_out",
    "parse_gencase_output",
    "session_history_values",
]
