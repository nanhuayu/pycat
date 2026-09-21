"""Expose configured single-call LLM capabilities as model tools."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from types import SimpleNamespace
from typing import Any, Dict

from pycat.core.capabilities import CapabilitiesConfig, CapabilityConfig, default_capabilities_config
from pycat.core.capabilities.exposure import capability_exposed_as_tool
from pycat.core.capabilities.validation import validate_json_value
from pycat.core.content.images import MAX_IMAGE_BYTES, MAX_INPUT_IMAGES, inspect_image
from pycat.core.content.resolver import SessionContentResolver
from pycat.core.tools.base import BaseTool, ToolContext, ToolResult

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
    schema = dict(custom) if custom.get("type") == "object" else _default_schema(capability)
    properties = dict(schema.get("properties") or {})
    properties["image_refs"] = {"type": "array", "maxItems": MAX_INPUT_IMAGES, "items": {"type": "string"},
        "description": "Explicit current-session input: or archive:<id>/images/<number> references, or workspace:<path>."}
    if capability.operation == "image":
        properties["image_refs"]["description"] += " Omit for text-to-image; include references for editing, transformation or reference-based generation."
        properties.update({
            "mask_ref": {"type": "string", "description": "Optional PNG mask reference; transparent areas are edited."},
            "size": {"type": "string", "description": "auto or WIDTHxHEIGHT; Seedream also accepts 1K/2K/4K. Check the selected model's limits."},
            "quality": {"type": "string", "enum": ["auto", "low", "medium", "high", "xhigh", "max"]},
            "output_format": {"type": "string", "enum": ["auto", "png", "jpeg", "webp"]},
            "background": {"type": "string", "enum": ["auto", "opaque", "transparent"]},
            "n": {"type": "integer", "minimum": 1, "maximum": 4},
        })
    schema["properties"] = properties
    return schema


def _resolve_images(arguments: dict, context: ToolContext) -> tuple[list[str], str | None]:
    refs = list(arguments.get("image_refs") or [])
    mask_ref = str(arguments.get("mask_ref") or "").strip()
    if mask_ref:
        refs.append(mask_ref)
    if not refs:
        return [], None
    if context.conversation is None or context.content_service is None:
        raise ValueError("图片引用需要当前会话的内容服务。")
    resolver = SessionContentResolver(context.content_service)
    images, total = [], 0
    for ref in refs:
        if not ref.startswith(("input:", "archive:", "workspace:")):
            raise ValueError("请先附加图片，或使用当前会话的 input:/archive:/workspace: 引用。")
        if ref.startswith("workspace:"):
            context.resolve_read_path(ref[len("workspace:"):])
        source = resolver.resolve_content(context.conversation, ref)
        with source.path.open("rb") as stream:
            raw = stream.read(MAX_IMAGE_BYTES + 1)
        total += len(raw)
        if total > 100 * 1024 * 1024:
            raise ValueError("参考图片总大小不能超过 100 MiB。")
        if source.digest and hashlib.sha256(raw).hexdigest() != source.digest:
            raise ValueError("图片原件已变化，请重新附加。")
        images.append(inspect_image(raw).data_url)
    return (images[:-1], images[-1]) if mask_ref else (images, None)


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

        schema_error = validate_json_value(arguments, self.input_schema)
        if schema_error:
            return ToolResult(f"Capability input: {schema_error}", is_error=True)
        values = {key: value for key, value in arguments.items() if key not in {"image_refs", "mask_ref"}}
        message = str(values.get("text") or "") if self.capability.operation == "image" else _format_input(values)
        if not message:
            return ToolResult("Capability input is required.", is_error=True)

        runtime = getattr(context, "runtime", None)
        executor = getattr(runtime, "capability_executor", None)
        provider = getattr(context, "provider", None)
        if executor is None or provider is None:
            return ToolResult("Capability runtime is unavailable.", is_error=True)

        try:
            images, mask = await asyncio.to_thread(_resolve_images, arguments, context)
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
                    cancel_event=getattr(runtime, "cancel_event", None),
                ),
                config=getattr(executor, "capabilities", None),
                title=f"tool_{self.capability.id}",
                input_data=values,
                images=images,
                mask=mask,
            )
        except Exception as exc:
            return ToolResult(f"Capability '{self.capability.id}' failed: {exc}", is_error=True)
        validation_error = str(getattr(result, "validation_error", "") or "")
        if validation_error:
            return ToolResult(f"Capability '{self.capability.id}' failed: {validation_error}", is_error=True)
        text = str(getattr(result, "content", "") or "")
        metadata = dict(getattr(result, "metadata", {}) or {})
        metadata["model"] = str(getattr(result, "model", "") or "")
        if getattr(result, "images", ()):
            blocks = [{"type": "text", "text": text}]
            for image in result.images:
                header, _, data = image.partition(",")
                blocks.append({"type": "image", "mimeType": header[5:-7], "data": data})
            return ToolResult(blocks, metadata=metadata)
        return ToolResult(text, metadata=metadata)


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
