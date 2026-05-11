"""Unified context condenser.

Merges ``core.condense.condenser.ContextCondenser`` and
``core.state.services.compression_service.CompressionService`` into a
single class with a clean ``auto_condense`` entry point that the Task
engine can call on every turn.

Main changes vs the old split design:
- ``auto_condense`` is the **only** public entry — it runs per-message
    condensation AND global archive in one pass.
- Token estimation is done locally (no circular deps).
- Summary model/prompt/options are owned by the ``context_compress`` capability.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, List, Optional

from models.conversation import Conversation, Message
from models.provider import Provider
from models.state import SessionState
from core.config import load_app_config
from core.llm.llm_config import LLMConfig
from core.llm.token_utils import estimate_conversation_tokens
from core.prompts.history import count_user_turn_blocks, is_control_message
from core.capabilities.defaults import SUMMARY_SYSTEM_PROMPT
from core.prompts.user_context import extract_user_request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Policy knobs
# ---------------------------------------------------------------------------

@dataclass
class CondensePolicy:
    # Per-message condensation
    per_message_lookback: int = 20
    tool_min_chars: int = 200
    assistant_min_chars: int = 800

    # Global archive triggers
    max_active_messages: int = 20
    token_threshold_ratio: float = 0.7

    # Archive behaviour - 修改为保留最后 3 条消息（参考 VSCode Copilot）
    keep_last_n: int = 3  # 从 10 改为 3，保留最后 3 条完整消息


# ---------------------------------------------------------------------------
# ContextCondenser
# ---------------------------------------------------------------------------

class ContextCondenser:
    """Unified condenser — per-message summaries + global archive."""

    def __init__(self, client: Any) -> None:
        self.client = client

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def auto_condense(
        self,
        *,
        conversation: Conversation,
        provider: Provider,
        context_window_limit: int,
        app_config: Any = None,
        policy: Optional[CondensePolicy] = None,
    ) -> None:
        """Run full condense pipeline: per-message + global archive."""
        pol = policy or self._policy_from_config(app_config)
        await self._per_message_condense(conversation, provider, pol)
        await self._maybe_archive(conversation, provider, context_window_limit, pol)

    async def condense_state(
        self,
        conversation: Conversation,
        provider: Provider,
        state: SessionState,
        keep_last_n: int = 5,
    ) -> bool:
        """Condense older messages into ``state.summary``.

        Non-destructive — tags condensed messages with ``condense_parent``.
        """
        messages = conversation.messages
        if not messages:
            return False

        active_indexed = [
            (i, m) for i, m in enumerate(messages)
            if m.role != "system"
            and not m.condense_parent
            and not getattr(m, "truncation_parent", None)
        ]

        blocks: List[List[tuple[int, Message]]] = []
        current_block: List[tuple[int, Message]] = []
        for item in active_indexed:
            index, msg = item
            if msg.role == "user" and not is_control_message(Message(role="user", content=extract_user_request(msg.content or ""))):
                if current_block:
                    blocks.append(current_block)
                current_block = [item]
                continue
            if not current_block:
                current_block = [item]
            else:
                current_block.append(item)
        if current_block:
            blocks.append(current_block)

        user_block_indexes = [
            idx for idx, block in enumerate(blocks)
            if any(msg.role == "user" and not is_control_message(Message(role="user", content=extract_user_request(msg.content or ""))) for _, msg in block)
        ]
        if len(user_block_indexes) <= keep_last_n:
            return False

        first_keep_block = user_block_indexes[-keep_last_n]
        indices_to_keep = {
            index
            for block in blocks[first_keep_block:]
            for index, _msg in block
        }
        indices_to_summarize = [index for index, _msg in active_indexed if index not in indices_to_keep]
        if not indices_to_summarize:
            return False

        start_idx = indices_to_summarize[0]
        end_idx = indices_to_summarize[-1] + 1
        messages_to_summarize = messages[start_idx:end_idx]

        logger.info("Condensing %d messages…", len(messages_to_summarize))

        try:
            app_cfg = load_app_config()
            context_cfg = getattr(app_cfg, "context", None)
        except Exception:
            app_cfg = None
            context_cfg = None

        compress_capability = None
        try:
            capabilities = getattr(app_cfg, "capabilities", None)
            compress_capability = capabilities.capability("context_compress") if capabilities is not None else None
        except Exception:
            compress_capability = None

        compress_options = getattr(compress_capability, "options", None) or {}
        include_tool_details = bool(
            compress_options.get("include_tool_details", False)
        )
        transcript = self._build_transcript(
            messages_to_summarize, include_tool_details=include_tool_details
        )

        prompt = f"## Previous Summary\n{state.summary}\n\n## New Conversation Delta\n{transcript}\n"
        from models.provider import normalize_provider_name, split_model_ref

        configured_summary_model = (
            (getattr(compress_capability, "model_ref", "") or "").strip()
            or conversation.model
            or provider.default_model
        )
        summary_provider_name, summary_model_name = split_model_ref(str(configured_summary_model or ""))
        if summary_provider_name and summary_provider_name != normalize_provider_name(provider.name):
            logger.debug(
                "Ignoring summary provider override '%s' because the active provider is '%s'",
                summary_provider_name,
                normalize_provider_name(provider.name),
            )
        summary_model = summary_model_name or str(configured_summary_model or "").strip()
        summary_system = (
            (getattr(compress_capability, "system_prompt", "") or "").strip()
            or SUMMARY_SYSTEM_PROMPT
        )

        summary_conv = Conversation(
            id="temp_condense",
            messages=[Message(role="user", content=prompt)],
            mode="chat",
        )
        summary_conv.set_llm_config(
            LLMConfig(
                model=summary_model,
                stream=False,
                system_prompt_override=str(summary_system).strip(),
            )
        )
        try:
            response = await self.client.send_message(
                provider, summary_conv,
                enable_thinking=False, prepared_tools=[],
            )
            new_summary = response.content or "No summary generated."
            if state.summary:
                state.archived_summaries.append(state.summary)
            state.summary = new_summary

            condense_id = str(uuid.uuid4())
            for msg in messages_to_summarize:
                msg.condense_parent = condense_id

            logger.info("Condense complete. Archived summaries: %d", len(state.archived_summaries))
            return True
        except Exception as e:
            logger.error("Condense failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Per-message condensation
    # ------------------------------------------------------------------

    async def _per_message_condense(
        self,
        conversation: Conversation,
        provider: Provider,
        pol: CondensePolicy,
    ) -> None:
        messages = conversation.messages
        start = max(0, len(messages) - pol.per_message_lookback)
        end = max(0, len(messages) - 1)

        for i in range(end - 1, start - 1, -1):
            msg = messages[i]
            if getattr(msg, "summary", None):
                continue
            should = False
            if msg.role == "assistant" and any(
                len(str((tool_call.get("result") or {}).get("content") if isinstance(tool_call.get("result"), dict) else tool_call.get("result") or "")) > pol.tool_min_chars
                for tool_call in (msg.tool_calls or [])
                if isinstance(tool_call, dict) and tool_call.get("result") is not None
            ):
                should = True
            elif msg.role == "assistant" and len(msg.content or "") > pol.assistant_min_chars:
                should = True
            if should:
                await self._condense_message(msg, provider)

    async def _condense_message(self, message: Message, provider: Provider) -> bool:
        if not message.content or message.summary:
            return False
        if len(message.content) < 500:
            return False

        prompt = (
            "请为以下消息生成一个简洁的摘要，保留关键信息和重要细节。\n\n"
            f"原始消息：\n{message.content}\n\n"
            "要求：\n"
            "1. 摘要长度控制在原文的20%以内\n"
            "2. 保留关键事实、数据、结论\n"
            "3. 使用清晰的要点形式\n"
            "4. 如果有代码或技术细节，请保留重要部分\n"
        )
        summary_conv = Conversation(
            title="Message Condensation",
            messages=[Message(role="user", content=prompt)],
        )
        summary_conv.set_llm_config(
            LLMConfig(
                stream=False,
                system_prompt_override="You are a concise summarizer. Summarize the given message. Do not call tools. Output text only.",
            )
        )
        try:
            resp = await self.client.send_message(
                provider, summary_conv,
                enable_thinking=False, prepared_tools=[],
            )
            message.summary = resp.content
            logger.debug("Generated summary for seq_id=%s", getattr(message, "seq_id", "?"))
            return True
        except Exception as e:
            logger.warning("Failed to summarize message: %s", e)
            return False

    # ------------------------------------------------------------------
    # Global archive
    # ------------------------------------------------------------------

    async def _maybe_archive(
        self,
        conversation: Conversation,
        provider: Provider,
        context_window_limit: int,
        pol: CondensePolicy,
    ) -> None:
        active_count = len([
            m for m in conversation.messages
            if not m.condense_parent
            and not getattr(m, "truncation_parent", None)
            and m.role != "system"
        ])
        turn_block_count = count_user_turn_blocks(conversation.messages)
        current_tokens = estimate_conversation_tokens(conversation)
        threshold = int(context_window_limit * pol.token_threshold_ratio)

        if (
            turn_block_count <= pol.keep_last_n
            and active_count <= pol.max_active_messages
            and current_tokens <= threshold
        ):
            return

        state = conversation.get_state()
        success = await self.condense_state(
            conversation, provider, state, keep_last_n=pol.keep_last_n
        )
        if success:
            conversation.set_state(state)

    # ------------------------------------------------------------------
    # Transcript builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_transcript(
        messages: List[Message],
        *,
        include_tool_details: bool = False,
    ) -> str:
        def _compact(s: str, limit: int = 240) -> str:
            s = (s or "").replace("\r", " ").replace("\n", " ").strip()
            while "  " in s:
                s = s.replace("  ", " ")
            return (s[:limit] + "...") if len(s) > limit else s

        blocks: List[str] = []
        for m in messages:
            if m.role in ("system", "tool"):
                continue
            role = (m.role or "").upper()
            if m.role == "user":
                base_content = extract_user_request(m.summary if m.summary else m.content)
                if not base_content or is_control_message(Message(role="user", content=base_content)):
                    continue
                content = base_content
            else:
                content = m.summary if m.summary else m.content
            block = f"{role}:\n{content or ''}".strip()

            if m.role == "assistant" and m.tool_calls:
                tool_lines: List[str] = []
                for tc in m.tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = (tc.get("function") or {}).get("name")
                    args = (tc.get("function") or {}).get("arguments")
                    result = tc.get("result")
                    if include_tool_details:
                        tool_lines.append(
                            f"- tool_call id={tc.get('id')} name={fn} "
                            f"args={_compact(str(args or ''))} "
                            f"result={_compact(str(result or ''))}"
                        )
                    else:
                        rs = _compact(str(result or ""))
                        if rs:
                            tool_lines.append(f"- tool_call name={fn} result={rs}")
                        else:
                            tool_lines.append(f"- tool_call name={fn}")
                if tool_lines:
                    block = f"{block}\n\nTOOL_CALLS:\n" + "\n".join(tool_lines)

            blocks.append(block)

        return "\n\n---\n\n".join(blocks).strip()

    # ------------------------------------------------------------------
    # Config → policy
    # ------------------------------------------------------------------

    @staticmethod
    def _policy_from_config(app_config: Any) -> CondensePolicy:
        pol = CondensePolicy()
        if app_config is None:
            return pol
        try:
            overrides = getattr(app_config, "compression_policy", None)
            if overrides:
                for field_name in ("per_message_lookback", "tool_min_chars",
                                   "assistant_min_chars", "max_active_messages",
                                   "token_threshold_ratio", "keep_last_n"):
                    val = getattr(overrides, field_name, None)
                    if val is not None:
                        setattr(pol, field_name, val)
        except Exception as e:
            logger.debug("Failed to apply compression policy overrides: %s", e)
        return pol
