"""Public core API for DualSPHysics Studio."""

from .document import CaseDocument
from .errors import (
    BackupError,
    CaseDocumentError,
    GeometryEditError,
    ReadOnlyGeometryError,
    SourceLocationError,
    UnsafeXmlError,
    XmlSyntaxError,
)
from .models import (
    BoundingBox,
    Diagnostic,
    DocumentFacts,
    GeometryType,
    MarkerKind,
    NodeExplanation,
    ObjectRole,
    SaveResult,
    SceneObject,
    Severity,
    Vec3,
    XmlParameterField,
)

__all__ = [
    "BackupError",
    "BoundingBox",
    "CaseDocument",
    "CaseDocumentError",
    "Diagnostic",
    "DocumentFacts",
    "GeometryEditError",
    "GeometryType",
    "MarkerKind",
    "NodeExplanation",
    "ObjectRole",
    "ReadOnlyGeometryError",
    "SaveResult",
    "SceneObject",
    "Severity",
    "SourceLocationError",
    "UnsafeXmlError",
    "Vec3",
    "XmlSyntaxError",
    "XmlParameterField",
]
