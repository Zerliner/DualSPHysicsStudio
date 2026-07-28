"""Abstract interfaces for local DualSPHysics pipeline stages."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from threading import Event
from typing import Callable

from .models import RunRequest, RunResult, ToolInfo


LogCallback = Callable[[str, str], None]


class PipelineError(RuntimeError):
    """Base error raised while preparing an external pipeline stage."""


class ToolNotFoundError(PipelineError):
    """The requested external executable or its configuration was not found."""


class IncompatibleToolError(PipelineError):
    """The external tool was found but is unsupported by this application."""


class PipelineAdapter(ABC):
    """Stable interface implemented by each executable pipeline stage."""

    stage: str

    @abstractmethod
    def probe(self, source_xml: str | Path | None = None) -> ToolInfo:
        """Locate the external tool and report its version and readiness."""

    @abstractmethod
    def build_command(
        self, request: RunRequest, tool_info: ToolInfo | None = None
    ) -> tuple[str, ...]:
        """Build an argument vector suitable for ``shell=False`` execution."""

    @abstractmethod
    def run(
        self,
        request: RunRequest,
        *,
        on_log: LogCallback | None = None,
        cancel_event: Event | None = None,
    ) -> RunResult:
        """Synchronously execute a stage, streaming output through ``on_log``."""

    @abstractmethod
    def cancel(self) -> bool:
        """Request cancellation of the active run, if any."""
