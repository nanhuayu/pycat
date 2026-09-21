from __future__ import annotations

from typing import List

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QToolButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from pycat.core.app.services.channel import ChannelService
from pycat.core.channel import ChannelDefinition, ChannelInstance
from pycat.core.channel.connection import ChannelConnectionSnapshot, ChannelConnectionState
from pycat.models.contracts.channel import ChannelConfig
from pycat.gui.dialogs.channel_instance_dialog import ChannelInstanceDialog
from pycat.gui.dialogs.channel_session_picker_dialog import ChannelSessionPickerDialog
from pycat.gui.settings.page_header import build_page_header
from pycat.gui.settings.components import (
    SettingsActionBar,
    SettingsListDetailLayout,
    SettingsStatusListItem,
    configure_settings_resource_list,
)
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.widgets.themed_line_edit import ThemedSelectableLabel


class ChannelTypeItem(QListWidgetItem):
    def __init__(self, definition: ChannelDefinition):
        super().__init__()
        self.definition = definition
        self.setText(definition.name)
        self.setIcon(Icons.get(definition.icon_name, scale_factor=0.95))
        tags = f"\n标签：{' / '.join(definition.tags)}" if definition.tags else ""
        self.setToolTip(f"{definition.description}{tags}")
        self.setSizeHint(QSize(0, 40))


class ChannelInstanceItem(SettingsStatusListItem):
    def __init__(self, instance: ChannelInstance, snapshot: ChannelConnectionSnapshot):
        super().__init__(instance)
        self.instance = instance
        validation = "\n".join(instance.validation_errors) if instance.validation_errors else "配置校验通过"
        summary = instance.summary or instance.config.source or "未填写摘要"
        state_label = {
            ChannelConnectionState.DISABLED: "已停用",
            ChannelConnectionState.INCOMPLETE: "配置不完整",
            ChannelConnectionState.CONNECTING: "连接中",
            ChannelConnectionState.WAITING_USER: "等待操作",
            ChannelConnectionState.READY: "已连接",
            ChannelConnectionState.RECONNECTING: "正在重连",
            ChannelConnectionState.ERROR: "异常",
        }.get(snapshot.state, snapshot.state.value)
        self.set_status(
            instance.title,
            enabled=snapshot.state in {ChannelConnectionState.READY, ChannelConnectionState.CONNECTING, ChannelConnectionState.RECONNECTING},
            detail=state_label,
            tooltip=(
                f"类型：{instance.definition.name}\n"
                f"来源：{instance.config.source}\n"
                f"摘要：{summary}\n"
                f"校验：{validation}"
            ),
            two_lines=True,
        )


