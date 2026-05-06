"""Input area widget - compact composer and attachments."""

import logging
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QFileDialog, QFrame, QSizePolicy, QVBoxLayout, QWidget

from core.commands import CommandRegistry
from core.modes.manager import ModeManager
from core.modes.features import get_mode_feature_policy, clamp_feature_flags
from models.provider import provider_matches_name, split_model_ref

from ui.utils.image_utils import extract_images_from_mime, is_supported_image_path

# Extracted sub-components
from ui.widgets.input.attachment_strip import AttachmentPreviewStrip
from ui.widgets.input.composer_toolbar import ComposerToolbar
from ui.widgets.input.text_editor import MessageTextEdit

logger = logging.getLogger(__name__)


class InputArea(QWidget):
    """Input area - single-row compact toolbar + input"""
    
    message_sent = pyqtSignal(str, list, object)
    slash_command_result = pyqtSignal(object)  # CommandResult from / or # commands
    cancel_requested = pyqtSignal()
    conversation_settings_requested = pyqtSignal()
    provider_settings_requested = pyqtSignal()  # New: quick access to provider config
    show_thinking_changed = pyqtSignal(bool)
    mcp_toggled = pyqtSignal(bool)  # MCP 开关
    search_toggled = pyqtSignal(bool)  # 搜索开关
    prompt_optimize_requested = pyqtSignal(str)  # Optimize current input prompt
    prompt_optimize_cancel_requested = pyqtSignal()
    
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
        self._suppress_tool_signals = False
        self._mcp_enabled = False
        self._search_enabled = False
        self._work_dir = ""
        self._is_streaming = False
        self._setup_ui()

    def set_work_dir(self, path: str):
        self._work_dir = path
        self.text_input.set_work_dir(path)
        try:
            self._refresh_modes(path)
        except Exception as e:
            logger.debug("Failed to refresh modes after work_dir change: %s", e)

        # Re-apply mode policy after refresh (modes.json may change groups).
        try:
            self._apply_mode_policy(apply_defaults=False)
        except Exception as e:
            logger.debug("Failed to apply mode policy after work_dir change: %s", e)

    def get_work_dir(self) -> str:
        return str(self._work_dir or "")

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
        self.toolbar.provider_settings_requested.connect(self.provider_settings_requested.emit)
        self.toolbar.prompt_optimize_requested.connect(self._on_prompt_optimize_clicked)
        self.toolbar.prompt_optimize_cancel_requested.connect(self.prompt_optimize_cancel_requested.emit)
        self.toolbar.send_requested.connect(self._handle_send_requested)
        wrapper_layout.addWidget(self.toolbar)
        layout.addWidget(input_wrapper)

        self.provider_combo = self.toolbar.provider_combo
        self.model_combo = self.toolbar.model_combo
        self.mode_combo = self.toolbar.mode_combo
        self.thinking_toggle = self.toolbar.thinking_toggle
        self.mcp_toggle = self.toolbar.mcp_toggle
        self.search_toggle = self.toolbar.search_toggle
        self.prompt_optimize_btn = self.toolbar.prompt_optimize_btn

        self._mode_manager = ModeManager(self._work_dir or None)
        for m in self._mode_manager.list_ui_modes():
            self.mode_combo.addItem(m.name, m.slug)

        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self.provider_combo.currentIndexChanged.connect(self._emit_provider_model_changed)
        self.model_combo.currentTextChanged.connect(self._emit_provider_model_changed)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.thinking_toggle.toggled.connect(self._on_thinking_toggled)
        self.mcp_toggle.toggled.connect(self._on_mcp_toggled)
        self.search_toggle.toggled.connect(self._on_search_toggled)

        # Apply initial policy (enable/disable only) based on default mode.
        # Do not force defaults here; the current conversation/settings will sync later.
        try:
            self._apply_mode_policy(apply_defaults=False)
        except Exception as e:
            logger.debug("Failed to apply initial mode policy: %s", e)

    def _on_mode_changed(self, index: int) -> None:
        try:
            self._apply_mode_policy(apply_defaults=True)
            self.mode_changed.emit(self.get_selected_mode_slug())
        except Exception as e:
            logger.debug("Failed to apply mode policy on change: %s", e)

    def _apply_mode_policy(self, apply_defaults: bool = True) -> None:
        """Apply mode-derived policy to Thinking/MCP/Search toggles.

        - Disallowed toggles are disabled and forced off.
        - If apply_defaults is True, allowed toggles are set to the mode defaults.
        """
        slug = self.get_selected_mode_slug()
        mode = self._mode_manager.get(slug)
        policy = get_mode_feature_policy(mode)

        # Thinking toggle is always allowed, but can have defaults.
        if apply_defaults:
            # Apply as a real user-visible toggle change so the app state stays in sync.
            self.thinking_toggle.setChecked(bool(policy.default_thinking))

        # MCP/Search toggles: suppress signals while applying.
        self._suppress_tool_signals = True
        try:
            # MCP
            self.mcp_toggle.setEnabled(bool(policy.allow_mcp) and (not self._is_streaming))
            if not policy.allow_mcp:
                self.mcp_toggle.setChecked(False)
            elif apply_defaults:
                self.mcp_toggle.setChecked(bool(policy.default_mcp))

            # Search
            self.search_toggle.setEnabled(bool(policy.allow_search) and (not self._is_streaming))
            if not policy.allow_search:
                self.search_toggle.setChecked(False)
            elif apply_defaults:
                self.search_toggle.setChecked(bool(policy.default_search))

            self._mcp_enabled = bool(self.mcp_toggle.isChecked())
            self._search_enabled = bool(self.search_toggle.isChecked())
        finally:
            self._suppress_tool_signals = False
    def set_streaming_state(self, is_streaming: bool):
        self._is_streaming = is_streaming
        self.toolbar.set_streaming_state(is_streaming, self.style())
        if is_streaming:
            self.text_input.setEnabled(False)
        else:
            self.text_input.setEnabled(True)
            self.text_input.setFocus()
            try:
                self._apply_mode_policy(apply_defaults=False)
            except Exception as e:
                logger.debug("Failed to apply mode policy after streaming stop: %s", e)


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
        desired_provider_id = str(selected_provider_id or self.get_selected_provider_id() or "").strip()
        desired_model = str(selected_model or self.get_selected_model() or "").strip()
        desired_model_ref = str(selected_model_ref or "").strip()

        if desired_model_ref:
            provider_name, model_name = split_model_ref(desired_model_ref)
            if provider_name:
                for provider in providers or []:
                    if provider_matches_name(provider, provider_name):
                        desired_provider_id = str(getattr(provider, "id", "") or "").strip()
                        desired_model = model_name or str(getattr(provider, "default_model", "") or "").strip()
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
            for model in provider.models:
                self.model_combo.addItem(model)

            desired_model = str(preferred_model or "").strip()
            if desired_model:
                idx = self.model_combo.findText(desired_model)
                if idx >= 0:
                    self.model_combo.setCurrentIndex(idx)
                else:
                    self.model_combo.setCurrentText(desired_model)
            elif provider.default_model:
                idx = self.model_combo.findText(provider.default_model)
                if idx >= 0:
                    self.model_combo.setCurrentIndex(idx)
                else:
                    self.model_combo.setCurrentText(provider.default_model)

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
            return True

        if target_model:
            self.model_combo.setCurrentText(target_model)
            if emit_signal:
                self._emit_provider_model_changed()
        return False

    def set_mode_selection(self, mode_slug: str, *, apply_defaults: bool = False) -> bool:
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
        self.apply_mode_policy(apply_defaults=apply_defaults)
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
    
    def _on_mcp_toggled(self, checked: bool):
        if self._suppress_tool_signals:
            self._mcp_enabled = bool(checked)
            return
        self._mcp_enabled = bool(checked)
        self.mcp_toggled.emit(bool(checked))
    
    def _on_search_toggled(self, checked: bool):
        if self._suppress_tool_signals:
            self._search_enabled = bool(checked)
            return
        self._search_enabled = bool(checked)
        self.search_toggled.emit(bool(checked))
    
    def is_mcp_enabled(self) -> bool:
        return self._mcp_enabled
    
    def is_search_enabled(self) -> bool:
        return self._search_enabled

    def get_effective_tool_flags(self) -> tuple[bool, bool]:
        """Return (enable_search, enable_mcp) clamped by current mode policy."""
        try:
            slug = self.get_selected_mode_slug()
            mode = self._mode_manager.get(slug)
            policy = get_mode_feature_policy(mode)
            _t, mcp, search = clamp_feature_flags(
                policy,
                enable_thinking=bool(self.thinking_toggle.isChecked()),
                enable_mcp=bool(self._mcp_enabled),
                enable_search=bool(self._search_enabled),
            )
            return bool(search), bool(mcp)
        except Exception as e:
            logger.debug("Failed to get effective tool flags from mode policy: %s", e)
            return bool(self._search_enabled), bool(self._mcp_enabled)

    def get_mode_default_tool_flags(self) -> tuple[bool, bool]:
        """Return (enable_search, enable_mcp) defaults derived from the current mode."""
        try:
            slug = self.get_selected_mode_slug()
            mode = self._mode_manager.get(slug)
            policy = get_mode_feature_policy(mode)
            return bool(policy.default_search), bool(policy.default_mcp)
        except Exception as e:
            logger.debug("Failed to get mode default tool flags: %s", e)
            return False, False

    def set_tool_toggles(self, *, enable_mcp: Optional[bool] = None, enable_search: Optional[bool] = None) -> None:
        """Synchronize MCP/Search toggles without re-emitting external signals."""
        self._suppress_tool_signals = True
        try:
            if enable_mcp is not None:
                self.mcp_toggle.setChecked(bool(enable_mcp))
                self._mcp_enabled = bool(enable_mcp)
            if enable_search is not None:
                self.search_toggle.setChecked(bool(enable_search))
                self._search_enabled = bool(enable_search)
        finally:
            self._suppress_tool_signals = False

    def apply_mode_policy(self, apply_defaults: bool = False) -> None:
        """Public wrapper for applying mode policy.

        Use apply_defaults=False when syncing UI from stored conversation settings.
        """
        self._apply_mode_policy(apply_defaults=apply_defaults)
    
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
        }

    def _try_handle_command(self, content: str) -> bool:
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
        from core.attachments import process_attachments

        result = process_attachments(self._attachments)
        if result.file_content_suffix:
            content += result.file_content_suffix
        self.message_sent.emit(content, result.encoded_images, None)

    def _clear_composer(self) -> None:
        self.text_input.clear()
        self._attachments.clear()
        self.attachment_strip.clear_attachments()

    def _send_message(self):
        content = self.text_input.toPlainText().strip()

        if self._try_handle_command(content):
            return

        if not content and not self._attachments:
            self.message_sent.emit("", [], None)
            return

        self._emit_message_payload(content)
        self._clear_composer()
    
    def set_enabled(self, enabled: bool):
        self.text_input.setEnabled(enabled)
        self.toolbar.send_btn.setEnabled(enabled)
    
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()
    
    def dropEvent(self, event: QDropEvent):
        data_urls, file_paths = extract_images_from_mime(event.mimeData())
        self.add_attachments(data_urls + file_paths)
