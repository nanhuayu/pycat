from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from models.conversation import Conversation, Message
from models.provider import Provider, build_model_ref
from models.streaming import ConversationStreamState

from core.llm.client import LLMClient
from core.runtime.events import TurnEvent, TurnEventKind
from core.runtime.turn_engine import TurnEngine
from core.runtime.turn_policy import TurnPolicy
from core.tools.manager import ToolManager
from core.task.types import RunPolicy, TaskStatus


logger = logging.getLogger(__name__)


class MessageRuntime(QObject):
    """UI runtime bridge for MessageEngine.

    - Owns worker threads + request routing
    - Exposes Qt signals (main-thread)
    - Keeps per-conversation streaming state for UI restore

    Core logic lives in `core.agent.message_engine.MessageEngine`.
    """

    stream_started = pyqtSignal(str, str, str)          # conversation_id, request_id, model
    token_received = pyqtSignal(str, str, str)          # conversation_id, request_id, token
    thinking_received = pyqtSignal(str, str, str)       # conversation_id, request_id, thinking
    response_step = pyqtSignal(str, str, object)        # conversation_id, request_id, Message
    response_complete = pyqtSignal(str, str, object)    # conversation_id, request_id, Message
    response_error = pyqtSignal(str, str, str)          # conversation_id, request_id, error
    retry_attempt = pyqtSignal(str, str, str)            # conversation_id, request_id, detail
    runtime_event = pyqtSignal(str, str, object)         # conversation_id, request_id, TurnEvent
    approval_requested = pyqtSignal(str, object)         # message, future
    question_requested = pyqtSignal(object, object)      # question spec, future

    _raw_token = pyqtSignal(str, str, str)
    _raw_thinking = pyqtSignal(str, str, str)
    _raw_step = pyqtSignal(str, str, object)
    _raw_complete = pyqtSignal(str, str, object)
    _raw_error = pyqtSignal(str, str, str)
    _raw_retry = pyqtSignal(str, str, str)
    _raw_runtime_event = pyqtSignal(str, str, object)

    def __init__(
        self,
        client: LLMClient,
        tool_manager: Optional[ToolManager] = None,
        turn_engine: Optional[TurnEngine] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._client = client
        self._tool_manager = tool_manager or client.tool_manager
        self._engine = turn_engine or TurnEngine(client=self._client, tool_manager=self._tool_manager)

        self._streams: dict[str, ConversationStreamState] = {}
        self._last_request_id: dict[str, str] = {}

        self._raw_token.connect(self._on_raw_token)
        self._raw_thinking.connect(self._on_raw_thinking)
        self._raw_step.connect(self._on_raw_step)
        self._raw_complete.connect(self._on_raw_complete)
        self._raw_error.connect(self._on_raw_error)
        self._raw_retry.connect(self._on_raw_retry)
        self._raw_runtime_event.connect(self._on_raw_runtime_event)
        self.approval_requested.connect(self._on_approval_requested)
        self.question_requested.connect(self._on_question_requested)

    def is_streaming(self, conversation_id: str) -> bool:
        return bool(conversation_id) and conversation_id in self._streams

    def get_state(self, conversation_id: str) -> Optional[ConversationStreamState]:
        return self._streams.get(conversation_id)

    def start(
        self,
        provider: Provider,
        conversation: Conversation,
        *,
        policy: RunPolicy | TurnPolicy,
        debug_log_path: Optional[str] = None,
    ) -> Optional[ConversationStreamState]:
        conversation_id = getattr(conversation, "id", "") or ""
        if not conversation_id:
            return None

        if isinstance(policy, TurnPolicy):
            turn_policy = policy
            effective_policy = policy.to_run_policy()
        else:
            effective_policy = policy
            turn_policy = TurnPolicy.from_run_policy(policy, conversation=conversation)

        request_id = str(uuid.uuid4())
        model_name = build_model_ref(provider.name, turn_policy.llm.resolved_model(provider))

        state = ConversationStreamState(
            conversation_id=conversation_id,
            request_id=request_id,
            model=model_name,
        )
        self._streams[conversation_id] = state
        self._last_request_id[conversation_id] = request_id

        self.stream_started.emit(conversation_id, request_id, model_name)

        try:
            conversation_snapshot = Conversation.from_dict(conversation.to_dict())
        except Exception:
            conversation_snapshot = conversation

        def run_worker() -> None:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                def on_token(t: str) -> None:
                    self._raw_token.emit(conversation_id, request_id, t)

                def on_thinking(t: str) -> None:
                    self._raw_thinking.emit(conversation_id, request_id, t)

                def on_step(m: Message) -> None:
                    self._raw_step.emit(conversation_id, request_id, m)

                async def approval_callback(message: str) -> bool:
                    future = loop.create_future()
                    self.approval_requested.emit(str(message or ""), future)
                    return bool(await future)

                async def questions_callback(question: dict) -> dict:
                    future = loop.create_future()
                    self.question_requested.emit(dict(question or {}), future)
                    result = await future
                    if isinstance(result, dict):
                        return result
                    return {"selected": [], "freeText": None, "skipped": True}

                async def run() -> None:
                    def on_event(evt: TurnEvent) -> None:
                        self._raw_runtime_event.emit(conversation_id, request_id, evt)
                        if evt.kind == TurnEventKind.STEP and isinstance(evt.data, Message):
                            on_step(evt.data)
                        elif evt.kind == TurnEventKind.RETRY:
                            self._raw_retry.emit(conversation_id, request_id, evt.detail or "")

                    result = await self._engine.run(
                        provider=provider,
                        conversation=conversation_snapshot,
                        policy=turn_policy,
                        on_event=on_event,
                        on_token=on_token,
                        on_thinking=on_thinking,
                        approval_callback=approval_callback,
                        questions_callback=questions_callback,
                        cancel_event=state.cancel_event,
                        debug_log_path=debug_log_path,
                    )
                    if result.status == TaskStatus.CANCELLED:
                        self._raw_error.emit(conversation_id, request_id, "已取消生成")
                        return
                    if result.status == TaskStatus.FAILED:
                        self._raw_error.emit(conversation_id, request_id, result.error or "生成失败")
                        return
                    self._raw_complete.emit(conversation_id, request_id, result.final_message)

                loop.run_until_complete(run())
                loop.run_until_complete(loop.shutdown_asyncgens())
                try:
                    loop.run_until_complete(loop.shutdown_default_executor())
                except Exception as exc:
                    logger.debug("Failed to shutdown runtime default executor: %s", exc)
                loop.close()

            except Exception as e:
                self._raw_error.emit(conversation_id, request_id, str(e))

        threading.Thread(target=run_worker, daemon=True).start()
        return state

    def cancel(self, conversation_id: str) -> None:
        state = self._streams.get(conversation_id)
        if state:
            state.cancel()

    # ===== Raw -> main thread normalization =====

    def _accept_event(self, conversation_id: str, request_id: str) -> bool:
        if not conversation_id or not request_id:
            return False
        live = self._streams.get(conversation_id)
        if live and live.request_id == request_id:
            return True
        return self._last_request_id.get(conversation_id) == request_id

    def _on_raw_token(self, conversation_id: str, request_id: str, token: str) -> None:
        if not self._accept_event(conversation_id, request_id):
            return
        state = self._streams.get(conversation_id)
        if state:
            try:
                state.visible_text += token
            except Exception as exc:
                logger.debug("Failed to append visible streaming token: %s", exc)
        self.token_received.emit(conversation_id, request_id, token)

    def _on_raw_thinking(self, conversation_id: str, request_id: str, thinking: str) -> None:
        if not self._accept_event(conversation_id, request_id):
            return
        state = self._streams.get(conversation_id)
        if state:
            try:
                state.thinking_text += thinking
            except Exception as exc:
                logger.debug("Failed to append thinking streaming token: %s", exc)
        self.thinking_received.emit(conversation_id, request_id, thinking)

    def _on_raw_step(self, conversation_id: str, request_id: str, message: Message) -> None:
        if not self._accept_event(conversation_id, request_id):
            return

        try:
            metadata = getattr(message, "metadata", {}) or {}
            if isinstance(metadata, dict) and metadata.get("subtask_trace_only"):
                return
        except Exception as exc:
            logger.debug("Failed to inspect step metadata: %s", exc)

        # When we publish an assistant step (tool_calls), the UI will finish the current bubble.
        # Reset the streaming buffers so switching conversations can restore the *next* bubble cleanly.
        try:
            if isinstance(message, Message) and getattr(message, "role", "") == "assistant":
                state = self._streams.get(conversation_id)
                if state:
                    state.visible_text = ""
                    state.thinking_text = ""
        except Exception as exc:
            logger.debug("Failed to reset streaming buffers after assistant step: %s", exc)

        self.response_step.emit(conversation_id, request_id, message)

    def _on_raw_complete(self, conversation_id: str, request_id: str, message: Optional[Message]) -> None:
        if not self._accept_event(conversation_id, request_id):
            return
        # Cleanup live state
        try:
            self._streams.pop(conversation_id, None)
        except Exception as exc:
            logger.debug("Failed to clear live stream state on completion: %s", exc)
        self.response_complete.emit(conversation_id, request_id, message)

    def _on_raw_error(self, conversation_id: str, request_id: str, error: str) -> None:
        if not self._accept_event(conversation_id, request_id):
            return
        try:
            self._streams.pop(conversation_id, None)
        except Exception as exc:
            logger.debug("Failed to clear live stream state on error: %s", exc)
        self.response_error.emit(conversation_id, request_id, error)

    def _on_raw_retry(self, conversation_id: str, request_id: str, detail: str) -> None:
        if not self._accept_event(conversation_id, request_id):
            return
        self.retry_attempt.emit(conversation_id, request_id, detail)

    def _on_raw_runtime_event(self, conversation_id: str, request_id: str, event: TurnEvent) -> None:
        if not self._accept_event(conversation_id, request_id):
            return
        state = self._streams.get(conversation_id)
        if state:
            try:
                kind_value = getattr(getattr(event, "kind", ""), "value", str(getattr(event, "kind", "")))
                event_payload = self._build_runtime_event_payload(event)
                state.record_event(
                    kind=kind_value,
                    detail=self._describe_runtime_event(kind_value, event_payload, fallback=str(getattr(event, "detail", "") or "")),
                    data=event_payload,
                )
            except Exception as exc:
                logger.debug("Failed to record runtime event: %s", exc)
        self.runtime_event.emit(conversation_id, request_id, event)

    @staticmethod
    def _settle_ui_future(future: object, *, result=None) -> None:
        try:
            loop = future.get_loop()
        except Exception:
            return

        def _apply_result() -> None:
            try:
                if not future.done():
                    future.set_result(result)
            except Exception as exc:
                logger.debug("Failed to settle runtime UI future: %s", exc)

        loop.call_soon_threadsafe(_apply_result)

    def _on_approval_requested(self, message: str, future: object) -> None:
        try:
            reply = QMessageBox.question(
                self.parent(),
                "工具执行确认",
                str(message or "请确认继续执行工具。"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            self._settle_ui_future(future, result=reply == QMessageBox.StandardButton.Yes)
        except Exception as exc:
            logger.debug("Failed to show tool approval dialog: %s", exc)
            self._settle_ui_future(future, result=False)

    def _on_question_requested(self, question: object, future: object) -> None:
        host = self.parent()
        try:
            chat_view = getattr(host, "chat_view", None)
            if chat_view is not None and hasattr(chat_view, "show_inline_question"):
                chat_view.show_inline_question(
                    dict(question or {}),
                    on_submit=lambda answer: self._settle_ui_future(future, result=answer),
                    on_cancel=lambda: self._settle_ui_future(
                        future,
                        result={"selected": [], "freeText": None, "skipped": True},
                    ),
                )
                return

            from ui.dialogs.questions_dialog import QuestionsDialog

            dialog = QuestionsDialog(dict(question or {}), parent=host)
            if dialog.exec():
                answer = dialog.get_answer()
            else:
                answer = {"selected": [], "freeText": None, "skipped": True}
            self._settle_ui_future(future, result=answer)
        except Exception as exc:
            logger.debug("Failed to show askQuestions dialog: %s", exc)
            self._settle_ui_future(
                future,
                result={"selected": [], "freeText": None, "skipped": True},
            )

    @staticmethod
    def _build_runtime_event_payload(event: TurnEvent) -> dict:
        payload: dict = {
            "turn": int(getattr(event, "turn", 0) or 0),
            "source": str(getattr(event, "source", "") or "task"),
            "subtask_id": str(getattr(event, "subtask_id", "") or ""),
            "parent_message_id": str(getattr(event, "parent_message_id", "") or ""),
            "parent_tool_call_id": str(getattr(event, "parent_tool_call_id", "") or ""),
            "root_tool_call_id": str(getattr(event, "root_tool_call_id", "") or ""),
        }
        data = getattr(event, "data", None)
        if isinstance(data, dict):
            for key, value in data.items():
                if key == "subtask" and isinstance(value, dict):
                    payload["subtask"] = {
                        "id": str(value.get("id") or ""),
                        "title": str(value.get("title") or value.get("name") or ""),
                        "status": str(value.get("status") or ""),
                        "tool_call_id": str(value.get("tool_call_id") or (value.get("metadata") or {}).get("tool_call_id") or ""),
                        "parent_message_id": str(value.get("parent_message_id") or (value.get("metadata") or {}).get("parent_message_id") or ""),
                        "parent_tool_call_id": str(value.get("parent_tool_call_id") or (value.get("metadata") or {}).get("parent_tool_call_id") or ""),
                        "root_tool_call_id": str(value.get("root_tool_call_id") or (value.get("metadata") or {}).get("root_tool_call_id") or ""),
                        "summary": str(value.get("final_message") or value.get("error") or value.get("goal") or "")[:220],
                    }
                    continue
                payload[key] = value
            return payload

        if isinstance(data, Message):
            payload["role"] = str(getattr(data, "role", "") or "").strip()
            payload["tool_call_id"] = str(getattr(data, "tool_call_id", "") or "").strip()
            summary = str(getattr(data, "summary", "") or getattr(data, "content", "") or "").strip()
            if summary:
                payload["summary"] = summary[:220]
            return payload

        if data is not None:
            payload["summary"] = str(data)[:220]
        return payload

    @staticmethod
    def _describe_runtime_event(kind: str, payload: dict, *, fallback: str = "") -> str:
        detail = str(fallback or "").strip()
        if detail:
            return detail

        summary = str(payload.get("summary") or "").strip()
        tool_name = str(payload.get("tool_name") or "").strip()
        role = str(payload.get("role") or "").strip()

        if kind == "tool_start":
            return f"正在执行 {tool_name or '工具'}"
        if kind == "tool_end":
            return summary or f"{tool_name or '工具'} 已返回结果"
        if kind == "step":
            if role == "tool_result":
                return summary or f"{tool_name or '工具'} 输出已写入会话"
            if role == "assistant":
                return summary or "助手消息已写入会话"
        if kind == "complete":
            return summary or "本轮任务已完成"
        if kind == "retry":
            return summary or "正在准备重试"
        return detail or summary or "-"
