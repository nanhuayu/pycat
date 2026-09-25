from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal

from pycat.core.agent.events.conversation import conversation_patch_payload
from pycat.core.agent.events.stream_batcher import StreamDeltaBatcher
from pycat.core.agent.run.control import RunControl
from pycat.core.app.services.run import RunService
from pycat.core.tools.base import ApprovalDecision, ToolApprovalRequest
from pycat.gui.runtime.loop_utils import cancel_loop_task
from pycat.models.contracts.agent import PersistenceError, RunEvent, RunEventKind, RunPolicy, RunStatus
from pycat.models.contracts.tooling import FilesystemScope, ToolPermissionConfig
from pycat.models.conversation import Conversation, Message
from pycat.models.model_ref import build_model_ref
from pycat.models.provider import Provider
from pycat.models.streaming import ConversationPatch, ConversationStreamState

logger = logging.getLogger(__name__)


@dataclass
class _WorkerControl:
    request_id: str
    run_control: RunControl
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task | None = None


@dataclass(frozen=True)
class UserInteraction:
    """A run-owned UI request. Futures never leave the runtime bridge."""

    id: str
    conversation_id: str
    request_id: str
    payload: ToolApprovalRequest | dict


@dataclass
class _PendingInteraction:
    request: UserInteraction
    future: asyncio.Future
    run_owned: bool = True


