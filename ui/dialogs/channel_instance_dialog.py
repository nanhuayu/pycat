from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Dict

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QCheckBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from core.channel import ChannelDefinition, default_channel_manager
from core.channel.models import ChannelConnectionSnapshot, ChannelConversationSummary
from core.channel.runtime import ChannelRuntimeService
from core.tools.catalog import TOOL_CATEGORIES, TOOL_CATEGORY_LABELS, ToolSelectionPolicy
from core.config.schema import ChannelConfig
from ui.dialogs.wechat_qr_connect_dialog import WeChatQRConnectDialog
from ui.utils.combo_box import configure_combo_popup
from ui.utils.icon_manager import Icons


class ChannelConversationItem(QListWidgetItem):
    def __init__(self, summary: ChannelConversationSummary):
        super().__init__()
        self.summary = summary

        badges: list[str] = []
        if summary.is_primary_session:
            badges.append("当前绑定")
        if summary.is_manual_test_session:
            badges.append("手动创建")
        if summary.participant_label:
            badges.append(summary.participant_label)
        subtitle = " · ".join(badges)

        self.setText(f"{summary.title}\n{subtitle}" if subtitle else summary.title)
        self.setIcon(Icons.get(Icons.CHAT, scale_factor=0.9))

        preview = str(summary.preview or "").strip() or "暂无消息预览"
        updated_at = "-"
        if summary.updated_at > 0:
            try:
                updated_at = datetime.fromtimestamp(summary.updated_at).strftime("%m-%d %H:%M")
            except Exception:
                updated_at = "-"
        self.setToolTip(
            f"会话 ID：{summary.conversation_id}\n"
            f"最近更新：{updated_at}\n"
            f"最近消息：{preview}"
        )
        self.setSizeHint(QSize(0, 46))


