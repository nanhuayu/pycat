from __future__ import annotations

import time
import uuid
from typing import List

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.channel import ChannelDefinition, ChannelInstance, default_channel_manager
from core.channel.runtime import ChannelRuntimeService
from core.config.schema import ChannelConfig
from ui.dialogs.channel_instance_dialog import ChannelInstanceDialog
from ui.settings.page_header import build_page_header
from ui.utils.icon_manager import Icons


class ChannelTypeItem(QListWidgetItem):
    def __init__(self, definition: ChannelDefinition):
        super().__init__()
        self.definition = definition
        self.setText(definition.name)
        self.setIcon(Icons.get(definition.icon_name, scale_factor=0.95))
        tags = f"\n标签：{' / '.join(definition.tags)}" if definition.tags else ""
        self.setToolTip(f"{definition.description}{tags}")
        self.setSizeHint(QSize(0, 40))


class ChannelInstanceItem(QListWidgetItem):
    def __init__(self, instance: ChannelInstance):
        super().__init__()
        self.instance = instance
        enabled_label = "启用" if instance.config.enabled else "停用"
        self.setText(f"{instance.title} · {enabled_label} · {instance.status_label}")
        self.setIcon(Icons.get(instance.definition.icon_name, scale_factor=0.9))
        validation = "\n".join(instance.validation_errors) if instance.validation_errors else "配置校验通过"
        summary = instance.summary or instance.config.source or "未填写摘要"
        self.setToolTip(
            f"类型：{instance.definition.name}\n"
            f"来源：{instance.config.source}\n"
            f"摘要：{summary}\n"
            f"校验：{validation}"
        )
        self.setSizeHint(QSize(0, 40))


