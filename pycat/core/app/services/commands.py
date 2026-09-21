"""Shared user input actions. Clients own panels; services own mutations and runs."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace

from pycat.core.commands import CommandAction, CommandResult, PromptInvocation, ShellInvocation
from pycat.core.commands.parser import parse_bang_command_text
from pycat.core.llm.model_selection import provider_has_model, resolve_provider_model_ref, select_default_provider_model
from pycat.models.contracts.agent import ConversationBusyError, InvalidRequestError, PersistenceError, RunRequest, RunResult, RunStatus, RunStopReason
from pycat.models.contracts.config import AppConfig
from pycat.models.conversation import Conversation, Message
from pycat.models.model_ref import build_model_ref, split_model_ref, provider_matches_name


@dataclass
class CommandExecution:
    kind: str
    message: str = ""
    conversation: Conversation | None = None
    handle: object = None
    panel: str = ""
    data: object = None


class CommandService:
    def __init__(self, *, registry, runs, tools, modes):
        self.registry, self.runs, self.tools, self.modes = registry, runs, tools, modes
        self.conversations = runs.conversations

    def create(self, *, work_dir: str = "", title: str = "", model: str | None = None,
               mode: str = "chat") -> Conversation:
        conversation = self.conversations.create(title or None)
        conversation.work_dir = self.runs.content.workspace_service.validate(work_dir)
        self._apply_selection(conversation, model=model, mode=mode)
        if not self.conversations.save(conversation):
            raise PersistenceError("Could not save the new session.")
        return conversation

    def sessions(self, *, work_dir: str | None = None, archived: bool = False) -> list[dict]:
        rows = [row for row in self.conversations.list_all() if archived or not row.get("archived")]
        if work_dir is not None:
            wanted = os.path.normcase(os.path.normpath(work_dir)) if work_dir else ""
            rows = [row for row in rows if (os.path.normcase(os.path.normpath(row.get("work_dir", "")))
                    if row.get("work_dir") else "") == wanted]
        return sorted(rows, key=lambda row: (str(row.get("updated_at", "")), str(row.get("id", ""))), reverse=True)

    def resume(self, target: str = "", *, last: bool = False, work_dir: str | None = None) -> Conversation:
        if target:
            exact = self.conversations.load(target)
            if exact is not None:
                return exact
        rows = self.sessions(work_dir=work_dir)
        matches = rows[:1] if last else [row for row in rows if row.get("title") == target]
        if len(matches) > 1:
            raise InvalidRequestError("Session name is ambiguous; choose its ID.")
        if not matches:
            raise InvalidRequestError(f"Session not found: {target or 'most recent'}")
        return self.require_session(matches[0]["id"])

    def require_session(self, conversation_id: str | None) -> Conversation:
        conversation = self.conversations.load(conversation_id) if conversation_id else None
        if conversation is None:
            raise InvalidRequestError(f"Conversation not found: {conversation_id or '(none)'}")
        return conversation

    def configure(self, conversation_id: str, *, model: str | None = None, mode: str | None = None,
                  expected_revision: str | None = None, settings: dict | None = None, llm: dict | None = None) -> Conversation:
        token = self.conversations.begin_lifecycle(conversation_id, "configure")
        if not token:
            raise ConversationBusyError("The session is running; retry the selection when it is idle.")
        try:
            conversation = self.require_session(conversation_id)
            if expected_revision and self.conversations.view_revision(conversation) != expected_revision:
                raise InvalidRequestError("Session changed; reload before applying settings.")
            self._apply_selection(conversation, model=model, mode=mode)
            if settings is not None:
                allowed = {'session_instructions': str, 'pycat_assistant_enabled': bool, 'max_context_messages': int,
                    'show_thinking': bool, 'memory_enabled': bool, 'tool_selection': dict,
                    'allowed_channel_sources': list, 'trusted_channel_sources': list, 'channel_notice_policy': str}
                for key, value in settings.items():
                    if key not in allowed or value is not None and type(value) is not allowed[key]:
                        raise InvalidRequestError(f'Invalid session setting: {key}')
                self.conversations.set_settings(conversation, settings)
            if llm is not None:
                if set(llm) - {'temperature', 'top_p', 'max_tokens', 'stream', 'reasoning_mode', 'reasoning_enabled', 'reasoning_effort'}:
                    raise InvalidRequestError('Unknown model generation setting.')
                conversation.set_llm_config(conversation.get_llm_config().with_updates(**llm))
            if not self.conversations.save(conversation, activity_token=token):
                raise PersistenceError("Could not save session settings.")
            return conversation
        finally:
            self.conversations.end_lifecycle(conversation_id, token)

    def _apply_selection(self, conversation, *, model=None, mode=None):
        if mode is not None:
            selected_mode = next((item for item in self.modes.list(conversation.work_dir)
                                  if item.slug == mode and item.is_primary_mode() and item.slug != "channel"), None)
            if selected_mode is None:
                raise InvalidRequestError(f"Unknown primary mode: {mode}")
            self.conversations.set_mode(conversation, mode)
        providers = [item for item in self.runs.models.current() if item.enabled]
        if model is not None:
            provider_name, model_name = split_model_ref(model)
            matches = [item for item in providers if provider_has_model(item, model_name)]
            if not provider_name and len(matches) > 1:
                raise InvalidRequestError("Model name is ambiguous; use provider|model.")
            selected = resolve_provider_model_ref(providers, model)
            if (selected.provider is None or not provider_has_model(selected.provider, selected.model)
                    or provider_name and not provider_matches_name(selected.provider, provider_name)):
                raise InvalidRequestError(f"Configured model not found: {model}")
        elif not conversation.model:
            selected = select_default_provider_model(providers,
                default_model_ref=self.runs.settings.load().get("default_chat_model", ""))
        else:
            return
        if selected.provider:
            provider = selected.provider
            self.conversations.configure_llm(conversation, provider_id=provider.id, provider_name=provider.name,
                api_type=provider.api_type, model=selected.model)

    @staticmethod
    def invocation_metadata(invocation: PromptInvocation) -> dict:
        if not isinstance(invocation, PromptInvocation):
            raise InvalidRequestError("Invalid prompt invocation.")
        return invocation.message_metadata()

    async def dispatch(self, request: RunRequest, *, source="cli", approval_callback=None,
                       questions_callback=None) -> CommandExecution:
        conversation = self.require_session(request.conversation_id) if request.conversation_id else None
        if request.revision:
            return CommandExecution('run', conversation=conversation, handle=self.runs.start(request, source=source,
                approval_callback=approval_callback, questions_callback=questions_callback))
        work_dir = conversation.work_dir if conversation is not None and request.work_dir is None else request.work_dir or ""
        context = {"conversation": conversation, "work_dir": work_dir,
            "current_mode": conversation.mode if conversation else request.mode or "chat"}
        text = request.text.strip()
        if text == '/tools':
            context['available_tools'] = await self.tools.list(conversation_id=request.conversation_id, work_dir=work_dir)
        shell = parse_bang_command_text(text)
        settings = self.runs.settings.load()
        if shell and AppConfig.from_dict(settings).shell.bang_command_behavior == "shell":
            result = CommandResult(CommandAction.SHELL_RUN, ShellInvocation(command=shell, original_text=text))
        else:
            result = self.registry.execute(text, context)
        if result is None:
            if text.startswith("/"):
                raise InvalidRequestError(f"Unknown command: {text.split()[0]}")
            handle = self.runs.start(request, source=source, approval_callback=approval_callback,
                questions_callback=questions_callback)
            return CommandExecution("run", conversation=conversation, handle=handle)
        action, value = result.action, result.data
        if action == CommandAction.DISPLAY:
            return CommandExecution("display", result.display_text, conversation)
        if action == CommandAction.EXIT:
            return CommandExecution("exit", conversation=conversation)
        if action == CommandAction.OPEN_PANEL:
            return CommandExecution("panel", conversation=conversation, panel=value["name"], data=value["args"])
        if action == CommandAction.RESUME:
            if not value:
                return CommandExecution("panel", panel="resume", data=self.sessions(work_dir=work_dir))
            return CommandExecution("session", conversation=await asyncio.to_thread(self.resume, str(value)))
        if action == CommandAction.CLEAR:
            created = await asyncio.to_thread(self.create, work_dir=work_dir,
                model=(build_model_ref(conversation.provider_name, conversation.model) if conversation and conversation.model else request.model),
                mode=conversation.mode if conversation else request.mode or "chat")
            return CommandExecution("session", "New session", created)
        if action in {CommandAction.MODE_SWITCH, CommandAction.MODEL_SWITCH}:
            if not value or value == "list":
                return CommandExecution("panel", conversation=conversation,
                    panel="mode" if action == CommandAction.MODE_SWITCH else "model")
            if conversation is None:
                conversation = await asyncio.to_thread(self.create, work_dir=work_dir)
            selected = await asyncio.to_thread(self.configure, conversation.id,
                **{"mode" if action == CommandAction.MODE_SWITCH else "model": str(value)})
            return CommandExecution("session", "Selection saved", selected)
        if action == CommandAction.PROMPT_RUN:
            self.invocation_metadata(value)
            request = replace(request, mode=value.mode_slug or request.mode)
            handle = self.runs.start(request, source=source, invocation=value,
                approval_callback=approval_callback, questions_callback=questions_callback)
            return CommandExecution("run", conversation=conversation, handle=handle)
        if action == CommandAction.EXPORT:
            return CommandExecution("panel", conversation=conversation, panel="export", data=value or "markdown")
        if action == CommandAction.SHELL_RUN and conversation is None:
            conversation = await asyncio.to_thread(self.create, work_dir=work_dir, mode='agent')
        if conversation is None:
            raise InvalidRequestError("This command requires an existing session.")
        if action == CommandAction.COMPACT:
            async def compact(handle):
                await self.runs.compact(conversation.id, expected_revision=request.expected_revision)
                return RunResult(conversation=self.require_session(conversation.id), final_message=Message(role='assistant', content='Context compacted'))
            return CommandExecution('run', conversation=conversation,
                handle=self.runs.start_action(compact, conversation_id=conversation.id, source=source))
        if action == CommandAction.RENAME:
            if not str(value or "").strip():
                return CommandExecution("panel", conversation=conversation, panel="rename")
            updated = await asyncio.to_thread(self.conversations.update_navigation, conversation.id, title=value)
            return CommandExecution("session", "Session renamed", updated)
        if action == CommandAction.SHELL_RUN:
            async def shell(handle):
                receipt = await self.tools.run_shell(value.command, conversation_id=conversation.id,
                    approval_callback=approval_callback)
                return RunResult(conversation=receipt.conversation, final_message=Message(role='assistant', content=receipt.content),
                    status=RunStatus.FAILED if receipt.is_error else RunStatus.COMPLETED,
                    stop_reason=RunStopReason.ERROR if receipt.is_error else RunStopReason.COMPLETED)
            return CommandExecution('run', conversation=conversation,
                handle=self.runs.start_action(shell, conversation_id=conversation.id, source=source))
        raise InvalidRequestError(f"Unsupported command action: {action}")
