from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable

from pycat.core.agent.events.stream_batcher import StreamDeltaBatcher
from pycat.core.agent.policy import RunPolicyBuilder
from pycat.core.app.services.run import RunService
from pycat.core.channel.events import ChannelEvent
from pycat.core.llm.model_selection import select_default_provider_model
from pycat.core.tools.base import ApprovalDecision, ToolApprovalRequest
from pycat.models.contracts.agent import RunEvent, RunEventKind, RunPolicy
from pycat.models.contracts.channel import ChannelConfig
from pycat.models.conversation import Conversation, Message
from pycat.models.provider import Provider
from pycat.models.contracts.tooling import (
    TOOL_CATEGORIES,
    FilesystemScope,
    ToolPermissionConfig,
    ToolPolicy,
    permission_config_for_approval,
)

logger = logging.getLogger(__name__)


def _confine_channel_permissions(
    configured: ToolPermissionConfig,
) -> ToolPermissionConfig:
    """Apply the unattended Channel ceiling without requiring a tool registry.

    Category defaults can only become stricter than PyCat's safe defaults.
    Explicit ``deny`` and ``ask`` rules remain useful narrowing. An explicit
    ``allow`` rule is discarded so it cannot silently promote an edit or
    execute tool above its category ceiling.
    """

    ceiling = ToolPermissionConfig()
    rank = {"deny": 0, "ask": 1, "allow": 2}
    defaults: dict[str, ToolPolicy] = {}
    for category in TOOL_CATEGORIES:
        configured_action = configured.category_defaults[category].action
        ceiling_action = ceiling.category_defaults[category].action
        action = (
            configured_action
            if rank[configured_action] <= rank[ceiling_action]
            else ceiling_action
        )
        defaults[category] = ToolPolicy(action=action)

    tools = {
        name: policy
        for name, policy in configured.tools.items()
        if policy.action in {"deny", "ask"}
    }
    return ToolPermissionConfig(category_defaults=defaults, tools=tools)


class ChannelTurnRunner:
    """Builds channel run policy and executes one Agent turn."""

    def __init__(
        self,
        *,
        run_service: RunService,
        provider_catalog_service: Any,
        app_settings_provider: Callable[[], dict[str, Any]],
        conv_service: Any,
        emit_event: Callable[[ChannelEvent], None],
    ) -> None:
        self._run_service = run_service
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
        configured_permissions = permission_config_for_approval(
            settings.get("tool_approval"),
            custom=ToolPermissionConfig.from_settings_dict(app_settings),
        )
        confined_permissions = _confine_channel_permissions(configured_permissions)

        return RunPolicyBuilder.build(
            conversation=conversation,
            app_settings=app_settings,
            mode_slug=mode_slug,
            show_thinking=bool(settings.get("show_thinking", False)),
            tool_selection=tool_selection,
            tool_permissions=confined_permissions,
            filesystem_scope=FilesystemScope(
                mode="confined",
                allow_home_read=False,
            ),
            denied_tools=("user__ask",),
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
        claim_token: str | None = None,
        assistant_step_callback: Callable[[Message], None] | None = None,
    ):
        channel_id = str(getattr(channel, "id", "") or "").strip()
        conversation_id = str(getattr(conversation, "id", "") or "").strip()
        request_token = str(request_id or "").strip() or str(uuid.uuid4())
        stream_batch: StreamDeltaBatcher | None = None

        def _flush_stream() -> None:
            if stream_batch is not None:
                stream_batch.flush()

        def _emit_stream_batch(visible: str, thinking: str) -> None:
            if visible:
                _emit_turn_event("turn-token", payload={"token": visible})
            if thinking:
                _emit_turn_event("turn-thinking", payload={"thinking": thinking})

        async def _approval_callback(_request: ToolApprovalRequest) -> ApprovalDecision:
            _flush_stream()
            return ApprovalDecision()

        async def _questions_callback(_question: dict) -> dict:
            _flush_stream()
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
            if stream_batch is not None:
                stream_batch.append_visible(token)

        def _on_thinking(thinking: str) -> None:
            if stream_batch is not None:
                stream_batch.append_thinking(thinking)

        def _on_event(event: RunEvent) -> None:
            _flush_stream()
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

        async def _run():
            loop = asyncio.get_running_loop()
            nonlocal stream_batch
            stream_batch = StreamDeltaBatcher(
                schedule=loop.call_later,
                emit=_emit_stream_batch,
            )
            try:
                return await self._run_service.execute(
                    claim_token=claim_token,
                    provider=provider,
                    conversation=conversation,
                    policy=policy,
                    on_event=_on_event,
                    on_token=_on_token,
                    on_thinking=_on_thinking,
                    approval_callback=_approval_callback,
                    questions_callback=_questions_callback,
                    run_id=request_token,
                )
            finally:
                _flush_stream()
                stream_batch = None

        result = self._run_service.schedule(_run()).result()
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
