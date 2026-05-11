"""Streaming message presenter.

Owns run-policy preparation, streaming UI callbacks, and response/error
materialization so MessagePresenter can focus on higher-level message actions.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Optional

from models.conversation import Conversation, Message
from models.provider import Provider
from core.runtime.events import TurnEvent

if TYPE_CHECKING:
    from ui.main_window import MainWindow

logger = logging.getLogger(__name__)


def _format_error_message(error: str) -> str:
    text = (error or "").strip()
    if not text:
        return "模型调用失败：未知错误"
    if text == "已取消生成":
        return text
    if text.startswith("Error sending message:"):
        detail = text.split(":", 1)[1].strip() if ":" in text else ""
        return f"模型调用失败：{detail or '未知错误'}"
    return f"错误: {text}"


_STATE_MUTATING_TOOLS = {
    "manage_todo",
    "manage_state",
    "manage_memory",
    "manage_document",
    "manage_artifact",
}


class StreamingMessagePresenter:
    """Handles streaming startup, callbacks, and runtime response updates."""

    def __init__(self, host: MainWindow) -> None:
        self._host = host

    def start_streaming(self, provider: Provider) -> None:
        host = self._host
        conversation = host.current_conversation
        conversation_id = getattr(conversation, "id", "") or ""
        if not conversation_id:
            return

        from services.agent_service import AgentService
        debug_log_path = AgentService.get_debug_log_path(host.app_settings, host.services.storage)

        enable_thinking = bool(
            (conversation.settings or {}).get(
                "show_thinking", host.app_settings.get("show_thinking", True)
            )
        )

        retry_cfg = None
        try:
            from core.config.schema import RetryConfig

            raw_retry = host.app_settings.get("retry")
            if raw_retry and isinstance(raw_retry, dict):
                retry_cfg = RetryConfig.from_dict(raw_retry)
        except Exception as e:
            logger.debug("Failed to load retry config: %s", e)

        skill_run = self._get_latest_skill_run_metadata(conversation)

        try:
            policy = self._build_request_policy(
                conversation=conversation,
                enable_thinking=enable_thinking,
                retry_cfg=retry_cfg,
                skill_run=skill_run,
            )
        except Exception as e:
            logger.warning("Failed to build run policy: %s", e)
            from core.task.types import RunPolicy

            policy = RunPolicy(mode="chat", enable_thinking=bool(enable_thinking))

        if skill_run is None:
            try:
                if conversation is not None:
                    host.services.app_coordinator.apply_mode(
                        conversation,
                        str(getattr(policy, "mode", "") or "")
                        or (conversation.mode or "chat"),
                    )
            except Exception as e:
                logger.debug("Failed to sync conversation mode from policy: %s", e)

        state = host.message_runtime.start(
            provider,
            conversation,
            policy=policy,
            debug_log_path=debug_log_path,
        )
        if not state:
            return

        host.services.app_coordinator.set_streaming(conversation_id, is_streaming=True)
        self._sync_runtime_state(conversation_id, stream_state=state)

        if host.current_conversation and host.current_conversation.id == conversation_id:
            host.chat_view.start_streaming_response(model=state.model)
            host.chat_view.restore_streaming_state("", "")
        host.window_state_presenter.sync_input_enabled()

    def on_token(self, conversation_id: str, request_id: str, token: str) -> None:
        host = self._host
        if host.current_conversation and host.current_conversation.id == conversation_id:
            if not host.chat_view.is_streaming():
                state = host.message_runtime.get_state(conversation_id)
                model = state.model if state else ""
                host.chat_view.start_streaming_response(model)
            host.chat_view.append_streaming_content(token)

    def on_thinking(self, conversation_id: str, request_id: str, thinking: str) -> None:
        host = self._host
        if host.current_conversation and host.current_conversation.id == conversation_id:
            if not host.chat_view.is_streaming():
                state = host.message_runtime.get_state(conversation_id)
                model = state.model if state else ""
                host.chat_view.start_streaming_response(model)
            if bool(
                (host.current_conversation.settings or {}).get(
                    "show_thinking", host.app_settings.get("show_thinking", True)
                )
            ):
                host.chat_view.append_streaming_thinking(thinking)

    def on_response_step(
        self, conversation_id: str, request_id: str, message: Message
    ) -> None:
        host = self._host
        metadata = getattr(message, "metadata", {}) or {}
        if isinstance(metadata, dict) and metadata.get("subtask_trace_only"):
            return

        target_conv = (
            host.current_conversation
            if (host.current_conversation and host.current_conversation.id == conversation_id)
            else host.services.conv_service.load(conversation_id)
        )
        if not target_conv:
            return

        msg_seq = getattr(message, "seq_id", None)
        if msg_seq and any(
            getattr(m, "seq_id", None) == msg_seq for m in target_conv.messages
        ):
            return

        target_conv.add_message(message)
        self._apply_runtime_message_updates(target_conv, message)
        host.services.conv_service.save(target_conv)
        host.services.app_coordinator.remember_current_conversation(
            target_conv,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=self._is_conversation_streaming(host, conversation_id),
        )

        if host.current_conversation and host.current_conversation.id == conversation_id:
            if message.role == "assistant":
                host.chat_view.finish_streaming_response(message, add_to_view=True)
                state = host.message_runtime.get_state(conversation_id)
                host.chat_view.start_streaming_response(model=state.model if state else "")
            else:
                host.chat_view.add_message(message)

            try:
                host.stats_panel.update_stats(host.current_conversation)
            except Exception as e:
                logger.debug("Failed to update stats in response step: %s", e)
            self._sync_runtime_state(conversation_id)

    def on_response_complete(
        self, conversation_id: str, request_id: str, response
    ) -> None:
        host = self._host
        host.window_state_presenter.sync_input_enabled()

        if response is None:
            if host.current_conversation and host.current_conversation.id == conversation_id:
                host.chat_view.finish_streaming_response(
                    Message(role="system", content=""), add_to_view=False
                )
                host.stats_panel.update_stats(host.current_conversation)
                self._sync_runtime_state(conversation_id, stream_state=None)
                self._update_header(conversation_id)
            host.window_state_presenter.sync_input_enabled()
            return

        if not isinstance(response, Message):
            return

        channel_meta = getattr(response, "metadata", {}) or {}
        if isinstance(channel_meta, dict) and channel_meta.get("channel_runtime_owned"):
            if host.current_conversation and host.current_conversation.id == conversation_id:
                host.chat_view.finish_streaming_response(response, add_to_view=False)
                host.stats_panel.update_stats(host.current_conversation)
                self._sync_runtime_state(conversation_id, stream_state=None)
                self._update_header(conversation_id)
            host.window_state_presenter.sync_input_enabled()
            return

        target = None
        if host.current_conversation and host.current_conversation.id == conversation_id:
            target = host.current_conversation
        else:
            target = host.services.conv_service.load(conversation_id)

        if not target:
            return

        message_already_exists = any(m.id == response.id for m in target.messages)
        if not message_already_exists:
            target.add_message(response)
        self._apply_runtime_message_updates(target, response)
        host.services.conv_service.save(target)
        host.services.app_coordinator.set_streaming(conversation_id, is_streaming=False)
        host.services.app_coordinator.remember_current_conversation(
            target,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=False,
        )

        try:
            conversations = host.services.conv_service.list_all()
            host.sidebar.update_conversations(conversations)
            host.services.app_coordinator.sync_catalog(
                providers=host.providers,
                conversation_count=len(conversations),
            )
        except Exception as e:
            logger.debug("Failed to refresh sidebar after response complete: %s", e)

        try:
            self._forward_channel_bound_response(target, response)
        except Exception as e:
            logger.warning("Failed to forward channel-bound response: %s", e)

        if host.current_conversation and host.current_conversation.id == conversation_id:
            host.chat_view.finish_streaming_response(
                response, add_to_view=not message_already_exists
            )
            host.stats_panel.update_stats(host.current_conversation)
            self._sync_runtime_state(conversation_id, stream_state=None)
            self._update_header(conversation_id)

        host.window_state_presenter.sync_input_enabled()

    def on_response_error(
        self, conversation_id: str, request_id: str, error: str
    ) -> None:
        host = self._host
        host.window_state_presenter.sync_input_enabled()

        content = _format_error_message(error)

        error_message = Message(role="assistant", content=content)
        try:
            error_message.metadata["runtime_error"] = True
        except Exception as exc:
            logger.debug("Failed to mark runtime error metadata: %s", exc)

        target = None
        if host.current_conversation and host.current_conversation.id == conversation_id:
            target = host.current_conversation
        else:
            target = host.services.conv_service.load(conversation_id)

        if target:
            target.add_message(error_message)
            host.services.conv_service.save(target)
            host.services.app_coordinator.set_streaming(conversation_id, is_streaming=False)
            host.services.app_coordinator.remember_current_conversation(
                target,
                providers=host.providers,
                app_settings=host.app_settings,
                is_streaming=False,
            )

        if host.current_conversation and host.current_conversation.id == conversation_id:
            host.chat_view.finish_streaming_response(error_message)
            stats_panel = getattr(host, "stats_panel", None)
            if stats_panel is not None:
                stats_panel.update_stats(host.current_conversation)
            self._sync_runtime_state(conversation_id, stream_state=None)
            self._update_header(conversation_id)

        host.window_state_presenter.sync_input_enabled()

    @staticmethod
    def _is_conversation_streaming(host, conversation_id: str) -> bool:
        return bool(host.message_runtime.is_streaming(conversation_id) or getattr(host.chat_view, "is_streaming", lambda: False)())

    def on_retry_attempt(
        self, conversation_id: str, request_id: str, detail: str
    ) -> None:
        host = self._host
        if host.current_conversation and host.current_conversation.id == conversation_id:
            host.statusBar().showMessage(f"重试中: {detail}", 5000)

    def on_runtime_event(
        self,
        conversation_id: str,
        request_id: str,
        event: TurnEvent,
    ) -> None:
        host = self._host
        if not (host.current_conversation and host.current_conversation.id == conversation_id):
            return
        data = getattr(event, "data", None)
        if isinstance(data, dict) and isinstance(data.get("subtask"), dict):
            try:
                host.chat_view.update_subtask_trace(data.get("subtask") or {})
            except Exception as exc:
                logger.debug("Failed to update live subtask trace: %s", exc)
        if self._runtime_event_may_change_state(data):
            try:
                host.stats_panel.update_stats(host.current_conversation)
            except Exception as exc:
                logger.debug("Failed to update stats after runtime state event: %s", exc)
        self._sync_runtime_state(conversation_id)

    @staticmethod
    def _runtime_event_may_change_state(data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        if isinstance(data.get("state_snapshot"), dict):
            return True
        tool_name = str(data.get("tool_name") or data.get("name") or "").strip()
        if tool_name in _STATE_MUTATING_TOOLS:
            return True
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            meta_tool_name = str(metadata.get("name") or metadata.get("tool_name") or "").strip()
            if meta_tool_name in _STATE_MUTATING_TOOLS:
                return True
        return False

    def _build_request_policy(
        self,
        *,
        conversation: Conversation,
        enable_thinking: bool,
        retry_cfg,
        skill_run: Optional[dict[str, Any]],
    ):
        host = self._host
        from core.task.builder import build_run_policy
        from core.runtime.turn_policy import TurnPolicy
        from core.config.schema import ToolPermissionConfig
        from core.tools.catalog import ToolSelectionPolicy

        tool_permissions = None
        try:
            raw_permissions = (host.app_settings or {}).get("permissions")
            if raw_permissions and isinstance(raw_permissions, dict):
                tool_permissions = ToolPermissionConfig.from_dict(raw_permissions)
        except Exception as e:
            logger.debug("Failed to load global tool permissions: %s", e)

        conversation_tool_selection = None
        try:
            raw_tool_selection = (conversation.settings or {}).get("tool_selection")
            if isinstance(raw_tool_selection, dict):
                conversation_tool_selection = ToolSelectionPolicy.from_dict(raw_tool_selection)
        except Exception as e:
            logger.debug("Failed to load conversation tool selection: %s", e)

        if skill_run:
            skill_name = str(skill_run.get("name") or "").strip().lower()
            work_dir = getattr(conversation, "work_dir", ".") or "."
            spec = host.services.skill_service.get_invocation_spec(skill_name, work_dir=work_dir)
            if spec is not None:
                return self._apply_agent_runtime_overrides(
                    TurnPolicy.from_run_policy(
                        build_run_policy(
                            mode_slug=spec.mode,
                            enable_thinking=bool(enable_thinking),
                            tool_selection=spec.tool_selection,
                            mode_manager=host.input_area.get_mode_manager(),
                            retry_config=retry_cfg,
                            tool_permissions=tool_permissions,
                        ),
                        conversation=conversation,
                    ),
                )

        try:
            mode_slug = host.input_area.get_selected_mode_slug()
            return self._apply_agent_runtime_overrides(
                TurnPolicy.from_run_policy(
                    build_run_policy(
                        mode_slug=str(mode_slug or "chat"),
                        enable_thinking=bool(enable_thinking),
                            tool_selection=conversation_tool_selection,
                        mode_manager=host.input_area.get_mode_manager(),
                        retry_config=retry_cfg,
                        tool_permissions=tool_permissions,
                    ),
                    conversation=conversation,
                ),
            )
        except Exception as e:
            logger.warning("Failed to build run policy from input state: %s", e)
            from core.task.types import RunPolicy

            return self._apply_agent_runtime_overrides(
                TurnPolicy.from_run_policy(
                    RunPolicy(
                        mode=str(getattr(conversation, "mode", "chat") or "chat"),
                        enable_thinking=bool(enable_thinking),
                    ),
                    conversation=conversation,
                ),
            )

    def _apply_agent_runtime_overrides(self, policy):
        settings = getattr(self._host, "app_settings", {}) or {}
        agent_settings = settings.get("agent") if isinstance(settings, dict) else None
        raw = agent_settings.get("max_turns") if isinstance(agent_settings, dict) else None
        try:
            max_turns = int(raw) if raw not in (None, "") else 0
        except Exception:
            max_turns = 0
        if max_turns <= 0:
            return policy
        return replace(policy, max_turns=max_turns)

    @staticmethod
    def _get_latest_skill_run_metadata(
        conversation: Optional[Conversation],
    ) -> Optional[dict[str, Any]]:
        for msg in reversed(getattr(conversation, "messages", []) or []):
            if getattr(msg, "role", "") != "user":
                continue
            metadata = getattr(msg, "metadata", {}) or {}
            skill_run = metadata.get("skill_run") if isinstance(metadata, dict) else None
            if isinstance(skill_run, dict) and str(skill_run.get("name") or "").strip():
                return skill_run
            break
        return None

    def _update_header(self, conversation_id: str) -> None:
        self._host.window_state_presenter.sync_chat_header_for_current_conversation(conversation_id)

    def _sync_runtime_state(
        self,
        conversation_id: str,
        *,
        stream_state=None,
    ) -> None:
        host = self._host
        if not (host.current_conversation and host.current_conversation.id == conversation_id):
            return
        try:
            resolved_state = (
                stream_state
                if stream_state is not None
                else host.message_runtime.get_state(conversation_id)
            )

            chat_view = getattr(host, "chat_view", None)
            if chat_view is not None and hasattr(chat_view, "update_runtime_state"):
                chat_view.update_runtime_state(resolved_state)

            stats_panel = getattr(host, "stats_panel", None)
            if stats_panel is not None and hasattr(stats_panel, "update_runtime_state"):
                stats_panel.update_runtime_state(resolved_state)
        except Exception as e:
            logger.debug("Failed to sync runtime state to UI: %s", e)

    def _apply_runtime_message_updates(self, conversation, message: Message) -> None:
        host = self._host
        try:
            metadata = getattr(message, "metadata", {}) or {}
            next_mode = str(metadata.get("mode_switch") or "").strip().lower()
        except Exception:
            next_mode = ""

        if not next_mode:
            return

        try:
            host.services.app_coordinator.apply_mode(conversation, next_mode)
        except Exception:
            return

        if host.current_conversation and host.current_conversation.id == getattr(conversation, "id", ""):
            try:
                host.input_area.set_mode_selection(next_mode, apply_defaults=True)
            except Exception as e:
                logger.debug("Failed to sync runtime mode switch to input area: %s", e)

    def _forward_channel_bound_response(self, conversation: Conversation, response: Message) -> None:
        if conversation is None or not isinstance(response, Message):
            return
        metadata = getattr(response, "metadata", {}) or {}
        if isinstance(metadata, dict) and metadata.get("channel_runtime_owned"):
            return
        if str(getattr(response, "role", "") or "").strip().lower() != "assistant":
            return
        if not str(getattr(response, "content", "") or "").strip():
            return

        host = self._host
        services = getattr(host, "services", None)
        channel_runtime = getattr(services, "channel_runtime", None)
        if channel_runtime is None or not hasattr(channel_runtime, "send_bound_conversation_message"):
            return

        channel_runtime.send_bound_conversation_message(conversation, response)