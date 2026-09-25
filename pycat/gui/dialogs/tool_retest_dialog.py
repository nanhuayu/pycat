"""A small editor for a new tool call in the selected conversation."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import Future

from PyQt6.QtCore import QCoreApplication, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from pycat.core.observability.reader import payload_has_gaps
from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.utils.window_geometry import apply_window_size
from pycat.gui.widgets.capsule import SingleLineLabel
from pycat.gui.widgets.themed_line_edit import ThemedPlainTextEdit


def tool_call_code(name: str, arguments: dict, conversation_id: str) -> str:
    # JSON is quoted as a Python string, never interpolated as Python statements.
    payload = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
    return (
        "import json\n\n"
        "# app 是已打开、拥有该会话的 PyCat 实例；在当前环境执行。\n"
        "# approval_callback 使用宿主已有审批；省略时需要审批的操作会被拒绝。\n"
        "async def retest(app, approval_callback=None):\n"
        f"    arguments = json.loads({payload!r})\n"
        "    return await app.tools.call(\n"
        f"        {name!r}, arguments, conversation_id={conversation_id!r},\n"
        "        approval_callback=approval_callback,\n"
        "    )\n\n"
        "# 在现有异步宿主中：receipt = await retest(app, approval_callback)\n"
    )


def show_sdk_dialog(parent, conversation_id: str, run_id: str = "", node_id: str = ""):
    dialog = QDialog(parent)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.setWindowTitle(QCoreApplication.translate('ToolRetestDialog', 'SDK 用法'))
    apply_window_size(dialog, preferred=(760, 560), minimum=(560, 400))
    layout = QVBoxLayout(dialog)
    note = QLabel(QCoreApplication.translate('ToolRetestDialog', '使用已有 PyCat 实例；独立脚本不能同时打开桌面正在使用的 data_dir。'))
    note.setWordWrap(True)
    layout.addWidget(note)
    code = ThemedPlainTextEdit()
    code.setReadOnly(True)
    code.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    code.setPlainText(
        f"conversation_id = {conversation_id!r}\nrun_id = {run_id!r}\nnode_id = {node_id!r}\n\n"
        "# 最近已保存状态；活动状态由 run.events() 的同一个消费者读取。\n"
        "conversation = app.conversations.load(conversation_id)\n"
        "if conversation is None:\n    raise ValueError('Conversation not found')\n"
        "state = conversation.get_state().to_dict()\n\n"
        "page = app.conversations.trace(conversation_id, run_id=run_id, cursor=None)\n"
        "node = app.conversations.trace_node(\n"
        "    conversation_id, node_id, run_id=run_id, events=page['events'],\n)\n\n"
        "# 在现有异步宿主中：\n"
        "# schemas = await app.tools.list(conversation_id=conversation_id)\n"
        "# 选中工具步骤后可复制具体调用代码；参数不完整时先核对补齐。\n"
        "# app.start(...) / run.submit_guidance(...) / run.cancel()\n"
    )
    layout.addWidget(code)
    copy = QPushButton(QCoreApplication.translate('ToolRetestDialog', '复制示例'))
    copy.clicked.connect(lambda: QApplication.clipboard().setText(code.toPlainText()))
    layout.addWidget(copy, alignment=Qt.AlignmentFlag.AlignRight)
    dialog.show()
    return dialog


class ToolRetestDialog(QDialog):
    approval_requested = pyqtSignal(object, object)

    def __init__(self, conversation_id: str, tool: dict, parent=None, *, services, on_finished=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(QCoreApplication.translate('ToolRetestDialog', '复测工具'))
        apply_window_size(self, preferred=(720, 610), minimum=(560, 460))
        self._services, self._conversation_id, self._name = services, conversation_id, tool["name"]
        self._on_finished = on_finished
        self._closed, self._running = False, False
        self._schema = None
        self._work_dir = None
        self._pending_approvals = []
        self._needs_edit = tool.get("arguments_status") != "ok"
        self._load_job = None
        self._load_future = None
        self.approval_requested.connect(self._approve)
        self.finished.connect(self._abandon)
        self.destroyed.connect(lambda: self._abandon())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        title = SingleLineLabel(self._name)
        title.setObjectName("trace_node_title")
        layout.addWidget(title)
        self.target_label = SingleLineLabel(QCoreApplication.translate('ToolRetestDialog', '正在读取目标会话与可用工具…'))
        layout.addWidget(self.target_label)
        self.notice = QLabel(tool.get("note") or QCoreApplication.translate('ToolRetestDialog', '在当前环境执行一次新调用，沿用会话权限，原 Trace 保持只读。'))
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        row = QHBoxLayout()
        row.addWidget(QLabel(QCoreApplication.translate('ToolRetestDialog', '参数 JSON')))
        row.addStretch()
        schema_btn = QPushButton(QCoreApplication.translate('ToolRetestDialog', '参数说明'))
        schema_btn.setCheckable(True)
        row.addWidget(schema_btn)
        layout.addLayout(row)
        self.schema_text = ThemedPlainTextEdit()
        self.schema_text.setReadOnly(True)
        self.schema_text.setMaximumHeight(130)
        self.schema_text.hide()
        schema_btn.toggled.connect(self.schema_text.setVisible)
        layout.addWidget(self.schema_text)
        self.arguments_edit = ThemedPlainTextEdit()
        self.arguments_edit.setAccessibleName(QCoreApplication.translate('ToolRetestDialog', '工具参数 JSON'))
        self.arguments_edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.arguments_edit.setPlainText(json.dumps(tool.get("arguments") or {}, ensure_ascii=False, indent=2))
        self.arguments_edit.textChanged.connect(self._edited)
        layout.addWidget(self.arguments_edit, 1)
        buttons = QHBoxLayout()
        self.copy_btn = QPushButton(QCoreApplication.translate('ToolRetestDialog', '复制调用代码'))
        self.copy_btn.clicked.connect(self._copy_code)
        buttons.addWidget(self.copy_btn)
        buttons.addStretch()
        self.execute_btn = QPushButton(QCoreApplication.translate('ToolRetestDialog', '复测工具'))
        self.execute_btn.setProperty("primary", True)
        self.execute_btn.clicked.connect(self._execute)
        buttons.addWidget(self.execute_btn)
        layout.addLayout(buttons)
        layout.addWidget(QLabel(QCoreApplication.translate('ToolRetestDialog', '本次复测回执')))
        self.result_text = ThemedPlainTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setPlaceholderText(QCoreApplication.translate('ToolRetestDialog', '执行后显示新的结果与 Archive 引用。'))
        layout.addWidget(self.result_text, 1)
        self._update_actions()
        self._load_catalog()

    def _load_catalog(self):
        services, cid = self._services, self._conversation_id
        async def load():
            return services.conv_service.load(cid), await services.tools.list(conversation_id=cid)
        try:
            self._load_future = services.run_service.schedule(load())
            self._load_job = BackgroundJob(self._load_future.result)
            self._load_job.signals.finished.connect(self._catalog_ready)
            QThreadPool.globalInstance().start(self._load_job)
        except Exception as exc:
            self.notice.setText(QCoreApplication.translate('ToolRetestDialog', '工具目录读取失败：{exc}').format(exc=exc))

    def _catalog_ready(self, result, error):
        self._load_job = None
        if self._closed:
            return
        if error:
            self.notice.setText(QCoreApplication.translate('ToolRetestDialog', '工具目录读取失败：{error}').format(error=error))
            return
        conversation, schemas = result
        if conversation is None:
            self.notice.setText(QCoreApplication.translate('ToolRetestDialog', '目标会话不存在。'))
            return
        self._work_dir = conversation.work_dir
        self.target_label.setText(QCoreApplication.translate('ToolRetestDialog', '{title} · {value} · 沿用当前会话权限').format(title=conversation.title, value=conversation.work_dir or QCoreApplication.translate('ToolRetestDialog', '未选择工作区')))
        self.target_label.setToolTip(QCoreApplication.translate('ToolRetestDialog', '会话：{_conversation_id}\n工作目录：{value}').format(_conversation_id=self._conversation_id, value=conversation.work_dir or QCoreApplication.translate('ToolRetestDialog', '未选择')))
        self._schema = next((s["function"] for s in schemas if s.get("function", {}).get("name") == self._name), None)
        self.schema_text.setPlainText(json.dumps(self._schema or {}, ensure_ascii=False, indent=2))
        if self._schema is None or self._name.startswith(("agent__", "user__", "context__")):
            self._schema = None
            self.notice.setText(QCoreApplication.translate('ToolRetestDialog', '该工具在当前会话不可独立调用，请通过 Agent 运行。'))
        self._update_actions()

    def _arguments(self):
        value = json.loads(self.arguments_edit.toPlainText())
        json.dumps(value, allow_nan=False)
        if not isinstance(value, dict) or payload_has_gaps(value):
            raise ValueError(QCoreApplication.translate('ToolRetestDialog', '参数必须是完整的 JSON 对象，不能包含裁剪或脱敏占位。'))
        required = (self._schema or {}).get("parameters", {}).get("required", [])
        if any(key not in value for key in required):
            raise ValueError(QCoreApplication.translate('ToolRetestDialog', '请按参数说明补齐必填字段。'))
        if self._needs_edit:
            raise ValueError(QCoreApplication.translate('ToolRetestDialog', '历史参数不完整，请核对并编辑后再复测。'))
        return value

    def _edited(self):
        self._needs_edit = False
        self._update_actions()

    def _update_actions(self):
        self.execute_btn.setToolTip("")
        try:
            self._arguments()
            valid = True
        except ValueError as exc:
            valid = False
            self.execute_btn.setToolTip(str(exc))
        enabled = valid and self._schema is not None and not self._running
        self.execute_btn.setEnabled(enabled)
        self.copy_btn.setEnabled(enabled)

    def _copy_code(self):
        QApplication.clipboard().setText(tool_call_code(self._name, self._arguments(), self._conversation_id))

    def _execute(self):
        if self._running or self._schema is None:
            return
        if self._services.conv_service.is_active(self._conversation_id):
            self.notice.setText(QCoreApplication.translate('ToolRetestDialog', '目标会话正在运行，请结束后再复测。'))
            return
        try:
            arguments = self._arguments()
        except ValueError as exc:
            self.notice.setText(str(exc))
            return
        async def approve(request):
            decision = Future()
            self._pending_approvals.append(decision)
            try:
                if self._closed:
                    return False
                self.approval_requested.emit(request, decision)
                return await asyncio.wrap_future(decision)
            except RuntimeError:
                return False  # The Qt host was destroyed before approval.
            finally:
                self._pending_approvals.remove(decision)
        try:
            future = self._services.run_service.schedule(self._services.tools.call(
                self._name, arguments, conversation_id=self._conversation_id, work_dir=self._work_dir,
                approval_callback=approve))
        except Exception as exc:
            self.notice.setText(str(exc))
            return
        self._running = True
        self.arguments_edit.setReadOnly(True)
        self.notice.setText(QCoreApplication.translate('ToolRetestDialog', '正在当前环境复测。关闭窗口后，已开始的调用仍会保存回执。'))
        self._update_actions()
        job = BackgroundJob(future.result)
        if self._on_finished:
            job.signals.finished.connect(self._on_finished)
        job.signals.finished.connect(self._finished)
        QThreadPool.globalInstance().start(job)

    def _approve(self, request, decision):
        if decision.done():
            return
        approved = False
        if not self._closed:
            approved = QMessageBox.question(self, QCoreApplication.translate('ToolRetestDialog', '工具执行确认'), str(request.message or request.tool_name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        if not decision.done():
            decision.set_result(approved)

    def _finished(self, receipt, error):
        if self._closed:
            return
        self._running = False
        self.arguments_edit.setReadOnly(False)
        if error:
            self.notice.setText(QCoreApplication.translate('ToolRetestDialog', '复测失败：{error}').format(error=error))
        else:
            self.notice.setText(QCoreApplication.translate('ToolRetestDialog', '工具返回错误，回执已保存。') if receipt.is_error else QCoreApplication.translate('ToolRetestDialog', '复测完成，新的回执已保存。'))
            self.result_text.setPlainText(json.dumps({"call_id": receipt.id, "tool_name": receipt.name,
                "is_error": receipt.is_error, "content": receipt.content, "result": receipt.record}, ensure_ascii=False, indent=2))
        self._update_actions()

    def _abandon(self):
        self._closed = True
        if self._load_job:
            self._load_job.abandon()
        if self._load_future:
            self._load_future.cancel()
        for decision in tuple(self._pending_approvals):
            if not decision.done():
                decision.set_result(False)

    def closeEvent(self, event):
        self._abandon()
        super().closeEvent(event)
