"""LLM execution module - handles LLM API calls with retry logic.

Owns request-scoped policy overrides, the pre-send context gate, provider
payload construction, streaming, and retry.
"""
from __future__ import annotations

import copy
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from pycat.models.conversation import Conversation, Message
from pycat.models.provider import Provider
from pycat.models.contracts.channel import channel_file_delivery_enabled

from pycat.core.llm.client import LLMClient
from pycat.core.context.maintainer import ContextMaintainer
from pycat.core.config import AppConfig
from pycat.core.llm.token_budget import (
    REQUEST_USAGE_METADATA_KEY,
    TokenBudget,
    estimate_conversation_tokens,
    estimate_request_tokens,
    request_usage_payload,
    resolve_token_budget,
)
from pycat.core.context.builder import prepare_api_messages, prepare_context_messages
from pycat.core.context.history import is_real_user_message
from pycat.core.content.session_content import (
    RequestContentCache,
    SessionContentService,
    text_attachment_byte_budget,
)
from pycat.core.prompts.renderer import PromptRenderer
from pycat.core.prompts.channel import build_channel_prompt_section
from pycat.core.prompts.sections import PromptSections
from pycat.core.prompts.project_instructions import ProjectInstructionService
from pycat.core.memory.service import MemoryService, memory_enabled
from pycat.core.skills import build_skill_prompt_section
from pycat.models.contracts.agent import RunPolicy, RunEventKind
from pycat.models.contracts.tooling import ToolAvailabilityContext, ToolSelectionPolicy
from pycat.core.tools.manager import ToolManager, MCP_AVAILABLE
from pycat.core.agent.request.retry import classify_error, retry_with_backoff
from pycat.core.observability.debug_trace import DebugTraceContext, ensure_debug_trace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedRequest:
    """One serialized provider request and the budget facts measured from it."""

    request_conversation: Conversation
    messages: list[Message]
    replay_pressure: str
    request_body: dict[str, Any]
    token_estimate: int
    token_budget: TokenBudget


