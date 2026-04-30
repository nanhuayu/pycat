"""Conversation command presenter.

Owns command-result handling, prompt invocations, explicit shell execution,
and export flows so conversation lifecycle concerns can stay focused.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Callable

from PyQt6.QtWidgets import QFileDialog, QMessageBox

from core.state.services.document_service import DocumentService
from core.tools.permissions import ToolPermissionPolicy
from models.conversation import Conversation, Message
from services.command_service import CommandExecutionDenied

if TYPE_CHECKING:
    from core.commands import PromptInvocation, ShellInvocation
    from ui.main_window import MainWindow

logger = logging.getLogger(__name__)


class ConversationCommandPresenter:
    """Handles command dispatch, export, and explicit shell execution."""

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

        conv = host.current_conversation
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
            shell_command = str(payload.command_text if payload else "").strip()
            if not shell_command:
                return
            self._run_shell_command(payload)
            return

        if result.action == CommandAction.EXPORT:
            self.export_current((result.data or "markdown").strip())
            return

        if result.action == CommandAction.DISPLAY and result.display_text:
            self._append_info_message(result.display_text)

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

        updates = getattr(payload, "document_updates", {}) or {}
        if isinstance(updates, dict) and updates:
            self._apply_document_updates(conversation, updates)
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

    def _apply_document_updates(self, conversation: Conversation, updates: dict[str, Any]) -> None:
        try:
            state = conversation.get_state()
        except Exception as exc:
            logger.debug("Failed to load state for prompt invocation updates: %s", exc)
            return

        changed = False
        current_seq = int(conversation.current_seq_id() or 0)
        for name, value in updates.items():
            doc_name = str(name or "").strip().lower()
            if not doc_name:
                continue
            payload = value if isinstance(value, dict) else {"content": value}
            next_content = str(payload.get("content") or "")
            next_abstract = payload.get("abstract")
            next_kind = payload.get("kind")
            next_references = DocumentService.normalize_references(payload.get("references"))
            existing = state.documents.get(doc_name)
            if existing is not None:
                if (
                    existing.content == next_content
                    and (next_abstract is None or existing.abstract == str(next_abstract or "").strip())
                    and (next_kind is None or existing.kind == str(next_kind or "").strip().lower())
                    and (payload.get("references") is None or existing.references == next_references)
                ):
                    continue
            DocumentService.upsert_document(
                state,
                name=doc_name,
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
            logger.debug("Failed to persist prompt invocation document updates: %s", exc)

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

    def _run_shell_command(self, payload: ShellInvocation | None) -> None:
        host = self._host
        if payload is None:
            return

        if not host.current_conversation:
            self._create_new_conversation()
        conversation = host.current_conversation
        if conversation is None:
            return

        command_text = str(payload.command_text or "").strip()
        if not command_text:
            return

        display_command = str(payload.original_text or "").strip() or f"{payload.source_prefix}{command_text}"

        user_msg = Message(role="user", content=display_command)
        user_msg.metadata["explicit_shell"] = True
        conversation.add_message(user_msg)
        host.chat_view.add_message(user_msg)

        try:
            result_text = host.services.command_service.execute_shell_invocation(
                payload,
                work_dir=getattr(conversation, "work_dir", "") or ".",
                permission_policy=ToolPermissionPolicy.from_config(host.app_settings),
                approval_callback=self._ask_command_approval,
            )
            assistant_msg = Message(role="assistant", content=result_text)
            assistant_msg.metadata["explicit_shell_result"] = True
            assistant_msg.metadata["command"] = command_text
        except CommandExecutionDenied as exc:
            assistant_msg = Message(role="assistant", content=str(exc))
            assistant_msg.metadata["explicit_shell_result"] = True
            assistant_msg.metadata["command"] = command_text
            assistant_msg.metadata["denied"] = True
        except Exception as exc:
            assistant_msg = Message(role="assistant", content=f"Shell execution failed: {exc}")
            assistant_msg.metadata["explicit_shell_result"] = True
            assistant_msg.metadata["command"] = command_text
            assistant_msg.metadata["error"] = True

        conversation.add_message(assistant_msg)
        host.chat_view.add_message(assistant_msg)
        host.stats_panel.update_stats(conversation)
        host.services.conv_service.save(conversation)
        self._remember_current_conversation(conversation)

        try:
            conversations = host.services.conv_service.list_all()
            host.sidebar.update_conversations(conversations)
            host.services.app_coordinator.sync_catalog(
                providers=host.providers,
                conversation_count=len(conversations),
            )
            host.sidebar.select_conversation(conversation.id)
        except Exception as exc:
            logger.debug("Failed to refresh sidebar after explicit shell command: %s", exc)

    def _ask_command_approval(self, message: str) -> bool:
        reply = QMessageBox.question(
            self._host,
            "命令执行确认",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _remember_current_conversation(self, conversation: Conversation) -> None:
        host = self._host
        host.services.app_coordinator.remember_current_conversation(
            conversation,
            providers=host.providers,
            app_settings=host.app_settings,
            is_streaming=host.message_runtime.is_streaming(conversation.id),
        )