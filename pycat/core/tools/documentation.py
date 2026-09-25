"""Render the canonical built-in tool catalog from runtime metadata."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from pycat.core.tools.base import BaseTool
from pycat.models.contracts.tooling import (
    RISK_LEVEL_LABELS,
    TOOL_CATEGORY_LABELS,
    TOOL_CATEGORY_SORT_ORDER,
)

TOOL_CATALOG_START = "<!-- BEGIN GENERATED TOOL CATALOG -->"
TOOL_CATALOG_END = "<!-- END GENERATED TOOL CATALOG -->"

_SOURCE_LABELS = {
    "builtin": "内置",
    "search": "搜索服务",
    "skill": "Skill",
    "capability": "Capability",
    "mcp": "MCP",
}


def _escape_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _schema_type(schema: Mapping[str, Any]) -> str:
    raw_type = schema.get("type")
    if isinstance(raw_type, list):
        return "/".join(str(item) for item in raw_type)
    if raw_type:
        return str(raw_type)
    if "$ref" in schema:
        return "ref"
    if "anyOf" in schema:
        return "anyOf"
    if "oneOf" in schema:
        return "oneOf"
    return "any"


def _format_parameters(schema: Mapping[str, Any]) -> str:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        return "无"
    required = {str(item) for item in schema.get("required") or ()}
    parts: list[str] = []
    for name, raw_property in properties.items():
        property_schema = raw_property if isinstance(raw_property, Mapping) else {}
        optional = "" if str(name) in required else "?"
        parts.append(f"`{name}{optional}:{_schema_type(property_schema)}`")
    return ", ".join(parts)


def _catalog_payload(tools: Iterable[BaseTool]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in tools:
        descriptor = tool.descriptor()
        if not descriptor.name or descriptor.name in seen:
            raise ValueError(f"Duplicate or empty tool name in catalog: {descriptor.name!r}")
        seen.add(descriptor.name)
        payload.append(
            {
                "name": descriptor.name,
                "display_name": descriptor.display_name,
                "description": descriptor.description,
                "category": descriptor.category,
                "source": descriptor.source,
                "risk": descriptor.risk,
                "schema": dict(tool.input_schema or {}),
            }
        )
    return sorted(
        payload,
        key=lambda item: (
            TOOL_CATEGORY_SORT_ORDER.get(str(item["category"]), 1000),
            str(item["name"]),
        ),
    )


def render_tool_catalog(tools: Iterable[BaseTool]) -> str:
    """Render a stable Markdown table plus a digest of the complete schemas."""
    payload = _catalog_payload(tools)
    lines = [
        TOOL_CATALOG_START,
        "| 工具 | 类别 | 来源 | 风险 | 顶层参数 | 模型可见描述 |",
        "|---|---|---|---|---|---|",
    ]
    for item in payload:
        display_name = _escape_cell(item["display_name"])
        technical_name = _escape_cell(item["name"])
        tool_cell = f"{display_name}<br>`{technical_name}`" if display_name != technical_name else f"`{technical_name}`"
        category = str(item["category"])
        source = str(item["source"])
        risk = str(item["risk"])
        lines.append(
            "| {tool} | {category} | {source} | {risk} | {parameters} | {description} |".format(
                tool=tool_cell,
                category=f"{TOOL_CATEGORY_LABELS.get(category, category)} (`{category}`)",
                source=_SOURCE_LABELS.get(source, source),
                risk=RISK_LEVEL_LABELS.get(risk, risk),
                parameters=_format_parameters(item["schema"]),
                description=_escape_cell(item["description"]),
            )
        )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    lines.extend((f"<!-- tool-catalog-schema-sha256: {digest} -->", TOOL_CATALOG_END))
    return "\n".join(lines)


def extract_tool_catalog(document: str) -> str:
    start = document.find(TOOL_CATALOG_START)
    end = document.find(TOOL_CATALOG_END)
    if start < 0 or end < start:
        raise ValueError("Tool catalog markers are missing or out of order.")
    return document[start : end + len(TOOL_CATALOG_END)]


def replace_tool_catalog(document: str, generated_catalog: str) -> str:
    current = extract_tool_catalog(document)
    return document.replace(current, generated_catalog, 1)
