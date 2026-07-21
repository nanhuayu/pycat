from __future__ import annotations

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from core.app.services.channel import ChannelService
from core.channel.sessions import ChannelConversationSummary
from models.contracts.channel import ChannelConfig
from gui.utils.icon_manager import Icons


class ChannelSessionChoiceItem(QListWidgetItem):
    def __init__(self, summary: ChannelConversationSummary | None, *, is_new: bool = False) -> None:
        super().__init__()
        self.summary = summary
        self.is_new = bool(is_new)

        if self.is_new:
            self.setText("新建对话\n保存后自动创建频道主会话")
            self.setIcon(Icons.get(Icons.PLUS, scale_factor=0.95))
            self.setToolTip("创建一个新的 PyCat 对话作为该频道的主绑定对话。")
            self.setSizeHint(QSize(0, 48))
            return

        assert summary is not None
        badges: list[str] = []
        if summary.is_primary_session:
            badges.append("当前绑定")
        if summary.is_manual_test_session:
            badges.append("手动会话")
        if summary.is_bound_to_other_channel:
            badges.append(f"已占用：{summary.bound_channel_name or summary.bound_channel_id}")
        if summary.participant_label:
            badges.append(summary.participant_label)

        subtitle = " · ".join(badges) if badges else "可绑定"
        self.setText(f"{summary.title}\n{subtitle}")
        self.setIcon(Icons.get(Icons.CHAT, scale_factor=0.9))
        self.setSizeHint(QSize(0, 52))
        self.setToolTip(
            f"会话 ID: {summary.conversation_id}\n"
            f"更新时间: {int(summary.updated_at) if summary.updated_at else '-'}\n"
            f"预览: {summary.preview or '-'}"
        )
        if summary.is_bound_to_other_channel:
            self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsEnabled)


class ChannelSessionPickerDialog(QDialog):
    def __init__(
        self,
        *,
        channel: ChannelConfig,
        channel_service: ChannelService,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._channel = channel
        self._channel_service = channel_service
        self._selected_conversation_id = ""
        self._setup_ui()
        self._load_choices()

    @property
    def selected_conversation_id(self) -> str:
        return str(self._selected_conversation_id or "").strip()

    def _setup_ui(self) -> None:
        self.setWindowTitle("选择绑定对话")
        self.setModal(True)
        self.resize(520, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel("默认新建对话；也可以选择现有 PyCat 对话作为主绑定。")
        title.setWordWrap(True)
        title.setProperty("muted", True)
        layout.addWidget(title)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("channel_session_picker_list")
        self.list_widget.setSpacing(3)
        self.list_widget.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.list_widget, 1)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_btn = button_box.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setText("使用选中对话")
            ok_btn.setProperty("primary", True)
        cancel_btn = button_box.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn is not None:
            cancel_btn.setText("取消")
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _load_choices(self) -> None:
        self.list_widget.clear()

        new_item = ChannelSessionChoiceItem(None, is_new=True)
        new_item.setData(Qt.ItemDataRole.UserRole, "")
        self.list_widget.addItem(new_item)

        current_session_id = str(getattr(self._channel, "session_id", "") or "").strip()
        bindable = list(self._channel_service.list_bindable_conversations(self._channel))
        current_row = 0
        for index, summary in enumerate(bindable, start=1):
            item = ChannelSessionChoiceItem(summary)
            item.setData(Qt.ItemDataRole.UserRole, summary.conversation_id)
            self.list_widget.addItem(item)
            if summary.conversation_id == current_session_id:
                current_row = index
        self.list_widget.setCurrentRow(current_row)

    def accept(self) -> None:
        item = self.list_widget.currentItem()
        if isinstance(item, ChannelSessionChoiceItem):
            self._selected_conversation_id = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        super().accept()


__all__ = ["ChannelSessionPickerDialog", "ChannelSessionChoiceItem"]
