from __future__ import annotations

from models.contracts.capability import CapabilitiesConfig, CapabilityConfig


DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT = """你是提示词优化器。保持原意、变量、链接、代码块和结构化片段不变，把输入改写得清晰、具体、可执行。使用与原文相同的语言，只输出优化后的提示词正文；信息不足时在末尾列出少量待确认问题。"""

TITLE_PROMPT = """Generate a short, specific conversation title in the user's language. Return the title only, without punctuation or commentary."""

COMPRESS_PROMPT = """Compress only the supplied material. Follow the purpose contract exactly. Do not add facts or describe the summarization operation. Return only the requested structured output, with no analysis."""

SUMMARIZE_PROMPT = """Summarize the supplied text. Preserve important facts, decisions, constraints, risks, references, and actionable next steps. Follow the requested focus when present and do not call tools."""

MEMORY_ADVISE_PROMPT = """You are PyCat's read-only Memory Advisor. Use only the supplied memory catalog and return strict JSON. Every advice item must cite supplied source_ids. Return {\"advice\": []} when no stored memory is clearly useful."""

MEMORY_CURATE_PROMPT = """You are PyCat's Memory Curator. Propose only concise, durable facts supported by supplied completed milestones or final artifacts. Exclude transient status, secrets, guesses, raw logs, and long reports. Return strict JSON with a candidates array; use refs from the supplied allowed refs only."""

MEMORY_ADVISE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "advice": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                    "verify": {"type": "string"},
                },
                "required": ["text", "source_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["advice"],
    "additionalProperties": False,
}

MEMORY_CURATE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "maxLength": 600},
                    "scope": {"type": "string", "enum": ["session", "workspace", "global"]},
                    "category": {
                        "type": "string",
                        "enum": ["preference", "fact", "decision", "convention", "command", "gotcha"],
                    },
                    "reason": {"type": "string", "maxLength": 600},
                    "refs": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                },
                "required": ["content", "scope", "category", "refs"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


def default_capabilities_config() -> CapabilitiesConfig:
    return CapabilitiesConfig(
        capabilities=(
            CapabilityConfig(
                id="prompt_optimize",
                name="提示词优化",
                exposure="internal",
                prompt=DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT.strip(),
                temperature=0.2,
                max_tokens=800,
            ),
            CapabilityConfig(
                id="title",
                name="标题提取",
                exposure="internal",
                prompt=TITLE_PROMPT.strip(),
            ),
            CapabilityConfig(
                id="compress",
                name="上下文压缩",
                exposure="internal",
                prompt=COMPRESS_PROMPT.strip(),
            ),
            CapabilityConfig(
                id="memory_advise",
                name="Memory advice",
                exposure="internal",
                runtime="single_turn",
                description="Provide source-backed advice from already stored memory.",
                prompt=MEMORY_ADVISE_PROMPT,
                output_schema=MEMORY_ADVISE_OUTPUT_SCHEMA,
            ),
            CapabilityConfig(
                id="memory_curate",
                name="Memory curation",
                exposure="internal",
                runtime="single_turn",
                description="Propose reviewable durable-memory candidates from completed work.",
                prompt=MEMORY_CURATE_PROMPT,
                output_schema=MEMORY_CURATE_OUTPUT_SCHEMA,
            ),
            CapabilityConfig(
                id="summarize",
                name="文本总结",
                exposure="tool",
                runtime="single_turn",
                description="Summarize supplied text; read files or archives first and pass their content here.",
                prompt=SUMMARIZE_PROMPT.strip(),
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "Text to summarize."},
                        "focus": {"type": "string", "description": "Optional aspect to emphasize."},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            ),
        )
    )