class ChannelsPage(QWidget):
    page_title = "频道"
    _WECHAT_BRIDGE_BASE = "https://ilinkai.weixin.qq.com"

    def __init__(self, channels: List[ChannelConfig], channel_runtime: ChannelRuntimeService | None = None, parent=None):
        super().__init__(parent)
        self._channel_manager = default_channel_manager()
        self._channel_runtime = channel_runtime
        self.channels = [self._channel_manager.ensure_channel(channel) for channel in list(channels or [])]
        self._definitions = list(self._channel_manager.definitions(featured_only=True))
        self._pending_focus_session_id = ""
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(
            build_page_header(
                "频道",
                "按频道类型管理连接。列表负责启用、编辑、删除；连接参数和会话绑定放入独立弹窗。",
            )
        )

        body = QHBoxLayout()
        body.setSpacing(14)

        left_panel = QVBoxLayout()
        left_panel.setSpacing(8)
        left_title = QLabel("频道类型")
        left_title.setProperty("heading", True)
        left_panel.addWidget(left_title)

        self.type_list = QListWidget()
        self.type_list.setObjectName("settings_list")
        self.type_list.setMinimumWidth(180)
        self.type_list.setMaximumWidth(220)
        self.type_list.currentRowChanged.connect(self._on_type_changed)
        left_panel.addWidget(self.type_list, 1)
        body.addLayout(left_panel, 2)

        right_panel = QVBoxLayout()
        right_panel.setSpacing(10)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.add_btn = QPushButton("新增")
        self.add_btn.setIcon(Icons.get(Icons.PLUS, scale_factor=1.0))
        self.add_btn.clicked.connect(self._add_channel)
        actions.addWidget(self.add_btn)

        self.edit_btn = QPushButton("编辑")
        self.edit_btn.setIcon(Icons.get(Icons.EDIT, scale_factor=1.0))
        self.edit_btn.clicked.connect(self._edit_channel)
        actions.addWidget(self.edit_btn)

        self.toggle_btn = QPushButton("启用")
        self.toggle_btn.setIcon(Icons.get(Icons.PLAY, scale_factor=1.0))
        self.toggle_btn.clicked.connect(self._toggle_channel_enabled)
        actions.addWidget(self.toggle_btn)

        self.remove_btn = QPushButton("删除")
        self.remove_btn.setProperty("danger", True)
        self.remove_btn.setIcon(Icons.get(Icons.TRASH, color=Icons.COLOR_ERROR, scale_factor=1.0))
        self.remove_btn.clicked.connect(self._remove_channel)
        actions.addWidget(self.remove_btn)
        actions.addStretch(1)
        right_panel.addLayout(actions)

        self.instance_list = QListWidget()
        self.instance_list.setObjectName("settings_list")
        self.instance_list.setSpacing(2)
        self.instance_list.setMinimumHeight(190)
        self.instance_list.currentRowChanged.connect(self._on_instance_changed)
        self.instance_list.itemDoubleClicked.connect(lambda _item: self._edit_channel())
        right_panel.addWidget(self.instance_list, 3)

        self.detail_card = QFrame()
        self.detail_card.setObjectName("task_card")
        detail_layout = QVBoxLayout(self.detail_card)
        detail_layout.setContentsMargins(14, 12, 14, 12)
        detail_layout.setSpacing(6)

        self.detail_title = QLabel("详情")
        self.detail_title.setProperty("heading", True)
        detail_layout.addWidget(self.detail_title)

        self.detail_meta = QLabel("")
        self.detail_meta.setWordWrap(True)
        self.detail_meta.setProperty("muted", True)
        detail_layout.addWidget(self.detail_meta)

        self.detail_summary = QLabel("请选择一个频道查看详情。")
        self.detail_summary.setWordWrap(True)
        detail_layout.addWidget(self.detail_summary)

        self.detail_hint = QLabel("扫码、长连接状态和会话绑定在编辑弹窗中处理。")
        self.detail_hint.setWordWrap(True)
        self.detail_hint.setProperty("muted", True)
        detail_layout.addWidget(self.detail_hint)
        right_panel.addWidget(self.detail_card, 2)

        body.addLayout(right_panel, 5)
        layout.addLayout(body, 1)

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

        instances = [self._channel_manager.build_instance(channel) for channel in self._channels_for_type(definition.type)]
        for instance in instances:
            self.instance_list.addItem(ChannelInstanceItem(instance))

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
        self.detail_title.setText("详情")
        self.detail_meta.setText("")
        self.detail_summary.setText(message)
        self.detail_hint.setText("编辑弹窗负责连接配置、会话绑定和扫码等细节。")

    def _render_instance_overview(self, channel: ChannelConfig) -> None:
        normalized = self._channel_manager.ensure_channel(channel)
        instance = self._channel_manager.build_instance(normalized)

        meta_parts = ["已启用" if normalized.enabled else "已停用", instance.status_label]
        if str(normalized.source or "").strip():
            meta_parts.append(f"来源：{normalized.source}")
        self.detail_title.setText(instance.title)
        self.detail_meta.setText(" · ".join(meta_parts))

        summary = instance.summary or normalized.source or "未填写摘要"
        if str(normalized.session_id or "").strip():
            summary += f"\n手动会话：{normalized.session_id}"
        self.detail_summary.setText(summary)

        hint_lines: list[str] = []
        if instance.validation_errors:
            hint_lines.append(f"校验：{'；'.join(instance.validation_errors)}")
        else:
            hint_lines.append("校验：配置字段完整。")

        if self._channel_runtime is not None:
            try:
                snapshot = self._channel_runtime.get_channel_connection_snapshot(normalized)
            except Exception as exc:
                hint_lines.append(f"连接说明读取失败：{exc}")
            else:
                if str(snapshot.detail or "").strip():
                    hint_lines.append(f"连接：{snapshot.detail}")

        channel_type = str(normalized.type or "").strip().lower()
        if channel_type == "wechat":
            hint_lines.append("二维码连接和会话切换在编辑弹窗中处理。")
        elif channel_type == "feishu":
            hint_lines.append("推荐使用长连接模式；Webhook 仅保留为高级兼容选项。")
        else:
            hint_lines.append("更多连接配置请在编辑弹窗中完成。")

        self.detail_hint.setText("\n".join(hint_lines))

    def _update_action_state(self, has_selection: bool) -> None:
        self.edit_btn.setEnabled(bool(has_selection))
        self.toggle_btn.setEnabled(bool(has_selection))
        self.remove_btn.setEnabled(bool(has_selection))
        current = self._current_selected_channel()
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

    def _build_new_channel(self, definition: ChannelDefinition) -> ChannelConfig:
        now = int(time.time())
        base_name = definition.default_name or definition.name
        next_index = len(self._channels_for_type(definition.type)) + 1
        name = f"{base_name} {next_index}"
        channel_id = uuid.uuid4().hex[:12]
        config = dict(definition.default_config or {})

        channel_type = str(definition.type or "").strip().lower()
        if channel_type == "wechat":
            config["connection_mode"] = "qr-bridge"
            config.setdefault("bridge_api_base", self._WECHAT_BRIDGE_BASE)
            config.setdefault("callback_path", f"/wechat/{channel_id}")
        elif channel_type == "feishu":
            config["connection_mode"] = "websocket"
            config.setdefault("callback_path", f"/feishu/{channel_id}")
        elif channel_type == "qqbot":
            config["connection_mode"] = "websocket"
            config.setdefault("callback_path", f"/qqbot/{channel_id}")

        return self._channel_manager.ensure_channel(
            ChannelConfig(
                id=channel_id,
                name=name,
                type=definition.type,
                enabled=False,
                source=definition.normalize_source(name),
                status="draft",
                created_at=now,
                updated_at=now,
                config=config,
            )
        )

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
        created = self._build_new_channel(definition)
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
        updated = self._channel_manager.ensure_channel(
            ChannelConfig.from_dict(
                {
                    **current.to_dict(),
                    "enabled": not bool(current.enabled),
                    "updated_at": int(time.time()),
                }
            )
        )
        self.channels[index] = updated
        self._refresh_instance_list(preferred_id=updated.id)

    def _open_instance_dialog(self, channel: ChannelConfig) -> ChannelConfig | None:
        dialog = ChannelInstanceDialog(
            channel=channel,
            channel_runtime=self._channel_runtime,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return None
        self._pending_focus_session_id = str(dialog.preferred_session_id or "").strip()
        return self._channel_manager.ensure_channel(dialog.channel)

    def get_preferred_session_id(self) -> str:
        return str(self._pending_focus_session_id or "").strip()

    def collect(self) -> List[ChannelConfig]:
        return list(self.channels or [])
