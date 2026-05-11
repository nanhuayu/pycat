"""Conversation command presenter.

Owns command-result handling, prompt invocations, and export flows so
conversation lifecycle concerns can stay focused.
"""
from __future__ import annotations

import json
import logging
import asyncio
from typing import TYPE_CHECKING, Any, Callable

from PyQt6.QtWidgets import QFileDialog, QMessageBox

from core.config.schema import ToolPermissionConfig
from core.tools.catalog import normalize_tool_category
from core.state.services.artifact_service import ArtifactService
from core.tools.base import ToolContext
from core.tools.permissions import ToolPermissionPolicy, ToolPermissionResolver
from models.conversation import Conversation, Message

if TYPE_CHECKING:
    from core.commands import PromptInvocation
    from ui.main_window import MainWindow

logger = logging.getLogger(__name__)


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
                host.statusBar().showMessage(f"已导出到 {path}", 3000)
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
        host.statusBar().showMessage(f"已导出到 {path}", 3000)

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
        tool = registry.get_tool("execute_command") if registry is not None and hasattr(registry, "get_tool") else None
        category = normalize_tool_category(str(getattr(tool, "category", "execute") or "execute"))

        permissions = ToolPermissionConfig.from_dict((getattr(host, "app_settings", {}) or {}).get("permissions"))
        effective = permissions.resolve("execute_command", category)
        if not effective.enabled:
            self._append_info_message("`execute_command` 已被当前权限设置禁用，无法执行 `!` Shell 命令。")
            return

        work_dir = str(getattr(payload, "cwd", "") or getattr(conversation, "work_dir", "") or ".")

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
        conversation.messages.append(user_msg)
        try:
            host.chat_view.add_message(user_msg)
        except Exception as exc:
            logger.debug("Failed to add shell command user message to chat view: %s", exc)

        async def _execute():
            approval_callback = (lambda _message: True) if effective.auto_approve else self._ask_shell_approval
            context = ToolContext(
                work_dir=work_dir,
                approval_callback=approval_callback,
                state=self._conversation_state_dict(conversation),
                conversation=conversation,
            )
            if tool is not None:
                runtime_policy = ToolPermissionPolicy.from_effective(
                    category_defaults=permissions.category_defaults,
                    tools=permissions.tools,
                )
                context = ToolPermissionResolver.wrap_context_with_policy(context, tool, runtime_policy)
            return await tool_manager.execute_tool_with_context(
                "execute_command",
                {"command": command, "cwd": ".", "timeout": 600, "background": False},
                context,
            )

        try:
            if tool_manager is None:
                raise RuntimeError("Tool manager is not available")
            result = asyncio.run(_execute())
            content = result.to_string() if hasattr(result, "to_string") else str(result)
            is_error = bool(getattr(result, "is_error", False))
        except Exception as exc:
            logger.debug("Explicit shell command failed: %s", exc)
            content = f"Shell 执行失败：{exc}"
            is_error = True

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
        conversation.messages.append(assistant_msg)
        try:
            host.chat_view.add_message(assistant_msg)
        except Exception as exc:
            logger.debug("Failed to add shell command result to chat view: %s", exc)
        try:
            host.services.conv_service.save(conversation)
        except Exception as exc:
            logger.debug("Failed to save conversation after shell invocation: %s", exc)
        self._remember_current_conversation(conversation)

    def _ask_shell_approval(self, message: str) -> bool:
        try:
            reply = QMessageBox.question(
                self._host,
                "Shell 执行确认",
                str(message or "确认执行 Shell 命令？"),
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
        host.current_conversation.messages.append(info_msg)
        host.chat_view.add_message(info_msg)
        host.services.conv_service.save(host.current_conversation)
        self._remember_current_conversation(host.current_conversation)

    def _switch_mode(self, mode_slug: str, *, persist: bool = True) -> None:
        host = self._host
        normalized = str(mode_slug or "").strip().lower()
        if not normalized:
            return

        if not host.input_area.set_mode_selection(normalized, apply_defaults=True):
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