"""Application submission, durable completion and bounded run observation."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections import deque
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from pycat.core.agent.policy import RunPolicyBuilder
from pycat.core.agent.run.control import RunControl
from pycat.core.app.runtime_paths import get_debug_log_path
from pycat.core.commands.types import PromptInvocation
from pycat.core.content.resolver import SessionContentResolver
from pycat.core.llm.model_selection import provider_has_model, resolve_provider_model_ref, select_default_provider_model
from pycat.core.modes.manager import ModeManager
from pycat.core.observability.debug_trace import create_run_debug_trace, finish_run_debug_trace
from pycat.models.contracts.agent import (
    ApplicationError,
    ConversationBusyError,
    InvalidRequestError,
    MentionRef,
    PersistenceError,
    RunEvent,
    RunEventKind,
    RunRequest,
    RunResult,
    RunStatus,
    RunStopReason,
    SlowConsumerError,
)
from pycat.models.contracts.config import AppConfig
from pycat.models.contracts.tooling import (
    ToolPermissionConfig,
    filesystem_scope_for_mode,
    permission_config_for_approval,
)
from pycat.models.conversation import Message
from pycat.models.model_ref import build_model_ref, provider_matches_name, split_model_ref


class RunHandle:
    """One task and one optional bounded event consumer, owned by RunService."""

    def __init__(self, service, request, *, observe=True, source="sdk", action=None, **callbacks):
        self.id = uuid.uuid4().hex
        self.source = source
        self.output = None
        self.control = RunControl()
        self._cancel_event = threading.Event()
        self._events = deque()
        self._ready = asyncio.Event()
        self._observe = observe
        self._consumer = False
        self._sequence = 0
        self._failure = None
        self._conversation_id = request.conversation_id or ""
        self._delta_kind = None
        self._delta_parts = []
        self._delta_size = 0
        self._delta_handle = None
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(
            action(self) if action else service.submit(
                request,
                source=source,
                run_id=self.id,
                run_control=self.control,
                cancel_event=self._cancel_event,
                on_event=self._event,
                on_token=(lambda text: self._delta(RunEventKind.TEXT_DELTA, text)) if observe else None,
                on_thinking=(lambda text: self._delta(RunEventKind.THINKING_DELTA, text)) if observe else None,
                **callbacks,
            ),
            name=f"pycat-run-{self.id}",
        )
        self._task.add_done_callback(self._finished)

    def _finished(self, _):
        self._flush_delta()
        self._ready.set()

    def _delta(self, kind, text):
        if not self._observe or self._failure or not text:
            return
        if self._delta_kind != kind or self._delta_size + len(text) > 65536:
            self._flush_delta()
        if len(text) > 65536:
            for offset in range(0, len(text), 65536):
                self._delta(kind, text[offset : offset + 65536])
            return
        self._delta_kind = kind
        self._delta_parts.append(text)
        self._delta_size += len(text)
        if self._delta_handle is None:
            self._delta_handle = self._loop.call_soon(self._flush_delta)

    def _flush_delta(self):
        if self._delta_handle is not None:
            self._delta_handle.cancel()
            self._delta_handle = None
        if self._delta_parts:
            event = RunEvent(self._delta_kind, "".join(self._delta_parts), source=self.source)
            self._delta_parts.clear()
            self._delta_size = 0
            self._enqueue(event)

    def _event(self, event):
        self._flush_delta()
        self._enqueue(event)

    def _enqueue(self, event):
        if not self._observe or self._failure:
            return
        if event.conversation_id:
            self._conversation_id = event.conversation_id
        # Adjacent deltas share a bounded chunk; state/tool events keep order.
        if (
            self._events
            and event.kind in {RunEventKind.TEXT_DELTA, RunEventKind.THINKING_DELTA}
            and self._events[-1].kind == event.kind
            and len(str(self._events[-1].data)) + len(str(event.data)) <= 65536
        ):
            self._events[-1].data += event.data
        elif len(self._events) >= 256:
            self._failure = SlowConsumerError("Event buffer full; consume events concurrently or use run().")
            self.cancel()
        else:
            self._sequence += 1
            self._events.append(
                replace(event, run_id=self.id, sequence=self._sequence, conversation_id=self._conversation_id)
            )
        self._ready.set()

    def cancel(self):
        if self._cancel_event.is_set():
            return
        self._cancel_event.set()
        if not self._task.done():
            self._task.cancel()

    def submit_guidance(self, text: str) -> bool:
        return self.control.submit(text)

    async def result(self) -> RunResult:
        if not self._consumer:
            self._observe = False
            self._events.clear()
        try:
            result = await asyncio.shield(self._task)
        except asyncio.CancelledError:
            if not self._task.cancelled():
                raise
            result = RunResult(status=RunStatus.CANCELLED, stop_reason=RunStopReason.CANCELLED)
        if self._failure:
            raise self._failure
        return result

    async def events(self) -> AsyncIterator[RunEvent]:
        if self._consumer:
            raise ApplicationError("A run has only one event consumer.")
        self._consumer = True
        while True:
            while self._events:
                yield self._events.popleft()
            if self._task.done():
                await self.result()
                return
            self._ready.clear()
            await self._ready.wait()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        if not self._task.done():
            self.cancel()
        # Exceptions already raised by events/result are not raised twice.
        await asyncio.gather(self._task, return_exceptions=True)


class RunService:
    """The application owns claims and saves; the Agent owns model/tool work."""

    def __init__(self, *, conversations, models, settings, content, runtime, data_dir, context=None):
        self.conversations = conversations
        self.models = models
        self.settings = settings
        self.content = content
        self.runtime = runtime
        self.data_dir = data_dir
        self.context = context
        self._handles = set()
        self._tasks = set()
        self._closed = False
        self._loop = None
        self._controls = {}
        self._controls_lock = threading.RLock()
        self.delegation = None

    def bind_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise ApplicationError("Use the application's owning event loop.")
        self._loop = loop
        if self._closed:
            raise ApplicationError("Application is closed.")
        if self.delegation is not None:
            self.delegation.start_recovery()

    def schedule(self, coroutine):
        if self._closed or self._loop is None or not self._loop.is_running():
            coroutine.close()
            raise ApplicationError("Application event loop is not running.")
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def start(self, request: RunRequest, *, observe=True, source="sdk", invocation=None,
              approval_callback=None, questions_callback=None, action=None):
        self.bind_loop()
        handle = RunHandle(
            self, request, observe=observe, source=source, invocation=invocation, action=action,
            approval_callback=approval_callback, questions_callback=questions_callback
        )
        self._handles.add(handle)

        def finished(task):
            self._handles.discard(handle)
            if not task.cancelled():
                task.exception()

        handle._task.add_done_callback(finished)
        return handle

    def start_action(self, action, *, conversation_id=None, source='sdk'):
        """Observe an existing application use case without another agent loop."""
        async def execute(handle):
            try:
                result = await action(handle)
                if handle._cancel_event.is_set():
                    result.status, result.stop_reason = RunStatus.CANCELLED, RunStopReason.CANCELLED
            except asyncio.CancelledError:
                result = RunResult(status=RunStatus.CANCELLED, stop_reason=RunStopReason.CANCELLED)
            except Exception as exc:
                result = RunResult(status=RunStatus.FAILED, stop_reason=RunStopReason.ERROR, error=str(exc))
            if result.conversation is None and conversation_id:
                result.conversation = await asyncio.to_thread(self.conversations.load, conversation_id)
            handle._event(RunEvent(RunEventKind.COMPLETE,
                {'status': result.status.value, 'stop_reason': result.stop_reason.value}, source=source,
                conversation_id=result.conversation.id if result.conversation else conversation_id or ''))
            return result
        return self.start(RunRequest(text='', conversation_id=conversation_id), source=source, action=execute)

    def active_runs(self):
        return [{'id': handle.id, 'conversation_id': handle._conversation_id, 'source': handle.source}
                for handle in tuple(self._handles) if not handle._task.done()]

    def update_access(self, conversation_id, *, tool_approval=None, filesystem_mode=None, expected_revision=None):
        if tool_approval not in {None, 'default', 'ask', 'allow', 'deny', 'custom'} or filesystem_mode not in {None, 'confined', 'full_access'}:
            raise InvalidRequestError('Invalid permission selection.')
        with self._controls_lock:
            control = self._controls.get(conversation_id)
            if control is None and self.conversations.is_active(conversation_id):
                raise ConversationBusyError('The session is preparing or being maintained; retry shortly.')
            conversation = self.conversations.load(conversation_id)
            if conversation is None:
                raise InvalidRequestError('Session not found.')
            if expected_revision and expected_revision != self.conversations.view_revision(conversation):
                raise InvalidRequestError('Session changed; reload before changing access.')
            self.conversations.set_settings(conversation, {key: value for key, value in
                {'tool_approval': tool_approval, 'filesystem_mode': filesystem_mode}.items() if value is not None})
            if not self.conversations.save(conversation):
                raise PersistenceError('Could not save access settings.')
            revision = None
            if control:
                custom = ToolPermissionConfig.from_settings_dict(self.settings.load())
                revision = control.update_access(permission_config_for_approval(conversation.settings.get('tool_approval', 'default'), custom=custom),
                    filesystem_scope_for_mode(conversation.settings.get('filesystem_mode', 'confined'), allow_home_read=True))
            return {'settings': conversation.settings, 'access_revision': revision,
                    'revision': self.conversations.view_revision(conversation)}

    def mention_catalog(self, work_dir):
        modes = ModeManager(work_dir, data_dir=str(self.data_dir)).list_subagent_profiles()
        channels = AppConfig.from_dict(self.settings.load()).channels
        return ([{'kind': 'agent', 'id': item.slug, 'label': item.name, 'description': item.purpose} for item in modes]
                + [{'kind': 'channel', 'id': item.id, 'label': item.name, 'scope': 'connection',
                    'description': 'Channel connection reference; select a concrete recipient before sending.'} for item in channels]
                + [{'kind': 'run', 'id': item['id'], 'label': item['id'][:8],
                    'conversation_id': item['conversation_id']} for item in self.active_runs()])

    def resolve_mentions(self, mentions, *, work_dir):
        if len(mentions) > 32:
            raise InvalidRequestError('At most 32 references can be selected per message.')
        catalog = {(item['kind'], item['id']): item for item in self.mention_catalog(work_dir)}
        resolved = []
        for item in mentions:
            if not isinstance(item, MentionRef) or (item.kind, item.id) not in catalog:
                raise InvalidRequestError('Selected reference no longer exists; select it again.')
            resolved.append(catalog[item.kind, item.id])
        return resolved

    async def run(self, request: RunRequest, **callbacks):
        async with self.start(request, observe=False, **callbacks) as handle:
            return await handle.result()

    def prepare(self, request: RunRequest, *, source="sdk", invocation: PromptInvocation | None = None, delegation_run_id=''):
        if invocation is not None and not isinstance(invocation, PromptInvocation):
            raise InvalidRequestError("Invalid prompt invocation.")
        if not isinstance(request, RunRequest) or (not request.text.strip() and not request.attachments and not request.references and not request.revision):
            raise InvalidRequestError("A message or attachment is required.")
        if request.revision and (request.revision.action not in {'retry', 'edit'} or not request.conversation_id or not request.expected_revision):
            raise InvalidRequestError('A revision requires a session, message and current revision.')
        if request.revision and request.revision.action == 'retry' and (request.attachments or request.references or request.mentions):
            raise InvalidRequestError('Retry reuses the original input; use edit to change its attachments.')
        if request.tool_approval not in {None, "default", "ask", "allow", "deny", "custom"}:
            raise InvalidRequestError("Invalid tool approval mode.")
        if request.filesystem_mode not in {None, "confined", "full_access"}:
            raise InvalidRequestError("Invalid filesystem mode.")
        convs = self.conversations
        conversation = convs.create() if not request.conversation_id else convs.load(request.conversation_id)
        if conversation is None:
            raise InvalidRequestError(f"Conversation not found: {request.conversation_id}")
        token = convs.begin_lifecycle(conversation.id, 'revision') if request.revision else convs.begin_turn(conversation.id)
        if not token:
            raise ConversationBusyError(f"Conversation is busy: {conversation.id}")
        try:
            if request.conversation_id:
                conversation = convs.load(request.conversation_id)
                if conversation is None:
                    raise InvalidRequestError("Conversation no longer exists.")
            if request.expected_revision and request.expected_revision != convs.view_revision(conversation):
                raise InvalidRequestError("Conversation changed; reload before submitting.")
            d = conversation.delegation
            if d:
                if delegation_run_id and (d.submission != 'queued' or d.run_id != delegation_run_id or d.cancel_requested):
                    raise ConversationBusyError('独立任务已启动或已停止，不能重复执行。')
                if not delegation_run_id and d.submission != 'settled':
                    raise ConversationBusyError('独立任务尚未交付；请先停止或从任务卡片继续。')
            settings = self.settings.load()
            providers = [p for p in self.models.current() if p.enabled]
            if request.model is not None:
                provider_name, model_name = split_model_ref(request.model)
                if not provider_name and sum(provider_has_model(item, model_name) for item in providers) > 1:
                    raise InvalidRequestError('Model name is ambiguous; use provider|model.')
                selected = resolve_provider_model_ref(providers, request.model)
                provider_name, _ = split_model_ref(request.model)
                if provider_name and (
                    selected.provider is None or not provider_matches_name(selected.provider, provider_name)
                ):
                    raise InvalidRequestError(f"Configured provider not found: {provider_name}")
            elif request.conversation_id:
                provider = next(
                    (
                        p
                        for p in providers
                        if p.id == conversation.provider_id or provider_matches_name(p, conversation.provider_name)
                    ),
                    None,
                )
                if provider is None:
                    raise InvalidRequestError("The conversation's provider is no longer configured; select a model.")
                selected = resolve_provider_model_ref([provider], conversation.model)
            else:
                selected = select_default_provider_model(
                    providers, default_model_ref=settings.get("default_chat_model", "")
                )
            provider, model = selected.provider, selected.model
            if provider is None or not provider_has_model(provider, model):
                raise InvalidRequestError(
                    f"Configured model not found: {request.model or conversation.model or 'default'}"
                )
            mode = (invocation.mode_slug if invocation else "") or request.mode or conversation.mode or "chat"
            work_dir = conversation.work_dir if request.work_dir is None else request.work_dir
            if request.conversation_id and work_dir != conversation.work_dir:
                raise InvalidRequestError("Change the conversation workspace before starting a run.")
            workspace_service = getattr(self.content, "workspace_service", None)
            if workspace_service is not None:
                try:
                    conversation.work_dir = workspace_service.validate(work_dir)
                except (OSError, ValueError) as exc:
                    raise InvalidRequestError(str(exc)) from exc
            else:
                if work_dir and not Path(work_dir).expanduser().is_dir():
                    raise InvalidRequestError(f"Workspace not found: {work_dir}")
                conversation.work_dir = str(Path(work_dir).expanduser().resolve()) if work_dir else ""
            manager = ModeManager(conversation.work_dir, data_dir=str(self.data_dir))
            if invocation and invocation.delegate_profile and invocation.delegate_profile not in {
                item.slug for item in manager.list_subagent_profiles()
            }:
                raise InvalidRequestError('Unknown delegated agent profile.')
            if manager.find(mode) is None:
                raise InvalidRequestError(f"Mode not found: {mode}")
            convs.configure_llm(
                conversation,
                provider_id=provider.id,
                provider_name=provider.name,
                api_type=provider.api_type,
                model=model,
            )
            convs.set_mode(conversation, mode)
            convs.set_settings(
                conversation,
                {
                    key: value
                    for key, value in {
                        "tool_approval": request.tool_approval,
                        "filesystem_mode": request.filesystem_mode,
                    }.items()
                    if value is not None
                },
            )
            message = Message(role="user", content=invocation.content if invocation else request.text,
                metadata=invocation.message_metadata() if invocation else {})
            if request.mentions:
                message.metadata['mentions'] = self.resolve_mentions(request.mentions, work_dir=conversation.work_dir)
            prepared = self.content.prepare_inputs(conversation, request.attachments, message_id=message.id)
            if prepared.failures:
                self.content.cleanup_unreferenced(conversation, prepared.created_refs)
                raise InvalidRequestError("; ".join(f.error for f in prepared.failures))
            message.content_refs = list(prepared.refs)
            for reference in request.references:
                try:
                    resolved = SessionContentResolver(self.content).resolve_content(conversation, reference)
                    message.content_refs.append(resolved.ref)
                except (OSError, ValueError):
                    self.content.cleanup_unreferenced(conversation, prepared.created_refs)
                    raise
            policy = RunPolicyBuilder.build(
                conversation=conversation, app_settings=settings, mode_manager=manager, source=source
            )
            try:
                if request.revision:
                    if request.revision.action == 'retry':
                        revised = convs.regenerate_user_turn(conversation.id, request.revision.message_id,
                            expected_fingerprint=convs.revision_fingerprint(conversation), activity_token=token)
                    else:
                        revised = convs.replace_user_turn(conversation.id, request.revision.message_id,
                            content=message.content, content_refs=message.content_refs if request.attachments or request.references else None, metadata=message.metadata,
                            expected_fingerprint=convs.revision_fingerprint(conversation), activity_token=token)
                    if not revised.ok:
                        raise InvalidRequestError(revised.error)
                    conversation = revised.conversation
                    convs.configure_llm(conversation, provider_id=provider.id, provider_name=provider.name,
                        api_type=provider.api_type, model=model)
                    convs.set_mode(conversation, mode)
                    convs.set_settings(conversation, {key: value for key, value in {
                        'tool_approval': request.tool_approval, 'filesystem_mode': request.filesystem_mode}.items() if value is not None})
                    if not convs.save(conversation, activity_token=token):
                        raise PersistenceError('Could not save the revised session.')
                    token = convs.begin_turn(conversation.id, activity_token=token)
                    if not token:
                        raise ConversationBusyError('Could not start the revised turn.')
                    policy = RunPolicyBuilder.build(conversation=conversation, app_settings=settings, mode_manager=manager, source=source)
                else:
                    convs.commit_message(conversation, message, activity_token=token)
            except BaseException:
                self.content.cleanup_unreferenced(conversation, prepared.created_refs)
                raise
            return provider, conversation, policy, token
        except BaseException:
            convs.end_turn(conversation.id, token)
            convs.end_lifecycle(conversation.id, token)
            raise

    async def submit(self, request: RunRequest, *, source="sdk", invocation=None, delegation_run_id='', **callbacks):
        self.bind_loop()
        preparation = asyncio.create_task(asyncio.to_thread(self.prepare, request, source=source, invocation=invocation,
                                                         delegation_run_id=delegation_run_id))
        try:
            provider, conversation, policy, token = await asyncio.shield(preparation)
        except asyncio.CancelledError:
            try:
                _provider, conversation, _policy, token = await asyncio.shield(preparation)
            except Exception:
                pass
            else:
                self.conversations.end_turn(conversation.id, token)
            raise
        return await self.execute(
            provider=provider,
            conversation=conversation,
            policy=policy,
            claim_token=token,
            input_saved=True,
            delegate_profile=invocation.delegate_profile if invocation else '',
            delegation_run_id=delegation_run_id,
            **callbacks,
        )

    async def execute(
        self,
        *,
        provider,
        conversation,
        policy,
        claim_token=None,
        on_event=None,
        on_token=None,
        on_thinking=None,
        approval_callback=None,
        questions_callback=None,
        cancel_event=None,
        run_control=None,
        debug_log_path=None,
        run_id=None,
        initial_runtime_messages=None,
        input_saved=False,
        delegate_profile='',
        delegation_run_id='',
    ):
        self.bind_loop()
        token = claim_token or self.conversations.begin_turn(conversation.id)
        if not token:
            raise ConversationBusyError(f"Conversation is busy: {conversation.id}")
        task = asyncio.current_task()
        self._tasks.add(task)
        original_message_ids = {message.id for message in conversation.messages}
        request_id = str(run_id or uuid.uuid4().hex)
        debug_trace = None

        def emit(event):
            if event.kind == RunEventKind.STEP and isinstance(event.data, Message) and request_id:
                event.data.metadata["request_id"] = request_id
            if on_event and event.kind not in {RunEventKind.COMPLETE, RunEventKind.ERROR}:
                on_event(replace(event, conversation_id=conversation.id,
                    source=policy.source if event.source == "parent" else event.source))

        result = None
        saved = False
        failure = ""
        try:
            # The conversation claim owns this turn. Available tools do not
            # reserve a workspace (or all workspaces) for the whole model run.
            latest = self.conversations.load(conversation.id) if conversation.delegation else None
            d = latest.delegation if latest else conversation.delegation
            if delegation_run_id:
                latest = self.delegation.revalidate(conversation.id, activity_token=token)
                d = latest.delegation
                if d.run_id != delegation_run_id:
                    raise ConversationBusyError('独立任务已取消或正在处理。')
                conversation.delegation = deepcopy(d)
                policy = RunPolicyBuilder.build(conversation=conversation, app_settings=self.settings.load(), source=policy.source)
                conversation.delegation.submission = 'started'
                input_saved = False
            elif d and d.submission != 'settled':
                raise ConversationBusyError('独立任务尚未交付。')
            settings = self.settings.load()
            capture = bool(debug_log_path or settings.get("log_stream", False))
            debug_trace = create_run_debug_trace(
                conversation=conversation, request_id=request_id,
                model_name=build_model_ref(provider.name, policy.model or conversation.model),
                mode=policy.mode, source=policy.source, capture_payloads=capture, capture_stream=capture,
            )
            if not input_saved and not self.conversations.save(conversation, activity_token=token):
                raise PersistenceError("Could not save input; execution was not started.")
            if delegation_run_id and self.delegation is not None:
                self.delegation._changed(conversation)
            if run_control is not None and run_control.access_snapshot()[:2] != (
                policy.tool_permissions,
                policy.filesystem_scope,
            ):
                run_control.update_access(policy.tool_permissions, filesystem_scope=policy.filesystem_scope)
            if run_control is not None:
                with self._controls_lock:
                    self._controls[conversation.id] = run_control
            try:
                result = await self.runtime.run(
                    provider=provider,
                    conversation=conversation,
                    policy=policy,
                    on_event=emit,
                    on_token=on_token,
                    on_thinking=on_thinking,
                    approval_callback=approval_callback,
                    questions_callback=questions_callback,
                    cancel_event=cancel_event,
                    run_control=run_control,
                    debug_log_path=debug_log_path or get_debug_log_path(settings, self.data_dir),
                    debug_trace=debug_trace,
                    initial_runtime_messages=initial_runtime_messages,
                    delegate_profile=delegate_profile,
                )
            except asyncio.CancelledError:
                result = RunResult(
                    status=RunStatus.CANCELLED, stop_reason=RunStopReason.CANCELLED, conversation=conversation
                )
            except Exception as exc:
                result = RunResult(
                    status=RunStatus.FAILED, error=str(exc), stop_reason=RunStopReason.ERROR, conversation=conversation
                )
            result.conversation = result.conversation or conversation
            if result.status in {RunStatus.CANCELLED, RunStatus.FAILED} and result.final_message is None:
                message = Message(
                    role="assistant",
                    content=result.error or "已取消生成",
                    metadata={"runtime_error": True, "run_status": result.status.value},
                )
                result.conversation.add_message(message)
                result.final_message = message
            for message in result.conversation.messages:
                if message.role == "assistant" and message.id not in original_message_ids:
                    message.metadata["run_status"] = result.status.value
                    if request_id:
                        message.metadata["request_id"] = request_id
            with self._controls_lock:
                latest = self.conversations.load(conversation.id)
                if latest is not None:
                    for key in ('tool_approval', 'filesystem_mode'):
                        if key in latest.settings:
                            result.conversation.settings[key] = latest.settings[key]
                    if latest.delegation is not None:
                        d = deepcopy(latest.delegation)
                        if delegation_run_id:
                            if d.run_id != delegation_run_id:
                                raise PersistenceError('旧执行不能覆盖新的独立任务结果。', result=result)
                            if d.cancel_requested:
                                result.status, result.stop_reason = RunStatus.CANCELLED, RunStopReason.CANCELLED
                            elif result.status == RunStatus.CANCELLED:
                                result.status = RunStatus.INTERRUPTED
                            if result.final_message is not None:
                                result.final_message.metadata['run_status'] = result.status.value
                                d.result_message_id = result.final_message.id
                            d.submission = 'settled'
                        result.conversation.delegation = d
                if not self.conversations.save(result.conversation, activity_token=token):
                    raise PersistenceError("Execution finished but the result could not be saved.", result=result)
            saved = True
            if on_event:
                kind = RunEventKind.ERROR if result.status == RunStatus.FAILED else RunEventKind.COMPLETE
                on_event(
                    RunEvent(
                        kind,
                        {"status": result.status.value, "error": getattr(result, "error", None),
                         "stop_reason": result.stop_reason.value},
                        conversation_id=conversation.id,
                        source=policy.source,
                    )
                )
            return result
        except BaseException as exc:
            failure = str(exc) or type(exc).__name__
            raise
        finally:
            finish_run_debug_trace(
                debug_trace, status=result.status.value if saved else "failed",
                summary=failure or str(getattr(result, "error", "") or getattr(getattr(result, "final_message", None), "content", "") or ""),
                result_saved=saved, execution_finished=result is not None,
            )
            self.conversations.end_turn(conversation.id, token)
            with self._controls_lock:
                self._controls.pop(conversation.id, None)
            self._tasks.discard(task)

    async def aclose(self):
        self._closed = True
        handles = tuple(self._handles)
        for handle in handles:
            handle.cancel()
        tasks = self._tasks | {h._task for h in handles}
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def background(self, coroutine):
        """Track application-owned auxiliary work on the same event loop."""
        try:
            self.bind_loop()
        except BaseException:
            coroutine.close()
            raise
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            return await coroutine
        finally:
            self._tasks.discard(task)

    async def compact(self, conversation_id: str, *, provider=None, expected_revision=None):
        """Compact and save one idle conversation before returning its projection."""
        self.bind_loop()
        token = self.conversations.begin_lifecycle(conversation_id, "compact")
        if not token:
            raise ConversationBusyError(f"Conversation is busy: {conversation_id}")
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            conversation = self.conversations.load(conversation_id)
            if conversation is None:
                raise InvalidRequestError(f"Conversation not found: {conversation_id}")
            if expected_revision and expected_revision != self.conversations.view_revision(conversation):
                raise InvalidRequestError("Conversation changed; reload before compacting.")
            if provider is None:
                provider = next(
                    (
                        p
                        for p in self.models.current()
                        if p.enabled
                        and (p.id == conversation.provider_id or provider_matches_name(p, conversation.provider_name))
                    ),
                    None,
                )
            if provider is None:
                raise InvalidRequestError("The conversation's provider is no longer configured.")
            report = await self.context.compact_async(conversation, provider)
            if not self.conversations.save(conversation, activity_token=token):
                raise PersistenceError("Could not save compacted conversation.")
            return report, conversation
        finally:
            self.conversations.end_lifecycle(conversation_id, token)
            self._tasks.discard(task)
