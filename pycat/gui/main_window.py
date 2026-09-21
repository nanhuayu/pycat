"""
Main application window - Chinese UI with fixed streaming
"""

import logging
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QSplitter, QMenu, QToolButton, QStackedWidget, QSizePolicy
)
from PyQt6.QtCore import Qt, QSize, QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from typing import Optional

from pycat.models.conversation import Conversation
from pycat.models.provider import Provider
from pycat.core.app.container import AppContainer
from pycat.gui.runtime.message_runtime import MessageRuntime
from pycat.gui.runtime.channel_gateway_bridge import ChannelGatewayBridge
from pycat.gui.runtime.prompt_optimizer_runtime import PromptOptimizer
from pycat.gui.runtime.tray_controller import TrayController
from pycat.gui.runtime.screenshot_controller import ScreenshotController
from pycat.gui.dialogs.debug_trace_dialog import DebugTraceDialog

from .widgets.sidebar import Sidebar
from .widgets.chat_view import ChatView
from .widgets.input_area import InputArea
from .widgets.inspector_panel import InspectorPanel
from .presenters.knowledge_presenter import KnowledgePresenter
from .utils.icon_manager import Icons
from .utils.theme import prepare_context_menu
from .utils.window_geometry import MAIN_WINDOW_MINIMUM, MAIN_WINDOW_PREFERRED, apply_window_size
from pycat.core.content.export import CONVERSATION_FORMATS
from .shortcuts import shortcut_sequence, validate_shortcuts

