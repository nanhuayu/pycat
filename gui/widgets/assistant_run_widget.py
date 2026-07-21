"""Collapsible rendering for one assistant response made of several tool steps."""

from __future__ import annotations

from typing import Callable, Iterable

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtWidgets import QFrame, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from gui.utils.icon_manager import Icons
from gui.view_models.message_runs import AssistantRunGroup
from gui.widgets.message_widget import MessageWidget
from models.conversation import Message


class AssistantRunWidget(QFrame):
    """Show intermediate assistant steps behind one compact process row."""

    edit_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)
    continue_requested = pyqtSignal(str)

    def __init__(
        self,
        messages: Iterable[Message],
        parent=None,
        *,
        work_dir: str = "",
        show_thinking: bool = True,
        artifact_lookup: Callable[[str], object | None] | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("assistant_run_group")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._messages = list(messages or [])
        self._work_dir = str(work_dir or "")
        self._show_thinking = bool(show_thinking)
        self._artifact_lookup = artifact_lookup
        self._process_expanded = False
        self.message_widgets: list[MessageWidget] = []
        self.process_message_widgets: list[MessageWidget] = []
        self.primary_widget: MessageWidget | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.process_toggle = QPushButton()
        self.process_toggle.setObjectName("assistant_run_toggle")
        self.process_toggle.setIcon(Icons.get_muted(Icons.TOOLS))
        self.process_toggle.setIconSize(QSize(18, 18))
        self.process_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.process_toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.process_toggle.clicked.connect(self._toggle_process)
        layout.addWidget(self.process_toggle)

        self.process_container = QFrame()
        self.process_container.setObjectName("assistant_run_process")
        self.process_layout = QVBoxLayout(self.process_container)
        self.process_layout.setContentsMargins(8, 2, 0, 4)
        self.process_layout.setSpacing(2)
        self.process_container.setVisible(False)
        layout.addWidget(self.process_container)

        self.primary_container = QWidget()
        self.primary_layout = QVBoxLayout(self.primary_container)
        self.primary_layout.setContentsMargins(0, 0, 0, 0)
        self.primary_layout.setSpacing(0)
        layout.addWidget(self.primary_container)
        self._rebuild()

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    @property
    def message_ids(self) -> tuple[str, ...]:
        return tuple(str(getattr(message, "id", "") or "") for message in self._messages)

    def set_messages(self, messages: Iterable[Message]) -> None:
        self._messages = list(messages or [])
        self._rebuild()

    def set_work_dir(self, work_dir: str) -> None:
        self._work_dir = str(work_dir or "")
        for widget in self.message_widgets:
            widget.set_work_dir(self._work_dir)

    def _clear_layout(self, layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _create_message_widget(self, message: Message, *, process: bool) -> MessageWidget:
        widget = MessageWidget(
            message,
            embedded=process,
            show_header=not process,
            show_thinking=self._show_thinking,
            work_dir=self._work_dir,
            artifact_lookup=self._artifact_lookup,
        )
        widget.edit_requested.connect(self.edit_requested.emit)
        widget.delete_requested.connect(self.delete_requested.emit)
        widget.continue_requested.connect(self.continue_requested.emit)
        return widget

    def _rebuild(self) -> None:
        self._clear_layout(self.process_layout)
        self._clear_layout(self.primary_layout)
        self.message_widgets = []
        self.process_message_widgets = []
        self.primary_widget = None

        if not self._messages:
            self.process_toggle.setVisible(False)
            return

        run = AssistantRunGroup(tuple(self._messages))
        for message in run.process_messages:
            widget = self._create_message_widget(message, process=True)
            self.process_layout.addWidget(widget)
            self.process_message_widgets.append(widget)
            self.message_widgets.append(widget)

        self.primary_widget = self._create_message_widget(run.primary_message, process=False)
        self.primary_layout.addWidget(self.primary_widget)
        self.message_widgets.append(self.primary_widget)

        process_count = len(run.process_messages)
        self.process_toggle.setVisible(process_count > 0)
        self.process_toggle.setText(
            f"执行过程 · {process_count} 步 · {run.tool_call_count} 次工具调用 >"
        )
        self.process_toggle.setToolTip("展开本次回复的中间思考、工具调用和阶段输出")
        self.process_container.setVisible(process_count > 0 and self._process_expanded)

    def _toggle_process(self) -> None:
        self._process_expanded = not self._process_expanded
        self.process_container.setVisible(self._process_expanded and bool(self.process_message_widgets))