class MessageRuntime(QObject):
    """Qt projection and user interaction bridge onto the shared application loop."""

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
    interactions_changed = pyqtSignal(str)              # conversation_id
    guidance_recovered = pyqtSignal(str, object)         # conversation_id, tuple[str, ...]

    _raw_stream_batch = pyqtSignal(str, str, str, str)
    _raw_step = pyqtSignal(str, str, object)
    _raw_complete = pyqtSignal(str, str, object)
    _raw_error = pyqtSignal(str, str, str)
    _raw_finished = pyqtSignal(str, str, object)
    _raw_retry = pyqtSignal(str, str, str)
    _raw_runtime_event = pyqtSignal(str, str, object)
    _raw_conversation_patch = pyqtSignal(str, str, object)
    _raw_interactions_changed = pyqtSignal(str)

    def __init__(
        self,
        run_service: RunService,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._runtime = run_service
        self._activity_service = run_service.conversations

        self._streams: dict[str, ConversationStreamState] = {}
        self._last_request_id: dict[str, str] = {}
        self._worker_controls: dict[str, _WorkerControl] = {}
        self._worker_controls_lock = threading.Lock()
        self._pending_interactions: dict[str, _PendingInteraction] = {}
        self._interactions_lock = threading.Lock()

        self._raw_stream_batch.connect(self._on_raw_stream_batch)
        self._raw_step.connect(self._on_raw_step)
        self._raw_complete.connect(self._on_raw_complete)
        self._raw_error.connect(self._on_raw_error)
        self._raw_finished.connect(self._on_raw_finished)
        self._raw_retry.connect(self._on_raw_retry)
        self._raw_runtime_event.connect(self._on_raw_runtime_event)
        self._raw_conversation_patch.connect(self._on_raw_conversation_patch)
        self._raw_interactions_changed.connect(self._on_raw_interactions_changed)

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

    def update_access(
        self,
        conversation_id: str,
        tool_permissions: ToolPermissionConfig,
        filesystem_scope: FilesystemScope,
    ) -> int | None:
        key = str(conversation_id or "").strip()
        with self._worker_controls_lock:
            worker = self._worker_controls.get(key)
        if worker is None:
            return None
        return worker.run_control.update_access(tool_permissions, filesystem_scope)

    def start(
        self,
        provider: Provider,
        conversation: Conversation,
        *,
        policy: RunPolicy,
        debug_log_path: Optional[str] = None,
        initial_runtime_messages: list[Message] | None = None,
        activity_token: str | None = None,
        delegate_profile: str = '',
    ) -> Optional[ConversationStreamState]:
        conversation_id = getattr(conversation, "id", "") or ""
        if not conversation_id:
            return None

        begin_turn = getattr(self._activity_service, "begin_turn", None)
        claim_token = begin_turn(conversation_id, activity_token=activity_token) if callable(begin_turn) else None
        if callable(begin_turn) and not claim_token:
            logger.debug("Conversation %s is already active; rejecting Desktop turn", conversation_id)
            return None
        activity_claimed = callable(begin_turn)

        request_id = str(uuid.uuid4())
        model_name = build_model_ref(provider.name, self._resolve_state_model(provider, conversation, policy))
        run_control = RunControl(
            policy.tool_permissions,
            filesystem_scope=policy.filesystem_scope,
        )
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
            conversation=conversation,
        )
        self._streams[conversation_id] = state
        self._last_request_id[conversation_id] = request_id

        self.stream_started.emit(conversation_id, request_id, model_name)

        try:
            conversation_snapshot = conversation.clone()
        except Exception:
            conversation_snapshot = conversation

        async def run_worker() -> None:
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
                self._raw_error.emit(conversation_id, request_id, "已取消生成")
                self._raw_finished.emit(conversation_id, request_id, RunStatus.CANCELLED)

            def emit_error(error: str) -> None:
                nonlocal terminal_emitted
                if terminal_emitted:
                    return
                terminal_emitted = True
                detail = str(error or "生成失败")
                flush_stream()
                self._raw_error.emit(conversation_id, request_id, detail)
                self._raw_finished.emit(conversation_id, request_id, RunStatus.FAILED)

            try:
                loop = asyncio.get_running_loop()

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

                async def approval_callback(request: ToolApprovalRequest) -> ApprovalDecision:
                    flush_stream()
                    return await self._request_interaction(state, request)

                async def questions_callback(question: dict) -> dict:
                    flush_stream()
                    return await self._request_interaction(state, dict(question or {}))

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

                    result = await self._runtime.execute(
                        claim_token=claim_token,
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
                        run_id=request_id,
                        initial_runtime_messages=list(initial_runtime_messages or []),
                        run_control=run_control,
                        delegate_profile=delegate_profile,
                    )
                    if result.conversation is not None:
                        on_patch(self._build_conversation_patch_payload(result.conversation))
                    if result.status == RunStatus.CANCELLED:
                        emit_cancelled()
                        return
                    if result.status == RunStatus.FAILED:
                        emit_error(result.error or "生成失败")
                        return
                    flush_stream()
                    terminal_emitted = True
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
                await task
            except asyncio.CancelledError:
                emit_cancelled()
            except Exception as exc:
                if isinstance(exc, PersistenceError) and exc.result is not None:
                    on_patch(self._build_conversation_patch_payload(exc.result.conversation))
                if state.cancel_event.is_set():
                    emit_cancelled()
                else:
                    emit_error(str(exc))
            finally:
                recovered = run_control.close_and_drain()
                if recovered:
                    self.guidance_recovered.emit(conversation_id, recovered)
                self.cancel_pending_interactions(request_id=request_id)
                self._unregister_worker_control(conversation_id, request_id, task)
                if activity_claimed:
                    end_turn = getattr(self._activity_service, "end_turn", None)
                    if callable(end_turn):
                        end_turn(conversation_id, claim_token)

        try:
            self._runtime.schedule(run_worker())
        except Exception:
            with self._worker_controls_lock:
                current = self._worker_controls.get(conversation_id)
                if current is worker_control:
                    self._worker_controls.pop(conversation_id, None)
            if activity_claimed:
                end_turn = getattr(self._activity_service, "end_turn", None)
                if callable(end_turn):
                    end_turn(conversation_id, claim_token)
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
        self.cancel_pending_interactions(request_id=state.request_id)
        with self._worker_controls_lock:
            control = self._worker_controls.get(conversation_id)
        if control is not None and control.request_id == state.request_id:
            cancel_loop_task(control.loop, control.task)
        return True

    async def _request_interaction(
        self, state: ConversationStreamState, payload: ToolApprovalRequest | dict,
    ) -> ApprovalDecision | dict:
        request = UserInteraction(uuid.uuid4().hex, state.conversation_id, state.request_id, payload)
        if state.cancel_event.is_set():
            raise asyncio.CancelledError
        return await self._await_interaction(request, run_owned=True)

    async def request_tool_approval(self, conversation_id: str, operation_id: str, payload: ToolApprovalRequest):
        """Standalone tool operations use the same scoped UI queue as Agent runs."""
        request = UserInteraction(uuid.uuid4().hex, conversation_id, operation_id, payload)
        return await self._await_interaction(request, run_owned=False)

    async def _await_interaction(self, request: UserInteraction, *, run_owned: bool):
        future = asyncio.get_running_loop().create_future()
        with self._interactions_lock:
            self._pending_interactions[request.id] = _PendingInteraction(request, future, run_owned)
        self._raw_interactions_changed.emit(request.conversation_id)
        try:
            return await future
        finally:
            with self._interactions_lock:
                removed = self._pending_interactions.pop(request.id, None)
            if removed is not None:
                self._raw_interactions_changed.emit(request.conversation_id)

    def pending_interactions(self, conversation_id: str | None = None) -> tuple[UserInteraction, ...]:
        """GUI-thread snapshot, in arrival order, excluding cancelled/obsolete runs."""
        with self._interactions_lock:
            pending = tuple(self._pending_interactions.values())
        return tuple(
            item.request for item in pending
            if (conversation_id is None or item.request.conversation_id == conversation_id)
            and not item.future.done()
            and (not item.run_owned or (
                (state := self._streams.get(item.request.conversation_id)) is not None
                and state.request_id == item.request.request_id
                and not state.cancel_event.is_set()))
        )

    def resolve_interaction(self, interaction_id: str, answer: ApprovalDecision | dict | None = None) -> bool:
        request = next((item for item in self.pending_interactions() if item.id == interaction_id), None)
        if request is None:
            return False
        if isinstance(request.payload, ToolApprovalRequest):
            result = answer if isinstance(answer, ApprovalDecision) else ApprovalDecision()
        else:
            result = dict(answer) if isinstance(answer, dict) else {"selected": [], "freeText": None, "skipped": True}
        return self._settle_interaction(interaction_id, result=result)

    def _settle_interaction(self, interaction_id: str, *, result=None, cancel: bool = False) -> bool:
        with self._interactions_lock:
            pending = self._pending_interactions.pop(interaction_id, None)
        if pending is None:
            return False

        def settle() -> None:
            if not pending.future.done():
                if cancel:
                    pending.future.cancel()
                else:
                    pending.future.set_result(result)

        try:
            pending.future.get_loop().call_soon_threadsafe(settle)
            return True
        except RuntimeError as exc:
            logger.debug("Failed to settle user interaction: %s", exc)
            return False
        finally:
            self._raw_interactions_changed.emit(pending.request.conversation_id)

    def cancel_pending_interactions(self, *, request_id: str = "") -> int:
        with self._interactions_lock:
            ids = [key for key, item in self._pending_interactions.items()
                   if not request_id or item.request.request_id == request_id]
        return sum(self._settle_interaction(key, cancel=True) for key in ids)

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
                phase = RunEventKind.TEXT_DELTA if visible else RunEventKind.THINKING_DELTA
                if (visible or thinking) and state.last_event_kind != phase.value:
                    # Phase changes update the existing projection, without
                    # appending every token batch to the recent-event history.
                    state.last_event_kind = phase.value
                    state.last_event_detail = ""
                    self.runtime_event.emit(conversation_id, request_id, RunEvent(kind=phase))
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
        state = self._streams.get(conversation_id)
        if state:
            state.visible_text = ""
            state.thinking_text = ""
        self.retry_attempt.emit(conversation_id, request_id, detail)

    def _on_raw_runtime_event(self, conversation_id: str, request_id: str, event: RunEvent) -> None:
        if not self._accept_event(conversation_id, request_id):
            return
        state = self._streams.get(conversation_id)
        if state:
            try:
                raw_data = getattr(event, "data", None)
                trace = raw_data.get('subtask') if isinstance(raw_data, dict) else None
                if isinstance(trace, dict) and state.conversation is not None:
                    # Keep live traces on the existing GUI conversation, including
                    # when another conversation is selected. Nested children remain
                    # inside their root trace, not separate navigation entities.
                    parent_id, call_id = event.parent_message_id, event.parent_tool_call_id
                    parent = next((message for message in reversed(state.conversation.messages)
                                   if message.id == parent_id), None)
                    if parent is not None and any(call.get('id') == call_id for call in parent.tool_calls or ()):
                        state.conversation.attach_tool_result(call_id, {'type': 'subtask_run', 'run': trace})
                request_usage = raw_data.get("request_usage") if isinstance(raw_data, dict) else None
                if isinstance(request_usage, dict):
                    state.request_usage = dict(request_usage)
                kind_value = getattr(getattr(event, "kind", ""), "value", str(getattr(event, "kind", "")))
                event_payload = self._build_runtime_event_payload(event)
                if not (isinstance(raw_data, dict) and set(raw_data) == {"request_usage"}):
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

    def _on_raw_interactions_changed(self, conversation_id: str) -> None:
        self.interactions_changed.emit(conversation_id)

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
