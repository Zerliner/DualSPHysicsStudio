from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class NodeExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str = Field(description="XML 节点路径或可识别选择器")
    title: str
    explanation: str


class RiskItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    severity: Literal["info", "warning", "error"]
    selector: str
    message: str


class EditSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str
    attribute: str
    old_value: str
    new_value: str
    reason: str


class ExplainResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    node_explanations: list[NodeExplanation]
    risks: list[RiskItem]
    suggested_edits: list[EditSuggestion]


@dataclass(slots=True)
class ExplainRequest:
    xml_text: str
    selected_selector: str = "/case"
    gencase_version: str = "v5.4"
    offline_diagnostics: str = ""
    log_excerpt: str = ""


class AIProvider(Protocol):
    def explain(self, request: ExplainRequest) -> ExplainResult: ...