from .presenters.conversation_presenter import ConversationPresenter
from .presenters.message_presenter import MessagePresenter
from .presenters.interaction_presenter import InteractionPresenter
from .presenters.shell_presenter import ShellPresenter
from .presenters.settings_presenter import SettingsPresenter
from .presenters.window_state_presenter import WindowStatePresenter

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Main application window"""
    app_state_changed = pyqtSignal()
    
    def __init__(self):
        super().__init__()

        # Centralized dependency container
        self.container = AppContainer(background_loop=True)
        self.services = self.container.services
        self.message_runtime = MessageRuntime(
            run_service=self.services.run_service,
            parent=self,
        )
        self.channel_gateway_bridge = ChannelGatewayBridge(self.services.channel_gateway, parent=self)
        
        self.providers: list[Provider] = []
        self.current_conversation: Optional[Conversation] = None
        self.app_settings: dict = {}
        self.is_syncing_input_selection: bool = False
        self._force_quit = False
        self._shutdown_started = False
        self._shutdown_complete = False
        self._shutdown_deadline_timer: QTimer | None = None
        self.window_state_presenter = WindowStatePresenter(self)
        self.app_state_changed.connect(self._on_app_state_store_changed, Qt.ConnectionType.QueuedConnection)
        self.unsubscribe_app_state = self.services.app_coordinator.store.subscribe(self.app_state_changed.emit)

        self.prompt_optimizer = PromptOptimizer(
            self.services.capability_executor,
            self.services.run_service,
            parent=self,
        )

        # Presenters — extract business logic out of this God Object
        self.conversation_presenter = ConversationPresenter(self)
        self.message_presenter = MessagePresenter(self)
        self.interaction_presenter = InteractionPresenter(self)
        self.shell_presenter = ShellPresenter(self)
        self.settings_presenter = SettingsPresenter(self)
        self.knowledge_presenter = KnowledgePresenter(self)
        self.tray_controller = TrayController(self)
        self.screenshot_controller = ScreenshotController(self)
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
        self.message_runtime.run_finished.connect(self.message_presenter.on_run_finished)
        self.message_runtime.retry_attempt.connect(self.message_presenter.on_retry_attempt)
        self.message_runtime.runtime_event.connect(self.message_presenter.on_runtime_event)
        self.message_runtime.conversation_patch.connect(self.message_presenter.on_conversation_patch)
        self.message_runtime.interactions_changed.connect(self.interaction_presenter.refresh)
        self.message_runtime.guidance_recovered.connect(self.message_presenter.on_guidance_recovered)
        self.channel_gateway_bridge.token_received.connect(self.message_presenter.on_token)
        self.channel_gateway_bridge.thinking_received.connect(self.message_presenter.on_thinking)
        self.channel_gateway_bridge.response_step.connect(self.message_presenter.on_response_step)
        self.channel_gateway_bridge.response_complete.connect(self.message_presenter.on_response_complete)
        self.channel_gateway_bridge.response_error.connect(self.message_presenter.on_response_error)
        self.channel_gateway_bridge.run_finished.connect(self.message_presenter.on_run_finished)
        self.channel_gateway_bridge.runtime_event.connect(self.message_presenter.on_runtime_event)
        self.channel_gateway_bridge.conversation_patch.connect(self.message_presenter.on_conversation_patch)
        self.channel_gateway_bridge.conversation_updated.connect(self.conversation_presenter.on_channel_gateway_event)
        
        self._setup_ui()
        self.chat_view.conversation_changed.connect(self.interaction_presenter.refresh)
        self._load_data()
        self.apply_shortcuts()
        self.settings_presenter.apply_theme()

    def _on_app_state_store_changed(self):
        if not self._shutdown_started:
            self.window_state_presenter.on_app_state_store_changed()
    
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
        self.sidebar.export_conversation.connect(self.conversation_presenter.export)
        self.sidebar.new_in_project.connect(lambda path: self.conversation_presenter.new(work_dir=path))
        self.sidebar.project_requested.connect(self.conversation_presenter.add_project)
        self.sidebar.navigation_requested.connect(self.conversation_presenter.update_navigation)
        self.sidebar.preferences_changed.connect(self.conversation_presenter.save_navigation_preferences)
        self.sidebar.settings_requested.connect(self.settings_presenter.open_settings)
        self.sidebar.materials_requested.connect(self.knowledge_presenter.show_library)
        self.sidebar.about_requested.connect(self.settings_presenter.show_about)
        self.sidebar.search_requested.connect(self._focus_conversation_search)
        
        # Chat area
        chat_widget = QWidget()
        chat_widget.setObjectName("chat_container")
        chat_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        chat_layout = QVBoxLayout(chat_widget)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)
        
        self.chat_view = ChatView(content_service=self.services.content_service)
        self.chat_view.reuse_message.connect(self.message_presenter.reuse)
        self.chat_view.edit_message.connect(self.message_presenter.edit)
        self.chat_view.regenerate_message.connect(self.message_presenter.regenerate)
        self.chat_view.delete_message.connect(self.message_presenter.delete_message)
        self.chat_view.continue_message.connect(self.message_presenter.resume_interrupted)
        self.chat_view.images_dropped.connect(self._on_images_dropped)
        self.chat_view.workspace_requested.connect(self.conversation_presenter.select_work_dir)
        self.chat_view.trace_requested.connect(self._open_debug_trace_dialog)
        self.chat_view.run_trace_requested.connect(self._open_debug_trace_dialog)
        
        self.input_area = InputArea(
            command_registry=self.services.command_registry,
            tool_schema_provider=self._get_input_available_tools,
        )
        self.input_area.message_sent.connect(self.message_presenter.send)
        self.input_area.cancel_requested.connect(self.message_presenter.cancel_current_generation)
        self.input_area.prompt_optimize_requested.connect(self.message_presenter.request_prompt_optimization)
        self.input_area.prompt_optimize_cancel_requested.connect(self.message_presenter.cancel_prompt_optimization)
        self.input_area.model_ref_changed.connect(self.conversation_presenter.update_model_ref)
        self.input_area.mode_changed.connect(self.conversation_presenter.update_mode)
        self.input_area.tool_approval_changed.connect(self.conversation_presenter.update_tool_approval)
        self.input_area.filesystem_mode_changed.connect(self.conversation_presenter.update_filesystem_mode)
        self.input_area.session_settings_requested.connect(self.conversation_presenter.open_settings)
        self.input_area.shell_requested.connect(self.shell_presenter.open)
        self.input_area.model_edit_requested.connect(self.settings_presenter.edit_current_model)
        self.input_area.slash_command_result.connect(self.conversation_presenter.handle_command_result)
        self.input_area.status_layout.insertWidget(0, self.chat_view.status_notice, 1)
        self.input_area.status_layout.setStretch(1, 0)
        self.chat_view.attach_requested.connect(self.input_area.toolbar.attach_requested)
        self.chat_view.image_edit_requested.connect(self.input_area.append_image_edit)
        self.chat_view.model_requested.connect(self.input_area.model_ref_combo.showPopup)

        # Vertical splitter: message area <-> input area (user-resizable)
        self.chat_splitter = QSplitter(Qt.Orientation.Vertical)
        self.chat_splitter.setObjectName("chat_splitter")
        self.chat_splitter.setChildrenCollapsible(False)
        self.chat_splitter.setHandleWidth(3)
        self.chat_splitter.addWidget(self.chat_view)
        self.chat_splitter.addWidget(self.input_area)
        self.chat_splitter.setSizes([520, 140])
        self.chat_splitter.splitterMoved.connect(self.settings_presenter.persist_chat_splitter_layout)

        chat_layout.addWidget(self.chat_splitter, stretch=1)

        self.inspector_panel = InspectorPanel()
        self.knowledge_presenter.bind(self.inspector_panel)
        self.inspector_panel.task_create_requested.connect(self.conversation_presenter.create_task)
        self.inspector_panel.task_complete_requested.connect(self.conversation_presenter.complete_task)
        self.inspector_panel.task_delete_requested.connect(self.conversation_presenter.delete_task)
        self.inspector_panel.process_stop_requested.connect(self.conversation_presenter.stop_process)
        self.inspector_panel.process_stop_all_requested.connect(self.conversation_presenter.stop_all_processes)
        self.inspector_panel.processes_refresh_requested.connect(self.conversation_presenter.refresh_processes)
        self.inspector_panel.process_open_requested.connect(self.shell_presenter.open)

        # Splitter
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setObjectName("main_splitter")
        self.splitter.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(chat_widget)
        self.splitter.addWidget(self.inspector_panel)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setChildrenCollapsible(False)

        self.splitter.setHandleWidth(1)
        self.splitter.setSizes([212, 780, 300])
        self.splitter.splitterMoved.connect(self.settings_presenter.persist_main_splitter_layout)

        self.workspace_stack = QStackedWidget()
        self.workspace_stack.addWidget(self.splitter)
        main_layout.addWidget(self.workspace_stack)
        self._create_header_actions()

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
            from pycat.models.contracts.tooling import ToolSelectionPolicy
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
    
    def _create_header_actions(self):
        self.menuBar().hide()

        corner_widget = QWidget(self.chat_view.header_bar)
        corner_widget.setObjectName("title_corner_controls")
        corner_widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        corner_layout = QHBoxLayout(corner_widget)
        corner_layout.setContentsMargins(0, 0, 6, 0)
        corner_layout.setSpacing(4)

        self.toggle_sidebar_btn = self._create_title_tool_button(
            Icons.PANEL_LEFT,
            "展开/折叠左侧会话栏",
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

        self.more_btn = self._create_title_tool_button(Icons.MORE, "更多操作")
        self.more_btn.setProperty("noMenuIndicator", True)
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        corner_layout.addWidget(self.more_btn)

        self.update_available_btn = self._create_title_tool_button(Icons.DOWNLOAD, "有新版本可用")
        self.update_available_btn.setIcon(Icons.get(Icons.DOWNLOAD, color=Icons.COLOR_PRIMARY, scale_factor=0.9))
        self.update_available_btn.clicked.connect(
            lambda _checked=False: self.settings_presenter.open_release_url()
        )
        self.update_available_btn.setVisible(False)
        corner_layout.addWidget(self.update_available_btn)
        self.chat_view.header_bar.layout().addWidget(corner_widget)

        self._shortcut_actions = {}
        # Actions with shortcuts belong to the window, independently of menu visibility.
        def action(label, callback, shortcut=""):
            item = QAction(label, self)
            item.triggered.connect(callback)
            if shortcut:
                item.setShortcut(shortcut_sequence(shortcut))
                self._shortcut_actions[shortcut] = item
            self.addAction(item)
            return item

        self.new_conversation_action = action("新建对话", self.conversation_presenter.new, "new_conversation")
        self.capture_action = action("截图...", self.screenshot_controller.start, "capture")
        self.capture_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.tray_controller.bind_actions(self.new_conversation_action, self.capture_action)
        self.import_conversation_action = action("导入 JSON...", self.sidebar.prompt_import_conversation, "import_conversation")
        action("设置...", self.settings_presenter.open_settings, "open_settings")
        self.cancel_action = action("停止运行 / 返回会话", self._cancel_or_return, "cancel_generation")

        menu = prepare_context_menu(QMenu(self), self)
        menu.aboutToShow.connect(lambda: prepare_context_menu(menu, self))
        self.conversation_settings_action = action("会话设置...", self.conversation_presenter.open_settings)
        menu.addAction(self.conversation_settings_action)
        compact = self.services.command_registry.get_menu_presentation("compact")
        self.compact_action = action(getattr(compact, "menu_label", "") or "压缩上下文", self.conversation_presenter.compact_current)
        self.compact_action.setToolTip(getattr(compact, "menu_tooltip", "") or "压缩上下文")
        menu.addAction(self.compact_action)
        export_menu = prepare_context_menu(QMenu("导出当前会话", self), self)
        self.export_actions = {}
        for fmt, (_, label) in CONVERSATION_FORMATS.items():
            item = export_menu.addAction(f"导出为 {label}...")
            item.triggered.connect(lambda checked=False, value=fmt: self.conversation_presenter.export_current(value))
            self.export_actions[fmt] = item
        menu.addMenu(export_menu)
        self.delete_conversation_action = action("删除当前会话", self.conversation_presenter.delete_current)
        menu.addAction(self.delete_conversation_action)
        menu.addSeparator()
        navigation = self.sidebar.management_menu()
        navigation.setTitle("项目与对话")
        menu.addMenu(navigation)

        def refresh_navigation():
            nonlocal navigation
            replacement = self.sidebar.management_menu()
            replacement.setTitle("项目与对话")
            menu.insertMenu(navigation.menuAction(), replacement)
            menu.removeAction(navigation.menuAction())
            navigation.deleteLater()
            navigation = replacement
        menu.aboutToShow.connect(refresh_navigation)
        menu.addAction(self.capture_action)
        self.shortcuts_action = action("快捷键...", self._show_shortcuts)
        menu.addAction(self.shortcuts_action)
        self.reset_layout_action = action("恢复默认布局", self.settings_presenter.reset_default_layout)
        menu.addAction(self.reset_layout_action)
        menu.addSeparator()
        menu.addAction(action("退出", self.request_quit, "quit"))
        self.more_btn.setMenu(menu)
        self.window_state_presenter.refresh_menu_action_states()
        self._search_shortcut = action("搜索对话或设置", self._focus_conversation_search, "search_conversations")

    def apply_shortcuts(self):
        try:
            overrides = validate_shortcuts(self.app_settings.get("shortcuts", {}))
        except ValueError as exc:
            overrides = {}
            self.chat_view.show_notice(f"快捷键配置冲突，暂用默认绑定：{exc}")
        for key, action in self._shortcut_actions.items():
            action.setShortcut(shortcut_sequence(key, overrides))
        self.screenshot_controller.bind_shortcut(shortcut_sequence('capture', overrides))
        for key, shortcut in self.chat_view._message_shortcuts.items():
            shortcut.setKey(shortcut_sequence(key, overrides))
        search_key = shortcut_sequence("search_conversations", overrides)
        self.sidebar.search_input.set_shortcut_text(search_key)
        self.sidebar.search_btn.setToolTip("搜索对话" + (f" · {search_key}" if search_key else ""))
        self.input_area.set_app_settings(self.app_settings)

    def _focus_conversation_search(self):
        settings = self.settings_presenter._settings_dialog
        if settings is not None:
            settings.search_input.setFocus()
            settings.search_input.selectAll()
            return
        self.settings_presenter.toggle_sidebar_panel(True)
        self.sidebar.search_input.setFocus()
        self.sidebar.search_input.selectAll()

    def _cancel_or_return(self):
        settings = self.settings_presenter._settings_dialog
        if settings is not None:
            settings.request_close()
        else:
            self.message_presenter.cancel_current_generation()

    def _create_title_tool_button(
        self,
        icon_name: str,
        tooltip: str,
        *,
        checkable: bool = False,
    ) -> QToolButton:
        button = QToolButton(self.chat_view.header_bar)
        button.setObjectName("title_tool_btn")
        button.setProperty("icon_name", icon_name)
        button.setIcon(Icons.get_muted(icon_name, scale_factor=0.9))
        button.setIconSize(QSize(18, 18))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setCheckable(bool(checkable))
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        button.setFixedSize(28, 28)
        return button

    def _open_debug_trace_dialog(self, request_ids=()) -> None:
        if not self.current_conversation:
            return
        request_ids = tuple(request_ids) if isinstance(request_ids, (tuple, list)) else ()
        existing = getattr(self, "_debug_trace_dialog", None)
        if existing is not None and existing.isVisible() and existing.target == (self.current_conversation.id, request_ids):
            existing.raise_()
            existing.activateWindow()
            return
        if existing is not None:
            existing.close()
            existing.deleteLater()
        try:
            dialog = DebugTraceDialog(self.current_conversation, self, request_ids=request_ids,
                                      state_provider=lambda: self.current_conversation, services=self.services,
                                      on_tool_finished=self._tool_retest_finished)
            self._debug_trace_dialog = dialog
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
        except Exception as exc:
            logger.debug("Failed to open debug trace dialog: %s", exc)

    def _tool_retest_finished(self, receipt, error):
        current = self.current_conversation
        if error is not None or receipt is None or current is None or current.id != receipt.conversation.id:
            return
        if self.services.conv_service.is_active(current.id):
            return  # A newer run owns the live projection now.
        latest = self.services.conv_service.load(current.id)
        if latest is None:
            return
        current.__dict__.update(latest.__dict__)
        self.services.app_coordinator.remember_current_conversation(current, providers=self.providers,
            app_settings=self.app_settings, is_streaming=False)
        self.inspector_panel.update_stats(current)
        self.conversation_presenter.refresh_processes()

    def _show_shortcuts(self) -> None:
        self.settings_presenter.open_settings(initial_page="shortcuts")
    
    def _load_data(self):
        bootstrap_state = self.services.app_bootstrap.load()
        self.window_state_presenter.apply_bootstrap_state(bootstrap_state)
        try:
            self.services.channel_gateway.start(
                self.services.channel_service.runtime_channels(self.app_settings)
            )
        except Exception as e:
            logger.debug("Failed to start channel gateway during bootstrap: %s", e)
        self._update_check_timer = QTimer(self)
        self._update_check_timer.setSingleShot(True)
        self._update_check_timer.timeout.connect(lambda: self.settings_presenter.check_for_updates(manual=False))
        self._update_check_timer.start(2500)

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
        self.capture_action.setEnabled(False)
        self.screenshot_controller.dispose()
        self._update_check_timer.stop()
        self.knowledge_presenter.dispose()
        self.settings_presenter.abandon_release_job()
        self.interaction_presenter.dispose()
        self.shell_presenter.dispose()
        for conversation_id in self.message_runtime.streaming_conversation_ids():
            self.message_runtime.cancel(conversation_id)
        # Background GUI jobs cannot be forcefully interrupted, but their
        # callbacks must not target a window that is closing.
        try:
            self.conversation_presenter.abandon_background_jobs()
        except Exception as exc:
            logger.debug("Failed to abandon GUI background jobs: %s", exc)
        try:
            self.message_presenter.abandon_background_jobs()
        except Exception as exc:
            logger.debug("Failed to abandon message background jobs: %s", exc)
        try:
            self.channel_gateway_bridge.dispose()
        except Exception as e:
            logger.debug("Failed to dispose channel gateway bridge: %s", e)
        self.window_state_presenter.shutdown(self._finish_shutdown)
        self._shutdown_deadline_timer = QTimer(self)
        self._shutdown_deadline_timer.setSingleShot(True)
        self._shutdown_deadline_timer.timeout.connect(self._finish_shutdown_timeout)
        self._shutdown_deadline_timer.start(8000)

    def _finish_shutdown(self, error: str | None = None) -> None:
        if self._shutdown_complete:
            return
        if error:
            logger.debug("Application shutdown completed with errors: %s", error)
        if self._shutdown_deadline_timer is not None:
            self._shutdown_deadline_timer.stop()
        self.tray_controller.dispose()
        self._shutdown_complete = True
        self._force_quit = True
        QTimer.singleShot(0, self.close)

    def _finish_shutdown_timeout(self) -> None:
        if self._shutdown_complete:
            return
        logger.warning("Application shutdown exceeded 8s; closing the GUI without waiting for workers")
        try:
            self.window_state_presenter.abandon_shutdown()
        except Exception as exc:
            logger.debug("Failed to abandon shutdown callback: %s", exc)
        self._finish_shutdown("shutdown deadline exceeded")

    def closeEvent(self, event) -> None:
        if not self._shutdown_started and not self.settings_presenter.allow_window_close():
            event.ignore()
            return
        if not self._shutdown_started and not self.knowledge_presenter.allow_context_change(self.close):
            event.ignore()
            return
        self._persist_window_size()
        if self.tray_controller.handle_close_event(event, force_quit=self._force_quit):
            return
        if not self._shutdown_complete:
            self._shutdown_once()
            event.ignore()
            return
        super().closeEvent(event)
