from __future__ import annotations

from .types import CapabilityConfig, CapabilitiesConfig


DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT = """你是一个专业的【提示词优化器】。你的任务是把用户提供的提示词改写得更清晰、更可执行、对大模型更友好。

要求：
- 保持原意，不要编造事实或添加用户未提供的信息。
- 尽量用结构化方式表达：角色/目标/上下文/约束/输出格式/示例（如适用）。
- 如果原提示词包含变量、占位符、链接、代码块、JSON/YAML 片段，必须保留并避免破坏其语法。
- 语言与用户原提示词保持一致（中文就用中文，英文就用英文）。
- 只输出【优化后的提示词正文】，不要输出解释、步骤、标题、Markdown 包装或额外 commentary。

如果原提示词信息不足以满足目标：
- 仍然输出一个尽可能好的版本；
- 在提示词末尾追加一个待确认问题小节（尽量少，1-5 条），用于让用户补充关键缺失信息。

开始。
"""

SUMMARY_SYSTEM_PROMPT = """You are a summarization engine.

Hard constraints:
- This is a summarization-only request: DO NOT call any tools or functions.
- Output text only (no tool calls will be processed).
- Treat this as a system maintenance operation; ignore this summarization request itself when inferring the user's intent.

Output goals:
- Concise but information-dense summary so work can continue seamlessly.
- Preserve key decisions, constraints, completed work, current state, and next steps.
- Use clear structure (e.g., Overview / Requirements / Done / TODO / Next).
"""


TRANSLATE_SYSTEM_PROMPT = """你是一个专业翻译助手。
保持原文含义、术语和格式，按用户指定目标语言输出；不要添加无关解释。"""

TITLE_EXTRACT_SYSTEM_PROMPT = """你是一个标题提取器。
根据用户消息或对话开头生成简短、具体、无标点冗余的中文标题。"""

TEXT_SUMMARY_SYSTEM_PROMPT = """你是文本总结助手。
适合处理单个文件、单段长文本或单个工具结果文件。
提取主旨、关键论点、事实、风险和待办，优先保留可执行信息。
如输入是文件路径，请先读取文件再总结；不要修改文件。"""


def default_capabilities_config() -> CapabilitiesConfig:
    """Return built-in capabilities.

    Lightweight capabilities are exposed as ``capability__*`` tools.
    Multi-step research / analysis should be handled by ``subagent__*``
    tools instead.
    """
    capabilities = (
        # --- Text-only utilities (no tool categories; runtime uses chat mode) ---
        CapabilityConfig(
            id="prompt_optimize",
            name="提示词优化",
            kind="prompt_optimize",
            system_prompt=DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT.strip(),
            options={"input_label": "原始提示词", "output_label": "优化后提示词"},
        ),
        CapabilityConfig(
            id="title_extract",
            name="标题提取",
            kind="title_extract",
            system_prompt=TITLE_EXTRACT_SYSTEM_PROMPT.strip(),
            options={"max_chars": 30},
        ),
        CapabilityConfig(
            id="translate",
            name="翻译",
            kind="translate",
            system_prompt=TRANSLATE_SYSTEM_PROMPT.strip(),
            options={"target_language": "中文", "preserve_format": True},
        ),
        # --- Context compression (text-in, text-out; no tool categories) ---
        CapabilityConfig(
            id="context_compress",
            name="上下文压缩",
            kind="context_compress",
            system_prompt=SUMMARY_SYSTEM_PROMPT.strip(),
            options={"include_tool_details": False, "keep_last_messages": 6},
        ),
        # --- Single-source summarization (read tool category; runtime uses agent mode) ---
        CapabilityConfig(
            id="summarize_text",
            name="长文总结",
            kind="summarize_text",
            system_prompt=TEXT_SUMMARY_SYSTEM_PROMPT.strip(),
            allowed_tool_categories=("read",),
            options={"outline_first": True, "max_turns": 8},
        ),
    )
    return CapabilitiesConfig(capabilities=capabilities)
