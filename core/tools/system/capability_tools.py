"""Capability tool adapters and child-task scheduling helpers.

Capability definitions live in :mod:`core.capabilities` and remain the single
source of truth.  This module adapts those definitions to the tool runtime:

- load and merge configured capabilities;
- expose each enabled capability as its own ``capability__*`` tool;
- render capability instructions into child-task messages;
- schedule child tasks through ``ToolContext.state``.

There is intentionally no single ``run_capability`` router here.  Each enabled
capability is a first-class tool with a stable prefixed name.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Sequence

from core.capabilities import CapabilitiesConfig, CapabilityConfig, default_capabilities_config
from core.capabilities.exposure import capability_exposed_as_tool
from core.capabilities.manager import CapabilitiesManager
from core.config import load_app_config
from core.tools.base import BaseTool, ToolContext, ToolResult
from core.tools.catalog import ToolSelectionPolicy


CAPABILITY_TOOL_PREFIX = "capability__"
_COMMON_CAPABILITY_FIELDS = {
    "task",
    "input_text",
    "path",
    "output_format",
    "max_length",
    "focus",
    "target_language",
    "instructions",
}


def load_capabilities_config() -> CapabilitiesConfig:
    """Return built-in capabilities merged with app-level overrides."""
    try:
        cfg = load_app_config()
        capabilities = getattr(cfg, "capabilities", None)
        if isinstance(capabilities, CapabilitiesConfig):
            return CapabilitiesManager.merge(default_capabilities_config(), capabilities)
    except Exception:
        pass
    return default_capabilities_config()


def capability_tool_name(capability_id: str) -> str:
    """Return the stable tool name for a capability id."""
    raw = str(capability_id or "").strip().lower()
    safe = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_") or "capability"
    return f"{CAPABILITY_TOOL_PREFIX}{safe}"


def normalize_string_list(value: Any) -> list[str]:
    """Normalize a list-like or comma-separated value into unique strings."""
    if value is None:
        items: Sequence[Any] = ()
    elif isinstance(value, str):
        items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = (value,)

    normalized: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def normalize_subtask_allowed_tool_categories(categories: Iterable[str] | None) -> list[str]:
    """Normalize a subtask tool-category list and always keep manage tools enabled."""
    from core.tools.catalog import normalize_tool_category

    normalized: list[str] = []
    for category in normalize_string_list(list(categories or ())):
        canonical = normalize_tool_category(category)
        if canonical not in normalized:
            normalized.append(canonical)
    if "manage" not in normalized:
        normalized.append("manage")
    return normalized


def capability_runtime_mode(capability: CapabilityConfig) -> str:
    """Infer child-task mode from the capability tool categories.

    Capabilities no longer expose a separate user-configured mode. Text-only
    capabilities run as chat subtasks; any capability with tool categories runs as
    an agent subtask so those tools can be used.
    """
    return "agent" if tuple(capability.allowed_tool_categories or ()) else "chat"


def capability_prompt_block(capability: CapabilityConfig) -> str:
    lines = [
        f"Capability: {capability.name} ({capability.id})",
        f"Kind: {capability.kind}",
    ]
    if capability.system_prompt:
        lines.append("Instructions:\n" + capability.system_prompt.strip())
    if capability.options:
        lines.append(f"Options: {capability.options}")
    return "\n".join(lines)


def build_capability_subtask_message(
    *,
    task: str,
    input_text: str = "",
    output_format: str = "",
    capability: CapabilityConfig | None = None,
    capabilities: Iterable[CapabilityConfig] | None = None,
    all_capabilities: CapabilitiesConfig | None = None,
    instructions: str = "",
) -> str:
    """Render a focused child-task message from capability config."""
    sections: list[str] = []

    selected_capabilities: list[CapabilityConfig] = []
    if capabilities is not None:
        selected_capabilities.extend(list(capabilities))
    if capability is not None:
        selected_capabilities.append(capability)

    seen_capabilities: set[str] = set()
    for cap in selected_capabilities:
        if cap.id in seen_capabilities:
            continue
        seen_capabilities.add(cap.id)
        sections.append(capability_prompt_block(cap))

    runtime_instructions = str(instructions or "").strip()
    if runtime_instructions:
        sections.append("Runtime instructions from parent agent:\n" + runtime_instructions)

    sections.append(
        "Task:\n"
        f"{str(task or '').strip()}\n\n"
        "Input:\n"
        f"{str(input_text or '').strip() or '-'}"
    )
    sections.append(
        "Output requirements:\n"
        f"- Return {str(output_format or '').strip() or 'a concise report'} to the parent agent.\n"
        "- If you create or read large artifacts, include their file paths.\n"
        "- Prefer structured findings, key evidence, risks, and next actions.\n"
        "- Finish by calling attempt_completion with the final report."
    )
    return "\n\n---\n\n".join(section for section in sections if section.strip())


def schedule_subtask(
    context: ToolContext,
    *,
    mode: str,
    message: str,
    title: str = "",
    kind: str = "subagent",
    model_ref: str = "",
    max_turns: int | None = None,
    allowed_tool_categories: Iterable[str] | None = None,
    capability: CapabilityConfig | None = None,
    capability_id: str = "",
    capabilities: Iterable[str] | None = None,
    instructions: str = "",
    auto_spillover: bool | None = None,
    tool_selection: ToolSelectionPolicy | None = None,
) -> None:
    """Schedule a child task using the Task engine's pending-subtask contract."""
    capability_ids = normalize_string_list(list(capabilities or ()))
    resolved_capability_id = capability.id if capability is not None else str(capability_id or "").strip()
    if resolved_capability_id and resolved_capability_id not in capability_ids:
        capability_ids.insert(0, resolved_capability_id)

    normalized_categories = normalize_subtask_allowed_tool_categories(allowed_tool_categories)
    payload: dict[str, Any] = {
        "mode": (mode or "agent").strip() or "agent",
        "message": message,
        "title": str(title or "").strip(),
        "kind": str(kind or "subagent").strip() or "subagent",
        "model_ref": model_ref or "",
        "max_turns": max_turns,
        "capability_id": resolved_capability_id,
        "capabilities": capability_ids,
    }
    if tool_selection is not None:
        payload["tool_selection"] = tool_selection.to_dict()
    else:
        payload["tool_selection"] = ToolSelectionPolicy.from_categories(normalized_categories).to_dict()
    runtime_instructions = str(instructions or "").strip()
    if runtime_instructions:
        payload["instructions"] = runtime_instructions
    if auto_spillover is not None:
        payload["auto_spillover"] = bool(auto_spillover)
    context.state["_pending_subtask"] = payload


