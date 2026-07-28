"""Domain exceptions used by the DualSPHysics Studio core.

The UI deliberately gets small, typed exceptions instead of parser or file-system
implementation details.  This keeps error handling useful in both Qt and tests.
"""

from __future__ import annotations


class CaseDocumentError(Exception):
    """Base class for document-model errors."""


class UnsafeXmlError(CaseDocumentError):
    """Raised when XML contains a construct that is intentionally forbidden."""


class XmlSyntaxError(CaseDocumentError):
    """Raised when XML is not well formed."""

    def __init__(self, message: str, *, line: int | None = None, column: int | None = None):
        super().__init__(message)
        self.line = line
        self.column = column


class SourceLocationError(CaseDocumentError):
    """Raised when a semantic node can no longer be mapped to source text."""


class GeometryEditError(CaseDocumentError):
    """Raised when a requested graphical edit cannot be represented safely."""


class ReadOnlyGeometryError(GeometryEditError):
    """Raised when an expression or complex command makes an object source-only."""


class BackupError(CaseDocumentError):
    """Raised when the mandatory pre-save backup cannot be created."""

