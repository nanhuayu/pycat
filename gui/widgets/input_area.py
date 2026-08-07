"""Input area widget - compact composer and attachments."""

import logging
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.commands import CommandRegistry
from core.commands.parser import parse_bang_command_text
from core.commands.types import CommandAction, CommandResult, ShellInvocation
from core.content.attachments import extract_composer_text
from core.context.sections import extract_user_request
from core.modes.manager import ModeManager
from gui.utils.icon_manager import Icons
from gui.utils.image_utils import extract_attachment_sources_from_mime, is_supported_image_path

# Extracted sub-components
from gui.widgets.input.attachment_strip import AttachmentPreviewStrip
from gui.widgets.input.composer_toolbar import ComposerToolbar
from gui.widgets.input.text_editor import MessageTextEdit
from models.model_ref import build_model_ref, provider_matches_name, split_model_ref

logger = logging.getLogger(__name__)


class InputArea(QWidget):
    """Input area - single-row compact toolbar + input"""
    
    message_sent = pyqtSignal(str, list, object)
    slash_command_result = pyqtSignal(object)  # CommandResult from / or # commands
    cancel_requested = pyqtSignal()
    prompt_optimize_requested = pyqtSignal(str)  # Optimize current input prompt
    prompt_optimize_cancel_requested = pyqtSignal()
    model_ref_changed = pyqtSignal(str)
    
    mode_changed = pyqtSignal(str)
    permission_preset_changed = pyqtSignal(str)
    session_settings_requested = pyqtSignal()
    model_edit_requested = pyqtSignal()

    def __init__(
        self,
        parent=None,
        *,
        command_registry: Optional[CommandRegistry] = None,
        tool_schema_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    ):
        super().__init__(parent)
        self.setObjectName("input_container")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._command_registry = command_registry or CommandRegistry()
        self._tool_schema_provider = tool_schema_provider
        self._attachments: List[Dict[str, Any]] = []  # [{'path': str, 'type': 'image'|'file'}]
        self._conversation = None
        self._providers = []
        self._work_dir = ""
        self._app_settings: Dict[str, Any] = {}
        self._context_busy = False
        self._submission_busy = False
        self._is_streaming = False
        self._input_enabled = True
        self._revision_state: Dict[str, Any] | None = None
        self._setup_ui()

    def set_app_settings(self, settings: Dict[str, Any] | None) -> None:
        self._app_settings = dict(settings or {})

    def refresh_theme(self) -> None:
        self.toolbar.refresh_theme()

    def _bang_command_behavior(self) -> str:
        try:
            shell = self._app_settings.get("shell") if isinstance(self._app_settings, dict) else None
            behavior = str((shell or {}).get("bang_command_behavior") or "shell").strip().lower()
        except Exception:
            behavior = "shell"
        return behavior if behavior in {"shell", "agent"} else "shell"

    def set_work_dir(self, path: str):
        self._work_dir = path
        self.text_input.set_work_dir(path)
        try:
            self.refresh_modes()
        except Exception as e:
            logger.debug("Failed to refresh modes after work_dir change: %s", e)

    def get_work_dir(self) -> str:
        return str(self._work_dir or "")

    def refresh_modes(self) -> None:
        """Reload global/project modes while preserving the current selection."""

        self._refresh_modes(self._work_dir)

    def _refresh_modes(self, work_dir: str) -> None:
        """Reload modes from global user config and keep selection if possible."""
        try:
            cur_slug = str(self.mode_combo.currentData() or '')
        except Exception as e:
            logger.debug("Failed to get current mode slug: %s", e)
            cur_slug = ''

        try:
            self.mode_combo.blockSignals(True)
            self.mode_combo.clear()
            self._mode_manager = ModeManager(work_dir or None)
            for m in self._mode_manager.list_ui_modes():
                self.mode_combo.addItem(m.name, m.slug)
            if cur_slug:
                idx = self.mode_combo.findData(cur_slug)
                if idx < 0:
                    current_mode = self._mode_manager.get(cur_slug)
                    if current_mode.slug == cur_slug:
                        self.mode_combo.addItem(current_mode.name, current_mode.slug)
                        idx = self.mode_combo.findData(cur_slug)
                if idx >= 0:
                    self.mode_combo.setCurrentIndex(idx)
        finally:
            try:
                self.mode_combo.blockSignals(False)
            except Exception as e:
                logger.debug("Failed to unblock mode_combo signals: %s", e)
        
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.revision_bar = QFrame()
        self.revision_bar.setObjectName("composer_revision_bar")
        revision_layout = QHBoxLayout(self.revision_bar)
        revision_layout.setContentsMargins(8, 4, 6, 4)
        revision_layout.setSpacing(6)
        self.revision_label = QLabel()
        self.revision_label.setObjectName("composer_revision_label")
        self.revision_label.setWordWrap(True)
        revision_layout.addWidget(self.revision_label, 1)
        self.revision_cancel_btn = QToolButton()
        self.revision_cancel_btn.setObjectName("composer_revision_cancel")
        self.revision_cancel_btn.setIcon(Icons.get_muted(Icons.XMARK))
        self.revision_cancel_btn.setIconSize(QSize(16, 16))
        self.revision_cancel_btn.setFixedSize(24, 24)
        self.revision_cancel_btn.setToolTip("取消编辑")
        self.revision_cancel_btn.clicked.connect(self.cancel_revision)
        revision_layout.addWidget(self.revision_cancel_btn)
        self.revision_bar.setVisible(False)
        layout.addWidget(self.revision_bar)

        self.attachment_strip = AttachmentPreviewStrip()
        self.attachment_strip.remove_requested.connect(self._remove_attachment)
        layout.addWidget(self.attachment_strip)
        
        # ===== Main input wrapper =====
        input_wrapper = QFrame()
        input_wrapper.setObjectName("input_wrapper")
        input_wrapper.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        wrapper_layout = QVBoxLayout(input_wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(4)
        
        # Text input
        self.text_input = MessageTextEdit()
        self.text_input.setObjectName("message_input")
        self.text_input.setPlaceholderText(self._command_registry.build_input_placeholder())
        self.text_input.setMinimumHeight(40)
        self.text_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.text_input.configure_command_registry(self._command_registry, self._build_command_context)
        self.text_input.send_requested.connect(self._send_message)
        self.text_input.cancel_requested.connect(self._request_cancel)
        self.text_input.textChanged.connect(self._sync_primary_action)
        self.text_input.attachments_received.connect(self.add_attachments)
        self.text_input.file_reference_added.connect(self._add_attachment_file)
        wrapper_layout.addWidget(self.text_input)

        self.toolbar = ComposerToolbar()
        self.toolbar.attach_requested.connect(self._attach_file)
        self.toolbar.prompt_optimize_requested.connect(self._on_prompt_optimize_clicked)
        self.toolbar.prompt_optimize_cancel_requested.connect(self.prompt_optimize_cancel_requested.emit)
        self.toolbar.primary_action_requested.connect(self._handle_primary_action_requested)
        self.toolbar.model_ref_changed.connect(self._on_model_ref_changed)
        self.toolbar.permission_preset_changed.connect(self.permission_preset_changed.emit)
        self.toolbar.compact_requested.connect(self._request_compact)
        self.toolbar.session_settings_requested.connect(self.session_settings_requested.emit)
        self.toolbar.model_edit_requested.connect(self.model_edit_requested.emit)
        wrapper_layout.addWidget(self.toolbar)
        layout.addWidget(input_wrapper)

        self.mode_combo = self.toolbar.mode_combo
        self.prompt_optimize_btn = self.toolbar.prompt_optimize_btn
        self.model_ref_combo = self.toolbar.model_ref_combo
        self.permission_btn = self.toolbar.permission_btn
        self.context_usage_btn = self.toolbar.context_usage_btn
        self.session_options_btn = self.toolbar.session_options_btn
        self.model_edit_btn = self.toolbar.model_edit_btn

        self._mode_manager = ModeManager(self._work_dir or None)
        for m in self._mode_manager.list_ui_modes():
            self.mode_combo.addItem(m.name, m.slug)

        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

    def _on_mode_changed(self, _index: int) -> None:
        try:
            self.mode_changed.emit(self.get_selected_mode_slug())
        except Exception as e:
            logger.debug("Failed to emit mode change: %s", e)

    def set_streaming_state(self, is_streaming: bool):
        self._is_streaming = bool(is_streaming)
        self.toolbar.set_streaming_state(self._is_streaming)
        self.text_input.setAcceptDrops(not is_streaming)
        self.text_input.setPlaceholderText(
            "追加要求，将在当前步骤完成后处理"
            if is_streaming
            else self._command_registry.build_input_placeholder()
        )
        self._sync_interaction_state()
        if self.text_input.isEnabled():
            self.text_input.setFocus()

    def set_context_busy(self, busy: bool) -> None:
        self._context_busy = bool(busy)
        self.toolbar.set_context_busy(self._context_busy)
        self._sync_interaction_state()

    def set_submission_busy(self, busy: bool) -> None:
        self._submission_busy = bool(busy)
        self._update_revision_bar()
        self._sync_interaction_state()


    def set_prompt_optimize_busy(self, busy: bool) -> None:
        self.toolbar.set_prompt_optimize_busy(busy, is_streaming=self._is_streaming)

    def _on_prompt_optimize_clicked(self) -> None:
        try:
            text = (self.text_input.toPlainText() or "").strip()
        except Exception as e:
            logger.debug("Failed to get text for optimize: %s", e)
            text = ""
        if not text:
            return
        self.prompt_optimize_requested.emit(text)

    def _sync_primary_action(self) -> None:
        attachments_ready = not self._has_attachment_errors()
        self.toolbar.set_primary_action_state(
            has_draft=self._has_actionable_draft(),
            enabled=self._is_streaming
            or (
                attachments_ready
                and (
                    self._input_enabled
                    and not self._submission_busy
                    and not self._context_busy
                )
            ),
        )

    def _sync_interaction_state(self) -> None:
        editable = (
            self._input_enabled
            and not self._context_busy
            and not self._submission_busy
        )
        self.text_input.setEnabled(editable)
        self.attachment_strip.setEnabled(editable)
        self.toolbar.setEnabled(not self._submission_busy or self._is_streaming)
        self._sync_primary_action()

    def _has_actionable_draft(self) -> bool:
        return (
            self.text_input.isEnabled()
            and not self._submission_busy
            and not self._has_attachment_errors()
            and bool(self.text_input.toPlainText().strip() or self._attachments)
        )

    def _has_attachment_errors(self) -> bool:
        return any(str(item.get("error") or "").strip() for item in self._attachments)

    def _handle_primary_action_requested(self) -> None:
        if self._is_streaming and not self._has_actionable_draft():
            self._request_cancel()
            return
        self._send_message()

    def _request_cancel(self) -> None:
        if self._is_streaming:
            self.cancel_requested.emit()

    def set_providers(
        self,
        providers: list,
        *,
        selected_provider_id: str | None = None,
        selected_model: str | None = None,
        selected_model_ref: str | None = None,
        emit_signal: bool = True,
    ):
        providers = [provider for provider in (providers or []) if bool(getattr(provider, "enabled", True))]
        self._providers = providers
        current_ref = str(selected_model_ref or "").strip()
        if not current_ref:
            current_ref = self._model_ref_for_selection(
                provider_id=str(selected_provider_id or self.get_selected_provider_id() or "").strip(),
                model=str(selected_model or self.get_selected_model() or "").strip(),
            )
        self.toolbar.set_model_ref_options(providers, current_model_ref=current_ref)
        if emit_signal:
            self.model_ref_changed.emit(self.model_ref())

    def _on_model_ref_changed(self, model_ref: str) -> None:
        self.model_ref_changed.emit(str(model_ref or "").strip())

    def get_selected_provider_id(self) -> str:
        provider_name, _model = split_model_ref(self.model_ref())
        if not provider_name:
            return ""
        for provider in self._providers:
            if provider_matches_name(provider, provider_name):
                return str(getattr(provider, "id", "") or "").strip()
        return ""
    
    def get_selected_model(self) -> str:
        _provider_name, model = split_model_ref(self.model_ref())
        return str(model or "").strip()

    def get_selected_mode(self) -> str:
        return self.mode_combo.currentText()

    def get_selected_mode_slug(self) -> str:
        try:
            return str(self.mode_combo.currentData() or '').strip() or (self.get_selected_mode() or '').strip().lower()
        except Exception as e:
            logger.debug("Failed to get mode slug from combo data: %s", e)
            return (self.get_selected_mode() or '').strip().lower()

    def get_mode_manager(self) -> ModeManager:
        return self._mode_manager

    def set_model_ref(self, model_ref: str) -> None:
        self.toolbar.set_model_ref(model_ref or "")

    def model_ref(self) -> str:
        return self.toolbar.model_ref()

    def _model_ref_for_selection(self, *, provider_id: str, model: str) -> str:
        provider_name = ""
        for provider in self._providers:
            if str(getattr(provider, "id", "") or "").strip() == str(provider_id or "").strip():
                provider_name = str(getattr(provider, "name", "") or "").strip()
                break
        return build_model_ref(provider_name, model) if provider_name and model else str(model or "").strip()

    def set_mode_selection(self, mode_slug: str) -> bool:
        normalized = str(mode_slug or "").strip()
        if not normalized:
            return False
        idx = self.mode_combo.findData(normalized)
        if idx < 0:
            mode = self._mode_manager.get(normalized)
            if mode.slug != normalized:
                return False
            self.mode_combo.blockSignals(True)
            try:
                self.mode_combo.addItem(mode.name, mode.slug)
            finally:
                self.mode_combo.blockSignals(False)
            idx = self.mode_combo.findData(normalized)
            if idx < 0:
                return False
        self.mode_combo.blockSignals(True)
        try:
            self.mode_combo.setCurrentIndex(idx)
        finally:
            self.mode_combo.blockSignals(False)
        return True

    def set_show_thinking(self, enabled: bool):
        self.toolbar.set_show_thinking(bool(enabled))

    def set_permission_preset(self, preset: str) -> None:
        self.toolbar.set_permission_preset(preset)

    def get_permission_preset(self) -> str:
        return self.toolbar.permission_preset()

    def set_permission_preset_enabled(self, enabled: bool) -> None:
        self.toolbar.set_permission_enabled(enabled)

    def is_show_thinking_enabled(self) -> bool:
        return self.toolbar.is_show_thinking_enabled()

    def set_token_snapshot(self, snapshot) -> None:
        self.toolbar.set_token_snapshot(snapshot)

    def _request_compact(self) -> None:
        """Route the toolbar action through the canonical command presenter."""
        self.slash_command_result.emit(CommandResult(action=CommandAction.COMPACT))
    
    def _attach_file(self):
        if self._is_streaming:
            return
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, '添加文件', '',
            '所有文件 (*);;Word/Excel (*.doc *.docx *.docm *.dotx *.dotm *.xls *.xlsx *.xlsm *.xltx *.xltm);;图片 (*.png *.jpg *.jpeg *.gif *.webp)'
        )
        for file_path in file_paths:
            self._add_attachment_file(file_path)

    def add_attachments(self, sources: list) -> None:
        if self._is_streaming or not sources:
            return
        for src in sources:
            if isinstance(src, str) and src:
                self._add_attachment_file(src)

        try:
            self.text_input.setFocus()
        except Exception as e:
            logger.debug("Failed to set focus after attachments: %s", e)

    def _add_attachment_file(
        self,
        path: str,
        *,
        error: str = "",
        name: str = "",
        mime: str = "",
    ):
        if self._is_streaming:
            return
        # Check if already attached
        for att in self._attachments:
            if att['path'] == path:
                if error:
                    att["error"] = str(error)
                    self.attachment_strip.add_attachment(
                        path,
                        is_image=str(att.get("type") or "") == "image",
                        error=str(error),
                        display_name=str(att.get("name") or name or ""),
                    )
                    self._sync_primary_action()
                return
        
        is_img = str(mime or "").lower().startswith("image/")
        if path.startswith("data:image") or is_supported_image_path(path):
            is_img = True
            
        attachment = {'path': path, 'type': 'image' if is_img else 'file'}
        if name:
            attachment["name"] = str(name)
        if mime:
            attachment["mime"] = str(mime)
        if error:
            attachment["error"] = str(error)
        self._attachments.append(attachment)
        self.attachment_strip.add_attachment(
            path,
            is_image=is_img,
            error=error,
            display_name=str(name or ""),
        )
        self._sync_primary_action()

    def _remove_attachment(self, path: str):
        for i, att in enumerate(self._attachments):
            if att['path'] == path:
                self._attachments.pop(i)
                break

        self.attachment_strip.remove_attachment(path)
        self._sync_primary_action()

    def set_conversation(self, conversation) -> None:
        """Set the current conversation for command context."""
        conversation_id = str(getattr(conversation, "id", "") or "")
        if self._revision_state and self._revision_state.get("conversation_id") != conversation_id:
            self.cancel_revision()
        self._conversation = conversation
        history: list[str] = []
        for message in getattr(conversation, "messages", []) or []:
            if str(getattr(message, "role", "") or "") != "user":
                continue
            text = extract_composer_text(
                getattr(message, "content", ""),
                getattr(message, "metadata", None),
            )
            text = extract_user_request(text)
            if text and not text.startswith(("[AUTO-CONTINUE]", "[WARNING]")):
                history.append(text)
        self.text_input.set_history_entries(history)

    def _build_command_context(self) -> Dict[str, Any]:
        try:
            available_tools = self._tool_schema_provider() if self._tool_schema_provider else []
        except Exception as e:
            logger.debug("Failed to get tool schemas for command context: %s", e)
            available_tools = []

        return {
            "current_mode": self.get_selected_mode_slug(),
            "conversation": self._conversation,
            "work_dir": self._work_dir,
            "available_tools": available_tools,
            "available_modes": [
                {"slug": mode.slug, "name": mode.name}
                for mode in self._mode_manager.list_ui_modes()
            ],
            "bang_command_behavior": self._bang_command_behavior(),
        }

    def _try_handle_command(self, content: str) -> bool:
        bang_command = parse_bang_command_text(content)
        if bang_command is not None:
            if self._bang_command_behavior() == "agent":
                return False
            self.text_input.remember_history_entry(content)
            result = CommandResult(
                action=CommandAction.SHELL_RUN,
                data=ShellInvocation(
                    command=bang_command,
                    cwd=self._work_dir,
                    source_prefix="!",
                    original_text=str(content or "").strip(),
                ),
            )
            self.text_input.clear()
            self.slash_command_result.emit(result)
            return True

        context = self._build_command_context()
        if not self._command_registry.is_command(content, context):
            return False

        result = self._command_registry.execute(content, context)
        if result is None:
            return False

        self.text_input.remember_history_entry(content)
        self.text_input.clear()
        self.slash_command_result.emit(result)
        return True

    def _emit_message_payload(self, content: str) -> None:
        attachments = [dict(item) for item in self._attachments]
        metadata: dict[str, Any] = {"composer_text": content}
        if self._revision_state:
            metadata["conversation_revision"] = dict(self._revision_state)
        self.message_sent.emit(content, attachments, metadata)

    def _clear_composer(self) -> None:
        self.text_input.clear()
        self._attachments.clear()
        self.attachment_strip.clear_attachments()
        self._sync_primary_action()

    def confirm_message_sent(self, content: str, attachments: list[dict[str, Any]]) -> bool:
        if self._revision_state:
            return False
        current_text = self.text_input.toPlainText().strip()
        current_attachments = [dict(item) for item in self._attachments]
        if current_text != str(content or "").strip():
            return False
        if current_attachments != [dict(item) for item in attachments or []]:
            return False
        if current_text:
            self.text_input.remember_history_entry(current_text)
        self._clear_composer()
        return True

    def begin_revision(
        self,
        *,
        conversation_id: str,
        message_id: str,
        fingerprint: str,
        turn_number: int,
        following_turns: int,
        content: str,
        attachments: list[dict[str, Any]],
    ) -> None:
        self._revision_state = {
            "conversation_id": str(conversation_id or ""),
            "message_id": str(message_id or ""),
            "fingerprint": str(fingerprint or ""),
        }
        self._clear_composer()
        self.text_input.setPlainText(str(content or ""))
        for item in attachments or []:
            if not isinstance(item, dict):
                continue
            self._add_attachment_file(
                str(item.get("path") or ""),
                error=str(item.get("error") or ""),
                name=str(item.get("name") or ""),
                mime=str(item.get("mime") or ""),
            )
        self.revision_label.setText(
            f"编辑第 {max(1, int(turn_number or 1))} 轮 · "
            f"发送后将移除后续 {max(0, int(following_turns or 0))} 轮"
        )
        self._update_revision_bar()
        self.toolbar.set_revision_state(True)
        self._sync_primary_action()
        self.text_input.setFocus()

    def revision_state(self) -> dict[str, str] | None:
        return dict(self._revision_state) if self._revision_state else None

    def has_draft(self) -> bool:
        return bool(self.text_input.toPlainText().strip() or self._attachments)

    def cancel_revision(self, *, clear_draft: bool = True) -> bool:
        if self._revision_state is None:
            return False
        self._revision_state = None
        self.revision_bar.setVisible(False)
        self.toolbar.set_revision_state(False)
        if clear_draft:
            self._clear_composer()
        self._sync_primary_action()
        return True

    def confirm_revision_sent(
        self,
        conversation_id: str,
        message_id: str,
        content: str,
        attachments: list[dict[str, Any]],
    ) -> bool:
        state = self._revision_state
        if not state:
            return False
        if (
            state.get("conversation_id") != str(conversation_id or "")
            or state.get("message_id") != str(message_id or "")
        ):
            return False
        unchanged = (
            self.text_input.toPlainText().strip() == str(content or "").strip()
            and [dict(item) for item in self._attachments]
            == [dict(item) for item in attachments or []]
        )
        self._revision_state = None
        self.revision_bar.setVisible(False)
        self.toolbar.set_revision_state(False)
        if unchanged:
            if str(content or "").strip():
                self.text_input.remember_history_entry(str(content or "").strip())
            self._clear_composer()
        self._sync_primary_action()
        return True

    def _update_revision_bar(self) -> None:
        active = self._revision_state is not None
        self.revision_bar.setVisible(active)
        self.revision_cancel_btn.setEnabled(
            active and not self._submission_busy and not self._context_busy
        )

    def restore_failed_message(self, content: str, attachments: list[dict[str, Any]]) -> bool:
        """Restore a failed submission only when the user has not started a new draft."""
        if self.text_input.toPlainText().strip() or self._attachments:
            return False
        self.text_input.setPlainText(str(content or ""))
        for item in attachments or []:
            if isinstance(item, dict):
                self._add_attachment_file(
                    str(item.get("path") or ""),
                    error=str(item.get("error") or ""),
                    name=str(item.get("name") or ""),
                    mime=str(item.get("mime") or ""),
                )
        return True

    def mark_attachment_errors(self, errors: dict[str, str]) -> None:
        normalized = {
            str(source or ""): str(error or "")
            for source, error in (errors or {}).items()
            if str(source or "")
        }
        for attachment in self._attachments:
            source = str(attachment.get("path") or "")
            if source not in normalized:
                continue
            attachment["error"] = normalized[source]
            self.attachment_strip.add_attachment(
                source,
                is_image=attachment.get("type") == "image",
                error=normalized[source],
                display_name=str(attachment.get("name") or ""),
            )
        self._sync_primary_action()

    def load_draft(self, content: str, attachments: list[dict[str, Any]]) -> None:
        """Replace the composer draft without mutating conversation history."""
        self.cancel_revision(clear_draft=False)
        self._clear_composer()
        self.text_input.setPlainText(str(content or ""))
        for item in attachments or []:
            if not isinstance(item, dict):
                continue
            self._add_attachment_file(
                str(item.get("path") or ""),
                error=str(item.get("error") or ""),
                name=str(item.get("name") or ""),
                mime=str(item.get("mime") or ""),
            )
        self.text_input.setFocus()

    def _send_message(self):
        if self._submission_busy:
            return
        if self._has_attachment_errors():
            return
        content = self.text_input.toPlainText().strip()

        if self._is_streaming and not content:
            return

        if not self._is_streaming and self._revision_state is None and self._try_handle_command(content):
            return

        if not content and not self._attachments:
            if self._revision_state is not None:
                return
            self.message_sent.emit("", [], None)
            return

        self._emit_message_payload(content)
    
    def set_enabled(self, enabled: bool):
        self._input_enabled = bool(enabled)
        self._sync_interaction_state()
    
    def dragEnterEvent(self, event: QDragEnterEvent):
        if self._is_streaming:
            event.ignore()
            return
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()
    
    def dropEvent(self, event: QDropEvent):
        if self._is_streaming:
            event.ignore()
            return
        data_urls, file_paths = extract_attachment_sources_from_mime(event.mimeData())
        self.add_attachments(data_urls + file_paths)
