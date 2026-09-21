"""Project run-owned questions and approvals into the selected conversation."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton

from pycat.core.tools.base import ApprovalDecision, ToolApprovalRequest
from pycat.gui.runtime.message_runtime import UserInteraction
from pycat.gui.widgets.question_form import QuestionForm

if TYPE_CHECKING:
    from pycat.gui.main_window import MainWindow

logger = logging.getLogger(__name__)


@dataclass
class _ApprovalView:
    request: UserInteraction
    dialog: QMessageBox
    auto_button: QPushButton
    run_button: QPushButton | None = None


class InteractionPresenter:
    """Own only UI lifetimes and unsent form input; MessageRuntime owns requests."""

    def __init__(self, host: MainWindow) -> None:
        self._host = host
        self._question_forms: dict[str, QuestionForm] = {}
        self._approval: _ApprovalView | None = None
        self._disposed = False

    def refresh(self, _conversation_id: str = "") -> None:
        if self._disposed:
            return
        host = self._host
        pending = host.message_runtime.pending_interactions()
        host.sidebar.set_waiting_conversations({item.conversation_id for item in pending})
        visible_id = host.chat_view.conversation_id
        current_id = getattr(host.current_conversation, "id", "")
        current = next((item for item in pending if item.conversation_id == visible_id == current_id), None)
        if self._approval is not None and (current is None or current.id != self._approval.request.id):
            self._close_approval()

        form = None
        if current is not None and isinstance(current.payload, dict):
            form = self._question_forms.get(current.id)
            if form is None:
                form = QuestionForm(current.payload, parent=host.chat_view)
                form.submitted.connect(lambda answer, key=current.id: self._resolve(key, answer))
                form.cancelled.connect(lambda key=current.id: self._resolve(key))
                self._question_forms[current.id] = form
        host.chat_view.set_inline_question(form)

        live_ids = {item.id for item in pending}
        for key in tuple(self._question_forms):
            if key not in live_ids:
                self._question_forms.pop(key).deleteLater()

        if current is not None and isinstance(current.payload, ToolApprovalRequest) and self._approval is None:
            self._show_approval(current)
        if visible_id and visible_id == current_id and (not _conversation_id or _conversation_id == current_id):
            host.window_state_presenter.sync_runtime_state(current_id)

    def _resolve(self, interaction_id: str, answer=None) -> None:
        if not self._disposed:
            self._host.message_runtime.resolve_interaction(interaction_id, answer)
            self.refresh()

    def _show_approval(self, interaction: UserInteraction) -> None:
        request = interaction.payload
        try:
            active_modal = QApplication.activeModalWidget()
            parent = active_modal or self._host
            dialog = QMessageBox(parent)
            dialog.setObjectName("tool_approval_dialog")
            title = "工具执行确认"
            if request.requires_path_approval:
                title = "工具与文件访问确认" if request.requires_tool_approval else "文件访问确认"
            dialog.setWindowTitle(title)
            dialog.setIcon(QMessageBox.Icon.Warning if request.risk == "high" else QMessageBox.Icon.Question)
            dialog.setText(request.message or f"是否允许执行 {request.tool_name}？")
            conversation = self._host.current_conversation
            details = f"会话：{conversation.title or '新会话'}\n{request.tool_name}  ·  {request.category}  ·  {request.risk}"
            if request.requires_path_approval:
                details += "\n授权仅在当前任务内有效，不会写入会话或全局设置。"
            dialog.setInformativeText(details)
            dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            dialog.setDefaultButton(QMessageBox.StandardButton.No)
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            dialog.button(QMessageBox.StandardButton.Yes).setText("允许本次")
            no_button = dialog.button(QMessageBox.StandardButton.No)
            no_button.setText("禁止")
            dialog.setEscapeButton(no_button)
            run_button = None
            if request.requires_path_approval:
                run_button = dialog.addButton("本次任务读取此文件夹", QMessageBox.ButtonRole.AcceptRole)
            auto_button = dialog.addButton("本会话自动执行", QMessageBox.ButtonRole.AcceptRole)
            view = _ApprovalView(interaction, dialog, auto_button, run_button)
            self._approval = view
            dialog.finished.connect(lambda result: self._finish_approval(view, result))
            dialog.open()
            self.raise_pending_approval()
        except Exception as exc:
            logger.warning("Failed to show tool approval: %s", exc)
            self._close_approval()
            self._resolve(interaction.id)

    def _finish_approval(self, view: _ApprovalView, result: int) -> None:
        if self._approval is not view or self._disposed:
            return
        interaction = view.request
        if not any(item.id == interaction.id for item in self._host.message_runtime.pending_interactions()):
            self.refresh()
            return
        clicked = None if int(result) == 0 else view.dialog.clickedButton()
        if clicked is view.auto_button:
            try:
                updated = self._host.conversation_presenter.update_tool_approval(
                    "allow", confirm_allow=False, conversation_id=interaction.conversation_id,
                )
            except Exception as exc:
                logger.warning("Failed to allow tools for conversation %s: %s", interaction.conversation_id, exc)
                updated = False
            if not updated:
                view.dialog.open()
                self.raise_pending_approval()
                return
        approved_once = clicked is not None and view.dialog.standardButton(clicked) == QMessageBox.StandardButton.Yes
        if view.run_button is not None and clicked is view.run_button:
            decision = ApprovalDecision(approved=True, read_scope="run")
        elif clicked is view.auto_button or approved_once:
            decision = ApprovalDecision(approved=True, read_scope="call" if interaction.payload.requires_path_approval else "")
        else:
            decision = ApprovalDecision()
        self._close_approval()
        self._resolve(interaction.id, decision)

    def _close_approval(self) -> None:
        view, self._approval = self._approval, None
        if view is not None:
            # Detach before close: a navigation or cancellation is not a denial.
            view.dialog.close()
            view.dialog.deleteLater()

    def raise_pending_approval(self) -> bool:
        if self._approval is None:
            return False
        dialog = self._approval.dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return True

    def dispose(self) -> None:
        self._disposed = True
        self._close_approval()
        self._host.chat_view.set_inline_question(None)
        for form in self._question_forms.values():
            form.deleteLater()
        self._question_forms.clear()
