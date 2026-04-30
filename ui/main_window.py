"""
Main application window - Chinese UI with fixed streaming
"""

import logging
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QMenu
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
from typing import Optional

from models.conversation import Conversation
from models.provider import Provider
from core.channel.runtime import ChannelRuntimeEvent
from core.container import AppContainer
from ui.runtime.message_runtime import MessageRuntime
from ui.runtime.channel_runtime_bridge import ChannelRuntimeBridge
from ui.runtime.prompt_optimizer_runtime import PromptOptimizer

from .widgets.sidebar import Sidebar
from .widgets.chat_view import ChatView
from .widgets.input_area import InputArea
from .widgets.stats_panel import StatsPanel

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
        self.services = AppContainer().services
        self.message_runtime = MessageRuntime(
            self.services.client,
            tool_manager=self.services.tool_manager,
            turn_engine=self.services.turn_engine,
            parent=self,
        )
        self.channel_runtime_bridge = ChannelRuntimeBridge(self.services.channel_runtime, parent=self)
        
        self.providers: list[Provider] = []
        self.current_conversation: Optional[Conversation] = None
        self.app_settings: dict = {}
        self.is_syncing_input_selection: bool = False
        self.window_state_presenter = WindowStatePresenter(self)
        self.unsubscribe_app_state = self.services.app_coordinator.store.subscribe(
            self.window_state_presenter.on_app_state_store_changed
        )

        self.prompt_optimizer = PromptOptimizer(self.services.client, parent=self)

        # Presenters — extract business logic out of this God Object
        self.conversation_presenter = ConversationPresenter(self)
        self.message_presenter = MessagePresenter(self)
        self.settings_presenter = SettingsPresenter(self)

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
        self.channel_runtime_bridge.token_received.connect(self.message_presenter.on_token)
        self.channel_runtime_bridge.thinking_received.connect(self.message_presenter.on_thinking)
        self.channel_runtime_bridge.response_step.connect(self.message_presenter.on_response_step)
        self.channel_runtime_bridge.response_complete.connect(self.message_presenter.on_response_complete)
        self.channel_runtime_bridge.response_error.connect(self.message_presenter.on_response_error)
        self.channel_runtime_bridge.runtime_event.connect(self.message_presenter.on_runtime_event)
        self.channel_runtime_bridge.conversation_updated.connect(self._on_channel_runtime_event)
        
        self._setup_ui()
        self._load_data()
        self.settings_presenter.apply_theme()
    
    def _setup_ui(self):
        self.setWindowTitle("PyCat Agent | LLM chat / agent / tools")
        self.setMinimumSize(1000, 600)
        
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
        self.chat_view.images_dropped.connect(self._on_images_dropped)
        self.chat_view.work_dir_changed.connect(self.conversation_presenter.update_work_dir)
        self.chat_view.model_ref_changed.connect(self.conversation_presenter.update_model_ref)
        
        self.input_area = InputArea(
            command_registry=self.services.command_registry,
            tool_schema_provider=self._get_input_available_tools,
        )
        self.input_area.message_sent.connect(self.message_presenter.send)
        self.input_area.cancel_requested.connect(self.message_presenter.cancel_current_generation)
        self.input_area.conversation_settings_requested.connect(self.conversation_presenter.open_settings)
        self.input_area.provider_settings_requested.connect(self.settings_presenter.open_provider_settings)
        self.input_area.show_thinking_changed.connect(self.conversation_presenter.update_show_thinking)
        self.input_area.mcp_toggled.connect(self.conversation_presenter.update_mcp)
        self.input_area.search_toggled.connect(self.conversation_presenter.update_search)
        self.input_area.prompt_optimize_requested.connect(self.message_presenter.request_prompt_optimization)
        self.input_area.prompt_optimize_cancel_requested.connect(self.message_presenter.cancel_prompt_optimization)
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
        self.chat_splitter.setSizes([720, 180])
        self.chat_splitter.splitterMoved.connect(self.settings_presenter.persist_chat_splitter_layout)

        chat_layout.addWidget(self.chat_splitter, stretch=1)

        # Stats panel
        self.stats_panel = StatsPanel()
        self.stats_panel.task_create_requested.connect(self.conversation_presenter.create_task)
        self.stats_panel.task_complete_requested.connect(self.conversation_presenter.complete_task)
        self.stats_panel.task_delete_requested.connect(self.conversation_presenter.delete_task)

        # Splitter
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("main_splitter")
        self.splitter.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(chat_widget)
        self.splitter.addWidget(self.stats_panel)
        self.splitter.setChildrenCollapsible(False)

        self.splitter.setHandleWidth(8)
        self.splitter.setSizes([180, 760, 200])
        self.splitter.splitterMoved.connect(self.settings_presenter.persist_main_splitter_layout)

        main_layout.addWidget(self.splitter)
        self._create_menu_bar()

    def _get_input_available_tools(self) -> list[dict]:
        try:
            mode_slug = self.input_area.get_selected_mode_slug()
            mode = self.input_area.get_mode_manager().get(mode_slug)
            return self.services.tool_manager.registry.get_all_tool_schemas(
                allowed_groups=mode.group_names(),
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
        compact_presentation = self.services.command_registry.get_menu_presentation("compact")
        clear_presentation = self.services.command_registry.get_menu_presentation("clear")
        
        file_menu = menubar.addMenu("文件")
        conversation_menu = menubar.addMenu("会话")
        
        new_action = QAction("新建会话", self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(self.conversation_presenter.new)
        file_menu.addAction(new_action)
        
        import_action = QAction("导入 JSON...", self)
        import_action.setShortcut("Ctrl+I")
        import_action.triggered.connect(self.sidebar.prompt_import_conversation)
        file_menu.addAction(import_action)

        export_menu = QMenu("导出当前会话", self)
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
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self.settings_presenter.open_settings)
        file_menu.addAction(settings_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("退出", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
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

        self.provider_settings_action = QAction("服务商设置...", self)
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
        self.cancel_action.setShortcut("Escape")
        self.cancel_action.triggered.connect(self.message_presenter.cancel_current_generation)
        edit_menu.addAction(self.cancel_action)
        
        view_menu = menubar.addMenu("视图")
        
        self.toggle_stats_action = QAction("显示统计", self)
        self.toggle_stats_action.setCheckable(True)
        self.toggle_stats_action.setChecked(True)
        self.toggle_stats_action.triggered.connect(self.settings_presenter.toggle_stats_panel)
        view_menu.addAction(self.toggle_stats_action)
        
        help_menu = menubar.addMenu("帮助")
        
        about_action = QAction("关于 PyCat Agent", self)
        about_action.triggered.connect(self.settings_presenter.show_about)
        help_menu.addAction(about_action)

        self.window_state_presenter.refresh_menu_action_states()
    
    def _load_data(self):
        bootstrap_state = self.services.app_bootstrap.load()
        self.window_state_presenter.apply_bootstrap_state(bootstrap_state)
        try:
            self.services.channel_runtime.start(self.app_settings)
        except Exception as e:
            logger.debug("Failed to start channel runtime during bootstrap: %s", e)

    def _on_channel_runtime_event(self, event: ChannelRuntimeEvent) -> None:
        try:
            conversations = self.services.conv_service.list_all()
            self.sidebar.update_conversations(conversations)
            self.services.app_coordinator.sync_catalog(
                providers=self.providers,
                conversation_count=len(conversations),
            )
        except Exception as exc:
            logger.debug("Failed to refresh sidebar from channel runtime event: %s", exc)

        conversation_id = str(getattr(event, "conversation_id", "") or "").strip()
        if not conversation_id:
            return

        current = self.current_conversation
        if current is not None and str(getattr(current, "id", "") or "") == conversation_id:
            try:
                self.conversation_presenter.select(conversation_id)
            except Exception as exc:
                logger.debug("Failed to refresh current conversation from channel runtime event: %s", exc)
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

    def closeEvent(self, event) -> None:
        try:
            self.channel_runtime_bridge.dispose()
        except Exception as e:
            logger.debug("Failed to dispose channel runtime bridge: %s", e)
        self.window_state_presenter.shutdown()
        super().closeEvent(event)