class CapabilityTool(BaseTool):
    """Run one configured capability through the child-task scheduler."""

    def __init__(self, capability: CapabilityConfig, all_capabilities: CapabilitiesConfig | None = None):
        self.capability = capability
        self.all_capabilities = all_capabilities or CapabilitiesConfig(capabilities=(capability,))
        self._tool_name = capability_tool_name(capability.id)

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        options = self.capability.options if isinstance(self.capability.options, dict) else {}
        configured = str(options.get("tool_description") or options.get("description") or "").strip()
        if configured:
            return configured
        # Short, model-friendly descriptions without leaking the full system prompt.
        defaults: dict[str, str] = {
            "translate": "Translate or polish text into the target language. Use this for localization, rewriting, or language conversion.",
            "prompt_optimize": "Optimize a user prompt to be clearer and more actionable for large language models.",
            "title_extract": "Generate a concise, punctuation-free Chinese title from the provided text or message.",
            "summarize_text": "Summarize one file, one long text, or one tool-result file into a concise structured report. For multi-file synthesis, use subagent__read_analyze.",
            "context_compress": "Compress a long conversation into a condensed summary while preserving key facts, decisions, and pending tasks. Use this when the context window is approaching its limit.",
        }
        return defaults.get(
            self.capability.id,
            f"Run the '{self.capability.name}' capability and return its result.",
        )


    @property
    def category(self) -> str:
        return "extension"

    @property
    def source(self) -> str:
        return "capability"

    @property
    def input_schema(self) -> Dict[str, Any]:
        schema: Dict[str, Any] = {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Focused task for this capability. If omitted, a task is inferred from the capability.",
                },
                "input_text": {
                    "type": "string",
                    "description": "Text, file path, URL content, or extracted material to process.",
                },
                "path": {
                    "type": "string",
                    "description": "Optional workspace file path or tool-result file path to read/analyze.",
                },
                "output_format": {
                    "type": "string",
                    "description": "Desired output shape, e.g. summary, bullet points, report, translation.",
                },
                "max_length": {
                    "type": "number",
                    "description": "Optional desired maximum output length in characters.",
                },
                "focus": {
                    "type": "string",
                    "description": "Optional focus area, e.g. errors, architecture, risks, data changes.",
                },
                "target_language": {
                    "type": "string",
                    "description": "Optional target language for translation or localization capabilities.",
                },
                "instructions": {
                    "type": "string",
                    "description": "Optional runtime instructions from the parent agent for this invocation.",
                },
            },
            "additionalProperties": False,
        }
        custom = self.capability.input_schema if isinstance(self.capability.input_schema, dict) else {}
        if custom.get("type") == "object":
            properties = custom.get("properties")
            if isinstance(properties, dict):
                schema["properties"].update(properties)
            required = custom.get("required")
            if isinstance(required, list):
                schema["required"] = [str(item) for item in required if str(item).strip()]
            if "additionalProperties" in custom:
                schema["additionalProperties"] = custom.get("additionalProperties")
        return schema

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> ToolResult:
        capability = self.capability
        if not capability.enabled:
            return ToolResult(f"Capability '{capability.id}' is disabled.", is_error=True)

        task = str(arguments.get("task") or "").strip() or self._default_task(capability)
        output_format = str(arguments.get("output_format") or "").strip() or self._default_output_format(capability)
        input_text = self._build_input_text(arguments)
        runtime_instructions = str(arguments.get("instructions") or "").strip()

        message = build_capability_subtask_message(
            task=task,
            input_text=input_text,
            output_format=output_format,
            capability=capability,
            all_capabilities=self.all_capabilities,
            instructions=runtime_instructions,
        )

        schedule_subtask(
            context,
            mode=capability_runtime_mode(capability),
            message=message,
            title=capability.name,
            kind="capability",
            model_ref=capability.model_ref or "",
            max_turns=self._max_turns(capability),
            allowed_tool_categories=capability.allowed_tool_categories or (),
            capability=capability,
            capabilities=(capability.id,),
            instructions=self._combined_instructions(capability, runtime_instructions),
        )

        return ToolResult(
            f"Capability tool '{self.name}' scheduled '{capability.name}' ({capability.id}). "
            "Its result will be returned to the parent agent."
        )

    @staticmethod
    def _build_input_text(arguments: Dict[str, Any]) -> str:
        parts: list[str] = []
        path = str(arguments.get("path") or "").strip()
        input_text = str(arguments.get("input_text") or "").strip()
        focus = str(arguments.get("focus") or "").strip()
        target_language = str(arguments.get("target_language") or "").strip()
        max_length = arguments.get("max_length")

        if path:
            parts.append(f"File path: {path}")
        if input_text:
            parts.append(input_text)
        if focus:
            parts.append(f"Focus: {focus}")
        if target_language:
            parts.append(f"Target language: {target_language}")
        if max_length not in (None, ""):
            try:
                parts.append(f"Desired maximum output length: {int(max_length)} characters")
            except Exception:
                parts.append(f"Desired maximum output length: {max_length}")

        for key, value in sorted((arguments or {}).items()):
            if key in _COMMON_CAPABILITY_FIELDS or value in (None, ""):
                continue
            parts.append(f"{key}: {value}")
        return "\n\n".join(parts).strip()

    @staticmethod
    def _combined_instructions(capability: CapabilityConfig, runtime_instructions: str = "") -> str:
        return "\n\n".join(
            part
            for part in [str(capability.system_prompt or "").strip(), str(runtime_instructions or "").strip()]
            if part
        )

    @staticmethod
    def _max_turns(capability: CapabilityConfig) -> int | None:
        options = capability.options if isinstance(capability.options, dict) else {}
        raw_value = options.get("max_turns")
        try:
            value = int(raw_value) if raw_value not in (None, "") else 0
        except Exception:
            value = 0
        return value if value > 0 else None

    @staticmethod
    def _default_task(capability: CapabilityConfig) -> str:
        defaults = {
            "prompt_optimize": "Optimize the provided prompt while preserving the user's intent.",
            "translate": "Translate or polish the provided text according to the capability instructions.",
            "title_extract": "Generate a concise Chinese title from the provided text or message.",
            "summarize_text": "Summarize the provided single long text, file path, or tool-result file into a concise structured report.",
            "context_compress": "Compress the provided conversation context into a condensed summary preserving key facts, decisions, and pending tasks.",
        }
        return defaults.get(capability.id, f"Run capability {capability.id} on the provided input.")

    @staticmethod
    def _default_output_format(capability: CapabilityConfig) -> str:
        defaults = {
            "prompt_optimize": "optimized prompt",
            "translate": "translated text",
            "title_extract": "concise title",
            "summarize_text": "structured summary",
            "context_compress": "condensed summary",
        }
        return defaults.get(capability.id, "concise report")


def build_capability_tools(config: CapabilitiesConfig | None = None) -> list[CapabilityTool]:
    """Build one tool instance for each configured capability.

    Capability definitions are first-class runtime entities.  Enabled
    capabilities are exposed as ``capability__*`` tools unless explicitly
    hidden with ``options.expose_as_tool = False``.
    """
    cfg = config or load_capabilities_config()
    tools: list[CapabilityTool] = []
    seen_tool_names: set[str] = set()
    for capability in cfg.capabilities:
        if not capability_exposed_as_tool(capability):
            continue
        tool = CapabilityTool(capability, cfg)
        if tool.name in seen_tool_names:
            continue
        seen_tool_names.add(tool.name)
        tools.append(tool)
    return tools
