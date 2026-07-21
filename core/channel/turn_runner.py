from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable

from core.agent.events import create_run_debug_trace, finish_run_debug_trace
from core.agent.policy import RunPolicyBuilder
from core.agent.run.runtime import AgentRuntime
from core.channel.events import ChannelEvent
from core.llm.model_selection import select_default_provider_model
from models.provider import Provider, build_model_ref
from models.contracts.agent import RunEvent, RunEventKind, RunPolicy
from models.contracts.channel import ChannelConfig
from models.conversation import Conversation, Message


logger = logging.getLogger(__name__)


class ChannelTurnRunner:
    """Builds channel run policy and executes one Agent turn."""

    def __init__(
        self,
        *,
        agent_runtime: AgentRuntime,
        provider_catalog_service: Any,
        app_settings_provider: Callable[[], dict[str, Any]],
        conv_service: Any,
        emit_event: Callable[[ChannelEvent], None],
    ) -> None:
        self._agent_runtime = agent_runtime
        self._provider_catalog_service = provider_catalog_service
        self._app_settings_provider = app_settings_provider
        self._conv_service = conv_service
        self._emit_event = emit_event

    def build_run_policy(self, conversation: Conversation, channel: ChannelConfig) -> RunPolicy:
        settings = getattr(conversation, "settings", {}) or {}
        mode_slug = str(getattr(channel, "mode_slug", "") or "").strip().lower()
        if not mode_slug:
            mode_slug = str(getattr(conversation, "mode", "") or "").strip().lower()
        if not mode_slug:
            mode_slug = "channel"

        app_settings = self._load_app_settings()

        tool_selection = getattr(channel, "tool_selection", None)

        return RunPolicyBuilder.build(
            conversation=conversation,
            app_settings=app_settings,
            mode_slug=mode_slug,
            show_thinking=bool(settings.get("show_thinking", False)),
            tool_selection=tool_selection,
            disabled_tools=("user__ask",),
            source="channel",
        )

    def run_turn(
        self,
        *,
        provider: Provider,
        conversation: Conversation,
        policy: RunPolicy,
        channel: ChannelConfig,
        request_id: str = "",
        assistant_step_callback: Callable[[Message], None] | None = None,
    ):
        channel_id = str(getattr(channel, "id", "") or "").strip()
        conversation_id = str(getattr(conversation, "id", "") or "").strip()
        request_token = str(request_id or "").strip() or str(uuid.uuid4())

        async def _approval_callback(_message: str) -> bool:
            return False

        async def _questions_callback(_question: dict) -> dict:
            return {"selected": [], "freeText": None, "skipped": True}

        def _emit_turn_event(kind: str, *, payload: dict[str, Any] | None = None, source: str = "channel-turn") -> None:
            self._emit_event(
                ChannelEvent(
                    kind=kind,
                    channel_id=channel_id,
                    conversation_id=conversation_id,
                    source=source,
                    request_id=request_token,
                    payload=dict(payload or {}),
                )
            )

        def _message_payload(message: Message) -> dict[str, Any]:
            metadata = getattr(message, "metadata", {}) or {}
            return {
                "message": message,
                "message_id": str(getattr(message, "id", "") or ""),
                "role": str(getattr(message, "role", "") or ""),
                "seq_id": int(getattr(message, "seq_id", 0) or 0),
                "tool_call_id": str(getattr(message, "tool_call_id", "") or ""),
                "tool_name": str(metadata.get("name", "") or "") if isinstance(metadata, dict) else "",
            }

        def _on_token(token: str) -> None:
            _emit_turn_event("turn-token", payload={"token": str(token or "")})

        def _on_thinking(thinking: str) -> None:
            _emit_turn_event("turn-thinking", payload={"thinking": str(thinking or "")})

        def _on_event(event: RunEvent) -> None:
            kind_value = getattr(getattr(event, "kind", ""), "value", str(getattr(event, "kind", "")))
            payload: dict[str, Any] = {
                "event": event,
                "event_kind": kind_value,
                "turn": int(getattr(event, "turn", 0) or 0),
                "detail": str(getattr(event, "detail", "") or ""),
            }
            data = getattr(event, "data", None)
            if isinstance(data, Message):
                payload.update(_message_payload(data))
                if event.kind == RunEventKind.STEP:
                    self._conv_service.save(conversation)
                    _emit_turn_event("turn-step", payload=payload)
                    if callable(assistant_step_callback):
                        assistant_step_callback(data)
            elif isinstance(data, dict):
                payload.update(data)
                if event.kind == RunEventKind.STEP and str(data.get("role") or "") == "tool_result":
                    _emit_turn_event("turn-step", payload=payload)
            elif data is not None:
                payload["data"] = data
            _emit_turn_event("turn-event", payload=payload)

        app_settings = self._load_app_settings()
        debug_trace = create_run_debug_trace(
            conversation=conversation,
            request_id=request_token,
            model_name=build_model_ref(
                str(getattr(provider, "name", "") or ""),
                str(getattr(conversation, "model", "") or ""),
            ),
            mode=str(getattr(policy, "mode", "") or getattr(conversation, "mode", "") or "channel"),
            source="channel",
            capture_payloads=bool(app_settings.get("log_stream", False)),
            capture_stream=bool(app_settings.get("log_stream", False)),
        )
        try:
            result = asyncio.run(
                self._agent_runtime.run(
                    provider=provider,
                    conversation=conversation,
                    policy=policy,
                    on_event=_on_event,
                    on_token=_on_token,
                    on_thinking=_on_thinking,
                    approval_callback=_approval_callback,
                    questions_callback=_questions_callback,
                    debug_trace=debug_trace,
                )
            )
        except Exception as exc:
            finish_run_debug_trace(debug_trace, status="error", summary=str(exc))
            raise

        final_message = getattr(result, "final_message", None)
        summary = str(getattr(final_message, "content", "") or getattr(result, "error", "") or "")
        status = getattr(getattr(result, "status", ""), "value", getattr(result, "status", "completed"))
        finish_run_debug_trace(debug_trace, status=str(status or "completed"), summary=summary)
        return result

    def resolve_provider(self, conversation: Conversation) -> Provider | None:
        providers = [
            provider
            for provider in self._provider_catalog_service.load()
            if bool(getattr(provider, "enabled", True))
        ]
        provider = self._conv_service.resolve_provider(
            providers,
            provider_id=str(getattr(conversation, "provider_id", "") or ""),
            provider_name=str(getattr(conversation, "provider_name", "") or ""),
        )
        selection = select_default_provider_model(
            providers,
            default_model_ref=self._default_model_ref(),
        )
        if provider is None:
            provider = selection.provider
        if provider is None:
            return None
        model = str(getattr(conversation, "model", "") or "").strip()
        if not model:
            provider_selection = select_default_provider_model(
                [provider],
                default_model_ref=self._default_model_ref(),
            )
            model = provider_selection.model or selection.model
        self._conv_service.configure_llm(
            conversation,
            providers=providers,
            provider_id=provider.id,
            provider_name=provider.name,
            api_type=provider.api_type,
            model=model,
        )
        return provider

    def _default_model_ref(self) -> str:
        settings = self._load_app_settings()
        return str(settings.get("default_chat_model", "") or "").strip()

    def _load_app_settings(self) -> dict[str, Any]:
        try:
            return dict(self._app_settings_provider() or {})
        except Exception as exc:
            logger.debug("Failed to load app settings for channel runtime: %s", exc)
            return {}


__all__ = ["ChannelTurnRunner"]
