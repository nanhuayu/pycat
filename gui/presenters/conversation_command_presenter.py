"""Conversation command presenter.

Owns command-result handling, prompt invocations, and export flows so
conversation lifecycle concerns can stay focused.
"""
from __future__ import annotations

import json
import logging
import asyncio
import threading
from typing import TYPE_CHECKING, Any, Callable

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QFileDialog, QMessageBox

from models.contracts.tooling import normalize_risk_level, normalize_tool_category
from core.agent.policy import RunPolicyBuilder
from core.state.artifact import ArtifactService
from core.agent.tooling.executor import ToolExecutor
from core.tools.base import ToolApprovalRequest
from models.contracts.config import AppConfig
from models.conversation import Conversation, Message

if TYPE_CHECKING:
    from core.commands import PromptInvocation
    from gui.main_window import MainWindow

logger = logging.getLogger(__name__)


class _ShellCommandSignals(QObject):
    finished = pyqtSignal(object, str, object, object)


class ConversationCommandPresenter:
    """Handles command dispatch, prompt invocations, and export."""

    def __init__(
        self,
        host: MainWindow,
        *,
        create_new_conversation: Callable[[], None],
        compact_current: Callable[[], None],
    ) -> None:
        self._host = host
        self._create_new_conversation = create_new_conversation
        self._compact_current = compact_current
        self._shell_signals = _ShellCommandSignals()
        self._shell_signals.finished.connect(self._finish_shell_command)

    def export_current(self, fmt: str = "markdown") -> None:
        host = self._host
        if not host.current_conversation:
            return

        self.export_conversation(host.current_conversation, fmt)

    def export_conversation(self, conversation: Conversation, fmt: str = "markdown") -> None:
        host = self._host
        if conversation is None:
            return

        conv = conversation
        default_name = (conv.title or "conversation").replace(" ", "_")

        if fmt == "json":
            path, _ = QFileDialog.getSaveFileName(
                host,
                "Export Conversation",
                f"{default_name}.json",
                "JSON (*.json)",
            )
            if path:
                data = conv.to_dict() if hasattr(conv, "to_dict") else {"messages": [m.to_dict() for m in conv.messages]}
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                host.chat_view.show_header_notice(
                    f"已导出到 {path}",
                    tone="success",
                    timeout_ms=5000,
                    conversation_id=conv.id,
                )
            return

        path, _ = QFileDialog.getSaveFileName(
            host,
            "Export Conversation",
            f"{default_name}.md",
            "Markdown (*.md)",
        )
        if not path:
            return

        lines = [f"# {conv.title or 'Conversation'}\n"]
        for msg in conv.messages:
            if msg.role == "system":
                continue
            role = msg.role.upper()
            content = msg.content or ""
            lines.append(f"## {role}\n\n{content}\n")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        host.chat_view.show_header_notice(
            f"已导出到 {path}",
            tone="success",
            timeout_ms=5000,
            conversation_id=conv.id,
        )

    def handle_command_result(self, result) -> None:
        from core.commands import CommandAction, CommandResult, PromptInvocation, ShellInvocation

        if isinstance(result, str):
            self._append_info_message(result)
            return

        if not isinstance(result, CommandResult):
            return

        if result.action == CommandAction.CLEAR:
            self._create_new_conversation()
            return

        if result.action == CommandAction.COMPACT:
            self._compact_current()
            return

        if result.action == CommandAction.MODE_SWITCH:
            self._switch_mode(str(result.data or ""))
            return

        if result.action == CommandAction.PROMPT_RUN:
            payload = result.data if isinstance(result.data, PromptInvocation) else None
            if payload is None:
                return
            self._run_prompt_invocation(payload)
            return

        if result.action == CommandAction.SHELL_RUN:
            payload = result.data if isinstance(result.data, ShellInvocation) else None
            if payload is None:
                return
            self._run_shell_command(payload)
            return

        if result.action == CommandAction.EXPORT:
            self.export_current((result.data or "markdown").strip())
            return

        if result.action == CommandAction.DISPLAY and result.display_text:
            self._append_info_message(result.display_text)

    def _run_shell_command(self, payload) -> None:
        host = self._host
        command = str(getattr(payload, "command", "") or "").strip()
        if not command:
            self._append_info_message("Shell 命令为空。")
            return

        if not host.current_conversation:
            try:
                host.current_conversation = host.conversation_presenter.ensure_current_conversation_shell()
            except Exception as exc:
                logger.debug("Failed to create conversation for shell invocation: %s", exc)
                self._append_info_message("无法创建会话来执行 Shell 命令。")
                return

        conversation = host.current_conversation
        if conversation is None:
            return

        tool_manager = getattr(getattr(host, "services", None), "tool_manager", None)
        registry = getattr(tool_manager, "registry", None)
        tool = registry.get_tool("shell__run") if registry is not None and hasattr(registry, "get_tool") else None
        category = normalize_tool_category(str(getattr(tool, "category", "execute") or "execute"))

        policy = RunPolicyBuilder.build(
            conversation=conversation,
            app_settings=getattr(host, "app_settings", {}) or {},
            source="desktop",
        )
        effective = policy.tool_permissions.resolve("shell__run", category)
        if effective.action == "deny":
            self._append_info_message("`shell__run` 已被当前权限设置禁用，无法执行 `!` Shell 命令。")
            return

        if tool_manager is None or tool is None:
            self._append_info_message("Shell 工具当前不可用。")
            return

        try:
            execution_conversation = Conversation.from_dict(conversation.to_dict())
        except Exception:
            execution_conversation = conversation
        executor = ToolExecutor(
            tool_manager,
            shell_config=AppConfig.from_dict(getattr(host, "app_settings", {}) or {}).shell,
        )
        context = executor.build_tool_context(
            conversation=execution_conversation,
            provider=None,
            approval_callback=None,
            questions_callback=None,
            llm_client=None,
            policy=policy,
            tool_name="shell__run",
        )
        tool_args = {"command": command, "cwd": "."}
        approved = True
        if effective.action == "ask":
            risk = normalize_risk_level(tool.assess_risk(tool_args, context))
            approved = self._ask_shell_approval(
                ToolApprovalRequest(
                    tool_name="shell__run",
                    tool_call_id="",
                    arguments=tool_args,
                    category=category,
                    risk=risk,
                    message=tool.approval_message(tool_args, context),
                )
            )
        context.approval_callback = lambda _request: approved

        user_msg = Message(
            role="user",
            content=str(getattr(payload, "original_text", "") or f"!{command}"),
            metadata={
                "command_run": {
                    "source_prefix": "!",
                    "action": "shell_run",
                    "command": command,
                }
            },
        )
        conversation.add_message(user_msg)
        try:
            host.chat_view.add_message(user_msg)
        except Exception as exc:
            logger.debug("Failed to add shell command user message to chat view: %s", exc)
        try:
            host.services.conv_service.save(conversation)
        except Exception as exc:
            logger.debug("Failed to save conversation before shell invocation: %s", exc)

        async def _execute():
            result = await executor.execute_tool(
                tool_name="shell__run",
                tool_args=tool_args,
                allowed=executor.is_tool_allowed("shell__run", policy),
                policy=policy,
                context=context,
            )
            return result

        def run() -> None:
            result = None
            error = None
            try:
                result = asyncio.run(_execute())
            except Exception as exc:
                error = exc
            self._shell_signals.finished.emit(conversation, command, result, error)

        threading.Thread(target=run, name="BangShell", daemon=True).start()

    def _finish_shell_command(
        self,
        conversation: Conversation,
        command: str,
        result: object,
        error: object,
    ) -> None:
        host = self._host
        conversation_id = str(getattr(conversation, "id", "") or "")
        current = getattr(host, "current_conversation", None)
        if str(getattr(current, "id", "") or "") == conversation_id:
            target = current
            visible = True
        else:
            loader = getattr(getattr(host.services, "conv_service", None), "load", None)
            if callable(loader):
                target = loader(conversation_id)
                if target is None:
                    logger.info(
                        "Discarding Shell completion for deleted conversation %s",
                        conversation_id,
                    )
                    return
            else:
                target = conversation
            visible = False
        if error is not None:
            logger.debug("Explicit shell command failed: %s", error)
            content = f"Shell 执行失败：{error}"
            is_error = True
        else:
            content = result.to_string() if hasattr(result, "to_string") else str(result)
            is_error = bool(getattr(result, "is_error", False))

        assistant_msg = Message(
            role="assistant",
            content=content,
            metadata={
                "command_run": {
                    "source_prefix": "!",
                    "action": "shell_run",
                    "command": command,
                    "is_error": is_error,
                }
            },
        )
        target.add_message(assistant_msg)
        if visible:
            try:
                host.chat_view.add_message(assistant_msg)
            except Exception as exc:
                logger.debug("Failed to add shell command result to chat view: %s", exc)
        try:
            host.services.conv_service.save(target)
        except Exception as exc:
            logger.debug("Failed to save conversation after shell invocation: %s", exc)
        if visible:
            self._remember_current_conversation(target)
            refresh_processes = getattr(getattr(host, "conversation_presenter", None), "refresh_processes", None)
            if callable(refresh_processes):
                refresh_processes()

    def _ask_shell_approval(self, request: ToolApprovalRequest) -> bool:
        try:
            reply = QMessageBox.question(
                self._host,
                "Shell 执行确认",
                str(request.message or "确认执行 Shell 命令？"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return reply == QMessageBox.StandardButton.Yes
        except Exception as exc:
            logger.debug("Failed to ask shell approval: %s", exc)
            return False

    @staticmethod
    def _conversation_state_dict(conversation: Conversation) -> dict[str, Any]:
        try:
            state = conversation.get_state().to_dict()
            return dict(state or {})
        except Exception:
            try:
                return dict(getattr(conversation, "_state_dict", {}) or {})
            except Exception:
                return {}

    def _run_prompt_invocation(self, payload: PromptInvocation) -> None:
        host = self._host
        if not host.current_conversation:
            self._create_new_conversation()

        conversation = host.current_conversation
        if conversation is None:
            return

        mode_slug = str(getattr(payload, "mode_slug", "") or "").strip().lower()
        if mode_slug:
            self._switch_mode(mode_slug, persist=False)
            try:
                host.services.app_coordinator.apply_mode(conversation, mode_slug)
                self._remember_current_conversation(conversation)
            except Exception as exc:
                logger.debug("Failed to sync conversation mode for prompt invocation: %s", exc)

        updates = getattr(payload, "artifact_updates", {}) or {}
        if isinstance(updates, dict) and updates:
            self._apply_artifact_updates(conversation, updates)
            try:
                host.services.conv_service.save(conversation)
            except Exception as exc:
                logger.debug("Failed to save prompt invocation state: %s", exc)

        metadata = dict(getattr(payload, "metadata", {}) or {})
        command_run = metadata.get("command_run") if isinstance(metadata, dict) else None
        if not isinstance(command_run, dict):
            metadata["command_run"] = {
                "source_prefix": getattr(payload, "source_prefix", "/"),
                "original_text": getattr(payload, "original_text", ""),
            }

        host.message_presenter.send(
            str(getattr(payload, "content", "") or "").strip(),
            [],
            metadata=metadata,
        )

    def _apply_artifact_updates(self, conversation: Conversation, updates: dict[str, Any]) -> None:
        try:
            state = conversation.get_state()
        except Exception as exc:
            logger.debug("Failed to load state for prompt invocation updates: %s", exc)
            return

        changed = False
        current_seq = int(conversation.current_seq_id() or 0)
        for name, value in updates.items():
            artifact_name = str(name or "").strip().lower()
            if not artifact_name:
                continue
            payload = value if isinstance(value, dict) else {"content": value}
            next_content = str(payload.get("content") or "")
            next_abstract = payload.get("abstract")
            next_kind = payload.get("kind")
            next_references = ArtifactService.normalize_references(payload.get("references"))
            existing = state.artifacts.get(artifact_name)
            if existing is not None:
                if (
                    existing.content == next_content
                    and (next_abstract is None or existing.abstract == str(next_abstract or "").strip())
                    and (next_kind is None or existing.kind == str(next_kind or "").strip().lower())
                    and (payload.get("references") is None or existing.references == next_references)
                ):
                    continue
            ArtifactService.upsert_artifact(
                state,
                name=artifact_name,
                content=next_content,
                current_seq=current_seq,
                abstract=next_abstract,
                kind=next_kind,
                references=payload.get("references"),
            )
            changed = True

        if not changed:
            return

        try:
            state.last_updated_seq = current_seq
            conversation.set_state(state)
        except Exception as exc:
            logger.debug("Failed to persist prompt invocation artifact updates: %s", exc)

    def _append_info_message(self, content: str) -> None:
        host = self._host
        if not host.current_conversation:
            return
        info_msg = Message(role="assistant", content=content)
        host.current_conversation.add_message(info_msg)
        host.chat_view.add_message(info_msg)
        host.services.conv_service.save(host.current_conversation)
        self._remember_current_conversation(host.current_conversation)

    def _switch_mode(self, mode_slug: str, *, persist: bool = True) -> None:
        host = self._host
        normalized = str(mode_slug or "").strip().lower()
        if not normalized:
            return

        if not host.input_area.set_mode_selection(normalized):
            self._append_info_message(f"Unknown mode: {normalized}")
            return

        conversation = host.current_conversation
        if conversation is None:
            return

        try:
            host.services.app_coordinator.apply_mode(conversation, normalized)
            if persist:
                host.services.conv_service.save(conversation)
            self._remember_current_conversation(conversation)
        except Exception as exc:
            logger.debug("Failed to persist mode switch: %s", exc)

    def _remember_current_conversation(self, conversation: Conversation) -> None:
        host = self._host
        host.services.app_coordinator.remember_current_conversation(
            conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=host.message_runtime.is_streaming(conversation.id),
        )
