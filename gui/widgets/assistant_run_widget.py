"""Collapsible rendering for one assistant response made of several tool steps."""

from __future__ import annotations

from typing import Callable, Iterable

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtWidgets import QFrame, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from gui.utils.icon_manager import Icons
from core.content.references import delivery_refs_for_messages
from gui.view_models.message_runs import AssistantRunGroup
from gui.widgets.message_widget import MessageWidget
from models.conversation import Message
from models.contracts.agent import RunStatus
from models.contracts.content import ContentRef


class AssistantRunWidget(QFrame):
    """Show intermediate assistant steps behind one compact process row."""

    continue_requested = pyqtSignal(str)
    regenerate_requested = pyqtSignal(str)
    delete_requested = pyqtSignal(str)

    def __init__(
        self,
        messages: Iterable[Message],
        parent=None,
        *,
        work_dir: str = "",
        show_thinking: bool = True,
        artifact_lookup: Callable[[str], object | None] | None = None,
        content_path_resolver: Callable[[ContentRef], object] | None = None,
        active: bool = False,
        allow_restart: bool = True,
    ):
        super().__init__(parent)
        self.setObjectName("assistant_run_group")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._messages = list(messages or [])
        self._work_dir = str(work_dir or "")
        self._show_thinking = bool(show_thinking)
        self._artifact_lookup = artifact_lookup
        self._content_path_resolver = content_path_resolver
        self._run_active = bool(active)
        self._allow_restart = bool(allow_restart)
        self._revision_enabled = True
        self._terminal_status: RunStatus | None = None
        self._manual_process_expanded: bool | None = None
        self._message_snapshots: dict[str, dict] = {}
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

    def set_active(self, active: bool = True) -> None:
        self._run_active = bool(active)
        if self._run_active:
            self._terminal_status = None
        self._sync_revision_enabled()
        self._sync_process_visibility()

    def finish(self, status: RunStatus) -> None:
        self._run_active = False
        self._terminal_status = status
        self._sync_revision_enabled()
        self._sync_process_visibility()

    def set_work_dir(self, work_dir: str) -> None:
        self._work_dir = str(work_dir or "")
        for widget in self.message_widgets:
            widget.set_work_dir(self._work_dir)

    @staticmethod
    def _take_layout_widgets(layout: QVBoxLayout) -> list[QWidget]:
        widgets: list[QWidget] = []
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widgets.append(widget)
        return widgets

    @staticmethod
    def _message_snapshot(message: Message) -> dict:
        return message.to_dict()

    def _effective_process_expanded(self) -> bool:
        if self._manual_process_expanded is not None:
            return self._manual_process_expanded
        if self._run_active:
            return True
        return self._terminal_status in {
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }

    def _sync_process_visibility(self) -> None:
        self.process_container.setVisible(
            self._effective_process_expanded() and bool(self.process_message_widgets)
        )

    def _create_message_widget(
        self,
        message: Message,
        *,
        process: bool,
        delivery_refs: Iterable[ContentRef] = (),
    ) -> MessageWidget:
        widget = MessageWidget(
            message,
            embedded=process,
            show_header=not process,
            show_thinking=self._show_thinking,
            work_dir=self._work_dir,
            artifact_lookup=self._artifact_lookup,
            content_path_resolver=self._content_path_resolver,
            delivery_refs=delivery_refs,
            allow_restart=self._allow_restart and not process,
        )
        widget.continue_requested.connect(self.continue_requested.emit)
        widget.regenerate_requested.connect(self.regenerate_requested.emit)
        if not process:
            widget.delete_requested.connect(self.delete_requested.emit)
        widget.set_revision_enabled(self._revision_enabled and not self._run_active)
        return widget

    def set_revision_enabled(self, enabled: bool) -> None:
        self._revision_enabled = bool(enabled)
        self._sync_revision_enabled()

    def _sync_revision_enabled(self) -> None:
        for widget in self.message_widgets:
            widget.set_revision_enabled(self._revision_enabled and not self._run_active)

    def _rebuild(self) -> None:
        old_widgets = self._take_layout_widgets(self.process_layout)
        old_widgets.extend(self._take_layout_widgets(self.primary_layout))
        old_by_id = {
            str(getattr(widget.message, "id", "") or ""): widget
            for widget in old_widgets
            if isinstance(widget, MessageWidget)
        }
        old_states = {
            message_id: widget.expansion_state()
            for message_id, widget in old_by_id.items()
            if message_id
        }
        self.message_widgets = []
        self.process_message_widgets = []
        self.primary_widget = None

        if not self._messages:
            self.process_toggle.setVisible(False)
            for widget in old_widgets:
                widget.deleteLater()
            self._message_snapshots = {}
            return

        run = AssistantRunGroup(tuple(self._messages))
        for message in run.process_messages:
            message_id = str(getattr(message, "id", "") or "")
            snapshot = self._message_snapshot(message)
            widget = old_by_id.get(message_id)
            if not (
                isinstance(widget, MessageWidget)
                and widget.embedded
                and self._message_snapshots.get(message_id) == snapshot
            ):
                widget = self._create_message_widget(message, process=True)
                widget.restore_expansion_state(old_states.get(message_id))
            self.process_layout.addWidget(widget)
            self.process_message_widgets.append(widget)
            self.message_widgets.append(widget)

        primary_message = run.primary_message
        primary_id = str(getattr(primary_message, "id", "") or "")
        self.primary_widget = self._create_message_widget(
            primary_message,
            process=False,
            delivery_refs=delivery_refs_for_messages(run.messages),
        )
        self.primary_widget.restore_expansion_state(old_states.get(primary_id))
        self.primary_layout.addWidget(self.primary_widget)
        self.message_widgets.append(self.primary_widget)

        retained = set(self.message_widgets)
        for widget in old_widgets:
            if widget not in retained:
                widget.deleteLater()
        self._message_snapshots = {
            str(getattr(message, "id", "") or ""): self._message_snapshot(message)
            for message in self._messages
            if str(getattr(message, "id", "") or "")
        }

        process_count = len(run.process_messages)
        self.process_toggle.setVisible(process_count > 0)
        self.process_toggle.setText(
            f"执行过程 · {process_count} 步 · {run.tool_call_count} 次工具调用 >"
        )
        self.process_toggle.setToolTip("展开本次回复的中间思考、工具调用和阶段输出")
        self._sync_process_visibility()

    def _toggle_process(self) -> None:
        self._manual_process_expanded = not self._effective_process_expanded()
        self._sync_process_visibility()
