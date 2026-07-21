"""Expose configured single-call LLM capabilities as model tools."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any, Dict

from core.capabilities import CapabilitiesConfig, CapabilityConfig, default_capabilities_config
from core.capabilities.exposure import capability_exposed_as_tool
from core.tools.base import BaseTool, ToolContext, ToolResult


CAPABILITY_TOOL_PREFIX = "capability__"


def capability_tool_name(capability_id: str) -> str:
    raw = str(capability_id or "").strip().lower()
    safe = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_") or "capability"
    return f"{CAPABILITY_TOOL_PREFIX}{re.sub(r'_+', '_', safe)}"


def _default_schema(capability: CapabilityConfig) -> dict[str, Any]:
    if capability.id == "summarize":
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to summarize."},
                "focus": {"type": "string", "description": "Optional aspect to emphasize."},
            },
            "required": ["text"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Input text."}},
        "required": ["text"],
        "additionalProperties": False,
    }


def _schema(capability: CapabilityConfig) -> dict[str, Any]:
    custom = capability.input_schema if isinstance(capability.input_schema, dict) else {}
    return dict(custom) if custom.get("type") == "object" else _default_schema(capability)


def _format_input(arguments: Dict[str, Any]) -> str:
    values = {str(key): value for key, value in (arguments or {}).items() if value not in (None, "")}
    text = values.pop("text", None)
    lines: list[str] = []
    if text is not None:
        lines.append(str(text))
    if values:
        lines.append("Additional input:\n" + json.dumps(values, ensure_ascii=False, indent=2))
    return "\n\n".join(lines).strip()


class CapabilityTool(BaseTool):
    """Run one configured capability as a single no-tool LLM conversion."""

    def __init__(self, capability: CapabilityConfig):
        self.capability = capability
        self._tool_name = capability_tool_name(capability.id)

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def display_name(self) -> str:
        return self.capability.name

    @property
    def description(self) -> str:
        return (
            self.capability.description.strip()
            or f"Run {self.capability.name} on supplied input and return the converted text."
        )

    @property
    def category(self) -> str:
        return "capability"

    @property
    def source(self) -> str:
        return "capability"

    @property
    def input_schema(self) -> Dict[str, Any]:
        return _schema(self.capability)

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        if not self.capability.enabled:
            return ToolResult(f"Capability '{self.capability.id}' is disabled.", is_error=True)

        message = _format_input(arguments)
        if not message:
            return ToolResult("Capability input is required.", is_error=True)

        runtime = getattr(context, "runtime", None)
        executor = getattr(runtime, "capability_executor", None)
        provider = getattr(context, "provider", None)
        if executor is None or provider is None:
            return ToolResult("Capability runtime is unavailable.", is_error=True)

        try:
            result = await executor.run_capability(
                provider=provider,
                capability_id=self.capability.id,
                message=message,
                conversation=getattr(context, "conversation", None),
                context=SimpleNamespace(
                    caller="tool",
                    source=str(getattr(runtime, "source", "desktop") or "desktop"),
                    entrypoint=self.name,
                    trace_id=str(getattr(runtime, "trace_id", "") or ""),
                    debug_trace=getattr(runtime, "debug_trace", None),
                    parent_policy=getattr(runtime, "run_policy", None),
                    approval_callback=getattr(context, "approval_callback", None),
                    questions_callback=getattr(context, "questions_callback", None),
                ),
                config=getattr(executor, "capabilities", None),
                title=f"tool_{self.capability.id}",
                input_data=dict(arguments or {}),
            )
        except Exception as exc:
            return ToolResult(f"Capability '{self.capability.id}' failed: {exc}", is_error=True)
        validation_error = str(getattr(result, "validation_error", "") or "")
        if validation_error:
            return ToolResult(f"Capability '{self.capability.id}' failed: {validation_error}", is_error=True)
        return ToolResult(str(getattr(result, "content", "") or ""))


def build_capability_tools(config: CapabilitiesConfig | None = None) -> list[CapabilityTool]:
    cfg = config or default_capabilities_config()
    tools: list[CapabilityTool] = []
    seen: set[str] = set()
    for capability in cfg.capabilities:
        if not capability_exposed_as_tool(capability):
            continue
        tool = CapabilityTool(capability)
        if tool.name in seen:
            continue
        seen.add(tool.name)
        tools.append(tool)
    return tools
