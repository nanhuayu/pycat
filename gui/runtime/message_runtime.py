from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from core.agent.events.conversation import conversation_patch_payload
from core.agent.events.stream_batcher import StreamDeltaBatcher
from core.agent.run.control import RunControl
from core.agent.run.runtime import AgentRuntime
from core.observability import create_run_debug_trace, finish_run_debug_trace
from core.tools.base import ToolApprovalRequest
from models.contracts.agent import RunEvent, RunEventKind, RunPolicy, RunStatus
from models.contracts.tooling import ToolPermissionConfig
from models.conversation import Conversation, Message
from models.model_ref import build_model_ref
from models.provider import Provider
from models.streaming import ConversationPatch, ConversationStreamState

logger = logging.getLogger(__name__)


@dataclass
class _WorkerControl:
    request_id: str
    run_control: RunControl
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task | None = None


@dataclass
class _PendingApproval:
    request_id: str
    request: ToolApprovalRequest
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future
    settling: bool = False


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
    run_finished = pyqtSignal(str, str, object)         # conversation_id, request_id, RunStatus
    retry_attempt = pyqtSignal(str, str, str)            # conversation_id, request_id, detail
    runtime_event = pyqtSignal(str, str, object)         # conversation_id, request_id, RunEvent
    conversation_patch = pyqtSignal(str, str, object)    # conversation_id, request_id, ConversationPatch
    approval_requested = pyqtSignal(str, str, object)    # conversation_id, approval_id, request
    approval_resolved = pyqtSignal(str)                  # approval_id
    guidance_recovered = pyqtSignal(str, object)         # conversation_id, tuple[str, ...]
    question_requested = pyqtSignal(object, object)      # question spec, future

    _raw_stream_batch = pyqtSignal(str, str, str, str)
    _raw_step = pyqtSignal(str, str, object)
    _raw_complete = pyqtSignal(str, str, object)
    _raw_error = pyqtSignal(str, str, str)
    _raw_finished = pyqtSignal(str, str, object)
    _raw_retry = pyqtSignal(str, str, str)
    _raw_runtime_event = pyqtSignal(str, str, object)
    _raw_conversation_patch = pyqtSignal(str, str, object)
    _raw_approval_requested = pyqtSignal(str, str, object)
    _raw_approval_resolved = pyqtSignal(str)

    def __init__(
        self,
        agent_runtime: AgentRuntime,
        parent: Optional[QObject] = None,
        *,
        activity_service=None,
    ):
        super().__init__(parent)
        self._runtime = agent_runtime
        self._activity_service = activity_service

        self._streams: dict[str, ConversationStreamState] = {}
        self._last_request_id: dict[str, str] = {}
        self._worker_controls: dict[str, _WorkerControl] = {}
        self._worker_controls_lock = threading.Lock()
        self._pending_approvals: dict[str, _PendingApproval] = {}
        self._pending_approvals_lock = threading.Lock()

        self._raw_stream_batch.connect(self._on_raw_stream_batch)
        self._raw_step.connect(self._on_raw_step)
        self._raw_complete.connect(self._on_raw_complete)
        self._raw_error.connect(self._on_raw_error)
        self._raw_finished.connect(self._on_raw_finished)
        self._raw_retry.connect(self._on_raw_retry)
        self._raw_runtime_event.connect(self._on_raw_runtime_event)
        self._raw_conversation_patch.connect(self._on_raw_conversation_patch)
        self._raw_approval_requested.connect(self._on_raw_approval_requested)
        self._raw_approval_resolved.connect(self._on_raw_approval_resolved)
        self.question_requested.connect(self._on_question_requested)

    def is_streaming(self, conversation_id: str) -> bool:
        return bool(conversation_id) and conversation_id in self._streams

    def streaming_conversation_ids(self) -> tuple[str, ...]:
        return tuple(self._streams.keys())

    def get_state(self, conversation_id: str) -> Optional[ConversationStreamState]:
        return self._streams.get(conversation_id)

    def pending_guidance_count(self, conversation_id: str) -> int:
        with self._worker_controls_lock:
            worker = self._worker_controls.get(str(conversation_id or ""))
        return worker.run_control.pending_count if worker is not None else 0

    def submit_guidance(self, conversation_id: str, text: str) -> bool:
        key = str(conversation_id or "").strip()
        with self._worker_controls_lock:
            worker = self._worker_controls.get(key)
        if worker is None or not worker.run_control.submit(text):
            return False
        return True

    def update_permissions(
        self,
        conversation_id: str,
        tool_permissions: ToolPermissionConfig,
    ) -> int | None:
        key = str(conversation_id or "").strip()
        with self._worker_controls_lock:
            worker = self._worker_controls.get(key)
        if worker is None:
            return None
        return worker.run_control.update_permissions(tool_permissions)

    def start(
        self,
        provider: Provider,
        conversation: Conversation,
        *,
        policy: RunPolicy,
        debug_log_path: Optional[str] = None,
        initial_runtime_messages: list[Message] | None = None,
    ) -> Optional[ConversationStreamState]:
        conversation_id = getattr(conversation, "id", "") or ""
        if not conversation_id:
            return None

        begin_turn = getattr(self._activity_service, "begin_turn", None)
        if callable(begin_turn) and not begin_turn(conversation_id):
            logger.debug("Conversation %s is already active; rejecting Desktop turn", conversation_id)
            return None
        activity_claimed = callable(begin_turn)

        request_id = str(uuid.uuid4())
        model_name = build_model_ref(provider.name, self._resolve_state_model(provider, conversation, policy))
        run_control = RunControl(policy.tool_permissions)
        worker_control = _WorkerControl(
            request_id=request_id,
            run_control=run_control,
        )
        with self._worker_controls_lock:
            self._worker_controls[conversation_id] = worker_control

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
        debug_trace = create_run_debug_trace(
            conversation=conversation_snapshot,
            request_id=request_id,
            model_name=model_name,
            mode=str(getattr(policy, "mode", "") or getattr(conversation_snapshot, "mode", "") or "chat"),
            source="gui",
            capture_payloads=bool(debug_log_path),
            capture_stream=bool(debug_log_path),
        )

        def run_worker() -> None:
            loop: asyncio.AbstractEventLoop | None = None
            task: asyncio.Task | None = None
            stream_batch: StreamDeltaBatcher | None = None
            terminal_emitted = False

            def flush_stream() -> None:
                if stream_batch is not None:
                    stream_batch.flush()

            def emit_cancelled() -> None:
                nonlocal terminal_emitted
                if terminal_emitted:
                    return
                terminal_emitted = True
                flush_stream()
                finish_run_debug_trace(debug_trace, status="cancelled", summary="Cancelled")
                self._raw_error.emit(conversation_id, request_id, "已取消生成")
                self._raw_finished.emit(conversation_id, request_id, RunStatus.CANCELLED)

            def emit_error(error: str) -> None:
                nonlocal terminal_emitted
                if terminal_emitted:
                    return
                terminal_emitted = True
                detail = str(error or "生成失败")
                flush_stream()
                finish_run_debug_trace(debug_trace, status="error", summary=detail)
                self._raw_error.emit(conversation_id, request_id, detail)
                self._raw_finished.emit(conversation_id, request_id, RunStatus.FAILED)

            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                stream_batch = StreamDeltaBatcher(
                    schedule=loop.call_later,
                    emit=lambda visible, thinking: self._raw_stream_batch.emit(
                        conversation_id,
                        request_id,
                        visible,
                        thinking,
                    ),
                )

                def on_token(t: str) -> None:
                    stream_batch.append_visible(t)

                def on_thinking(t: str) -> None:
                    stream_batch.append_thinking(t)

                def on_step(m: Message) -> None:
                    flush_stream()
                    self._raw_step.emit(conversation_id, request_id, m)

                def on_patch(payload: object) -> None:
                    flush_stream()
                    self._raw_conversation_patch.emit(conversation_id, request_id, payload)

                async def approval_callback(request: ToolApprovalRequest) -> bool:
                    flush_stream()
                    approval_id = (
                        f"{request_id}:{request.tool_call_id or 'tool'}:{uuid.uuid4().hex[:12]}"
                    )
                    future = loop.create_future()
                    pending = _PendingApproval(
                        request_id=request_id,
                        request=request,
                        loop=loop,
                        future=future,
                    )
                    with self._pending_approvals_lock:
                        self._pending_approvals[approval_id] = pending
                    self._raw_approval_requested.emit(conversation_id, approval_id, request)
                    try:
                        return bool(await future)
                    finally:
                        with self._pending_approvals_lock:
                            self._pending_approvals.pop(approval_id, None)
                        self._raw_approval_resolved.emit(approval_id)

                async def questions_callback(question: dict) -> dict:
                    flush_stream()
                    future = loop.create_future()
                    self.question_requested.emit(dict(question or {}), future)
                    result = await future
                    if isinstance(result, dict):
                        return result
                    return {"selected": [], "freeText": None, "skipped": True}

                async def run() -> None:
                    nonlocal terminal_emitted

                    def on_event(evt: RunEvent) -> None:
                        flush_stream()
                        self._raw_runtime_event.emit(conversation_id, request_id, evt)
                        if isinstance(evt.data, dict) and isinstance(evt.data.get("conversation_patch"), dict):
                            on_patch(evt.data.get("conversation_patch") or {})
                        if evt.kind == RunEventKind.STEP and isinstance(evt.data, Message):
                            on_step(evt.data)
                        elif evt.kind == RunEventKind.RETRY:
                            self._raw_retry.emit(conversation_id, request_id, evt.detail or "")

                    result = await self._runtime.run(
                        provider=provider,
                        conversation=conversation_snapshot,
                        policy=policy,
                        on_event=on_event,
                        on_token=on_token,
                        on_thinking=on_thinking,
                        approval_callback=approval_callback,
                        questions_callback=questions_callback,
                        cancel_event=state.cancel_event,
                        debug_log_path=debug_log_path,
                        debug_trace=debug_trace,
                        initial_runtime_messages=list(initial_runtime_messages or []),
                        run_control=run_control,
                    )
                    if result.status == RunStatus.CANCELLED:
                        emit_cancelled()
                        return
                    if result.status == RunStatus.FAILED:
                        emit_error(result.error or "生成失败")
                        return
                    flush_stream()
                    final_conversation = getattr(result, "conversation", None)
                    if final_conversation is not None:
                        on_patch(self._build_conversation_patch_payload(final_conversation))
                    final_summary = ""
                    try:
                        final_summary = str(getattr(result.final_message, "summary", "") or getattr(result.final_message, "content", "") or "")
                    except Exception:
                        final_summary = ""
                    terminal_emitted = True
                    finish_run_debug_trace(debug_trace, status=result.status.value, summary=final_summary)
                    self._raw_complete.emit(conversation_id, request_id, result.final_message)
                    self._raw_finished.emit(conversation_id, request_id, result.status)

                task = loop.create_task(run())
                self._register_worker_control(
                    conversation_id,
                    request_id,
                    loop,
                    task,
                    state,
                )
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                emit_cancelled()
            except Exception as exc:
                if state.cancel_event.is_set():
                    emit_cancelled()
                else:
                    emit_error(str(exc))
            finally:
                recovered = run_control.close_and_drain()
                if recovered:
                    self.guidance_recovered.emit(conversation_id, recovered)
                self.reject_pending_approvals(request_id=request_id)
                self._unregister_worker_control(conversation_id, request_id, task)
                if activity_claimed:
                    end_turn = getattr(self._activity_service, "end_turn", None)
                    if callable(end_turn):
                        end_turn(conversation_id)
                if loop is not None and not loop.is_closed():
                    try:
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    except Exception as exc:
                        logger.debug("Failed to shutdown runtime async generators: %s", exc)
                    try:
                        loop.run_until_complete(loop.shutdown_default_executor())
                    except Exception as exc:
                        logger.debug("Failed to shutdown runtime default executor: %s", exc)
                    asyncio.set_event_loop(None)
                    loop.close()

        try:
            threading.Thread(target=run_worker, daemon=True).start()
        except Exception:
            with self._worker_controls_lock:
                current = self._worker_controls.get(conversation_id)
                if current is worker_control:
                    self._worker_controls.pop(conversation_id, None)
            if activity_claimed:
                end_turn = getattr(self._activity_service, "end_turn", None)
                if callable(end_turn):
                    end_turn(conversation_id)
            raise
        return state

    @staticmethod
    def _resolve_state_model(provider: Provider, conversation: Conversation, policy: RunPolicy) -> str:
        try:
            llm_config = conversation.get_llm_config()
            if policy.model:
                llm_config = llm_config.with_updates(model=str(policy.model))
            return llm_config.resolved_model()
        except Exception:
            return (
                str(getattr(policy, "model", "") or "").strip()
                or str(getattr(conversation, "model", "") or "").strip()
            )

    def cancel(self, conversation_id: str) -> bool:
        state = self._streams.get(conversation_id)
        if state is None:
            return False
        state.cancel()
        self.reject_pending_approvals(request_id=state.request_id)
        with self._worker_controls_lock:
            control = self._worker_controls.get(conversation_id)
        if control is not None and control.request_id == state.request_id:
            try:
                if control.loop is not None and control.task is not None:
                    control.loop.call_soon_threadsafe(self._cancel_task, control.task)
            except RuntimeError as exc:
                logger.debug("Failed to interrupt message runtime task: %s", exc)
        return True

    def pending_approvals(self) -> dict[str, ToolApprovalRequest]:
        with self._pending_approvals_lock:
            return {key: item.request for key, item in self._pending_approvals.items()}

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        with self._pending_approvals_lock:
            pending = self._pending_approvals.get(str(approval_id or ""))
            if pending is None or pending.settling:
                return False
            pending.settling = True

        def settle() -> None:
            try:
                if not pending.future.done():
                    pending.future.set_result(bool(approved))
            except Exception as exc:
                logger.debug("Failed to resolve tool approval: %s", exc)

        try:
            pending.loop.call_soon_threadsafe(settle)
            return True
        except RuntimeError as exc:
            logger.debug("Failed to schedule tool approval result: %s", exc)
            with self._pending_approvals_lock:
                self._pending_approvals.pop(str(approval_id or ""), None)
            self._raw_approval_resolved.emit(str(approval_id or ""))
            return False

    def reject_pending_approvals(self, *, request_id: str = "") -> int:
        with self._pending_approvals_lock:
            approval_ids = [
                approval_id
                for approval_id, pending in self._pending_approvals.items()
                if not request_id or pending.request_id == request_id
            ]
        resolved = 0
        for approval_id in approval_ids:
            resolved += int(self.resolve_approval(approval_id, False))
        return resolved

    def _register_worker_control(
        self,
        conversation_id: str,
        request_id: str,
        loop: asyncio.AbstractEventLoop,
        task: asyncio.Task,
        state: ConversationStreamState,
    ) -> None:
        with self._worker_controls_lock:
            control = self._worker_controls.get(conversation_id)
            if control is None or control.request_id != request_id:
                return
            control.loop = loop
            control.task = task
        if state.cancel_event.is_set():
            task.cancel()

    def _unregister_worker_control(
        self,
        conversation_id: str,
        request_id: str,
        task: asyncio.Task | None,
    ) -> None:
        with self._worker_controls_lock:
            control = self._worker_controls.get(conversation_id)
            if control is None:
                return
            if control.request_id == request_id and (task is None or control.task is task):
                self._worker_controls.pop(conversation_id, None)

    @staticmethod
    def _cancel_task(task: asyncio.Task) -> None:
        if not task.done():
            task.cancel()

    # ===== Raw -> main thread normalization =====

    def _accept_event(self, conversation_id: str, request_id: str) -> bool:
        if not conversation_id or not request_id:
            return False
        live = self._streams.get(conversation_id)
        if live and live.request_id == request_id:
            return True
        return self._last_request_id.get(conversation_id) == request_id

    def _on_raw_stream_batch(
        self,
        conversation_id: str,
        request_id: str,
        visible: str,
        thinking: str,
    ) -> None:
        if not self._accept_event(conversation_id, request_id):
            return
        state = self._streams.get(conversation_id)
        if state:
            try:
                state.visible_text += visible
                state.thinking_text += thinking
            except Exception as exc:
                logger.debug("Failed to append streaming batch: %s", exc)
        if visible:
            self.token_received.emit(conversation_id, request_id, visible)
        if thinking:
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

    def _on_raw_finished(
        self,
        conversation_id: str,
        request_id: str,
        status: RunStatus,
    ) -> None:
        if not self._accept_event(conversation_id, request_id):
            return
        self.run_finished.emit(conversation_id, request_id, status)

    def _on_raw_retry(self, conversation_id: str, request_id: str, detail: str) -> None:
        if not self._accept_event(conversation_id, request_id):
            return
        self.retry_attempt.emit(conversation_id, request_id, detail)

    def _on_raw_runtime_event(self, conversation_id: str, request_id: str, event: RunEvent) -> None:
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

    def _on_raw_conversation_patch(self, conversation_id: str, request_id: str, payload: object) -> None:
        if not self._accept_event(conversation_id, request_id):
            return
        patch = self._coerce_conversation_patch(conversation_id, payload)
        if patch is None:
            return
        self.conversation_patch.emit(conversation_id, request_id, patch)

    def _on_raw_approval_requested(
        self,
        conversation_id: str,
        approval_id: str,
        request: object,
    ) -> None:
        if isinstance(request, ToolApprovalRequest):
            self.approval_requested.emit(conversation_id, approval_id, request)
            return
        self.resolve_approval(approval_id, False)

    def _on_raw_approval_resolved(self, approval_id: str) -> None:
        self.approval_resolved.emit(approval_id)

    @staticmethod
    def _build_conversation_patch_payload(conversation: Conversation) -> dict:
        payload = conversation_patch_payload(conversation)
        return dict(payload.get("conversation_patch") or {})

    @staticmethod
    def _coerce_conversation_patch(conversation_id: str, payload: object) -> ConversationPatch | None:
        return ConversationPatch.from_payload(
            payload,
            conversation_id=conversation_id,
        )

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

            from gui.dialogs.questions_dialog import QuestionsDialog

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
    def _build_runtime_event_payload(event: RunEvent) -> dict:
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
                if key == "conversation_patch" and isinstance(value, dict):
                    payload["conversation_patch"] = {
                        "conversation_id": str(value.get("conversation_id") or ""),
                        "changed_messages": len(value.get("changed_messages") or []),
                        "condensed_messages": len(value.get("condensed_message_ids") or {}),
                        "has_state": isinstance(value.get("state"), dict),
                    }
                    continue
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