class RequestPipeline:
    """Handles LLM API calls with retry and streaming support."""

    def __init__(
        self,
        client: LLMClient,
        *,
        tool_manager: ToolManager,
        context_maintenance: ContextMaintainer,
        prompt_renderer: PromptRenderer,
        content_service: SessionContentService | None = None,
        app_config: AppConfig | None = None,
    ):
        self._client = client
        self._tool_manager = tool_manager
        self._context_maintenance = context_maintenance
        self._prompt_renderer = prompt_renderer
        self._content_service = content_service
        self._app_config = app_config or AppConfig()

    async def call_with_retry(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        runtime_messages: Optional[list[Message]] = None,
        on_token: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        debug_log_path: Optional[str] = None,
        debug_trace: DebugTraceContext | None = None,
        debug_turn: int = 0,
        debug_purpose: str = "main",
        memory_snapshot: str | None = None,
        stable_prompt_sections: PromptSections | None = None,
        emit: Optional[Callable[..., None]] = None,
        compression_tasks: Any = None,
    ) -> Message:
        """Call LLM with retry logic on transient errors.

        Args:
            provider: LLM provider configuration
            conversation: Current conversation
            policy: Execution policy
            on_token: Callback for streaming tokens
            on_thinking: Callback for thinking content
            cancel_event: Event to signal cancellation
            debug_log_path: Path to save debug logs
            emit: Event emission callback

        Returns:
            Assistant message with response

        Raises:
            Exception: On non-retryable errors or after max retries
        """

        trace_context = ensure_debug_trace(debug_trace)
        attempt_count = 0
        prepared_request = await self._prepare_request(
            provider=provider,
            conversation=conversation,
            policy=policy,
            runtime_messages=runtime_messages,
            memory_snapshot=memory_snapshot,
            stable_prompt_sections=stable_prompt_sections,
            debug_trace=trace_context,
            content_cache=RequestContentCache(),
            compression_tasks=compression_tasks,
        )
        usage = request_usage_payload(
            token_estimate=prepared_request.token_estimate,
            budget=prepared_request.token_budget,
            replay_pressure=prepared_request.replay_pressure,
            active_messages=len(prepared_request.messages),
            provider_id=str(getattr(prepared_request.request_conversation, "provider_id", "") or ""),
        )
        if emit:
            emit(
                kind=RunEventKind.STEP,
                data={REQUEST_USAGE_METADATA_KEY: dict(usage)},
            )

        async def attempt() -> Message:
            nonlocal attempt_count
            attempt_count += 1
            purpose = debug_purpose if attempt_count <= 1 else "retry"
            message = await self._send_prepared_request(
                provider=provider,
                request_conversation=prepared_request.request_conversation,
                request_body=prepared_request.request_body,
                policy=policy,
                on_token=on_token,
                on_thinking=on_thinking,
                cancel_event=cancel_event,
                debug_log_path=debug_log_path,
                debug_trace=trace_context,
                debug_turn=debug_turn,
                debug_purpose=purpose,
            )
            message.metadata = dict(getattr(message, "metadata", {}) or {})
            message.metadata[REQUEST_USAGE_METADATA_KEY] = dict(usage)
            return message

        def on_retry_fn(attempt_num: int, delay: float, exc: str | Exception) -> None:
            kind = classify_error(exc)
            logger.warning(
                "LLM call failed (attempt %d): %s - retrying in %.1fs",
                attempt_num,
                kind.name,
                delay,
            )
            if emit:
                emit(
                    kind=RunEventKind.RETRY,
                    detail=f"Retry {attempt_num} after {delay:.1f}s ({kind.name})",
                )
            if trace_context is not None:
                trace_context.record_event(
                    kind="retry",
                    phase="event",
                    turn=int(debug_turn or trace_context.turn or 0),
                    node_id=f"retry{attempt_num:02d}",
                    parent_id=trace_context.parent_id,
                    name=kind.name.lower(),
                    status="scheduled",
                    summary=f"Retry {attempt_num} after {delay:.1f}s ({kind.name})",
                    data={"attempt": attempt_num, "delay_seconds": delay, "error": str(exc)},
                )

        return await retry_with_backoff(
            attempt,
            policy=policy.retry,
            on_retry=on_retry_fn,
        )

    async def _prepare_request(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        runtime_messages: Optional[list[Message]] = None,
        memory_snapshot: str | None = None,
        stable_prompt_sections: PromptSections | None = None,
        debug_trace: DebugTraceContext | None = None,
        content_cache: RequestContentCache | None = None,
        compression_tasks: Any = None,
    ) -> PreparedRequest:
        """Build one immutable logical request before transport retries."""
        request_conversation = self._build_request_conversation(
            conversation=conversation,
            provider=provider,
            policy=policy,
        )
        token_budget = self._resolve_prompt_budget(
            conversation=request_conversation,
            provider=provider,
        )
        prepared_tools = await self._get_request_tools(
            conversation=request_conversation,
            policy=policy,
        )
        return await self._apply_context_gate(
            conversation=conversation,
            request_conversation=request_conversation,
            provider=provider,
            policy=policy,
            tools=prepared_tools,
            runtime_messages=runtime_messages,
            memory_snapshot=memory_snapshot,
            stable_prompt_sections=stable_prompt_sections,
            debug_trace=debug_trace,
            content_cache=content_cache,
            compression_tasks=compression_tasks,
            token_budget=token_budget,
        )

    async def _send_prepared_request(
        self,
        *,
        provider: Provider,
        request_conversation: Conversation,
        request_body: dict[str, Any],
        policy: RunPolicy,
        on_token: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        debug_log_path: Optional[str] = None,
        debug_trace: DebugTraceContext | None = None,
        debug_turn: int = 0,
        debug_purpose: str = "main",
    ) -> Message:
        """Send an already prepared request without rebuilding prompt state."""
        message = await self._client.send_request(
            provider=provider,
            request_body=request_body,
            on_token=on_token,
            on_thinking=on_thinking,
            show_thinking=bool(policy.show_thinking),
            debug_log_path=debug_log_path,
            cancel_event=cancel_event,
            debug_trace=debug_trace,
            debug_turn=debug_turn,
            debug_purpose=debug_purpose,
            conversation_id=str(getattr(request_conversation, "id", "") or ""),
            model_hint=str(getattr(request_conversation, "model", "") or ""),
        )
        metadata = getattr(message, "metadata", {}) or {}
        if isinstance(metadata, dict) and metadata.get("runtime_error"):
            detail = str(getattr(message, "content", "") or "").strip()
            raise RuntimeError(detail or "模型接口返回错误。")
        return message

    async def _apply_context_gate(
        self,
        *,
        conversation: Conversation,
        request_conversation: Conversation,
        provider: Provider,
        policy: RunPolicy,
        tools: list[dict],
        runtime_messages: Optional[list[Message]],
        memory_snapshot: str | None = None,
        stable_prompt_sections: PromptSections | None = None,
        debug_trace: DebugTraceContext | None = None,
        content_cache: RequestContentCache | None = None,
        compression_tasks: Any = None,
        token_budget: TokenBudget | None = None,
    ) -> PreparedRequest:
        """Materialize, measure, and select one provider request."""
        app_config = self._app_config
        compression_policy = getattr(getattr(app_config, "context", None), "compression_policy", None)
        tight_replay_enabled = bool(
            getattr(compression_policy, "tight_replay_enabled", True)
        )
        tight_threshold_ratio = float(
            getattr(compression_policy, "tight_replay_threshold_ratio", 0.35) or 0.35
        )
        threshold_ratio = float(getattr(compression_policy, "token_threshold_ratio", 0.80) or 0.80)
        tight_threshold_ratio = max(0.10, min(0.94, tight_threshold_ratio))
        threshold_ratio = max(tight_threshold_ratio + 0.01, min(0.95, threshold_ratio))
        budget = token_budget or self._resolve_prompt_budget(
            conversation=conversation,
            provider=provider,
        )
        prompt_limit = int(budget.effective_prompt_limit or budget.context_window or 0)
        attachment_budget = text_attachment_byte_budget(max(1, prompt_limit))
        request_content_cache = content_cache or RequestContentCache()
        captured_at = datetime.now().astimezone()
        # ``""`` is a meaningful frozen snapshot: it means the run started
        # with no durable memory (or memory was disabled).  Only callers that
        # omit the argument (``None``) request the legacy lazy fallback.  Using
        # truthiness here would reread memory after a mid-run write and make
        # the prompt change between turns.
        memory_prompt = (
            self._build_memory_prompt(request_conversation)
            if memory_snapshot is None
            else str(memory_snapshot)
        )
        tight_threshold_tokens = int(prompt_limit * tight_threshold_ratio)
        threshold_tokens = int(prompt_limit * threshold_ratio)
        current_messages: list[Message] | None = None
        compression_errors: list[str] = []
        attempted_targets: set[int] = set()

        async def prepare_messages() -> list[Message]:
            messages = await self._prepare_messages(
                conversation=request_conversation,
                provider=provider,
                policy=policy,
                tools=tools,
                debug_trace=debug_trace,
                memory_prompt=memory_prompt,
                captured_at=captured_at,
                stable_prompt_sections=stable_prompt_sections,
                token_budget=budget,
            )
            if runtime_messages:
                runtime_copy = copy.deepcopy(runtime_messages)
                if messages and messages[-1].metadata.get("context_kind") == "current_state":
                    current_state = messages.pop()
                    messages.extend(runtime_copy)
                    messages.append(current_state)
                else:
                    messages.extend(runtime_copy)
            if not any(getattr(message, "content_refs", None) for message in messages):
                return messages
            if self._content_service is None:
                raise RuntimeError("content service is required for session input references")
            return self._content_service.materialize_messages(
                request_conversation,
                messages,
                text_byte_budget=attachment_budget,
                cache=request_content_cache,
            )

        def materialize(replay_pressure: str) -> PreparedRequest:
            if current_messages is None:
                raise RuntimeError("context messages are not prepared")
            return self._materialize_request(
                request_conversation=request_conversation,
                provider=provider,
                policy=policy,
                messages=current_messages,
                tools=tools,
                replay_pressure=replay_pressure,
                token_budget=budget,
            )

        async def compact(*, reason: str, recent_turn_target: int, selected: PreparedRequest) -> None:
            nonlocal captured_at, request_conversation, current_messages
            attempted_targets.add(int(recent_turn_target))
            try:
                report = await self._context_maintenance.maintain_async(
                    conversation,
                    provider=provider,
                    policy=policy,
                    client=self._client,
                    prompt_limit=prompt_limit,
                    force=True,
                    honor_auto_enabled=False,
                    token_threshold_ratio=threshold_ratio,
                    recent_turn_target=recent_turn_target,
                    protect_current_turn=True,
                    request_token_estimate=selected.token_estimate,
                    conversation_token_estimate=0,
                    debug_trace=debug_trace.with_purpose("condense") if debug_trace is not None else None,
                )
                logger.info(
                    "Pre-send compact (%s, recent_turn_target=%s): %s",
                    reason,
                    recent_turn_target,
                    report,
                )
            except Exception as exc:
                compression_errors.append(str(exc))
                logger.warning("Pre-send compact failed (%s): %s", reason, exc)
            request_conversation = self._build_request_conversation(
                conversation=conversation,
                provider=provider,
                policy=policy,
            )
            captured_at = datetime.now().astimezone()
            current_messages = await prepare_messages()

        async def choose_replay() -> PreparedRequest:
            normal = materialize("normal")
            if not tight_replay_enabled:
                return normal
            if normal.token_estimate < tight_threshold_tokens:
                if compression_tasks is not None:
                    compression_tasks.tight_pressure = False
                return normal
            if compression_tasks is not None and not bool(getattr(compression_tasks, "tight_pressure", False)):
                await compression_tasks.drain()
                try:
                    compression_tasks.tight_pressure = True
                except Exception:
                    pass
                normal = materialize("normal")
                if normal.token_estimate < tight_threshold_tokens:
                    compression_tasks.tight_pressure = False
                    return normal
            tight = materialize("tight")
            if tight.token_estimate < normal.token_estimate:
                logger.info(
                    "Tight tool replay selected: normal=%s tight=%s threshold=%s limit=%s",
                    normal.token_estimate,
                    tight.token_estimate,
                    tight_threshold_tokens,
                    prompt_limit,
                )
                return tight
            return normal

        current_messages = await prepare_messages()
        if prompt_limit <= 1:
            return materialize("normal")
        selected = await choose_replay()

        if (
            selected.token_estimate >= threshold_tokens
            and self._context_maintenance.auto_enabled(policy)
        ):
            await compact(reason="threshold", recent_turn_target=3, selected=selected)
            selected = await choose_replay()

        if selected.token_estimate <= prompt_limit:
            return selected

        for recent_turn_target in (3, 2, 1):
            if recent_turn_target in attempted_targets:
                continue
            await compact(
                reason="hard_limit",
                recent_turn_target=recent_turn_target,
                selected=selected,
            )
            selected = await choose_replay()
            if selected.token_estimate <= prompt_limit:
                return selected

        # Deterministic last-resort fallback: bound the oldest tool results
        # in a request-scoped copy so a hard-limit overflow degrades instead
        # of failing the run. Persisted history is never modified.
        if current_messages:
            for keep_batches, max_chars in ((3, 400), (1, 400), (1, 120)):
                truncated_messages, changed = self._truncate_oldest_tool_results(
                    current_messages,
                    keep_recent_batches=keep_batches,
                    max_result_chars=max_chars,
                )
                if not changed:
                    continue
                current_messages = truncated_messages
                selected = await choose_replay()
                if selected.token_estimate <= prompt_limit:
                    logger.warning(
                        "Hard-limit fallback truncated old tool results: "
                        "selected=%s limit=%s keep_batches=%s",
                        selected.token_estimate,
                        prompt_limit,
                        keep_batches,
                    )
                    return selected

        detail = f"；压缩失败：{compression_errors[-1]}" if compression_errors else ""
        current_user_tokens = self._current_user_tokens(current_messages)
        if current_user_tokens >= prompt_limit:
            error_code = "current_user_too_large"
        elif self._has_unavailable_recovery(selected.messages):
            error_code = "recovery_tool_unavailable"
        else:
            error_code = "context_limit"
        raise RuntimeError(
            f"{error_code}: 上下文超过模型可用窗口，压缩和紧凑重放后仍无法发送："
            f"selected={selected.token_estimate}, mode={selected.replay_pressure}, "
            f"limit={prompt_limit}{detail}"
        )

    def _materialize_request(
        self,
        *,
        request_conversation: Conversation,
        provider: Provider,
        policy: RunPolicy,
        messages: list[Message],
        tools: list[dict],
        replay_pressure: str,
        token_budget: TokenBudget,
    ) -> PreparedRequest:
        prepared_messages, tool_result_renderer = prepare_api_messages(
            messages,
            conversation=request_conversation,
            replay_pressure=replay_pressure,
            visible_tool_names=self._tool_names(tools),
        )
        api_messages = self._prompt_renderer.build_api_messages(
            prepared_messages,
            provider,
            conversation=request_conversation,
            tool_result_renderer=tool_result_renderer,
        )
        request_body = self._prompt_renderer.build_request_body(
            provider,
            request_conversation,
            api_messages,
            tools=tools,
            reasoning_mode=policy.reasoning_mode,
            token_budget=token_budget,
            pycat_assistant_enabled=policy.pycat_assistant_enabled,
            completion_policy=policy.completion_policy,
        )
        return PreparedRequest(
            request_conversation=request_conversation,
            messages=prepared_messages,
            replay_pressure=str(replay_pressure or "normal"),
            request_body=request_body,
            token_estimate=estimate_request_tokens(request_body),
            token_budget=token_budget,
        )

    @staticmethod
    def _truncate_oldest_tool_results(
        messages: list[Message],
        *,
        keep_recent_batches: int,
        max_result_chars: int,
    ) -> tuple[list[Message], bool]:
        """Bound old tool-result content in a request-scoped copy.

        Tool results older than the most recent ``keep_recent_batches``
        assistant tool batches are replaced by a deterministic bounded view.
        Only the copied request messages change; persisted history and the
        tool-call/result pairing protocol stay intact. The boolean indicates
        whether any result was actually shortened.
        """
        batch_indexes = [
            index
            for index, message in enumerate(messages)
            if message.role == "assistant" and message.tool_calls
        ]
        keep = max(0, int(keep_recent_batches))
        protected = set(batch_indexes[-keep:]) if keep else set()
        truncated = copy.deepcopy(messages)
        changed = False

        def _bound(content: str) -> str:
            limit = max(1, int(max_result_chars))
            marker = f"[truncated by hard-limit fallback: {len(content)} chars total]"
            if len(marker) >= limit:
                return marker[:limit]
            prefix_chars = limit - len(marker) - 1
            return f"{content[:prefix_chars]}\n{marker}"

        for index in batch_indexes:
            if index in protected:
                continue
            for tool_call in truncated[index].tool_calls or []:
                if not isinstance(tool_call, dict):
                    continue
                result = tool_call.get("result")
                if isinstance(result, dict):
                    content = result.get("content")
                    if isinstance(content, str) and len(content) > max_result_chars:
                        result["content"] = _bound(content)
                        changed = True
                elif isinstance(result, str) and len(result) > max_result_chars:
                    tool_call["result"] = _bound(result)
                    changed = True
        return (truncated, True) if changed else (messages, False)

    @staticmethod
    def _current_user_tokens(messages: list[Message]) -> int:
        for message in reversed(messages or []):
            if is_real_user_message(message):
                return estimate_conversation_tokens([message])
        return 0

    @staticmethod
    def _has_unavailable_recovery(messages: list[Message]) -> bool:
        for message in messages or []:
            if bool((message.metadata or {}).get("context_recovery_unavailable")):
                return True
            for tool_call in message.tool_calls or []:
                if not isinstance(tool_call, dict) or tool_call.get("result") is None:
                    continue
                result = tool_call.get("result")
                metadata = result.get("metadata") if isinstance(result, dict) else None
                if (
                    isinstance(metadata, dict)
                    and metadata.get("tool_result_replay_reason") == "recovery_tool_unavailable"
                ):
                    return True
        return False

    async def _prepare_messages(
        self,
        *,
        conversation: Conversation,
        provider: Provider,
        policy: RunPolicy,
        tools: list[dict],
        debug_trace: DebugTraceContext | None = None,
        memory_prompt: str | None = None,
        captured_at: datetime | None = None,
        stable_prompt_sections: PromptSections | None = None,
        token_budget: TokenBudget | None = None,
    ) -> list[Message]:
        """Prepare effective history + system prompt."""
        app_config = self._app_config

        sections = self._build_prompt_sections(
            conversation,
            app_config,
            tools,
            pycat_assistant_enabled=policy.pycat_assistant_enabled,
            stable_sections=stable_prompt_sections,
            workspace_service=getattr(self._tool_manager, "workspace_service", None),
        )
        if memory_prompt is None:
            memory_prompt = self._build_memory_prompt(conversation)
        context_messages = prepare_context_messages(
            conversation=conversation,
            app_config=app_config,
            default_work_dir=str(getattr(conversation, "work_dir", "") or ""),
            memory_prompt=memory_prompt,
            include_environment=policy.pycat_assistant_enabled,
            captured_at=captured_at,
            prompt_limit=int(
                getattr(token_budget, "effective_prompt_limit", 0)
                or getattr(token_budget, "context_window", 0)
                or 0
            ),
            visible_tool_names=self._tool_names(tools),
        )
        system_content = self._prompt_renderer.resolve_system_prompt(
            conversation,
            tools,
            provider,
            app_config=app_config,
            sections=sections,
            pycat_assistant_enabled=policy.pycat_assistant_enabled,
            completion_policy=policy.completion_policy,
        )
        if system_content:
            return [Message(role="system", content=system_content)] + context_messages
        return context_messages

    @classmethod
    def _build_prompt_sections(
        cls,
        conversation: Conversation,
        app_config: AppConfig,
        tools: list[dict],
        *,
        pycat_assistant_enabled: bool = True,
        stable_sections: PromptSections | None = None,
        workspace_service=None,
    ) -> PromptSections:
        stable = stable_sections or cls.build_stable_prompt_sections(
            conversation,
            app_config,
            pycat_assistant_enabled=pycat_assistant_enabled,
            workspace_service=workspace_service,
        )
        return PromptSections(
            channel=stable.channel,
            project_instructions=stable.project_instructions,
            skills=cls._build_skill_prompt_section(
                conversation,
                tools,
                pycat_assistant_enabled=pycat_assistant_enabled,
            ),
        )

    @classmethod
    def build_stable_prompt_sections(
        cls,
        conversation: Conversation,
        app_config: AppConfig,
        *,
        pycat_assistant_enabled: bool = True,
        workspace_service=None,
    ) -> PromptSections:
        """Capture run-local provenance and project instructions."""
        return PromptSections(
            channel=cls._build_channel_prompt_section(conversation, app_config),
            project_instructions=(
                cls._build_project_instruction_section(conversation, workspace_service=workspace_service)
                if pycat_assistant_enabled
                else ""
            ),
        )

    @staticmethod
    def _build_channel_prompt_section(conversation: Conversation, app_config: AppConfig) -> str:
        settings = getattr(conversation, "settings", {}) or {}
        try:
            binding = settings.get("channel_binding") if isinstance(settings, dict) else None
            if not isinstance(binding, dict):
                return ""

            binding_source = str(binding.get("source") or "").strip()
            binding_channel_id = str(binding.get("channel_id") or "").strip()
            if not binding_channel_id or not binding_source:
                return ""

            matched_channel = None
            for channel in getattr(app_config, "channels", []) or []:
                if not bool(getattr(channel, "enabled", False)):
                    continue
                channel_id = str(getattr(channel, "id", "") or "").strip()
                source = str(getattr(channel, "source", "") or "").strip()
                if source != binding_source:
                    continue
                if channel_id != binding_channel_id:
                    continue
                matched_channel = channel
                break
            if matched_channel is None:
                return ""

            def _normalize(values) -> tuple[str, ...]:
                if isinstance(values, str):
                    candidates = [part.strip() for part in values.split(",")]
                elif isinstance(values, (list, tuple, set)):
                    candidates = [str(item).strip() for item in values]
                else:
                    candidates = []
                result: list[str] = []
                for item in candidates:
                    if item and item not in result:
                        result.append(item)
                return tuple(result)

            configured = (binding_source,)
            allowed_values = _normalize(settings.get("allowed_channel_sources"))
            allowed = tuple(source for source in (allowed_values or configured) if source == binding_source)
            trusted = tuple(item for item in _normalize(settings.get("trusted_channel_sources")) if item in allowed)
            notice_policy = str(settings.get("channel_notice_policy", "notice") or "notice").strip().lower() or "notice"

            messages = []
            for message in getattr(conversation, "messages", []) or []:
                metadata = getattr(message, "metadata", {}) or {}
                channel_meta = metadata.get("channel") if isinstance(metadata, dict) else None
                if not isinstance(channel_meta, dict):
                    continue
                source = str(channel_meta.get("source") or channel_meta.get("channel_source") or "").strip()
                if source == binding_source:
                    messages.append(message)
            return build_channel_prompt_section(
                messages,
                configured_sources=configured,
                allowed_sources=allowed,
                trusted_sources=trusted,
                notice_policy=notice_policy,
            )
        except Exception as exc:
            logger.debug("Failed to build channel prompt section: %s", exc)
            return ""

    @staticmethod
    def _build_memory_prompt(conversation: Conversation) -> str:
        """Lazy fallback: render the durable-memory snapshot for this run."""
        try:
            return MemoryService.build_run_snapshot(conversation, data_dir=getattr(conversation, "data_dir", None))
        except Exception as exc:
            logger.debug("Failed to build memory prompt section: %s", exc)
            return ""

    @staticmethod
    def _build_project_instruction_section(conversation: Conversation, *, workspace_service=None) -> str:
        try:
            return ProjectInstructionService.build_prompt_section(
                str(getattr(conversation, "work_dir", "") or ""),
                workspace_service=workspace_service,
            )
        except Exception as exc:
            if str(getattr(conversation, "work_dir", "")).startswith("ssh://"):
                raise
            logger.debug("Failed to build project instruction section: %s", exc)
            return ""

    @staticmethod
    def _build_skill_prompt_section(
        conversation: Conversation,
        tools: list[dict],
        *,
        pycat_assistant_enabled: bool = True,
    ) -> str:
        try:
            return build_skill_prompt_section(
                conversation,
                tools,
                pycat_assistant_enabled=pycat_assistant_enabled,
            )
        except Exception as exc:
            logger.debug("Failed to build skill prompt section: %s", exc)
            return ""

    @staticmethod
    def _latest_user_query(conversation: Conversation) -> str:
        for msg in reversed(getattr(conversation, "messages", []) or []):
            if getattr(msg, "role", "") != "user":
                continue
            content = str(getattr(msg, "content", "") or "").strip()
            if content:
                return content
        return ""

    @staticmethod
    def _resolve_prompt_budget(*, conversation: Conversation, provider: Provider):
        return resolve_token_budget(
            conversation,
            provider=provider,
        )

    async def _get_request_tools(
        self,
        *,
        conversation: Conversation,
        policy: RunPolicy,
    ) -> list[dict]:
        selection = getattr(policy, "tool_selection", None) or ToolSelectionPolicy.all()
        try:
            return await self._tool_manager.get_all_tools(
                tool_selection=selection,
                availability_context=ToolAvailabilityContext(
                    work_dir=str(getattr(conversation, "work_dir", "") or ""),
                    data_dir=conversation.data_dir,
                    conversation_id=str(getattr(conversation, "id", "") or ""),
                    source=str(getattr(policy, "source", "") or "desktop"),
                    filesystem_mode=str(
                        getattr(getattr(policy, "filesystem_scope", None), "mode", "confined")
                        or "confined"
                    ),
                    search_available=self._tool_manager.search_service.is_available(),
                    mcp_available=MCP_AVAILABLE,
                    completion_policy=str(getattr(policy, "completion_policy", "text") or "text"),
                    memory_enabled=memory_enabled(conversation),
                    channel_file_delivery=channel_file_delivery_enabled(conversation.settings),
                ),
                tool_permissions=policy.tool_permissions,
            )
        except Exception as e:
            logger.warning("Failed to load request tools: %s", e)
            return []

    @staticmethod
    def _tool_names(tools: list[dict]) -> set[str]:
        names: set[str] = set()
        for schema in tools or []:
            function = schema.get("function") if isinstance(schema, dict) else None
            name = str(function.get("name") or "").strip() if isinstance(function, dict) else ""
            if name:
                names.add(name)
        return names

    def _build_request_conversation(
        self,
        *,
        conversation: Conversation,
        provider: Provider,
        policy: RunPolicy,
    ) -> Conversation:
        """Create a request-scoped conversation with RunPolicy overrides applied."""
        try:
            request_conversation = conversation.clone()
            try:
                setattr(request_conversation, "_source_conversation", conversation)
            except Exception:
                pass
        except Exception as e:
            logger.warning("Failed to clone conversation for LLM request: %s", e)
            request_conversation = conversation

        request_conversation.mode = str(getattr(policy, "mode", "") or request_conversation.mode or "chat")

        try:
            llm_config = request_conversation.get_llm_config()
            updates: dict[str, object] = {}
            if policy.temperature is not None:
                updates["temperature"] = float(policy.temperature)
            if policy.max_tokens is not None:
                updates["max_tokens"] = int(policy.max_tokens)
            if policy.model is not None:
                updates["model"] = str(policy.model).strip()
            if updates:
                llm_config = llm_config.with_updates(**updates)
            request_conversation.set_llm_config(llm_config)
        except Exception as e:
            logger.warning("Failed to apply llm_config overrides to request conversation: %s", e)
            request_settings = dict(request_conversation.settings or {})
            if policy.temperature is not None:
                request_settings["temperature"] = float(policy.temperature)
            if policy.max_tokens is not None:
                request_settings["max_tokens"] = int(policy.max_tokens)
            request_conversation.settings = request_settings
            request_conversation.model = (
                str(policy.model or "").strip()
                or str(getattr(request_conversation, "model", "") or "").strip()
            )
        return request_conversation