class ChannelInstanceDialog(QDialog):
    _PERMISSION_OPTIONS = (
        ("默认", "default"),
        ("自动", "auto"),
        ("需确认", "manual"),
    )
    _WECHAT_BRIDGE_BASE = "https://ilinkai.weixin.qq.com"

    def __init__(
        self,
        *,
        channel: ChannelConfig,
        channel_runtime: ChannelRuntimeService | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._channel_manager = default_channel_manager()
        self._channel_runtime = channel_runtime
        self._channel = self._channel_manager.ensure_channel(channel)
        self._definition = self._channel_manager.get_definition(self._channel.type)
        self._dynamic_inputs: Dict[str, QLineEdit] = {}
        self._preferred_session_id = ""
        self._loading = False

        self.setWindowTitle(f"编辑 {self._definition.name}")
        self.resize(820, 720)
        self._setup_ui()
        self._load_channel(self._channel)

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

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(10)

        icon_label = QLabel()
        icon_label.setPixmap(Icons.get(self._definition.icon_name, scale_factor=1.1).pixmap(24, 24))
        icon_label.setFixedSize(28, 28)
        header_layout.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignVCenter)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        title = QLabel(self._definition.name)
        title.setProperty("heading", True)
        text_layout.addWidget(title)
        subtitle = QLabel("编辑连接配置与会话绑定")
        subtitle.setProperty("muted", True)
        text_layout.addWidget(subtitle)
        header_layout.addLayout(text_layout, 1)
        layout.addLayout(header_layout)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(12)
        self.scroll_area.setWidget(scroll_content)

        base_group = QGroupBox("基础")
        base_form = QFormLayout(base_group)
        base_form.setContentsMargins(12, 12, 12, 12)
        base_form.setSpacing(8)
        base_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("例如：运营通知、飞书开发群、微信客服")
        base_form.addRow("名称", self.name_input)

        self.permission_mode_combo = QComboBox()
        configure_combo_popup(self.permission_mode_combo)
        for label, value in self._PERMISSION_OPTIONS:
            self.permission_mode_combo.addItem(label, value)
        base_form.addRow("权限模式", self.permission_mode_combo)

        self.tool_category_checks: dict[str, QCheckBox] = {}
        tool_group = QGroupBox("工具类别")
        tool_layout = QVBoxLayout(tool_group)
        tool_layout.setContentsMargins(12, 8, 12, 8)
        for category in TOOL_CATEGORIES:
            if category in {"edit", "execute"}:
                continue
            check = QCheckBox(f"{TOOL_CATEGORY_LABELS.get(category, category)} ({category})")
            check.setToolTip("频道会话可见的工具类别；具体工具仍受全局权限控制。")
            tool_layout.addWidget(check)
            self.tool_category_checks[category] = check
        base_form.addRow("工具能力", tool_group)

        self.connection_mode_combo = QComboBox()
        configure_combo_popup(self.connection_mode_combo)
        self.connection_mode_combo.currentIndexChanged.connect(self._on_connection_mode_changed)
        self.connection_mode_label = QLabel("连接方式")
        base_form.addRow(self.connection_mode_label, self.connection_mode_combo)

        self.connection_mode_hint = QLabel("")
        self.connection_mode_hint.setWordWrap(True)
        self.connection_mode_hint.setProperty("muted", True)
        base_form.addRow("", self.connection_mode_hint)

        scroll_layout.addWidget(base_group)

        connection_actions = QHBoxLayout()
        connection_actions.setSpacing(8)

        self.connect_btn = QPushButton("扫码连接")
        self.connect_btn.setIcon(Icons.get(Icons.MESSAGE, scale_factor=1.0))
        self.connect_btn.clicked.connect(self._open_wechat_qr_dialog)
        connection_actions.addWidget(self.connect_btn)

        self.refresh_status_btn = QPushButton("刷新状态")
        self.refresh_status_btn.setIcon(Icons.get(Icons.REFRESH, scale_factor=1.0))
        self.refresh_status_btn.clicked.connect(self._refresh_connection_snapshot)
        connection_actions.addWidget(self.refresh_status_btn)

        self.ensure_session_btn = QPushButton("生成会话")
        self.ensure_session_btn.setIcon(Icons.get(Icons.PLUS, scale_factor=1.0))
        self.ensure_session_btn.clicked.connect(self._prepare_channel_test_session)
        connection_actions.addWidget(self.ensure_session_btn)
        connection_actions.addStretch(1)
        scroll_layout.addLayout(connection_actions)

        self.dynamic_group = QGroupBox("连接配置")
        self.dynamic_form = QFormLayout(self.dynamic_group)
        self.dynamic_form.setContentsMargins(12, 12, 12, 12)
        self.dynamic_form.setSpacing(8)
        self.dynamic_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        scroll_layout.addWidget(self.dynamic_group)

        self.session_group = QGroupBox("频道会话")
        session_layout = QVBoxLayout(self.session_group)
        session_layout.setContentsMargins(12, 12, 12, 12)
        session_layout.setSpacing(8)

        self.session_hint_label = QLabel("保存并启用后，这里会显示已关联会话。")
        self.session_hint_label.setWordWrap(True)
        self.session_hint_label.setProperty("muted", True)
        session_layout.addWidget(self.session_hint_label)

        self.channel_session_list = QListWidget()
        self.channel_session_list.setMinimumHeight(170)
        session_layout.addWidget(self.channel_session_list)

        session_actions = QHBoxLayout()
        session_actions.setSpacing(8)

        self.use_session_btn = QPushButton("使用选中会话")
        self.use_session_btn.setIcon(Icons.get(Icons.CHECK, scale_factor=1.0))
        self.use_session_btn.clicked.connect(self._use_selected_channel_session)
        session_actions.addWidget(self.use_session_btn)

        self.refresh_sessions_btn = QPushButton("刷新列表")
        self.refresh_sessions_btn.setIcon(Icons.get(Icons.REFRESH, scale_factor=1.0))
        self.refresh_sessions_btn.clicked.connect(self._refresh_channel_session_list)
        session_actions.addWidget(self.refresh_sessions_btn)
        session_actions.addStretch(1)
        session_layout.addLayout(session_actions)

        scroll_layout.addWidget(self.session_group, 1)

        self.detail_label = QLabel("状态和校验结果会在这里展示。")
        self.detail_label.setWordWrap(True)
        self.detail_label.setProperty("muted", True)
        self.detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        scroll_layout.addWidget(self.detail_label)
        scroll_layout.addStretch(1)

        layout.addWidget(self.scroll_area, 1)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = button_box.button(QDialogButtonBox.StandardButton.Save)
        if self.save_button is not None:
            self.save_button.setText("保存")
            self.save_button.setProperty("primary", True)
        cancel_button = button_box.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setText("取消")
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _load_channel(self, channel: ChannelConfig) -> None:
        self._loading = True
        try:
            self._channel = self._channel_manager.ensure_channel(channel)
            instance = self._channel_manager.build_instance(self._channel)
            config = self._config_with_legacy_fields(self._channel)

            self.name_input.setText(str(self._channel.name or ""))
            tool_selection = getattr(self._channel, "tool_selection", None) or ToolSelectionPolicy.from_categories(("read", "search", "manage"))
            selected_categories = tool_selection.allowed_categories
            if selected_categories is None:
                selected_categories = set(TOOL_CATEGORIES)
            for category, check in self.tool_category_checks.items():
                check.setChecked(category in selected_categories)

            permission_index = self.permission_mode_combo.findData(self._channel.permission_mode or "default")
            self.permission_mode_combo.setCurrentIndex(permission_index if permission_index >= 0 else 0)

            self._reload_connection_mode_options(config)
            self._rebuild_dynamic_form(config)

            self._update_session_value()
            self._update_connection_actions()
            self._update_detail_text(instance=instance)
            self._refresh_channel_session_list(preferred_conversation_id=self._preferred_session_id or self._channel.session_id)
        finally:
            self._loading = False

    def _reload_connection_mode_options(self, config: dict) -> None:
        self.connection_mode_combo.blockSignals(True)
        try:
            self.connection_mode_combo.clear()
            options = self._connection_mode_options()
            visible = bool(options)
            self.connection_mode_combo.setVisible(visible)
            self.connection_mode_label.setVisible(visible)
            self.connection_mode_hint.setVisible(visible)
            if not visible:
                return
            for label, value in options:
                self.connection_mode_combo.addItem(label, value)
            current_mode = self._normalize_connection_mode(str(config.get("connection_mode", "") or ""))
            index = self.connection_mode_combo.findData(current_mode)
            self.connection_mode_combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.connection_mode_combo.blockSignals(False)
        self._update_connection_mode_hint()

    def _rebuild_dynamic_form(self, config: dict) -> None:
        self._dynamic_inputs.clear()
        while self.dynamic_form.rowCount() > 0:
            self.dynamic_form.removeRow(0)

        current_mode = self._selected_connection_mode(config)
        for field_def in self._definition.fields:
            if field_def.show_for_modes and current_mode not in set(field_def.show_for_modes):
                continue
            line_edit = QLineEdit()
            line_edit.setPlaceholderText(field_def.placeholder)
            line_edit.setToolTip(field_def.help_text or field_def.label)
            if field_def.secret:
                line_edit.setEchoMode(QLineEdit.EchoMode.Password)
            line_edit.setText(str(config.get(field_def.key, "") or ""))
            self.dynamic_form.addRow(field_def.label, line_edit)
            self._dynamic_inputs[field_def.key] = line_edit

    def _connection_mode_options(self) -> tuple[tuple[str, str], ...]:
        channel_type = str(self._definition.type or "").strip().lower()
        if channel_type == "wechat":
            return (
                ("二维码连接（推荐）", "qr-bridge"),
                ("公众号 Webhook", "official-webhook"),
            )
        if channel_type == "feishu":
            return (
                ("长连接（推荐）", "websocket"),
                ("Webhook 回调", "webhook"),
            )
        if channel_type == "qqbot":
            return (
                ("官方 Gateway 长连接（推荐）", "websocket"),
                ("Webhook 回调（高级兼容）", "webhook"),
            )
        return ()

    def _normalize_connection_mode(self, value: str) -> str:
        channel_type = str(self._definition.type or "").strip().lower()
        normalized = str(value or "").strip().lower()
        if channel_type == "wechat":
            return normalized if normalized in {"qr-bridge", "official-webhook"} else "qr-bridge"
        if channel_type == "feishu":
            return normalized if normalized in {"webhook", "websocket"} else "websocket"
        if channel_type == "qqbot":
            return normalized if normalized in {"webhook", "websocket"} else "websocket"
        return normalized

    def _selected_connection_mode(self, config: dict | None = None) -> str:
        if self.connection_mode_combo.isVisible():
            return self._normalize_connection_mode(str(self.connection_mode_combo.currentData() or ""))
        config_mapping = dict(config or {})
        return self._normalize_connection_mode(str(config_mapping.get("connection_mode", "") or ""))

    def _update_connection_mode_hint(self) -> None:
        channel_type = str(self._definition.type or "").strip().lower()
        mode = self._selected_connection_mode()
        if channel_type == "wechat":
            if mode == "official-webhook":
                text = "公众号 Webhook 适合公网可达的公众号/服务号场景；二维码模式更适合桌面端。"
            else:
                text = "二维码连接会弹出独立扫码窗口；真实联系人发来消息后会自动创建并绑定会话。"
        elif channel_type == "feishu":
            if mode == "webhook":
                text = "Webhook 是兼容模式，需要公网可达回调、Verification Token 和回调地址配置。"
            else:
                text = "长连接模式会直接连接飞书开放平台，桌面端无需公网回调，推荐优先使用。"
        elif channel_type == "qqbot":
            if mode == "webhook":
                text = "Webhook 是高级兼容模式，需要公网 HTTPS 回调；桌面端推荐使用官方 Gateway 长连接。"
            else:
                text = "官方 Gateway 长连接按 OpenClaw/QQ 官方实现，只需 App ID 与 App Secret，无需公网回调。"
        else:
            text = "按频道类型填写必要配置即可；状态会在频道详情中统一显示。"
        self.connection_mode_hint.setText(text)

    def _update_connection_actions(self) -> None:
        is_wechat_qr = self._definition.type == "wechat" and self._selected_connection_mode() == "qr-bridge"
        self.connect_btn.setVisible(is_wechat_qr)
        self.connect_btn.setEnabled(is_wechat_qr and self._channel_runtime is not None)
        self.refresh_status_btn.setEnabled(self._channel_runtime is not None)
        self.ensure_session_btn.setEnabled(self._channel_runtime is not None)
        self.session_group.setEnabled(self._channel_runtime is not None)

    def _update_session_value(self) -> None:
        session_id = str(self._preferred_session_id or self._channel.session_id or "").strip()
        self.session_group.setTitle(f"频道会话 · {session_id}" if session_id else "频道会话")

    def _update_detail_text(self, *, instance=None, snapshot: ChannelConnectionSnapshot | None = None) -> None:
        current_instance = instance or self._channel_manager.build_instance(self._channel)
        summary = current_instance.summary or self._channel.source or "未填写摘要"
        enabled_label = "已启用" if bool(getattr(self._channel, "enabled", False)) else "已停用"
        source = str(getattr(self._channel, "source", "") or "").strip()
        detail_lines = [f"状态：{enabled_label} · {current_instance.status_label}"]
        if source:
            detail_lines.append(f"来源：{source}")
        detail_lines.append(summary)
        if current_instance.validation_errors:
            detail_lines.append(f"校验：{'；'.join(current_instance.validation_errors)}")
        else:
            detail_lines.append("校验：当前字段完整，可保存并启用。")
        if snapshot is not None and str(snapshot.detail or "").strip():
            detail_lines.append(f"连接：{snapshot.detail}")
        self.detail_label.setText("\n".join(detail_lines))

    def _selected_tool_selection(self) -> ToolSelectionPolicy:
        categories = [category for category, check in self.tool_category_checks.items() if check.isChecked()]
        if "manage" not in categories:
            categories.append("manage")
        return ToolSelectionPolicy.from_categories(categories)

    def _config_with_legacy_fields(self, channel: ChannelConfig) -> dict:
        config = dict(getattr(channel, "config", {}) or {})
        if channel.webhook_url and not config.get("webhook_url"):
            config["webhook_url"] = channel.webhook_url
        if channel.token and not config.get("token"):
            config["token"] = channel.token
        if channel.secret and not config.get("secret"):
            config["secret"] = channel.secret
        return config

    def _build_channel_from_form(self) -> ChannelConfig:
        existing = self._channel
        config = self._config_with_legacy_fields(existing)
        if self.connection_mode_combo.isVisible():
            config["connection_mode"] = self._selected_connection_mode()
        for key, widget in self._dynamic_inputs.items():
            config[key] = str(widget.text() or "").strip()

        channel_type = str(self._definition.type or "").strip().lower()
        if channel_type == "wechat":
            mode = self._normalize_connection_mode(str(config.get("connection_mode", "") or ""))
            config["connection_mode"] = mode
            if mode == "qr-bridge":
                config.setdefault("bridge_api_base", self._WECHAT_BRIDGE_BASE)
            else:
                config.setdefault("callback_path", f"/wechat/{existing.id or 'instance'}")
        elif channel_type == "feishu":
            mode = self._normalize_connection_mode(str(config.get("connection_mode", "") or ""))
            config["connection_mode"] = mode
            if mode == "webhook":
                config.setdefault("callback_path", f"/feishu/{existing.id or 'instance'}")
        elif channel_type == "qqbot":
            mode = self._normalize_connection_mode(str(config.get("connection_mode", "") or ""))
            config["connection_mode"] = mode
            if mode == "webhook":
                config.setdefault("callback_path", f"/qqbot/{existing.id or 'instance'}")

        name = str(self.name_input.text() or "").strip() or self._definition.default_name or self._definition.name
        source = str(existing.source or "").strip() or self._definition.normalize_source(name)
        now = int(time.time())
        token = self._pick_first(config, ("bot_token", "verification_token", "token"), fallback=existing.token)
        secret = self._pick_first(config, ("app_secret", "secret", "encrypt_key", "signing_secret"), fallback=existing.secret)
        webhook_url = self._pick_first(config, ("webhook_url",), fallback=existing.webhook_url)

        return self._channel_manager.ensure_channel(
            ChannelConfig(
                id=existing.id or uuid.uuid4().hex[:12],
                name=name,
                type=self._definition.type,
                enabled=bool(existing.enabled),
                tool_selection=self._selected_tool_selection(),
                source=source,
                description=str(existing.description or "").strip(),
                agent_id=str(getattr(existing, "agent_id", "") or "").strip(),
                session_id=str(self._preferred_session_id or existing.session_id or "").strip(),
                permission_mode=str(self.permission_mode_combo.currentData() or "default").strip() or "default",
                status=str(existing.status or "draft").strip() or "draft",
                created_at=int(existing.created_at or now),
                updated_at=now,
                webhook_url=webhook_url,
                token=token,
                secret=secret,
                config=config,
            )
        )

    def _refresh_connection_snapshot(self) -> None:
        current = self._build_channel_from_form()
        snapshot = self._snapshot_for_channel(current)
        self._channel = self._apply_connection_snapshot_to_channel(current, snapshot)
        self._load_channel(self._channel)
        self._update_detail_text(snapshot=snapshot)

    def _snapshot_for_channel(self, channel: ChannelConfig) -> ChannelConnectionSnapshot:
        if self._channel_runtime is None:
            config = dict(getattr(channel, "config", {}) or {})
            return ChannelConnectionSnapshot(
                channel_id=str(getattr(channel, "id", "") or ""),
                channel_type=str(getattr(channel, "type", "") or "channel"),
                mode=str(config.get("connection_mode", "") or ""),
                status=str(getattr(channel, "status", "draft") or "draft"),
                detail="保存设置后，运行时会按当前配置启动。",
                raw=config,
            )

        channel_type = str(getattr(channel, "type", "") or "").strip().lower()
        if channel_type == "wechat" and self._selected_connection_mode(dict(getattr(channel, "config", {}) or {})) == "qr-bridge":
            return self._channel_runtime.get_channel_connection_snapshot(channel)
        return self._channel_runtime.get_channel_connection_snapshot(channel)

    def _apply_connection_snapshot_to_channel(self, channel: ChannelConfig, snapshot: ChannelConnectionSnapshot) -> ChannelConfig:
        config = dict(getattr(channel, "config", {}) or {})
        config.update(snapshot.to_config_patch())

        snapshot_status = str(snapshot.status or "").strip().lower()
        next_status = str(getattr(channel, "status", "draft") or "draft")
        if snapshot_status in {"connected", "ready"}:
            next_status = "ready"
        elif snapshot_status == "error":
            next_status = "error"
        elif snapshot_status == "expired":
            next_status = "draft"

        updated = self._channel_manager.ensure_channel(
            ChannelConfig.from_dict(
                {
                    **channel.to_dict(),
                    "status": next_status,
                    "updated_at": int(time.time()),
                    "config": config,
                }
            )
        )
        return updated

    def _prepare_channel_test_session(self) -> None:
        if self._channel_runtime is None:
            self.detail_label.setText("当前窗口未注入 channel runtime，无法生成会话。")
            return
        channel = self._build_channel_from_form()
        try:
            updated = self._channel_runtime.ensure_channel_session(channel, persist=False)
        except Exception as exc:
            self.detail_label.setText(f"预创建会话失败：{exc}")
            return
        self._channel = updated
        self._preferred_session_id = str(getattr(updated, "session_id", "") or "").strip()
        self._load_channel(updated)
        self.detail_label.setText(
            f"已预留手动测试会话：{self._preferred_session_id or '未生成'}\n该会话只用于桌面端手动测试；真实外部联系人发消息时会创建独立绑定会话。"
        )

    def _refresh_channel_session_list(self, *, preferred_conversation_id: str = "") -> None:
        self.channel_session_list.clear()
        if self._channel_runtime is None:
            self.session_hint_label.setText("当前窗口未注入 channel runtime，无法读取频道会话。")
            return

        channel = self._build_channel_from_form()
        try:
            summaries = list(self._channel_runtime.list_channel_conversations(channel))
        except Exception as exc:
            self.session_hint_label.setText(f"读取频道会话失败：{exc}")
            return

        if not summaries:
            self.session_hint_label.setText("当前还没有该频道的关联会话。启用后，这里会显示已绑定会话。")
            return

        self.session_hint_label.setText("这里列出关联会话；选中后可设为当前频道会话。")
        target_id = str(preferred_conversation_id or self._preferred_session_id or channel.session_id or "").strip()
        target_row = 0
        for row, summary in enumerate(summaries):
            self.channel_session_list.addItem(ChannelConversationItem(summary))
            if target_id and summary.conversation_id == target_id:
                target_row = row

        if self.channel_session_list.count() > 0:
            self.channel_session_list.setCurrentRow(target_row)

    def _use_selected_channel_session(self) -> None:
        item = self.channel_session_list.currentItem()
        if not isinstance(item, ChannelConversationItem):
            return

        summary = item.summary
        self._preferred_session_id = str(summary.conversation_id or "").strip()
        self._update_session_value()

        preview = str(summary.preview or "").strip()
        detail = f"已切换到频道会话：{summary.title}"
        if preview:
            detail += f"\n最近消息：{preview}"
        detail += "\n保存设置后，侧边栏会自动定位到该会话。"
        self.detail_label.setText(detail)
        self._refresh_channel_session_list(preferred_conversation_id=self._preferred_session_id)

    def _open_wechat_qr_dialog(self) -> None:
        if self._channel_runtime is None:
            self.detail_label.setText("当前窗口未注入 channel runtime，无法请求扫码会话。")
            return
        channel = self._build_channel_from_form()
        dialog = WeChatQRConnectDialog(
            channel=channel,
            channel_runtime=self._channel_runtime,
            force_new=True,
            parent=self,
        )
        result = dialog.exec()
        snapshot = dialog.snapshot
        if snapshot is None:
            return

        updated = self._apply_connection_snapshot_to_channel(channel, snapshot)
        self._channel = updated
        self._preferred_session_id = str(self._preferred_session_id or "").strip()
        self._load_channel(updated)
        if result == dialog.DialogCode.Accepted and str(snapshot.status or "").strip().lower() in {"connected", "ready"}:
            self.detail_label.setText(
                f"{snapshot.detail or '微信二维码连接完成。'}\n真实联系人发来消息后会自动创建并绑定会话。"
            )

    def _on_connection_mode_changed(self, _index: int) -> None:
        if self._loading:
            return
        channel = self._build_channel_from_form()
        config = dict(getattr(channel, "config", {}) or {})
        config["connection_mode"] = self._selected_connection_mode(config)
        channel = self._channel_manager.ensure_channel(
            ChannelConfig.from_dict(
                {
                    **channel.to_dict(),
                    "config": config,
                    "updated_at": int(time.time()),
                }
            )
        )
        self._update_connection_mode_hint()
        self._load_channel(channel)

    @staticmethod
    def _pick_first(config: dict, keys: tuple[str, ...], *, fallback: str = "") -> str:
        for key in keys:
            value = str(config.get(key, "") or "").strip()
            if value:
                return value
        return str(fallback or "").strip()

    def accept(self) -> None:
        channel = self._build_channel_from_form()
        self._channel = channel
        self._preferred_session_id = str(self._preferred_session_id or "").strip()
        super().accept()


__all__ = ["ChannelInstanceDialog"]
