"""Streaming message presenter.

Owns run-policy preparation, streaming UI callbacks, and response/error
materialization so MessagePresenter can focus on higher-level message actions.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from models.conversation import Conversation, Message
from models.provider import Provider
from models.contracts.session_state import SessionState
from models.streaming import ConversationPatch
from core.app.runtime_paths import get_debug_log_path
from core.agent.policy import RunPolicyBuilder
from models.contracts.agent import RunEvent, RunStatus

if TYPE_CHECKING:
    from gui.main_window import MainWindow

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


_STATE_BOOKKEEPING_FIELDS = {
    "state_version",
    "last_updated_seq",
    "last_maintenance_seq",
    "last_memory_review_seq",
}


class StreamingMessagePresenter:
    """Handles streaming startup, callbacks, and runtime response updates."""

    def __init__(self, host: MainWindow) -> None:
        self._host = host
        self._pending_tool_steps: dict[str, list[Message]] = {}

    def _set_sidebar_streaming(self, conversation_id: str, streaming: bool) -> None:
        sidebar = getattr(self._host, "sidebar", None)
        setter = getattr(sidebar, "set_conversation_streaming", None)
        if callable(setter):
            setter(conversation_id, streaming)

    def start_streaming(
        self,
        provider: Provider,
        *,
        conversation: Conversation | None = None,
        initial_runtime_messages: list[Message] | None = None,
    ):
        host = self._host
        conversation = conversation or host.current_conversation
        conversation_id = getattr(conversation, "id", "") or ""
        if not conversation_id:
            return None

        debug_log_path = get_debug_log_path(host.app_settings, host.services.data_dir)

        show_thinking = bool(
            (conversation.settings or {}).get(
                "show_thinking", host.app_settings.get("show_thinking", True)
            )
        )

        skill_run = self._get_latest_skill_run_metadata(conversation)

        try:
            policy = self._build_request_policy(
                conversation=conversation,
                show_thinking=show_thinking,
                skill_run=skill_run,
            )
        except Exception as e:
            logger.warning("Failed to build run policy: %s", e)
            policy = self._build_fallback_policy(
                conversation=conversation,
                show_thinking=show_thinking,
            )

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
            initial_runtime_messages=initial_runtime_messages,
        )
        if not state:
            return None

        is_current = bool(host.current_conversation and host.current_conversation.id == conversation_id)
        if is_current:
            host.services.app_coordinator.set_streaming(conversation_id, is_streaming=True)
        self._set_sidebar_streaming(conversation_id, True)
        self._sync_runtime_state(conversation_id, stream_state=state)

        if is_current:
            host.chat_view.start_streaming_response(model=state.model)
            host.chat_view.restore_streaming_state("", "")
            host.window_state_presenter.sync_input_enabled()
        return state

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

        if getattr(message, "role", "") == "tool":
            self._apply_tool_result_step(target_conv, message)
            host.services.conv_service.save(target_conv)
            host.services.app_coordinator.remember_current_conversation(
                target_conv,
                providers=host.providers,
                app_settings=host.app_settings,
                is_streaming=self._is_conversation_streaming(host, conversation_id),
            )
            if host.current_conversation and host.current_conversation.id == conversation_id:
                updated_parent = self._find_message_for_tool_call(
                    target_conv,
                    str(getattr(message, "tool_call_id", "") or ""),
                )
                try:
                    refreshed = host.chat_view.refresh_message_tool_calls(
                        str(metadata.get("parent_message_id") or getattr(updated_parent, "id", "") or ""),
                        str(getattr(message, "tool_call_id", "") or ""),
                        updated_message=updated_parent,
                    )
                    if not refreshed and updated_parent is not None:
                        host.chat_view.update_message(updated_parent)
                    elif not refreshed:
                        self._remember_pending_tool_step(conversation_id, message)
                except Exception as e:
                    logger.debug("Failed to refresh tool result in chat view: %s", e)
                self._sync_runtime_state(conversation_id)
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
                self._apply_pending_tool_steps(target_conv, message)
                state = host.message_runtime.get_state(conversation_id)
                host.chat_view.start_streaming_response(model=state.model if state else "")
            else:
                host.chat_view.add_message(message)

            self._refresh_inspector_state(target_conv, {"messages"})
            self._sync_runtime_state(conversation_id)

    def on_response_complete(
        self, conversation_id: str, request_id: str, response
    ) -> None:
        host = self._host
        self._set_sidebar_streaming(conversation_id, False)
        host.window_state_presenter.sync_input_enabled()

        if response is None:
            if host.current_conversation and host.current_conversation.id == conversation_id:
                host.chat_view.finish_streaming_response(
                    Message(role="system", content=""), add_to_view=False
                )
                self._sync_runtime_state(conversation_id, stream_state=None)
                self._update_header(conversation_id)
            host.window_state_presenter.sync_input_enabled()
            return

        if not isinstance(response, Message):
            return

        channel_meta = getattr(response, "metadata", {}) or {}
        if isinstance(channel_meta, dict) and channel_meta.get("channel_owned"):
            if host.current_conversation and host.current_conversation.id == conversation_id:
                host.chat_view.finish_streaming_response(response, add_to_view=False)
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
            if message_already_exists:
                try:
                    if hasattr(host.chat_view, "upsert_message"):
                        host.chat_view.upsert_message(response)
                    else:
                        host.chat_view.update_message(response)
                except Exception as exc:
                    logger.debug("Failed to refresh completed response in chat view: %s", exc)
            self._refresh_inspector_state(host.current_conversation, {"messages"})
            self._sync_runtime_state(conversation_id, stream_state=None)
            self._update_header(conversation_id)

        host.window_state_presenter.sync_input_enabled()

    def on_response_error(
        self, conversation_id: str, request_id: str, error: str
    ) -> None:
        host = self._host
        self._set_sidebar_streaming(conversation_id, False)
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
            self._refresh_inspector_state(host.current_conversation, {"messages"})
            self._sync_runtime_state(conversation_id, stream_state=None)
            self._update_header(conversation_id)

        host.window_state_presenter.sync_input_enabled()

    def on_run_finished(
        self,
        conversation_id: str,
        request_id: str,
        status: RunStatus,
    ) -> None:
        host = self._host
        if not (host.current_conversation and host.current_conversation.id == conversation_id):
            return
        finish_run = getattr(host.chat_view, "finish_active_run", None)
        if callable(finish_run):
            finish_run(status)

    @staticmethod
    def _is_conversation_streaming(host, conversation_id: str) -> bool:
        return bool(host.message_runtime.is_streaming(conversation_id) or getattr(host.chat_view, "is_streaming", lambda: False)())

    def on_retry_attempt(
        self, conversation_id: str, request_id: str, detail: str
    ) -> None:
        del request_id, detail
        self._sync_runtime_state(conversation_id)

    def on_runtime_event(
        self,
        conversation_id: str,
        request_id: str,
        event: RunEvent,
    ) -> None:
        host = self._host
        if not (host.current_conversation and host.current_conversation.id == conversation_id):
            return
        data = getattr(event, "data", None)
        kind_value = str(getattr(getattr(event, "kind", ""), "value", getattr(event, "kind", "")))
        if kind_value == "condense":
            self._show_condense_notice(data)
        if isinstance(data, dict) and isinstance(data.get("subtask"), dict):
            try:
                host.chat_view.update_subtask_trace(data.get("subtask") or {})
            except Exception as exc:
                logger.debug("Failed to update live subtask trace: %s", exc)
        self._sync_runtime_state(conversation_id)

    def on_conversation_patch(
        self,
        conversation_id: str,
        request_id: str,
        patch: ConversationPatch,
    ) -> None:
        host = self._host
        if not isinstance(patch, ConversationPatch):
            return
        if patch.conversation_id and str(patch.conversation_id) != str(conversation_id or ""):
            return
        active_state = host.message_runtime.get_state(conversation_id)
        active_request_id = str(getattr(active_state, "request_id", "") or "")
        if active_request_id and request_id and active_request_id != str(request_id):
            return
        current = host.current_conversation
        is_current = bool(current and current.id == conversation_id)
        target = current if is_current else host.services.conv_service.load(conversation_id)
        if target is None:
            return
        try:
            new_condensed = self._new_condensed_message_ids(target, patch)
            changed_fields = self._apply_conversation_patch(target, patch)
            if not changed_fields and not new_condensed:
                # Idempotent patch (e.g. the final run patch duplicating the
                # last state patch already merged and saved): nothing new to
                # persist or repaint.
                return
            host.services.conv_service.save(target)
            if not is_current:
                return
            host.services.app_coordinator.remember_current_conversation(
                target,
                providers=host.providers,
                app_settings=host.app_settings,
                is_streaming=self._is_conversation_streaming(host, conversation_id),
            )
            if new_condensed:
                self._show_condense_notice(
                    {
                        "archived_messages": len(new_condensed),
                        "history_ids": list(dict.fromkeys(str(v) for v in new_condensed.values() if str(v).strip())),
                        "reason": "conversation_patch",
                    }
                )
            self._refresh_inspector_state(target, changed_fields)
            self._update_header(conversation_id)
        except Exception as exc:
            logger.debug("Failed to apply runtime conversation patch: %s", exc)

    @staticmethod
    def _new_condensed_message_ids(
        conversation: Conversation,
        patch: ConversationPatch,
    ) -> dict[str, str]:
        existing = {
            str(getattr(message, "id", "") or ""): str(getattr(message, "archived_content_id", "") or "")
            for message in getattr(conversation, "messages", []) or []
        }
        return {
            str(message_id): str(content_id)
            for message_id, content_id in (patch.condensed_message_ids or {}).items()
            if str(content_id or "").strip() and existing.get(str(message_id), "") != str(content_id)
        }

    @staticmethod
    def _apply_conversation_patch(conversation: Conversation, patch: ConversationPatch) -> set[str]:
        changed_fields: set[str] = set()
        if isinstance(patch.state, dict):
            try:
                current_state = conversation.get_state()
                incoming_state = SessionState.from_dict(dict(patch.state or {}))
                if int(incoming_state.state_version or 0) < int(current_state.state_version or 0):
                    return changed_fields
                current_payload = current_state.to_dict()
                incoming_payload = incoming_state.to_dict()
                changed_fields.update(
                    key
                    for key in current_payload.keys() | incoming_payload.keys()
                    if key not in _STATE_BOOKKEEPING_FIELDS
                    and current_payload.get(key) != incoming_payload.get(key)
                )
                if current_payload != incoming_payload:
                    conversation.set_state(incoming_state)
            except Exception as exc:
                logger.debug("Failed to merge patch state: %s", exc)
        if patch.changed_messages:
            incoming_messages = [Message.from_dict(msg.to_dict()) for msg in patch.changed_messages]
            current_payload = [msg.to_dict() for msg in getattr(conversation, "messages", []) or []]
            incoming_payload = [msg.to_dict() for msg in incoming_messages]
            if current_payload != incoming_payload:
                conversation.messages = incoming_messages
                changed_fields.add("messages")
        elif patch.condensed_message_ids:
            for msg in getattr(conversation, "messages", []) or []:
                parent = patch.condensed_message_ids.get(str(getattr(msg, "id", "") or ""))
                if parent and str(getattr(msg, "archived_content_id", "") or "") != str(parent):
                    msg.archived_content_id = parent
                    changed_fields.add("messages")
        if not changed_fields:
            return changed_fields
        try:
            from datetime import datetime

            conversation.updated_at = datetime.now()
        except Exception:
            pass
        return changed_fields

    def _refresh_inspector_state(
        self,
        conversation: Conversation,
        changed_fields: set[str],
    ) -> None:
        if not changed_fields:
            return
        panel = getattr(self._host, "inspector_panel", None)
        if panel is None:
            return
        updater = getattr(panel, "update_conversation_state", None)
        if callable(updater):
            updater(conversation, changed_fields)
            return
        panel.update_stats(conversation)

    def _remember_pending_tool_step(self, conversation_id: str, message: Message) -> None:
        tool_call_id = str(getattr(message, "tool_call_id", "") or "").strip()
        if not tool_call_id:
            return
        self._pending_tool_steps.setdefault(str(conversation_id or ""), []).append(Message.from_dict(message.to_dict()))

    def _apply_pending_tool_steps(self, conversation: Conversation, assistant_message: Message) -> None:
        conversation_id = str(getattr(conversation, "id", "") or "")
        pending = self._pending_tool_steps.get(conversation_id) or []
        if not pending:
            return
        remaining: list[Message] = []
        for tool_message in pending:
            tool_call_id = str(getattr(tool_message, "tool_call_id", "") or "").strip()
            if not tool_call_id or not self._assistant_has_tool_call(assistant_message, tool_call_id):
                remaining.append(tool_message)
                continue
            self._apply_tool_result_step(conversation, tool_message)
            updated_parent = self._find_message_for_tool_call(conversation, tool_call_id)
            if updated_parent is not None:
                try:
                    self._host.chat_view.refresh_message_tool_calls(
                        str(getattr(updated_parent, "id", "") or ""),
                        tool_call_id,
                        updated_message=updated_parent,
                    )
                except Exception as exc:
                    logger.debug("Failed to apply pending tool result to chat view: %s", exc)
        if remaining:
            self._pending_tool_steps[conversation_id] = remaining[-20:]
        else:
            self._pending_tool_steps.pop(conversation_id, None)

    @staticmethod
    def _assistant_has_tool_call(message: Message, tool_call_id: str) -> bool:
        call_id = str(tool_call_id or "").strip()
        if not call_id:
            return False
        return any(str(tc.get("id") or "").strip() == call_id for tc in (getattr(message, "tool_calls", None) or []) if isinstance(tc, dict))

    def _show_condense_notice(self, data: Any) -> None:
        host = self._host
        if not isinstance(data, dict):
            return
        archived = int(data.get("archived_messages") or 0)
        snipped = int(data.get("snipped_messages") or 0)
        archive_updates = int(data.get("archive_updates") or 0)
        reason = str(data.get("reason") or "").strip()
        history_ids = [str(item) for item in (data.get("history_ids") or []) if str(item).strip()]
        parts = []
        if archived:
            parts.append(f"archived {archived} message(s)")
        if snipped:
            parts.append(f"removed {snipped} folded message(s)")
        if archive_updates and not archived:
            parts.append(f"updated {archive_updates} archive summary view(s)")
        if not parts:
            return
        target = history_ids[-1] if history_ids else ""
        text = "上下文已压缩：" + ", ".join(parts)
        if target:
            text += f" -> {target}"
        if reason:
            text += f" ({reason})"
        chat_view = getattr(host, "chat_view", None)
        if chat_view is not None and hasattr(chat_view, "append_transcript_notice"):
            chat_view.append_transcript_notice(text, kind="condense")

    def _build_request_policy(
        self,
        *,
        conversation: Conversation,
        show_thinking: bool,
        skill_run: Optional[dict[str, Any]],
    ):
        host = self._host

        if skill_run:
            skill_name = str(skill_run.get("name") or "").strip().lower()
            work_dir = getattr(conversation, "work_dir", ".") or "."
            spec = host.services.skill_service.get_invocation_spec(skill_name, work_dir=work_dir)
            if spec is not None:
                return RunPolicyBuilder.build(
                    conversation=conversation,
                    app_settings=host.app_settings,
                    mode_slug=spec.mode,
                    show_thinking=bool(show_thinking),
                    tool_selection=spec.tool_selection,
                    mode_manager=host.input_area.get_mode_manager(),
                    source="desktop",
                )

        try:
            mode_slug = host.input_area.get_selected_mode_slug()
            return RunPolicyBuilder.build(
                conversation=conversation,
                app_settings=host.app_settings,
                mode_slug=str(mode_slug or "chat"),
                show_thinking=bool(show_thinking),
                mode_manager=host.input_area.get_mode_manager(),
                source="desktop",
            )
        except Exception as e:
            logger.warning("Failed to build run policy from input state: %s", e)
            return self._build_fallback_policy(
                conversation=conversation,
                show_thinking=show_thinking,
            )

    def _build_fallback_policy(self, *, conversation: Conversation, show_thinking: bool):
        return RunPolicyBuilder.build(
            conversation=conversation,
            app_settings=getattr(self._host, "app_settings", {}) or {},
            mode_slug=str(getattr(conversation, "mode", "chat") or "chat"),
            show_thinking=bool(show_thinking),
            source="desktop",
        )

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

            presenter = getattr(host, "window_state_presenter", None)
            if presenter is not None and hasattr(presenter, "sync_runtime_state"):
                presenter.sync_runtime_state(conversation_id)
            else:
                chat_view = getattr(host, "chat_view", None)
                if chat_view is not None and hasattr(chat_view, "update_runtime_state"):
                    chat_view.update_runtime_state(resolved_state)

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
                host.input_area.set_mode_selection(next_mode)
            except Exception as e:
                logger.debug("Failed to sync runtime mode switch to input area: %s", e)

    @staticmethod
    def _apply_tool_result_step(conversation, message: Message) -> bool:
        metadata = getattr(message, "metadata", {}) or {}
        if not isinstance(metadata, dict):
            metadata = {}
        result_payload = metadata.get("result")
        if not isinstance(result_payload, dict):
            result_payload = {
                "type": "tool_result",
                "content": str(getattr(message, "content", "") or ""),
                "summary": str(getattr(message, "summary", "") or metadata.get("summary") or ""),
                "metadata": dict(metadata),
            }
        result_metadata = dict(result_payload.get("metadata") or {})
        for key, value in metadata.items():
            if key in {"result", "role"}:
                continue
            result_metadata.setdefault(str(key), value)
        result_payload = dict(result_payload)
        result_payload["metadata"] = result_metadata
        return bool(conversation.attach_tool_result(
            getattr(message, "tool_call_id", "") or "",
            result_payload,
            summary=str(result_payload.get("summary") or getattr(message, "summary", "") or ""),
            metadata=result_metadata,
            images=list(getattr(message, "images", []) or []),
            state_snapshot=getattr(message, "state_snapshot", None) if isinstance(getattr(message, "state_snapshot", None), dict) else None,
        ))

    @staticmethod
    def _find_message_for_tool_call(conversation, tool_call_id: str) -> Message | None:
        call_id = str(tool_call_id or "").strip()
        if not call_id:
            return None
        for msg in reversed(getattr(conversation, "messages", []) or []):
            if str(getattr(msg, "role", "") or "") != "assistant":
                continue
            for tool_call in getattr(msg, "tool_calls", None) or []:
                if isinstance(tool_call, dict) and str(tool_call.get("id") or "").strip() == call_id:
                    return msg
        return None

    def _forward_channel_bound_response(self, conversation: Conversation, response: Message) -> None:
        if conversation is None or not isinstance(response, Message):
            return
        metadata = getattr(response, "metadata", {}) or {}
        if isinstance(metadata, dict) and metadata.get("channel_owned"):
            return
        if str(getattr(response, "role", "") or "").strip().lower() != "assistant":
            return
        if not str(getattr(response, "content", "") or "").strip():
            return

        host = self._host
        services = getattr(host, "services", None)
        channel_gateway = getattr(services, "channel_gateway", None)
        if channel_gateway is None or not hasattr(channel_gateway, "send_bound_conversation_message"):
            return

        channel_gateway.send_bound_conversation_message(conversation, response)

