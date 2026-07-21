"""
Main application window - Chinese UI with fixed streaming
"""

import logging
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QMenu, QToolButton
)
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QAction
from typing import Optional

from models.conversation import Conversation
from models.provider import Provider
from core.channel.events import ChannelEvent
from core.app.container import AppContainer
from gui.runtime.message_runtime import MessageRuntime
from gui.runtime.channel_gateway_bridge import ChannelGatewayBridge
from gui.runtime.prompt_optimizer_runtime import PromptOptimizer
from gui.runtime.tray_controller import TrayController

from .widgets.sidebar import Sidebar
from .widgets.chat_view import ChatView
from .widgets.input_area import InputArea
from .widgets.inspector_panel import InspectorPanel
from .utils.icon_manager import Icons
from .utils.window_geometry import MAIN_WINDOW_MINIMUM, MAIN_WINDOW_PREFERRED, apply_window_size
from .about_content import PRODUCT_NAME
from .shortcuts import shortcut_sequence

from .presenters.conversation_presenter import ConversationPresenter
from .presenters.message_presenter import MessagePresenter
from .presenters.settings_presenter import SettingsPresenter
from .presenters.window_state_presenter import WindowStatePresenter

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Main application window"""
    
    def __init__(self):
        super().__init__()

        # Centralized dependency container
        self.container = AppContainer()
        self.services = self.container.services
        self.message_runtime = MessageRuntime(
            self.services.client,
            agent_runtime=self.services.agent_runtime,
            parent=self,
        )
        self.channel_gateway_bridge = ChannelGatewayBridge(self.services.channel_gateway, parent=self)
        
        self.providers: list[Provider] = []
        self.current_conversation: Optional[Conversation] = None
        self.app_settings: dict = {}
        self.is_syncing_input_selection: bool = False
        self._force_quit = False
        self._shutdown_started = False
        self.window_state_presenter = WindowStatePresenter(self)
        self.unsubscribe_app_state = self.services.app_coordinator.store.subscribe(
            self.window_state_presenter.on_app_state_store_changed
        )

        self.prompt_optimizer = PromptOptimizer(
            self.services.client,
            self.services.prompt_renderer,
            parent=self,
        )

        # Presenters — extract business logic out of this God Object
        self.conversation_presenter = ConversationPresenter(self)
        self.message_presenter = MessagePresenter(self)
        self.settings_presenter = SettingsPresenter(self)
        self.tray_controller = TrayController(self)
        self.tray_controller.quit_requested.connect(self.request_quit)

        self.prompt_optimizer.optimize_started.connect(self.message_presenter.on_prompt_optimize_started)
        self.prompt_optimizer.optimize_complete.connect(self.message_presenter.on_prompt_optimize_complete)
        self.prompt_optimizer.optimize_error.connect(self.message_presenter.on_prompt_optimize_error)
        self.prompt_optimizer.optimize_cancelled.connect(self.message_presenter.on_prompt_optimize_cancelled)

        # Streaming events (thread-safe; runtime normalizes + guards request_id)
        self.message_runtime.token_received.connect(self.message_presenter.on_token)
        self.message_runtime.thinking_received.connect(self.message_presenter.on_thinking)
        self.message_runtime.response_step.connect(self.message_presenter.on_response_step)
        self.message_runtime.response_complete.connect(self.message_presenter.on_response_complete)
        self.message_runtime.response_error.connect(self.message_presenter.on_response_error)
        self.message_runtime.retry_attempt.connect(self.message_presenter.on_retry_attempt)
        self.message_runtime.runtime_event.connect(self.message_presenter.on_runtime_event)
        self.message_runtime.conversation_patch.connect(self.message_presenter.on_conversation_patch)
        self.channel_gateway_bridge.token_received.connect(self.message_presenter.on_token)
        self.channel_gateway_bridge.thinking_received.connect(self.message_presenter.on_thinking)
        self.channel_gateway_bridge.response_step.connect(self.message_presenter.on_response_step)
        self.channel_gateway_bridge.response_complete.connect(self.message_presenter.on_response_complete)
        self.channel_gateway_bridge.response_error.connect(self.message_presenter.on_response_error)
        self.channel_gateway_bridge.runtime_event.connect(self.message_presenter.on_runtime_event)
        self.channel_gateway_bridge.conversation_updated.connect(self._on_channel_gateway_event)
        
        self._setup_ui()
        self._load_data()
        self.settings_presenter.apply_theme()
    
    def _setup_ui(self):
        self.setWindowTitle("PyCat | LLM chat · agent · tools")
        self.setMinimumSize(*MAIN_WINDOW_MINIMUM)
        self.apply_window_size()
        
        central = QWidget()
        central.setObjectName("central_widget")
        central.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCentralWidget(central)
        
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Sidebar
        self.sidebar = Sidebar()
        self.sidebar.conversation_selected.connect(self.conversation_presenter.select)
        self.sidebar.new_conversation.connect(self.conversation_presenter.new)
        self.sidebar.import_conversation.connect(self.conversation_presenter.import_from_file)
        self.sidebar.delete_conversation.connect(self.conversation_presenter.delete)
        self.sidebar.duplicate_conversation.connect(self.conversation_presenter.duplicate)
        self.sidebar.export_conversation.connect(self.conversation_presenter.export)
        
        # Chat area
        chat_widget = QWidget()
        chat_widget.setObjectName("chat_container")
        chat_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        chat_layout = QVBoxLayout(chat_widget)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)
        
        self.chat_view = ChatView()
        self.chat_view.edit_message.connect(self.message_presenter.edit)
        self.chat_view.delete_message.connect(self.message_presenter.delete)
        self.chat_view.continue_message.connect(self.message_presenter.resume_interrupted)
        self.chat_view.images_dropped.connect(self._on_images_dropped)
        self.chat_view.work_dir_changed.connect(self.conversation_presenter.update_work_dir)
        
        self.input_area = InputArea(
            command_registry=self.services.command_registry,
            tool_schema_provider=self._get_input_available_tools,
        )
        self.input_area.message_sent.connect(self.message_presenter.send)
        self.input_area.cancel_requested.connect(self.message_presenter.cancel_current_generation)
        self.input_area.conversation_settings_requested.connect(self.conversation_presenter.open_settings)
        self.input_area.model_edit_requested.connect(self.settings_presenter.edit_current_model)
        self.input_area.show_thinking_changed.connect(self.conversation_presenter.update_show_thinking)
        self.input_area.prompt_optimize_requested.connect(self.message_presenter.request_prompt_optimization)
        self.input_area.prompt_optimize_cancel_requested.connect(self.message_presenter.cancel_prompt_optimization)
        self.input_area.model_ref_changed.connect(self.conversation_presenter.update_model_ref)
        self.input_area.provider_model_changed.connect(self.conversation_presenter.update_provider_model)
        self.input_area.mode_changed.connect(self.conversation_presenter.update_mode)
        self.input_area.slash_command_result.connect(self.conversation_presenter.handle_command_result)

        # Vertical splitter: message area <-> input area (user-resizable)
        self.chat_splitter = QSplitter(Qt.Orientation.Vertical)
        self.chat_splitter.setObjectName("chat_splitter")
        self.chat_splitter.setChildrenCollapsible(False)
        self.chat_splitter.setHandleWidth(8)
        self.chat_splitter.addWidget(self.chat_view)
        self.chat_splitter.addWidget(self.input_area)
        self.chat_splitter.setSizes([520, 140])
        self.chat_splitter.splitterMoved.connect(self.settings_presenter.persist_chat_splitter_layout)

        chat_layout.addWidget(self.chat_splitter, stretch=1)

        # Stats panel
        self.inspector_panel = InspectorPanel()
        self.inspector_panel.task_create_requested.connect(self.conversation_presenter.create_task)
        self.inspector_panel.task_complete_requested.connect(self.conversation_presenter.complete_task)
        self.inspector_panel.task_delete_requested.connect(self.conversation_presenter.delete_task)
        self.inspector_panel.memory_candidate_promote_requested.connect(
            self.conversation_presenter.promote_memory_candidate
        )
        self.inspector_panel.memory_candidate_reject_requested.connect(
            self.conversation_presenter.reject_memory_candidate
        )
        self.inspector_panel.debug_trace_requested.connect(self._open_debug_trace_dialog)

        # Splitter
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("main_splitter")
        self.splitter.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(chat_widget)
        self.splitter.addWidget(self.inspector_panel)
        self.splitter.setChildrenCollapsible(False)

        self.splitter.setHandleWidth(8)
        self.splitter.setSizes([180, 700, 200])
        self.splitter.splitterMoved.connect(self.settings_presenter.persist_main_splitter_layout)

        main_layout.addWidget(self.splitter)
        self._create_menu_bar()

    def apply_window_size(self, size: object = None) -> None:
        values = list(size) if isinstance(size, (list, tuple)) else []
        preferred_width = int(values[0]) if len(values) >= 2 else MAIN_WINDOW_PREFERRED[0]
        preferred_height = int(values[1]) if len(values) >= 2 else MAIN_WINDOW_PREFERRED[1]
        apply_window_size(
            self,
            preferred=(preferred_width, preferred_height),
            minimum=MAIN_WINDOW_MINIMUM,
            screen_margin=32,
        )

    def _get_input_available_tools(self) -> list[dict]:
        try:
            mode_slug = self.input_area.get_selected_mode_slug()
            mode = self.input_area.get_mode_manager().get(mode_slug)
            from models.contracts.tooling import ToolSelectionPolicy
            return self.services.tool_manager.registry.get_all_tool_schemas(
                tool_selection=ToolSelectionPolicy.from_categories(mode.tool_category_names()),
            )
        except Exception as e:
            logger.debug("Failed to get tool schemas for input: %s", e)
            return []

    def _on_images_dropped(self, image_sources: list) -> None:
        # Forward images dropped onto the chat area into the input area's attachments.
        try:
            self.input_area.add_attachments(image_sources)
        except Exception as e:
            logger.warning("Failed to add dropped images: %s", e)
    
    def _create_menu_bar(self):
        menubar = self.menuBar()
        menubar.clear()
        menubar.setNativeMenuBar(False)

        corner_widget = QWidget(menubar)
        corner_widget.setObjectName("title_corner_controls")
        corner_layout = QHBoxLayout(corner_widget)
        corner_layout.setContentsMargins(0, 0, 6, 0)
        corner_layout.setSpacing(4)

        self.toggle_sidebar_btn = self._create_title_tool_button(
            Icons.PANEL_LEFT,
            "显示/隐藏左侧会话栏",
            checkable=True,
        )
        self.toggle_sidebar_btn.clicked.connect(self.settings_presenter.toggle_sidebar_panel)
        corner_layout.addWidget(self.toggle_sidebar_btn)

        self.toggle_inspector_btn = self._create_title_tool_button(
            Icons.PANEL_RIGHT,
            "显示/隐藏右侧辅助栏",
            checkable=True,
        )
        self.toggle_inspector_btn.clicked.connect(self.settings_presenter.toggle_inspector_panel)
        corner_layout.addWidget(self.toggle_inspector_btn)

        self.open_debug_trace_btn = self._create_title_tool_button(
            Icons.NETWORK,
            "查看调用链路",
        )
        self.open_debug_trace_btn.clicked.connect(self._open_debug_trace_dialog)
        corner_layout.addWidget(self.open_debug_trace_btn)

        settings_btn = self._create_title_tool_button(Icons.SETTINGS, "打开设置")
        settings_btn.clicked.connect(self.settings_presenter.open_settings)
        corner_layout.addWidget(settings_btn)
        menubar.setCornerWidget(corner_widget, Qt.Corner.TopRightCorner)

        compact_presentation = self.services.command_registry.get_menu_presentation("compact")
        clear_presentation = self.services.command_registry.get_menu_presentation("clear")
        
        file_menu = menubar.addMenu("文件")
        conversation_menu = menubar.addMenu("会话")
        
        new_action = QAction("新建会话", self)
        new_action.setShortcut(shortcut_sequence("new_conversation"))
        new_action.triggered.connect(self.conversation_presenter.new)
        file_menu.addAction(new_action)
        
        import_action = QAction("导入 JSON...", self)
        import_action.setShortcut(shortcut_sequence("import_conversation"))
        import_action.triggered.connect(self.sidebar.prompt_import_conversation)
        file_menu.addAction(import_action)

        export_menu = QMenu("导出当前会话", self)
        export_menu.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.export_markdown_action = QAction("导出为 Markdown...", self)
        self.export_markdown_action.triggered.connect(
            lambda: self.conversation_presenter.export_current("markdown")
        )
        export_menu.addAction(self.export_markdown_action)

        self.export_json_action = QAction("导出为 JSON...", self)
        self.export_json_action.triggered.connect(
            lambda: self.conversation_presenter.export_current("json")
        )
        export_menu.addAction(self.export_json_action)
        file_menu.addMenu(export_menu)
        
        file_menu.addSeparator()
        
        settings_action = QAction("设置...", self)
        settings_action.setShortcut(shortcut_sequence("open_settings"))
        settings_action.triggered.connect(self.settings_presenter.open_settings)
        file_menu.addAction(settings_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("退出", self)
        exit_action.setShortcut(shortcut_sequence("quit"))
        exit_action.triggered.connect(self.request_quit)
        file_menu.addAction(exit_action)
        
        self.duplicate_conversation_action = QAction("复制当前会话", self)
        self.duplicate_conversation_action.triggered.connect(self.conversation_presenter.duplicate_current)
        conversation_menu.addAction(self.duplicate_conversation_action)

        self.delete_conversation_action = QAction("删除当前会话", self)
        self.delete_conversation_action.triggered.connect(self.conversation_presenter.delete_current)
        conversation_menu.addAction(self.delete_conversation_action)

        conversation_menu.addSeparator()

        clear_label = getattr(clear_presentation, "menu_label", "") or "清空并新建会话"
        self.clear_conversation_action = QAction(clear_label, self)
        self.clear_conversation_action.triggered.connect(self.conversation_presenter.new)
        clear_tip = getattr(clear_presentation, "menu_tooltip", "") or clear_label
        self.clear_conversation_action.setToolTip(clear_tip)
        self.clear_conversation_action.setStatusTip(clear_tip)
        conversation_menu.addAction(self.clear_conversation_action)

        self.conversation_settings_action = QAction("会话设置...", self)
        self.conversation_settings_action.triggered.connect(self.conversation_presenter.open_settings)
        conversation_menu.addAction(self.conversation_settings_action)

        self.provider_settings_action = QAction("模型...", self)
        self.provider_settings_action.triggered.connect(self.settings_presenter.open_provider_settings)
        conversation_menu.addAction(self.provider_settings_action)

        compact_label = getattr(compact_presentation, "menu_label", "") or "压缩上下文"
        self.compact_action = QAction(compact_label, self)
        self.compact_action.triggered.connect(self.conversation_presenter.compact_current)
        compact_tip = getattr(compact_presentation, "menu_tooltip", "") or compact_label
        self.compact_action.setToolTip(compact_tip)
        self.compact_action.setStatusTip(compact_tip)
        conversation_menu.addAction(self.compact_action)

        edit_menu = menubar.addMenu("编辑")
        
        self.cancel_action = QAction("取消生成", self)
        self.cancel_action.setShortcut(shortcut_sequence("cancel_generation"))
        self.cancel_action.triggered.connect(self.message_presenter.cancel_current_generation)
        edit_menu.addAction(self.cancel_action)
        
        view_menu = menubar.addMenu("视图")

        self.toggle_sidebar_action = QAction("显示会话栏", self)
        self.toggle_sidebar_action.setCheckable(True)
        self.toggle_sidebar_action.setChecked(True)
        self.toggle_sidebar_action.triggered.connect(self.settings_presenter.toggle_sidebar_panel)
        view_menu.addAction(self.toggle_sidebar_action)
        
        self.toggle_inspector_action = QAction("显示辅助栏", self)
        self.toggle_inspector_action.setCheckable(True)
        self.toggle_inspector_action.setChecked(True)
        self.toggle_inspector_action.triggered.connect(self.settings_presenter.toggle_inspector_panel)
        view_menu.addAction(self.toggle_inspector_action)

        view_menu.addSeparator()

        self.reset_layout_action = QAction("恢复默认布局", self)
        self.reset_layout_action.triggered.connect(self.settings_presenter.reset_default_layout)
        view_menu.addAction(self.reset_layout_action)

        view_menu.addSeparator()

        self.debug_trace_action = QAction("查看调用链路", self)
        self.debug_trace_action.triggered.connect(self._open_debug_trace_dialog)
        view_menu.addAction(self.debug_trace_action)
        
        help_menu = menubar.addMenu("帮助")

        self.shortcuts_action = QAction("快捷键...", self)
        self.shortcuts_action.triggered.connect(self._show_shortcuts)
        help_menu.addAction(self.shortcuts_action)
        help_menu.addSeparator()

        about_action = QAction(f"关于 {PRODUCT_NAME}", self)
        about_action.triggered.connect(self.settings_presenter.show_about)
        help_menu.addAction(about_action)

        self.window_state_presenter.refresh_menu_action_states()

    def _create_title_tool_button(
        self,
        icon_name: str,
        tooltip: str,
        *,
        checkable: bool = False,
    ) -> QToolButton:
        button = QToolButton(self.menuBar())
        button.setObjectName("title_tool_btn")
        button.setIcon(Icons.get_muted(icon_name, scale_factor=0.9))
        button.setIconSize(QSize(18, 18))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tooltip)
        button.setCheckable(bool(checkable))
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setFixedSize(28, 28)
        return button

    def _open_debug_trace_dialog(self) -> None:
        if not self.current_conversation:
            return
        try:
            from gui.dialogs.debug_trace_dialog import DebugTraceDialog

            dialog = DebugTraceDialog(self.current_conversation, self)
            self._debug_trace_dialog = dialog
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
        except Exception as exc:
            logger.debug("Failed to open debug trace dialog: %s", exc)

    def _show_shortcuts(self) -> None:
        from gui.dialogs.shortcut_help_dialog import ShortcutHelpDialog

        ShortcutHelpDialog(self).exec()
    
    def _load_data(self):
        bootstrap_state = self.services.app_bootstrap.load()
        self.window_state_presenter.apply_bootstrap_state(bootstrap_state)
        try:
            self.services.channel_gateway.start(
                self.services.channel_service.runtime_channels(self.app_settings)
            )
        except Exception as e:
            logger.debug("Failed to start channel gateway during bootstrap: %s", e)

    def _on_channel_gateway_event(self, event: ChannelEvent) -> None:
        try:
            conversations = self.services.conv_service.list_all()
            self.sidebar.update_conversations(conversations)
            self.services.app_coordinator.sync_catalog(
                providers=self.providers,
                conversation_count=len(conversations),
            )
        except Exception as exc:
            logger.debug("Failed to refresh sidebar from channel gateway event: %s", exc)

        conversation_id = str(getattr(event, "conversation_id", "") or "").strip()
        if not conversation_id:
            return

        current = self.current_conversation
        if current is not None and str(getattr(current, "id", "") or "") == conversation_id:
            try:
                self.conversation_presenter.select(conversation_id)
            except Exception as exc:
                logger.debug("Failed to refresh current conversation from channel gateway event: %s", exc)
            return

        if not bool(getattr(event, "focus_requested", False)):
            return

        settings = getattr(current, "settings", {}) or {} if current is not None else {}
        binding = settings.get("channel_binding") if isinstance(settings, dict) else None
        current_channel_id = str((binding or {}).get("channel_id", "") if isinstance(binding, dict) else "").strip()
        current_is_manual = bool((binding or {}).get("manual_test_session", False)) if isinstance(binding, dict) else False
        target_channel_id = str(getattr(event, "channel_id", "") or "").strip()

        should_focus = current is None or (current_is_manual and current_channel_id == target_channel_id)
        if not should_focus:
            return

        try:
            self.sidebar.select_conversation(conversation_id)
            self.conversation_presenter.select(conversation_id)
        except Exception as exc:
            logger.debug("Failed to focus channel conversation from runtime event: %s", exc)

    def request_quit(self) -> None:
        self._force_quit = True
        self.close()

    def _persist_window_size(self) -> None:
        if not self.isMaximized() and not self.isFullScreen():
            try:
                self.app_settings["main_window_size"] = [int(self.width()), int(self.height())]
                self.services.app_settings_service.save(self.app_settings)
            except Exception as exc:
                logger.debug("Failed to persist main window size: %s", exc)

    def _shutdown_once(self) -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self.tray_controller.dispose()
        try:
            self.channel_gateway_bridge.dispose()
        except Exception as e:
            logger.debug("Failed to dispose channel gateway bridge: %s", e)
        self.window_state_presenter.shutdown()

    def closeEvent(self, event) -> None:
        self._persist_window_size()
        if self.tray_controller.handle_close_event(event, force_quit=self._force_quit):
            return
        self._shutdown_once()
        super().closeEvent(event)
