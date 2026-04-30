"""Message & streaming presenter.

Extracts message sending, streaming control, and response handling
from MainWindow, reducing it by ~300 lines.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from PyQt6.QtWidgets import QMessageBox

from models.conversation import Conversation, Message
from models.provider import Provider, build_model_ref
from core.runtime.events import TurnEvent
from ui.presenters.prompt_optimization_presenter import PromptOptimizationPresenter
from ui.presenters.streaming_message_presenter import StreamingMessagePresenter

if TYPE_CHECKING:
    from ui.main_window import MainWindow

logger = logging.getLogger(__name__)


class MessagePresenter:
    """Handles message sending, streaming, and response lifecycle."""

    def __init__(self, host: MainWindow) -> None:
        self._host = host
        self._prompt_optimization_presenter = PromptOptimizationPresenter(host)
        self._streaming_presenter = StreamingMessagePresenter(host)

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def send(self, content: str, images: list, metadata: Optional[dict[str, Any]] = None) -> None:
        host = self._host

        if not host.current_conversation:
            host.current_conversation = host.conversation_presenter.ensure_current_conversation_shell()

        if host.message_runtime.is_streaming(host.current_conversation.id):
            QMessageBox.information(host, "提示", "当前会话正在生成中，请稍候或先取消生成。")
            return

        provider_id = host.input_area.get_selected_provider_id()
        model = host.input_area.get_selected_model()

        provider = self._find_provider(provider_id)
        if not provider:
            QMessageBox.warning(host, "错误", "请先在设置中配置服务商")
            return
        if not model:
            QMessageBox.warning(host, "错误", "请选择一个模型")
            return

        host.current_conversation = host.conversation_presenter.seed_from_input(host.current_conversation)
        host.services.app_coordinator.update_provider_model(
            host.current_conversation,
            providers=host.providers,
            provider_id=provider_id,
            model=model,
        )
        host.stats_panel.update_stats(host.current_conversation)
        host.services.conv_service.save(host.current_conversation)
        host.services.app_coordinator.remember_current_conversation(
            host.current_conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=host.message_runtime.is_streaming(host.current_conversation.id),
        )

        extra_metadata = dict(metadata or {})

        # Empty input: if last message is user, just re-stream
        if not content and not images and not extra_metadata:
            if (
                host.current_conversation.messages
                and host.current_conversation.messages[-1].role == "user"
            ):
                self.start_streaming(provider)
            return

        user_message = Message(role="user", content=content, images=images)
        if extra_metadata:
            user_message.metadata.update(extra_metadata)
        user_message.metadata.update(
            {
                "provider_id": provider_id,
                "provider_name": getattr(provider, "name", ""),
                "model": model,
                "model_ref": build_model_ref(getattr(provider, "name", ""), model),
            }
        )
        host.current_conversation.add_message(user_message)

        if len(host.current_conversation.messages) == 1:
            host.current_conversation.generate_title_from_first_message()

        host.chat_view.add_message(user_message)
        host.services.conv_service.save(host.current_conversation)

        conversations = host.services.conv_service.list_all()
        host.sidebar.update_conversations(conversations)
        host.services.app_coordinator.sync_catalog(
            providers=host.providers,
            conversation_count=len(conversations),
        )
        host.sidebar.select_conversation(host.current_conversation.id)
        host.services.app_coordinator.remember_current_conversation(
            host.current_conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=False,
        )

        self.start_streaming(provider)

    def cancel_current_generation(self) -> None:
        host = self._host
        if not host.current_conversation:
            return
        host.message_runtime.cancel(host.current_conversation.id)

    # ------------------------------------------------------------------
    # Start streaming
    # ------------------------------------------------------------------

    def start_streaming(self, provider: Provider) -> None:
        self._streaming_presenter.start_streaming(provider)

    # ------------------------------------------------------------------
    # Streaming callbacks
    # ------------------------------------------------------------------

    def on_token(self, conversation_id: str, request_id: str, token: str) -> None:
        self._streaming_presenter.on_token(conversation_id, request_id, token)

    def on_thinking(self, conversation_id: str, request_id: str, thinking: str) -> None:
        self._streaming_presenter.on_thinking(conversation_id, request_id, thinking)

    def on_response_step(
        self, conversation_id: str, request_id: str, message: Message
    ) -> None:
        self._streaming_presenter.on_response_step(conversation_id, request_id, message)

    def on_response_complete(
        self, conversation_id: str, request_id: str, response
    ) -> None:
        self._streaming_presenter.on_response_complete(conversation_id, request_id, response)

    def on_response_error(
        self, conversation_id: str, request_id: str, error: str
    ) -> None:
        self._streaming_presenter.on_response_error(conversation_id, request_id, error)

    def on_retry_attempt(
        self, conversation_id: str, request_id: str, detail: str
    ) -> None:
        self._streaming_presenter.on_retry_attempt(conversation_id, request_id, detail)

    def on_runtime_event(
        self,
        conversation_id: str,
        request_id: str,
        event: TurnEvent,
    ) -> None:
        self._streaming_presenter.on_runtime_event(conversation_id, request_id, event)

    def on_prompt_optimize_started(
        self,
        conversation_id: str,
        request_id: str,
    ) -> None:
        self._prompt_optimization_presenter.on_started(conversation_id, request_id)

    def on_prompt_optimize_complete(
        self,
        conversation_id: str,
        request_id: str,
        text: str,
    ) -> None:
        self._prompt_optimization_presenter.on_complete(conversation_id, request_id, text)

    def on_prompt_optimize_error(
        self,
        conversation_id: str,
        request_id: str,
        err: str,
    ) -> None:
        self._prompt_optimization_presenter.on_error(conversation_id, request_id, err)

    def on_prompt_optimize_cancelled(
        self,
        conversation_id: str,
        request_id: str,
    ) -> None:
        self._prompt_optimization_presenter.on_cancelled(conversation_id, request_id)

    def cancel_prompt_optimization(self) -> None:
        self._prompt_optimization_presenter.cancel()

    def request_prompt_optimization(self, raw_text: str) -> None:
        self._prompt_optimization_presenter.request(raw_text)

    # ------------------------------------------------------------------
    # Edit / Delete
    # ------------------------------------------------------------------

    def edit(self, message_id: str) -> None:
        host = self._host
        if not host.current_conversation:
            return

        message = None
        for msg in host.current_conversation.messages:
            if msg.id == message_id:
                message = msg
                break
        if not message:
            return

        from ui.dialogs.message_editor import MessageEditorDialog

        dialog = MessageEditorDialog(message, host)
        if dialog.exec():
            message.content = dialog.get_edited_content()
            message.images = dialog.get_edited_images()
            host.chat_view.update_message(message)
            host.services.conv_service.save(host.current_conversation)
            host.services.app_coordinator.remember_current_conversation(
                host.current_conversation,
                providers=host.providers,
                app_settings=host.app_settings,
                is_streaming=host.message_runtime.is_streaming(host.current_conversation.id),
            )

    def delete(self, message_id: str) -> None:
        host = self._host
        if not host.current_conversation:
            return

        reply = QMessageBox.question(
            host,
            "删除消息",
            "确定要删除这条消息吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            deleted_ids = host.current_conversation.delete_message(message_id) or []
            for mid in deleted_ids:
                host.chat_view.remove_message(mid)
            host.stats_panel.update_stats(host.current_conversation)
            host.services.conv_service.save(host.current_conversation)
            host.services.app_coordinator.remember_current_conversation(
                host.current_conversation,
                providers=host.providers,
                app_settings=host.app_settings,
                is_streaming=host.message_runtime.is_streaming(host.current_conversation.id),
            )
            self._sync_header(host.current_conversation.id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_provider(self, provider_id: str):
        for p in self._host.providers:
            if p.id == provider_id:
                return p
        return None

    def _sync_header(self, conversation_id: str) -> None:
        presenter = getattr(self._host, "window_state_presenter", None)
        if presenter is None or not hasattr(presenter, "sync_chat_header_for_current_conversation"):
            return
        try:
            presenter.sync_chat_header_for_current_conversation(conversation_id)
        except Exception as exc:
            logger.debug("Failed to sync chat header after message update: %s", exc)

