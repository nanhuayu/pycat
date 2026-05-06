from __future__ import annotations

from core.prompts.templates import DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT

from .types import CapabilityConfig, CapabilitiesConfig


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
        # --- Text-only utilities (chat mode, no tools needed) ---
        CapabilityConfig(
            id="prompt_optimize",
            name="提示词优化",
            kind="prompt_optimize",
            mode="chat",
            system_prompt=DEFAULT_PROMPT_OPTIMIZER_SYSTEM_PROMPT.strip(),
            options={"input_label": "原始提示词", "output_label": "优化后提示词"},
        ),
        CapabilityConfig(
            id="title_extract",
            name="标题提取",
            kind="title_extract",
            mode="chat",
            system_prompt=TITLE_EXTRACT_SYSTEM_PROMPT.strip(),
            options={"max_chars": 30},
        ),
        CapabilityConfig(
            id="translate",
            name="翻译",
            kind="translate",
            mode="chat",
            system_prompt=TRANSLATE_SYSTEM_PROMPT.strip(),
            options={"target_language": "中文", "preserve_format": True},
        ),
        # --- Context compression (chat mode: text-in, text-out) ---
        CapabilityConfig(
            id="context_compress",
            name="上下文压缩",
            kind="context_compress",
            mode="chat",
            system_prompt=SUMMARY_SYSTEM_PROMPT.strip(),
            options={"include_tool_details": False, "keep_last_messages": 6},
        ),
        # --- Single-source summarization (agent mode with read tool) ---
        CapabilityConfig(
            id="summarize_text",
            name="长文总结",
            kind="summarize_text",
            mode="agent",
            system_prompt=TEXT_SUMMARY_SYSTEM_PROMPT.strip(),
            tool_groups=("read",),
            options={"outline_first": True, "max_turns": 8},
        ),
    )
    return CapabilitiesConfig(capabilities=capabilities)
