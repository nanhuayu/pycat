"""Prompt optimization presenter.

Owns prompt optimizer request/cancel and callback handling so message
streaming responsibilities can stay focused on conversation transport.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import QMessageBox

from pycat.core.capabilities.defaults import default_capabilities_config
from pycat.core.llm.model_selection import resolve_model_target

if TYPE_CHECKING:
    from pycat.gui.main_window import MainWindow

logger = logging.getLogger(__name__)


class PromptOptimizationPresenter:
    """Handles prompt optimizer UI lifecycle and request preparation."""

    def __init__(self, host: MainWindow) -> None:
        self._host = host

    def on_started(
        self,
        conversation_id: str,
        request_id: str,
    ) -> None:
        host = self._host
        if host.current_conversation and host.current_conversation.id == conversation_id:
            try:
                host.input_area.set_prompt_optimize_busy(True)
            except Exception as e:
                logger.debug("Failed to set prompt optimize busy state: %s", e)

    def on_complete(
        self,
        conversation_id: str,
        request_id: str,
        text: str,
    ) -> None:
        host = self._host
        if not host.current_conversation or host.current_conversation.id != conversation_id:
            return
        try:
            host.input_area.set_prompt_optimize_busy(False)
            host.input_area.text_input.setPlainText((text or '').strip())
            cursor = host.input_area.text_input.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            host.input_area.text_input.setTextCursor(cursor)
            host.input_area.text_input.setFocus()
        except Exception as e:
            logger.debug("Failed to apply optimized prompt: %s", e)

    def on_error(
        self,
        conversation_id: str,
        request_id: str,
        err: str,
    ) -> None:
        host = self._host
        if not host.current_conversation or host.current_conversation.id != conversation_id:
            return
        try:
            host.input_area.set_prompt_optimize_busy(False)
        except Exception as e:
            logger.debug("Failed to reset prompt optimize busy: %s", e)
        QMessageBox.warning(host, '提示词优化失败', err or '未知错误')

    def on_cancelled(
        self,
        conversation_id: str,
        request_id: str,
    ) -> None:
        host = self._host
        if not host.current_conversation or host.current_conversation.id != conversation_id:
            return
        try:
            host.input_area.set_prompt_optimize_busy(False)
        except Exception as e:
            logger.debug("Failed to reset prompt optimize busy after cancel: %s", e)

    def cancel(self) -> None:
        host = self._host
        if not host.current_conversation:
            return
        if not host.prompt_optimizer.cancel(host.current_conversation.id):
            return
        try:
            host.input_area.set_prompt_optimize_busy(False)
        except Exception as e:
            logger.debug("Failed to reset optimize busy on manual cancel: %s", e)

    def request(self, raw_text: str) -> None:
        host = self._host
        if not host.current_conversation:
            host.current_conversation = host.conversation_presenter.ensure_current_conversation_shell()

        if host.message_runtime.is_streaming(host.current_conversation.id):
            QMessageBox.information(host, '提示', '当前会话正在生成中，请先停止或等待完成。')
            return

        text = (raw_text or '').strip()
        if not text:
            return

        provider_id = host.input_area.get_selected_provider_id()
        base_model = host.input_area.get_selected_model()

        provider = host.services.conv_service.find_provider(host.providers, provider_id)
        if not provider:
            QMessageBox.warning(host, '错误', '请先在设置中配置服务商')
            return

        if not base_model:
            QMessageBox.warning(host, '错误', '请选择一个模型')
            return

        try:
            prompt_capability = host.services.capability_executor.get_capability("prompt_optimize")
        except Exception as e:
            logger.debug("Failed to read prompt optimizer capability from container: %s", e)
            prompt_capability = default_capabilities_config().capability("prompt_optimize")
        if prompt_capability is None or not prompt_capability.enabled:
            QMessageBox.warning(host, '错误', '提示词优化能力已停用。')
            return

        target = prompt_capability.model_target
        selection = resolve_model_target(
            host.providers,
            target,
            primary_provider=provider,
            primary_model=base_model,
            auxiliary_model_ref=str(host.app_settings.get("default_auxiliary_model", "") or "").strip(),
        )
        provider = selection.provider or provider

        host.input_area.set_prompt_optimize_busy(True)

        host.prompt_optimizer.start(
            provider=provider,
            conversation_id=host.current_conversation.id,
            raw_prompt=text,
            resolved_selection=selection,
        )
