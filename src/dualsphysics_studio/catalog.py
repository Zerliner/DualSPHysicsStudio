from __future__ import annotations

import json
from importlib.resources import files
from typing import Mapping


class SemanticCatalog:
    def __init__(self) -> None:
        resource = files("dualsphysics_studio.resources").joinpath("semantic_catalog.json")
        self._data = json.loads(resource.read_text(encoding="utf-8"))

    @property
    def version(self) -> str:
        return str(self._data.get("version", "5.4"))

    def lookup(self, tag: str, attributes: Mapping[str, str]) -> dict[str, object] | None:
        entries: dict[str, dict[str, object]] = self._data.get("entries", {})
        if tag == "parameter" and attributes.get("key"):
            item = entries.get(f"parameter:{attributes['key']}")
            if item:
                return item
        return entries.get(tag)

    def explain(self, tag: str, attributes: Mapping[str, str]) -> str:
        item = self.lookup(tag, attributes)
        if not item:
            return "暂无可靠的 DualSPHysics v5.4 离线解释；已保留原始节点与属性。"
        lines = [str(item.get("title", tag)), str(item.get("description", ""))]
        unit = item.get("unit")
        if unit:
            lines.append(f"单位：{unit}")
        values = item.get("values")
        if isinstance(values, dict) and "value" in attributes:
            meaning = values.get(str(attributes["value"]))
            if meaning:
                lines.append(f"当前值：{attributes['value']}（{meaning}）")
        warning = item.get("warning")
        if warning:
            lines.append(f"提示：{warning}")
        return "\n".join(line for line in lines if line)

