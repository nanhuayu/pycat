from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Dict

from PyQt6.QtCore import QCoreApplication, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from pycat.core.app.services.channel import ChannelService
from pycat.core.channel.connection import (
    ChannelConnectionSnapshot,
    ChannelConnectionState,
    ChannelRequiredAction,
)
from pycat.core.modes.manager import ModeManager
from pycat.gui.dialogs.channel_session_picker_dialog import ChannelSessionPickerDialog
from pycat.gui.settings.components import build_dialog_button_box
from pycat.gui.utils.combo_box import configure_combo_popup
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.qr_code import build_qr_code_pixmap
from pycat.gui.utils.settings_controls import SettingsFormLayout
from pycat.gui.view_models.channel_status import (
    channel_detail_label,
    channel_metadata_text,
    channel_state_label,
    channel_type_name,
)
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit, ThemedSelectableLabel
from pycat.gui.widgets.tool_category_selector import ToolCategorySelector
from pycat.models.contracts.channel import ChannelConfig


class ChannelInstanceDialog(QDialog):
    _login_result = pyqtSignal(str, object)
    _login_error = pyqtSignal(str, str)

    def __init__(
        self,
        *,
        channel: ChannelConfig,
        channel_service: ChannelService,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._service = channel_service
        self._catalog = channel_service.catalog
        self._channel = self._catalog.ensure_channel(channel)
        self._definition = self._catalog.get_definition(self._channel.type)
        self._dynamic_inputs: Dict[str, QLineEdit] = {}
        self._preferred_session_id = ""
        self._loading = False
        self._login_session = None
        self._login_request_id = ""
        self._login_request_running = False
        self._poll_timer = QTimer(self)
        self._poll_timer.setSingleShot(True)
        self._poll_timer.timeout.connect(self._poll_login)
        self._login_result.connect(self._on_login_result)
        self._login_error.connect(self._on_login_error)

        self.setWindowTitle(QCoreApplication.translate('ChannelInstanceDialog', '编辑 {value}').format(value=channel_type_name(self._definition)))
        self.resize(650, 750)
        self._setup_ui()
        self._load_channel(self._channel)
        QTimer.singleShot(0, self._begin_login_if_needed)

    @property
    def channel(self) -> ChannelConfig:
        return self._channel

    @property
    def preferred_session_id(self) -> str:
        return str(self._preferred_session_id or "").strip()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(Icons.get(self._definition.icon_name, scale_factor=1.1).pixmap(24, 24))
        icon.setFixedSize(28, 28)
        header.addWidget(icon)
        heading = QVBoxLayout()
        title = QLabel(channel_type_name(self._definition))
        title.setProperty("heading", True)
        heading.addWidget(title)
        subtitle = QLabel(QCoreApplication.translate('ChannelInstanceDialog', '配置连接；主对话会在完成后自动创建'))
        subtitle.setProperty("muted", True)
        heading.addWidget(subtitle)
        header.addLayout(heading, 1)
        layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)
        scroll.setWidget(content)

        base_group = QGroupBox(QCoreApplication.translate('ChannelInstanceDialog', '基础'))
        base_form = SettingsFormLayout(base_group)
        base_form.setContentsMargins(12, 12, 12, 12)
        base_form.setSpacing(8)
        base_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.name_input = ThemedLineEdit()
        self.name_input.setPlaceholderText(QCoreApplication.translate('ChannelInstanceDialog', '连接名称'))
        base_form.addRow(QCoreApplication.translate('ChannelInstanceDialog', '名称'), self.name_input)
        self.agent_mode_combo = QComboBox()
        configure_combo_popup(self.agent_mode_combo)
        for mode in ModeManager(None, include_project=False).list_modes():
            if mode.is_primary_mode():
                self.agent_mode_combo.addItem(mode.name, mode.slug)
        self.agent_mode_combo.currentIndexChanged.connect(self._on_agent_mode_changed)
        base_form.addRow(QCoreApplication.translate('ChannelInstanceDialog', 'Agent 模式'), self.agent_mode_combo)
        self.connection_mode_combo = QComboBox()
        configure_combo_popup(self.connection_mode_combo)
        self.connection_mode_combo.currentIndexChanged.connect(self._on_connection_mode_changed)
        self.connection_mode_label = QLabel(QCoreApplication.translate('ChannelInstanceDialog', '连接方式'))
        base_form.addRow(self.connection_mode_label, self.connection_mode_combo)
        self.connection_mode_hint = QLabel()
        self.connection_mode_hint.setWordWrap(True)
        self.connection_mode_hint.setProperty("muted", True)
        base_form.addRow("", self.connection_mode_hint)
        body.addWidget(base_group)

        tool_group = QGroupBox(QCoreApplication.translate('ChannelInstanceDialog', '工具类别'))
        tool_layout = QVBoxLayout(tool_group)
        tool_layout.setContentsMargins(12, 10, 12, 10)
        self.tool_category_selector = ToolCategorySelector(
            columns=4,
            object_prefix="channel_tool_category",
            allow_inherit=True,
        )
        tool_layout.addWidget(self.tool_category_selector)
        body.addWidget(tool_group)

        self.dynamic_group = QGroupBox(QCoreApplication.translate('ChannelInstanceDialog', '连接配置'))
        self.dynamic_form = SettingsFormLayout(self.dynamic_group)
        self.dynamic_form.setContentsMargins(12, 12, 12, 12)
        self.dynamic_form.setSpacing(8)
        self.dynamic_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        body.addWidget(self.dynamic_group)

        self.qr_group = QGroupBox(QCoreApplication.translate('ChannelInstanceDialog', '个人微信扫码（实验）'))
        qr_layout = QVBoxLayout(self.qr_group)
        qr_layout.setContentsMargins(12, 12, 12, 12)
        qr_layout.setSpacing(8)
        self.connection_status_label = QLabel(QCoreApplication.translate('ChannelInstanceDialog', '连接状态：正在准备'))
        self.connection_status_label.setProperty("heading", True)
        qr_layout.addWidget(self.connection_status_label)
        self.connection_detail_label = QLabel(QCoreApplication.translate('ChannelInstanceDialog', '二维码会自动生成并持续检查扫码状态。'))
        self.connection_detail_label.setWordWrap(True)
        self.connection_detail_label.setProperty("muted", True)
        qr_layout.addWidget(self.connection_detail_label)
        self.qr_label = QLabel(QCoreApplication.translate('ChannelInstanceDialog', '正在生成二维码'))
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setMinimumSize(220, 220)
        self.qr_label.setProperty("muted", True)
        qr_layout.addWidget(self.qr_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self.verify_row = QWidget()
        verify_layout = QHBoxLayout(self.verify_row)
        verify_layout.setContentsMargins(0, 0, 0, 0)
        verify_layout.setSpacing(8)
        self.verify_code_input = ThemedLineEdit()
        self.verify_code_input.setPlaceholderText(QCoreApplication.translate('ChannelInstanceDialog', '输入手机微信显示的数字'))
        self.verify_code_input.returnPressed.connect(self._submit_verification_code)
        verify_layout.addWidget(self.verify_code_input, 1)
        self.verify_button = QPushButton(QCoreApplication.translate('ChannelInstanceDialog', '验证'))
        self.verify_button.clicked.connect(self._submit_verification_code)
        verify_layout.addWidget(self.verify_button)
        self.verify_row.setVisible(False)
        qr_layout.addWidget(self.verify_row)

        qr_actions = QHBoxLayout()
        self.regenerate_qr_button = QPushButton(QCoreApplication.translate('ChannelInstanceDialog', '重新生成二维码'))
        self.regenerate_qr_button.setIcon(Icons.get(Icons.REFRESH, scale_factor=1.0))
        self.regenerate_qr_button.clicked.connect(lambda: self._begin_login(force=True))
        qr_actions.addWidget(self.regenerate_qr_button)
        qr_actions.addStretch(1)
        qr_layout.addLayout(qr_actions)
        body.addWidget(self.qr_group)

        self.session_group = QGroupBox(QCoreApplication.translate('ChannelInstanceDialog', '绑定对话'))
        session_layout = QVBoxLayout(self.session_group)
        session_layout.setContentsMargins(12, 12, 12, 12)
        session_layout.setSpacing(8)
        self.session_summary_label = ThemedSelectableLabel()
        self.session_summary_label.setWordWrap(True)
        self.session_summary_label.setProperty("muted", True)
        self.session_summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        session_layout.addWidget(self.session_summary_label)
        session_actions = QHBoxLayout()
        self.change_session_btn = QPushButton(QCoreApplication.translate('ChannelInstanceDialog', '更换对话'))
        self.change_session_btn.setIcon(Icons.get(Icons.CHAT, scale_factor=1.0))
        self.change_session_btn.clicked.connect(self._open_session_picker)
        session_actions.addWidget(self.change_session_btn)
        session_actions.addStretch(1)
        session_layout.addLayout(session_actions)
        body.addWidget(self.session_group)

        self.diagnostics_group = QGroupBox(QCoreApplication.translate('ChannelInstanceDialog', '诊断信息'))
        diagnostics = SettingsFormLayout(self.diagnostics_group)
        diagnostics.setContentsMargins(12, 10, 12, 10)
        self.diagnostic_id = ThemedSelectableLabel()
        self.diagnostic_id.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.diagnostic_source = ThemedSelectableLabel()
        self.diagnostic_source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.diagnostic_credentials = QLabel()
        diagnostics.addRow("ID", self.diagnostic_id)
        diagnostics.addRow("Source", self.diagnostic_source)
        diagnostics.addRow(QCoreApplication.translate('ChannelInstanceDialog', '凭据'), self.diagnostic_credentials)
        body.addWidget(self.diagnostics_group)

        self.detail_label = QLabel()
        self.detail_label.setWordWrap(True)
        self.detail_label.setProperty("muted", True)
        body.addWidget(self.detail_label)
        body.addStretch(1)
        layout.addWidget(scroll, 1)

        buttons = build_dialog_button_box(self, accept_text=QCoreApplication.translate('ChannelInstanceDialog', '完成'))
        self.done_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_channel(self, channel: ChannelConfig) -> None:
        self._loading = True
        try:
            self._channel = self._catalog.ensure_channel(channel)
            config = dict(self._channel.config or {})
            self.name_input.setText(self._channel.name)
            mode_index = self.agent_mode_combo.findData(self._channel.mode_slug or "channel")
            self.agent_mode_combo.setCurrentIndex(mode_index if mode_index >= 0 else 0)
            self.tool_category_selector.set_policy(
                ceiling=self._selected_agent_mode_categories(),
                policy=self._channel.tool_selection,
                inheritance_path=QCoreApplication.translate('ChannelInstanceDialog', '继承路径：Channel Mode ∩ 频道实例'),
            )
            self._reload_connection_modes(config)
            self._rebuild_dynamic_form(config)
            self._update_mode_hint()
            self._update_session_summary()
            self._update_diagnostics()
            self._render_snapshot(self._service.connection_snapshot(self._channel))
            self._update_done_state()
        finally:
            self._loading = False

    def _connection_modes(self) -> tuple[tuple[str, str], ...]:
        if self._definition.type == "wechat":
            return ((QCoreApplication.translate('ChannelInstanceDialog', '个人微信扫码（实验）'), "ilink"), (QCoreApplication.translate('ChannelInstanceDialog', '公众号 Webhook（稳定）'), "webhook"))
        if self._definition.type == "feishu":
            return ((QCoreApplication.translate('ChannelInstanceDialog', '长连接'), "websocket"), ("Webhook", "webhook"))
        if self._definition.type == "qqbot":
            return ((QCoreApplication.translate('ChannelInstanceDialog', 'Gateway 长连接'), "websocket"), ("Webhook", "webhook"))
        if self._definition.type == "dingtalk":
            return ((QCoreApplication.translate('ChannelInstanceDialog', 'Stream 长连接'), "stream"),)
        return ()

    def _reload_connection_modes(self, config: dict) -> None:
        self.connection_mode_combo.blockSignals(True)
        try:
            self.connection_mode_combo.clear()
            modes = self._connection_modes()
            visible = bool(modes)
            self.connection_mode_combo.setVisible(visible)
            self.connection_mode_label.setVisible(visible)
            self.connection_mode_hint.setVisible(visible)
            for label, value in modes:
                self.connection_mode_combo.addItem(label, value)
            if modes:
                current = self._normalize_mode(str(config.get("connection_mode", "") or ""))
                index = self.connection_mode_combo.findData(current)
                self.connection_mode_combo.setCurrentIndex(max(0, index))
        finally:
            self.connection_mode_combo.blockSignals(False)

    def _normalize_mode(self, value: str) -> str:
        value = str(value or "").strip().lower()
        if self._definition.type == "wechat":
            return value if value in {"ilink", "webhook"} else "ilink"
        if self._definition.type in {"feishu", "qqbot"}:
            return value if value in {"websocket", "webhook"} else "websocket"
        if self._definition.type == "telegram":
            return "polling"
        if self._definition.type == "dingtalk":
            return "stream"
        return value

    def _selected_mode(self, config: dict | None = None) -> str:
        if self.connection_mode_combo.isVisible():
            return self._normalize_mode(str(self.connection_mode_combo.currentData() or ""))
        return self._normalize_mode(str((config or {}).get("connection_mode", "") or ""))

    def _selected_agent_mode_categories(self) -> set[str]:
        slug = str(self.agent_mode_combo.currentData() or "channel")
        return set(ModeManager(None, include_project=False).get(slug).tool_category_names())

    def _on_agent_mode_changed(self, _index: int) -> None:
        if self._loading or not hasattr(self, "tool_category_selector"):
            return
        policy = self.tool_category_selector.selection_policy()
        self.tool_category_selector.set_policy(
            ceiling=self._selected_agent_mode_categories(),
            policy=policy,
            inheritance_path=QCoreApplication.translate('ChannelInstanceDialog', '继承路径：Channel Mode ∩ 频道实例'),
        )

    def _rebuild_dynamic_form(self, config: dict) -> None:
        self._dynamic_inputs.clear()
        while self.dynamic_form.rowCount() > 0:
            self.dynamic_form.removeRow(0)
        mode = self._selected_mode(config)
        for field_def in self._definition.fields:
            if field_def.show_for_modes and mode not in field_def.show_for_modes:
                continue
            line_edit = ThemedLineEdit()
            line_edit.setPlaceholderText(channel_metadata_text(field_def.placeholder))
            line_edit.setToolTip(channel_metadata_text(field_def.help_text or field_def.label))
            if field_def.secret:
                line_edit.setEchoMode(QLineEdit.EchoMode.Password)
            line_edit.setText(str(config.get(field_def.key, "") or ""))
            self.dynamic_form.addRow(channel_metadata_text(field_def.label), line_edit)
            self._dynamic_inputs[field_def.key] = line_edit
        self.dynamic_group.setVisible(bool(self._dynamic_inputs))

    def _update_mode_hint(self) -> None:
        mode = self._selected_mode()
        if self._definition.type == "wechat" and mode == "ilink":
            text = QCoreApplication.translate('ChannelInstanceDialog', '实验功能。二维码由微信 iLink 服务生成，扫码页可能显示上游品牌。')
        elif self._definition.type == "wechat":
            text = QCoreApplication.translate('ChannelInstanceDialog', '稳定入口，适合具有公网回调地址的公众号或服务号。')
        elif self._definition.type == "feishu" and mode == "websocket":
            text = QCoreApplication.translate('ChannelInstanceDialog', '无需公网回调，使用 App ID 和 App Secret 建立长连接。')
        elif self._definition.type == "qqbot" and mode == "websocket":
            text = QCoreApplication.translate('ChannelInstanceDialog', '通过 QQ 官方 Gateway 建立长连接。')
        elif self._definition.type == "dingtalk":
            text = QCoreApplication.translate('ChannelInstanceDialog', '无需公网地址；在钉钉开发者后台启用机器人，选择 Stream 接收模式并发布应用。')
        else:
            text = QCoreApplication.translate('ChannelInstanceDialog', '填写连接所需字段，主设置窗口保存后启动。')
        self.connection_mode_hint.setText(text)
        is_ilink = self._definition.type == "wechat" and mode == "ilink"
        self.qr_group.setVisible(is_ilink)

    def _build_channel(self) -> ChannelConfig:
        config = dict(self._channel.config or {})
        config["connection_mode"] = self._selected_mode(config)
        for key, widget in self._dynamic_inputs.items():
            config[key] = str(widget.text() or "").strip()
        if self._definition.type in {"wechat", "feishu", "qqbot"} and config["connection_mode"] == "webhook":
            config.setdefault("callback_path", f"/{self._definition.type}/{self._channel.id}")
        name = str(self.name_input.text() or "").strip() or self._definition.default_name
        source = self._channel.source or self._definition.normalize_source(name)
        return self._catalog.ensure_channel(
            ChannelConfig(
                id=self._channel.id or uuid.uuid4().hex[:12],
                name=name,
                type=self._definition.type,
                enabled=self._channel.enabled,
                tool_selection=self.tool_category_selector.selection_policy(),
                source=source,
                mode_slug=str(self.agent_mode_combo.currentData() or "channel"),
                session_id=self._preferred_session_id or self._channel.session_id,
                config=config,
            )
        )

    def _begin_login_if_needed(self) -> None:
        channel = self._build_channel()
        if not self._is_ilink(channel):
            return
        if str((channel.config or {}).get("ilink_token", "") or "").strip():
            return
        self._begin_login(force=False)

    def _begin_login(self, *, force: bool) -> None:
        if self._login_request_running:
            return
        channel = self._build_channel()
        if not self._is_ilink(channel):
            return
        self._poll_timer.stop()
        self._login_session = None if force else self._login_session
        self._run_login_request("begin", lambda: self._service.begin_wechat_login(channel))
        self.connection_status_label.setText(QCoreApplication.translate('ChannelInstanceDialog', '连接状态：正在生成二维码'))
        self.connection_detail_label.setText(QCoreApplication.translate('ChannelInstanceDialog', '正在连接微信服务，请稍候。'))

    def _poll_login(self) -> None:
        if self._login_session is None or self._login_request_running:
            return
        self._run_login_request(
            "poll",
            lambda: self._service.poll_wechat_login(self._login_session),
        )

    def _submit_verification_code(self) -> None:
        code = str(self.verify_code_input.text() or "").strip()
        if not code or self._login_session is None or self._login_request_running:
            return
        self._run_login_request(
            "verify",
            lambda: self._service.poll_wechat_login(self._login_session, verification_code=code),
        )

    def _run_login_request(self, _operation: str, callback) -> None:
        request_id = str(uuid.uuid4())
        self._login_request_id = request_id
        self._login_request_running = True
        self._update_login_controls()

        def _run() -> None:
            try:
                result = callback()
            except Exception as exc:
                self._login_error.emit(request_id, str(exc))
                return
            self._login_result.emit(request_id, result)

        threading.Thread(
            target=_run,
            name=f"PyCat-WeChatLogin-{self._channel.id}",
            daemon=True,
        ).start()

    def _on_login_result(self, request_id: str, session) -> None:
        if request_id != self._login_request_id:
            return
        self._login_request_running = False
        self._login_session = session
        self._channel = session.channel
        self._preferred_session_id = str(self._channel.session_id or "").strip()
        self._render_snapshot(session.snapshot)
        self._update_session_summary()
        self._update_diagnostics()
        self._update_done_state()
        self._update_login_controls()

        action = session.snapshot.required_action
        state = session.snapshot.state
        self.verify_row.setVisible(action == ChannelRequiredAction.VERIFY_CODE)
        if action == ChannelRequiredAction.VERIFY_CODE:
            self.verify_code_input.setFocus()
            return
        if state in {
            ChannelConnectionState.WAITING_USER,
            ChannelConnectionState.CONNECTING,
            ChannelConnectionState.RECONNECTING,
        }:
            self._poll_timer.start(750)

    def _on_login_error(self, request_id: str, error: str) -> None:
        if request_id != self._login_request_id:
            return
        self._login_request_running = False
        snapshot = ChannelConnectionSnapshot(
            channel_id=self._channel.id,
            channel_type="wechat",
            mode="ilink",
            state=ChannelConnectionState.ERROR,
            required_action=ChannelRequiredAction.RETRY,
            detail=QCoreApplication.translate('ChannelInstanceDialog', '二维码生成失败：{error}').format(error=error),
        )
        self._render_snapshot(snapshot)
        self._update_login_controls()
        self._update_done_state()

    def _render_snapshot(self, snapshot: ChannelConnectionSnapshot) -> None:

        self.connection_status_label.setText(QCoreApplication.translate('ChannelInstanceDialog', '连接状态：{value}').format(value=channel_state_label(snapshot.state)))
        self.connection_detail_label.setText(channel_detail_label(snapshot) or QCoreApplication.translate('ChannelInstanceDialog', '等待连接状态。'))
        if snapshot.qr_text:
            pixmap = build_qr_code_pixmap(snapshot.qr_text, size=220)
            if not pixmap.isNull():
                self.qr_label.setPixmap(pixmap)
                self.qr_label.setText("")
            else:
                self.qr_label.clear()
                self.qr_label.setText(QCoreApplication.translate('ChannelInstanceDialog', '二维码渲染失败'))
        elif snapshot.state == ChannelConnectionState.READY:
            self.qr_label.clear()
            self.qr_label.setText(QCoreApplication.translate('ChannelInstanceDialog', '微信已连接'))
        elif self._is_ilink(self._channel):
            self.qr_label.clear()
            self.qr_label.setText(QCoreApplication.translate('ChannelInstanceDialog', '暂无二维码'))
        self.detail_label.setText(channel_detail_label(snapshot) or "")

    def _update_login_controls(self) -> None:
        self.regenerate_qr_button.setEnabled(not self._login_request_running)
        self.verify_button.setEnabled(not self._login_request_running)

    def _update_done_state(self) -> None:
        if self.done_button is None:
            return
        channel = self._build_channel()
        if self._is_ilink(channel):
            ready = bool(str((channel.config or {}).get("ilink_token", "") or "").strip())
            self.done_button.setEnabled(ready and not self._login_request_running)
            return
        self.done_button.setEnabled(not self._login_request_running)

    def _update_session_summary(self) -> None:
        session_id = str(self._preferred_session_id or self._channel.session_id or "").strip()
        if not session_id:
            self.session_summary_label.setText(QCoreApplication.translate('ChannelInstanceDialog', '完成后会自动创建一个 PyCat 对话作为主绑定。'))
            return
        try:
            summaries = self._service.list_bindable_conversations(self._channel)
        except Exception:
            self.session_summary_label.setText(f"Session ID: {session_id}")
            return
        for summary in summaries:
            if summary.conversation_id != session_id:
                continue
            updated_at = "-"
            if summary.updated_at > 0:
                updated_at = datetime.fromtimestamp(summary.updated_at).strftime("%m-%d %H:%M")
            self.session_summary_label.setText(
                QCoreApplication.translate('ChannelInstanceDialog', '{title}\nSession ID: {session_id}\n更新：{updated_at}').format(title=summary.title, session_id=session_id, updated_at=updated_at)
            )
            return
        self.session_summary_label.setText(f"Session ID: {session_id}")

    def _update_diagnostics(self) -> None:
        config = dict(self._channel.config or {})
        self.diagnostic_id.setText(self._channel.id or "-")
        self.diagnostic_source.setText(self._channel.source or "-")
        has_credentials = bool(
            str(config.get("ilink_token", "") or "").strip()
            or str(config.get("bot_token", "") or "").strip()
            or str(config.get("app_secret", "") or "").strip()
        )
        self.diagnostic_credentials.setText(QCoreApplication.translate('ChannelInstanceDialog', '已配置') if has_credentials else QCoreApplication.translate('ChannelInstanceDialog', '未配置'))

    def _open_session_picker(self) -> None:
        channel = self._build_channel()
        dialog = ChannelSessionPickerDialog(
            channel=channel,
            channel_service=self._service,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        try:
            updated = self._service.bind_session(channel, dialog.selected_conversation_id)
        except Exception as exc:
            self.detail_label.setText(QCoreApplication.translate('ChannelInstanceDialog', '更换绑定对话失败：{exc}').format(exc=exc))
            return
        self._channel = updated
        self._preferred_session_id = str(updated.session_id or "").strip()
        self._update_session_summary()

    def _on_connection_mode_changed(self, _index: int) -> None:
        if self._loading:
            return
        channel = self._build_channel()
        self._channel = channel
        self._rebuild_dynamic_form(dict(channel.config or {}))
        self._update_mode_hint()
        self._update_done_state()
        if self._is_ilink(channel):
            QTimer.singleShot(0, self._begin_login_if_needed)
        else:
            self._poll_timer.stop()

    @staticmethod
    def _is_ilink(channel: ChannelConfig) -> bool:
        return (
            channel.type == "wechat"
            and str((channel.config or {}).get("connection_mode", "") or "").strip().lower() == "ilink"
        )

    def accept(self) -> None:
        channel = self._build_channel()
        errors = self._service.validation_errors(channel)
        if channel.enabled and errors:
            self.detail_label.setText("；".join(errors))
            return
        self._channel = channel
        self._preferred_session_id = str(channel.session_id or "").strip()
        super().accept()

    def done(self, result: int) -> None:
        self._poll_timer.stop()
        self._login_request_id = ""
        super().done(result)


__all__ = ["ChannelInstanceDialog"]