class ChannelsPage(QWidget):
    page_title = "频道"
    def __init__(
        self,
        channels: List[ChannelConfig],
        *,
        channel_service: ChannelService,
        parent=None,
    ):
        super().__init__(parent)
        self._channel_service = channel_service
        self._channel_catalog = channel_service.catalog
        self.channels = [self._channel_catalog.ensure_channel(channel) for channel in list(channels or [])]
        self._definitions = list(self._channel_catalog.definitions(featured_only=True))
        self._pending_focus_session_id = ""
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(
            build_page_header(
                "频道",
                "连接消息平台，管理绑定的对话。",
            )
        )

        body = SettingsListDetailLayout()
        left_panel = body.list_layout

        self.type_list = configure_settings_resource_list(QListWidget())
        self.type_list.currentRowChanged.connect(self._on_type_changed)
        left_panel.addWidget(self.type_list, 1)
        body.bind(self.type_list)
        right_panel = body.detail_layout
        right_panel.setSpacing(10)
        self._detail_layout = right_panel

        actions = SettingsActionBar(spacing=8)
        self.add_btn = actions.add_action("新增", Icons.get(Icons.PLUS), self._add_channel)
        self.edit_btn = actions.add_action("编辑", Icons.get(Icons.EDIT), self._edit_channel)
        self.toggle_btn = actions.add_action("启用", Icons.get(Icons.PLAY), self._toggle_channel_enabled)
        self.remove_btn = actions.add_action(
            "删除",
            Icons.get(Icons.XMARK, color=Icons.COLOR_ERROR),
            self._remove_channel,
            danger=True,
        )
        actions.add_stretch()
        right_panel.addWidget(actions)

        self.instance_list = configure_settings_resource_list(QListWidget(), minimum_width=180)
        self.instance_list.setMinimumHeight(190)
        self.instance_list.currentRowChanged.connect(self._on_instance_changed)
        self.instance_list.itemDoubleClicked.connect(lambda _item: self._edit_channel())
        right_panel.addWidget(self.instance_list, 3)

        self.detail_card = QFrame()
        self.detail_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.detail_card.setObjectName("settings_detail_card")
        detail_layout = QVBoxLayout(self.detail_card)
        detail_layout.setContentsMargins(10, 8, 10, 8)
        detail_layout.setSpacing(6)

        self.detail_summary = QLabel("请选择一个频道查看详情。")
        self.detail_summary.setWordWrap(True)
        detail_layout.addWidget(self.detail_summary)

        self.detail_hint = QLabel("")
        self.detail_hint.setWordWrap(True)
        self.detail_hint.setProperty("muted", True)
        detail_layout.addWidget(self.detail_hint)

        self.diagnostics_toggle = QToolButton()
        self.diagnostics_toggle.setText("诊断信息")
        self.diagnostics_toggle.setCheckable(True)
        self.diagnostics_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.diagnostics_toggle.toggled.connect(self._toggle_diagnostics)
        detail_layout.addWidget(self.diagnostics_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.diagnostics_label = ThemedSelectableLabel("")
        self.diagnostics_label.setWordWrap(True)
        self.diagnostics_label.setProperty("muted", True)
        self.diagnostics_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.diagnostics_label.setVisible(False)
        detail_layout.addWidget(self.diagnostics_label)

        detail_actions = SettingsActionBar(spacing=8)
        self.open_session_btn = detail_actions.add_action(
            "打开对话",
            Icons.get(Icons.CHAT),
            self._focus_bound_session,
        )
        self.change_session_btn = detail_actions.add_action(
            "更换对话",
            Icons.get(Icons.EDIT),
            self._change_bound_session,
        )
        detail_actions.add_stretch()
        detail_layout.addWidget(detail_actions)
        right_panel.addWidget(self.detail_card)
        right_panel.addStretch(0)

        layout.addWidget(body, 1)

        self._populate_type_list()
        self._update_action_state(False)
        if self.type_list.count() > 0:
            self.type_list.setCurrentRow(0)

    def _populate_type_list(self) -> None:
        self.type_list.clear()
        for definition in self._definitions:
            self.type_list.addItem(ChannelTypeItem(definition))

    def _current_definition(self) -> ChannelDefinition | None:
        item = self.type_list.currentItem()
        if isinstance(item, ChannelTypeItem):
            return item.definition
        return self._definitions[0] if self._definitions else None

    def _channels_for_type(self, channel_type: str) -> List[ChannelConfig]:
        normalized = str(channel_type or "").strip().lower()
        return [channel for channel in self.channels if str(channel.type or "").strip().lower() == normalized]

    def _on_type_changed(self, _row: int) -> None:
        definition = self._current_definition()
        if definition is None:
            return
        self._refresh_instance_list()

    def _refresh_instance_list(self, *, preferred_id: str = "") -> None:
        definition = self._current_definition()
        self.instance_list.clear()
        if definition is None:
            self._set_empty_overview("当前没有可用的频道类型。")
            self._update_action_state(False)
            return

        instances = [self._channel_catalog.build_instance(channel) for channel in self._channels_for_type(definition.type)]
        for instance in instances:
            self.instance_list.addItem(
                ChannelInstanceItem(instance, self._channel_service.connection_snapshot(instance.config))
            )

        if not instances:
            self._set_empty_overview("该频道类型还没有配置，点击“新增”开始。")
            self._update_action_state(False)
            return

        target_row = 0
        if preferred_id:
            for row in range(self.instance_list.count()):
                item = self.instance_list.item(row)
                if isinstance(item, ChannelInstanceItem) and item.instance.config.id == preferred_id:
                    target_row = row
                    break
        self.instance_list.setCurrentRow(target_row)

    def _on_instance_changed(self, row: int) -> None:
        item = self.instance_list.item(row)
        if not isinstance(item, ChannelInstanceItem):
            self._set_empty_overview("请选择一个频道查看详情。")
            self._update_action_state(False)
            return
        self._render_instance_overview(item.instance.config)
        self._update_action_state(True)

    def _set_empty_overview(self, message: str) -> None:
        self.instance_list.setVisible(self.instance_list.count() > 0)
        self._detail_layout.setStretch(self._detail_layout.count() - 1, 1)
        self.detail_card.show()
        self.detail_summary.setText(message)
        definition = self._current_definition()
        self.detail_hint.setText(definition.description if definition else '')
        self.detail_hint.setVisible(bool(definition))
        self.diagnostics_toggle.hide()
        self.diagnostics_label.setText("")
        self.open_session_btn.setEnabled(False)
        self.change_session_btn.setEnabled(False)
        self.open_session_btn.hide()
        self.change_session_btn.hide()

    def _render_instance_overview(self, channel: ChannelConfig) -> None:
        self.instance_list.show()
        self._detail_layout.setStretch(self._detail_layout.count() - 1, 0)
        self.detail_card.show()
        self.open_session_btn.show()
        self.change_session_btn.show()
        normalized = self._channel_catalog.ensure_channel(channel)
        instance = self._channel_catalog.build_instance(normalized)

        snapshot = self._channel_service.connection_snapshot(normalized)
        state_label = {
            ChannelConnectionState.DISABLED: "已停用",
            ChannelConnectionState.INCOMPLETE: "配置不完整",
            ChannelConnectionState.CONNECTING: "连接中",
            ChannelConnectionState.WAITING_USER: "等待操作",
            ChannelConnectionState.READY: "已连接",
            ChannelConnectionState.RECONNECTING: "正在重连",
            ChannelConnectionState.ERROR: "异常",
        }.get(snapshot.state, snapshot.state.value)
        session = str(normalized.session_id or "").strip()
        self.detail_summary.setText(f"{state_label} · " + ("已绑定对话" if session else "保存后自动新建对话"))
        self.detail_summary.setToolTip(session)

        hint_lines: list[str] = []
        if instance.validation_errors:
            hint_lines.append(f"校验：{'；'.join(instance.validation_errors)}")
        if str(snapshot.detail or "").strip():
            hint_lines.append(snapshot.detail)

        self.detail_hint.setText("\n".join(hint_lines))
        self.detail_hint.setVisible(bool(hint_lines))
        self.diagnostics_toggle.show()
        self.diagnostics_label.setText(
            f"ID: {normalized.id or '-'}\nSource: {normalized.source or '-'}\nMode: {snapshot.mode or '-'}\n绑定对话：{session or '-'}"
        )
        self.open_session_btn.setEnabled(bool(str(normalized.session_id or "").strip()))
        self.change_session_btn.setEnabled(True)

    def _update_action_state(self, has_selection: bool) -> None:
        self.edit_btn.setEnabled(bool(has_selection))
        self.toggle_btn.setEnabled(bool(has_selection))
        self.remove_btn.setEnabled(bool(has_selection))
        current = self._current_selected_channel()
        self.open_session_btn.setEnabled(bool(has_selection and current is not None and str(current.session_id or "").strip()))
        self.change_session_btn.setEnabled(bool(has_selection))
        if current is not None and current.enabled:
            self.toggle_btn.setText("停用")
            self.toggle_btn.setIcon(Icons.get(Icons.PAUSE, scale_factor=1.0))
        else:
            self.toggle_btn.setText("启用")
            self.toggle_btn.setIcon(Icons.get(Icons.PLAY, scale_factor=1.0))

    def _current_selected_channel(self) -> ChannelConfig | None:
        item = self.instance_list.currentItem()
        if isinstance(item, ChannelInstanceItem):
            return item.instance.config
        return None

    def _find_channel_index(self, channel_id: str) -> int:
        target = str(channel_id or "").strip()
        for index, channel in enumerate(self.channels):
            if str(channel.id or "").strip() == target:
                return index
        return -1

    def _edit_channel(self) -> None:
        current = self._current_selected_channel()
        if current is None:
            return
        updated = self._open_instance_dialog(current)
        if updated is None:
            return
        index = self._find_channel_index(current.id)
        if index < 0:
            return
        self.channels[index] = updated
        self._refresh_instance_list(preferred_id=updated.id)

    def _add_channel(self) -> None:
        definition = self._current_definition()
        if definition is None:
            return
        created = self._channel_service.create(definition.type)
        updated = self._open_instance_dialog(created)
        if updated is None:
            return
        self.channels.append(updated)
        self._refresh_instance_list(preferred_id=updated.id)

    def _remove_channel(self) -> None:
        current = self._current_selected_channel()
        if current is None:
            return
        index = self._find_channel_index(current.id)
        if index < 0:
            return
        self.channels.pop(index)
        if str(self._pending_focus_session_id or "").strip() == str(current.session_id or "").strip():
            self._pending_focus_session_id = ""
        self._refresh_instance_list()

    def _toggle_channel_enabled(self) -> None:
        current = self._current_selected_channel()
        if current is None:
            return
        index = self._find_channel_index(current.id)
        if index < 0:
            return
        try:
            updated = self._channel_service.set_enabled(current, not bool(current.enabled))
        except ValueError as exc:
            self.detail_hint.setText(str(exc))
            return
        self.channels[index] = updated
        self._refresh_instance_list(preferred_id=updated.id)

    def _focus_bound_session(self) -> None:
        current = self._current_selected_channel()
        if current is None:
            return
        session_id = str(getattr(current, "session_id", "") or "").strip()
        if session_id:
            self._pending_focus_session_id = session_id

    def _change_bound_session(self) -> None:
        current = self._current_selected_channel()
        if current is None:
            return
        dialog = ChannelSessionPickerDialog(
            channel=current,
            channel_service=self._channel_service,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        try:
            updated = self._channel_service.bind_session(current, dialog.selected_conversation_id)
        except Exception:
            return
        index = self._find_channel_index(current.id)
        if index < 0:
            return
        self.channels[index] = self._channel_catalog.ensure_channel(updated)
        self._pending_focus_session_id = str(getattr(updated, "session_id", "") or "").strip()
        self._refresh_instance_list(preferred_id=updated.id)

    def _open_instance_dialog(self, channel: ChannelConfig) -> ChannelConfig | None:
        dialog = ChannelInstanceDialog(
            channel=channel,
            channel_service=self._channel_service,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return None
        self._pending_focus_session_id = str(dialog.preferred_session_id or "").strip()
        return self._channel_catalog.ensure_channel(dialog.channel)

    def get_preferred_session_id(self) -> str:
        return str(self._pending_focus_session_id or "").strip()

    def collect(self) -> List[ChannelConfig]:
        return list(self.channels or [])

    def _toggle_diagnostics(self, visible: bool) -> None:
        self.diagnostics_label.setVisible(bool(visible))

    @staticmethod
    def _connection_label(channel: ChannelConfig) -> str:
        mode = str((channel.config or {}).get("connection_mode", "") or "").strip().lower()
        labels = {
            "ilink": "个人微信扫码",
            "webhook": "Webhook",
            "websocket": "长连接",
            "polling": "长轮询",
            "stream": "Stream 长连接",
        }
        return labels.get(mode, mode or "连接")
