"""Input area widget - compact composer and attachments."""

import logging
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QFileDialog, QFrame, QSizePolicy, QVBoxLayout, QWidget

from core.commands import CommandRegistry
from core.commands.parser import parse_bang_command_text
from core.commands.types import CommandAction, CommandResult, ShellInvocation
from core.content.attachments import extract_composer_text
from core.context.sections import extract_user_request
from core.modes.manager import ModeManager
from core.llm.model_selection import provider_model_ids
from models.provider import provider_matches_name, split_model_ref

from gui.utils.image_utils import extract_images_from_mime, is_supported_image_path

# Extracted sub-components
from gui.widgets.input.attachment_strip import AttachmentPreviewStrip
from gui.widgets.input.composer_toolbar import ComposerToolbar
from gui.widgets.input.text_editor import MessageTextEdit

logger = logging.getLogger(__name__)


class InputArea(QWidget):
    """Input area - single-row compact toolbar + input"""
    
    message_sent = pyqtSignal(str, list, object)
    slash_command_result = pyqtSignal(object)  # CommandResult from / or # commands
    cancel_requested = pyqtSignal()
    conversation_settings_requested = pyqtSignal()
    model_edit_requested = pyqtSignal()
    show_thinking_changed = pyqtSignal(bool)
    prompt_optimize_requested = pyqtSignal(str)  # Optimize current input prompt
    prompt_optimize_cancel_requested = pyqtSignal()
    model_ref_changed = pyqtSignal(str)
    
    provider_model_changed = pyqtSignal(str, str)  # provider_id, model
    mode_changed = pyqtSignal(str)

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
        self._suppress_thinking_signal = False
        self._suppress_model_ref_signal = False
        self._work_dir = ""
        self._app_settings: Dict[str, Any] = {}
        self._is_streaming = False
        self._setup_ui()

    def set_app_settings(self, settings: Dict[str, Any] | None) -> None:
        self._app_settings = dict(settings or {})

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
        self.text_input.attachments_received.connect(self.add_attachments)
        self.text_input.file_reference_added.connect(self._add_attachment_file)
        wrapper_layout.addWidget(self.text_input)

        self.toolbar = ComposerToolbar()
        self.toolbar.attach_requested.connect(self._attach_file)
        self.toolbar.conversation_settings_requested.connect(self.conversation_settings_requested.emit)
        self.toolbar.model_edit_requested.connect(self.model_edit_requested.emit)
        self.toolbar.prompt_optimize_requested.connect(self._on_prompt_optimize_clicked)
        self.toolbar.prompt_optimize_cancel_requested.connect(self.prompt_optimize_cancel_requested.emit)
        self.toolbar.send_requested.connect(self._handle_send_requested)
        self.toolbar.model_ref_changed.connect(self._on_model_ref_changed)
        wrapper_layout.addWidget(self.toolbar)
        layout.addWidget(input_wrapper)

        self.provider_combo = self.toolbar.provider_combo
        self.model_combo = self.toolbar.model_combo
        self.mode_combo = self.toolbar.mode_combo
        self.thinking_toggle = self.toolbar.thinking_toggle
        self.prompt_optimize_btn = self.toolbar.prompt_optimize_btn
        self.model_ref_combo = self.toolbar.model_ref_combo

        self._mode_manager = ModeManager(self._work_dir or None)
        for m in self._mode_manager.list_ui_modes():
            self.mode_combo.addItem(m.name, m.slug)

        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self.provider_combo.currentIndexChanged.connect(self._emit_provider_model_changed)
        self.model_combo.currentTextChanged.connect(self._emit_provider_model_changed)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.thinking_toggle.toggled.connect(self._on_thinking_toggled)

    def _on_mode_changed(self, _index: int) -> None:
        try:
            self.mode_changed.emit(self.get_selected_mode_slug())
        except Exception as e:
            logger.debug("Failed to emit mode change: %s", e)

    def set_streaming_state(self, is_streaming: bool):
        self._is_streaming = is_streaming
        self.toolbar.set_streaming_state(is_streaming, self.style())
        if is_streaming:
            self.text_input.setEnabled(False)
        else:
            self.text_input.setEnabled(True)
            self.text_input.setFocus()


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

    def _handle_send_requested(self):
        if self._is_streaming:
            self.cancel_requested.emit()
        else:
            self._send_message()

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
        desired_provider_id = str(selected_provider_id or self.get_selected_provider_id() or "").strip()
        desired_model = str(selected_model or self.get_selected_model() or "").strip()
        desired_model_ref = str(selected_model_ref or "").strip()

        if desired_model_ref:
            provider_name, model_name = split_model_ref(desired_model_ref)
            if provider_name:
                for provider in providers or []:
                    if provider_matches_name(provider, provider_name):
                        desired_provider_id = str(getattr(provider, "id", "") or "").strip()
                        models = provider_model_ids(provider)
                        desired_model = model_name or (models[0] if models else "")
                        break
            elif model_name:
                desired_model = model_name

        self.provider_combo.blockSignals(True)
        self.provider_combo.clear()
        self._providers = providers
        
        for provider in providers:
            self.provider_combo.addItem(provider.name, provider.id)
        
        self.provider_combo.blockSignals(False)
        if not providers:
            self.model_combo.clear()
            self.toolbar.set_model_ref_options([], "")
            if emit_signal:
                self._emit_provider_model_changed()
            return

        restore_index = self.provider_combo.findData(desired_provider_id)
        if restore_index < 0:
            restore_index = 0

        self.provider_combo.blockSignals(True)
        try:
            self.provider_combo.setCurrentIndex(restore_index)
        finally:
            self.provider_combo.blockSignals(False)

        self._populate_models_for_provider(
            restore_index,
            preferred_model=desired_model,
            emit_signal=emit_signal,
        )
        try:
            current_ref = desired_model_ref or self._build_selected_model_ref()
            self.toolbar.set_model_ref_options(providers or [], current_model_ref=current_ref)
        except Exception as e:
            logger.debug("Failed to sync bottom model selector options: %s", e)
    
    def _on_provider_changed(self, index: int):
        self._populate_models_for_provider(index, emit_signal=True)

    def _populate_models_for_provider(
        self,
        index: int,
        *,
        preferred_model: str = "",
        emit_signal: bool = True,
    ) -> None:
        self.model_combo.clear()
        
        if 0 <= index < len(self._providers):
            provider = self._providers[index]
            for model in provider_model_ids(provider):
                self.model_combo.addItem(model)

            desired_model = str(preferred_model or "").strip()
            if desired_model:
                idx = self.model_combo.findText(desired_model)
                if idx >= 0:
                    self.model_combo.setCurrentIndex(idx)
                else:
                    self.model_combo.addItem(desired_model)
                    self.model_combo.setItemData(
                        self.model_combo.count() - 1,
                        "当前会话模型（未加入常用模型目录）",
                        Qt.ItemDataRole.ToolTipRole,
                    )
                    self.model_combo.setCurrentText(desired_model)

        if emit_signal:
            self._emit_provider_model_changed()
    
    def _emit_provider_model_changed(self) -> None:
        try:
            provider_id = self.get_selected_provider_id()
            model = (self.get_selected_model() or "").strip()
        except Exception as e:
            logger.debug("Failed to get provider/model for signal: %s", e)
            return
        self.provider_model_changed.emit(provider_id, model)

    def _on_model_ref_changed(self, model_ref: str) -> None:
        if self._suppress_model_ref_signal:
            return
        provider_name, model_name = split_model_ref(str(model_ref or "").strip())
        provider_id = ""
        if provider_name:
            for provider in self._providers or []:
                if provider_matches_name(provider, provider_name):
                    provider_id = str(getattr(provider, "id", "") or "")
                    break
        self._suppress_model_ref_signal = True
        try:
            self.set_provider_model_selection(
                provider_id=provider_id,
                model=model_name or str(model_ref or "").strip(),
                emit_signal=False,
            )
        finally:
            self._suppress_model_ref_signal = False
        self.model_ref_changed.emit(str(model_ref or "").strip())

    def get_selected_provider_id(self) -> str:
        return self.provider_combo.currentData() or ""
    
    def get_selected_model(self) -> str:
        return self.model_combo.currentText()

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

    def set_provider_model_selection(
        self,
        *,
        provider_id: str | None = None,
        model: str | None = None,
        emit_signal: bool = False,
    ) -> bool:
        target_provider_id = str(provider_id or "").strip()
        target_model = str(model or "").strip()
        provider_index = self.provider_combo.findData(target_provider_id) if target_provider_id else -1

        self.provider_combo.blockSignals(True)
        try:
            if provider_index >= 0:
                self.provider_combo.setCurrentIndex(provider_index)
        finally:
            self.provider_combo.blockSignals(False)

        if provider_index >= 0:
            self._populate_models_for_provider(
                provider_index,
                preferred_model=target_model,
                emit_signal=emit_signal,
            )
            try:
                self.toolbar.set_model_ref(self._build_selected_model_ref())
            except Exception as e:
                logger.debug("Failed to sync bottom model selector after provider/model selection: %s", e)
            return True

        if target_model:
            self.model_combo.setCurrentText(target_model)
            if emit_signal:
                self._emit_provider_model_changed()
            try:
                self.toolbar.set_model_ref(self._build_selected_model_ref())
            except Exception as e:
                logger.debug("Failed to sync bottom model selector fallback selection: %s", e)
        return False

    def set_model_ref_options(self, providers: list, current_model_ref: str = "") -> None:
        self.toolbar.set_model_ref_options(providers or [], current_model_ref=current_model_ref)

    def set_model_ref(self, model_ref: str) -> None:
        self.toolbar.set_model_ref(model_ref or "")

    def model_ref(self) -> str:
        return self.toolbar.model_ref()

    def _build_selected_model_ref(self) -> str:
        provider_id = self.get_selected_provider_id()
        provider_name = ""
        for provider in self._providers or []:
            if str(getattr(provider, "id", "") or "") == str(provider_id or ""):
                provider_name = str(getattr(provider, "name", "") or "")
                break
        model = self.get_selected_model()
        if provider_name and model:
            return f"{provider_name}|{model}"
        return model or ""

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
        self._suppress_thinking_signal = True
        try:
            self.thinking_toggle.setChecked(bool(enabled))
        finally:
            self._suppress_thinking_signal = False

    def is_show_thinking_enabled(self) -> bool:
        try:
            return bool(self.thinking_toggle.isChecked())
        except Exception as e:
            logger.debug("Failed to read thinking toggle state: %s", e)
            return True

    def _on_thinking_toggled(self, checked: bool):
        if self._suppress_thinking_signal:
            return
        self.show_thinking_changed.emit(bool(checked))
    
    def _attach_file(self):
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, '添加文件', '',
            '所有文件 (*);;图片 (*.png *.jpg *.jpeg *.gif *.webp)'
        )
        for file_path in file_paths:
            self._add_attachment_file(file_path)

    def add_attachments(self, sources: list) -> None:
        if not sources:
            return
        for src in sources:
            if isinstance(src, str) and src:
                self._add_attachment_file(src)

        try:
            self.text_input.setFocus()
        except Exception as e:
            logger.debug("Failed to set focus after attachments: %s", e)

    def _add_attachment_file(self, path: str):
        # Check if already attached
        for att in self._attachments:
            if att['path'] == path:
                return
        
        is_img = False
        if path.startswith("data:image"):
            is_img = True
        elif is_supported_image_path(path):
            is_img = True
            
        self._attachments.append({'path': path, 'type': 'image' if is_img else 'file'})
        self.attachment_strip.add_attachment(path, is_image=is_img)

    def _remove_attachment(self, path: str):
        for i, att in enumerate(self._attachments):
            if att['path'] == path:
                self._attachments.pop(i)
                break

        self.attachment_strip.remove_attachment(path)

    def set_conversation(self, conversation) -> None:
        """Set the current conversation for command context."""
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

        self.text_input.clear()
        self.slash_command_result.emit(result)
        return True

    def _emit_message_payload(self, content: str) -> None:
        from core.content.attachments import process_attachments

        composer_text = content
        result = process_attachments(self._attachments)
        metadata = None
        if result.file_content_suffix:
            content += result.file_content_suffix
            metadata = {"composer_text": composer_text}
        self.message_sent.emit(content, result.encoded_images, metadata)

    def _clear_composer(self) -> None:
        self.text_input.clear()
        self._attachments.clear()
        self.attachment_strip.clear_attachments()

    def _send_message(self):
        content = self.text_input.toPlainText().strip()

        if content:
            self.text_input.remember_history_entry(content)

        if self._try_handle_command(content):
            return

        if not content and not self._attachments:
            self.message_sent.emit("", [], None)
            return

        self._emit_message_payload(content)
        self._clear_composer()
    
    def set_enabled(self, enabled: bool):
        self.text_input.setEnabled(enabled)
        self.toolbar.send_btn.setEnabled(True if self._is_streaming else bool(enabled))
    
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()
    
    def dropEvent(self, event: QDropEvent):
        data_urls, file_paths = extract_images_from_mime(event.mimeData())
        self.add_attachments(data_urls + file_paths)
