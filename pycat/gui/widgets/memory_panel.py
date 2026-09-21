"""Scope and record projection for short memory; editing belongs to shared detail."""
import re

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QToolButton, QLabel, QListWidgetItem
from pycat.gui.widgets.capsule import CapsuleList, CapsuleDelegate
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import INSPECTOR_MARGIN, configure_icon_button
from pycat.models.session_paths import has_active_workspace


class MemoryPanel(QWidget):
    opened = pyqtSignal(str, str)
    retry_requested = pyqtSignal()
    assign_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.snapshot = {}
        self._workspace = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(INSPECTOR_MARGIN, INSPECTOR_MARGIN, INSPECTOR_MARGIN, INSPECTOR_MARGIN)
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.setSpacing(4)
        self.scope = QComboBox()
        self.scope.setAccessibleName("记忆范围")
        self.scope.currentIndexChanged.connect(self.render)
        header.addWidget(self.scope, 1)
        self.add = QToolButton()
        configure_icon_button(self.add, Icons.get_muted(Icons.PLUS), "新增记忆")
        self.add.clicked.connect(lambda: self.opened.emit(self.scope.currentData(), ""))
        header.addWidget(self.add)
        layout.addLayout(header)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setProperty("muted", True)
        layout.addWidget(self.status)
        self.details_toggle = QToolButton()
        self.details_toggle.setText("整理详情")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        layout.addWidget(self.details_toggle)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setProperty("muted", True)
        layout.addWidget(self.details)
        self.details_toggle.toggled.connect(self._toggle_details)
        self.entries = CapsuleList()
        self.entries.setAccessibleName("记忆条目")
        self.entries.itemClicked.connect(self._open)
        self.entries.itemActivated.connect(self._open)
        layout.addWidget(self.entries, 1)
        self.counter = QLabel()
        self.counter.setProperty("muted", True)
        layout.addWidget(self.counter)
        self.retry = QToolButton()
        self.retry.setText("重试整理")
        self.retry.clicked.connect(self.retry_requested)
        layout.addWidget(self.retry)
        self.assign = QToolButton()
        self.assign.setText("导入待归属的旧记忆")
        self.assign.clicked.connect(self.assign_requested)
        layout.addWidget(self.assign)
        self.set_workspace("")

    def set_workspace(self, workspace):
        if workspace != self._workspace:
            self._workspace = workspace
            self.scope.blockSignals(True)
            self.scope.clear()
            if has_active_workspace(workspace):
                self.scope.addItem("项目记忆", "memory")
            self.scope.addItem("用户偏好", "user")
            self.scope.blockSignals(False)
            self.snapshot = {}
            self.render()

    def apply(self, snapshot):
        self.snapshot = snapshot
        self.render()

    def render(self, _index=0):
        selected = self.entries.currentItem()
        entry_id = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        scroll = self.entries.verticalScrollBar().value()
        target = next((item for item in self.snapshot.get("targets", []) if item["target"] == self.scope.currentData()), {})
        records = target.get("records", [])
        status = str(self.snapshot.get("status", "正在读取…"))
        scope_hint = "本项目的会话共用" if self.scope.currentData() == "memory" else "跨项目使用的用户偏好"
        self.scope.setToolTip(scope_hint)
        self.scope.setAccessibleDescription(scope_hint)
        if status.startswith("已记住"):
            status = "" if records else "暂无记忆；任务结束后会在后台整理有用事实。"
        evolution = self.snapshot.get("evolution") or {}
        project_jobs = self.scope.currentData() == "memory" or not has_active_workspace(self._workspace)
        if not project_jobs:
            status = "未启用" if not self.snapshot.get("enabled", True) else "" if records else "暂无用户偏好"
        error = str(target.get("error") or self.snapshot.get("error") or "")
        self.set_status("\n".join(part for part in (status, error if error not in status else "") if part))
        attention = int(evolution.get("attention_count", evolution.get("failure_count", 0)) or 0)
        details_visible = project_jobs and bool(attention or not evolution.get("available", True))
        self.details_toggle.setVisible(details_visible)
        labels = {"version_conflict": "版本冲突", "capacity": "容量不足", "invalid_plan": "整理格式需修正", "transient": "暂时无法处理"}
        detail = ["项目后台整理（包含记忆、知识和技能）" if has_active_workspace(self._workspace) else "后台整理"]
        detail += [f"{labels.get(code, code)}：{count} 项" for code, count in evolution.get("reason_counts", {}).items()]
        if evolution.get("last_error"):
            detail.append(str(evolution["last_error"]))
        self.details.setText("\n".join(detail))
        self.details.setVisible(details_visible and self.details_toggle.isChecked())
        self.status.setToolTip(detail[0] if project_jobs else scope_hint)
        self.counter.setText(f"{len(records)} 条记录")
        self.counter.setVisible(bool(records))
        self.entries.clear()
        for entry in records:
            title = self._entry_title(entry)
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, entry["id"])
            item.setIcon(Icons.get_muted(Icons.BRAIN))
            note = "旧版记录" if entry.get("origin") == "legacy" else ""
            item.setData(CapsuleDelegate.DetailRole, note)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, "\n".join(filter(None, [title, note, entry["text"]])))
            item.setToolTip(entry["text"])
            self.entries.addItem(item)
            if entry["id"] == entry_id:
                self.entries.setCurrentItem(item)
        self.entries.verticalScrollBar().setValue(scroll)
        self.retry.setVisible(project_jobs and bool(attention))
        self.retry.setEnabled(bool(self.snapshot.get("enabled") and not evolution.get("inflight")))
        self.assign.setVisible(bool(self.snapshot.get("unassigned_count")))
        self.add.setEnabled(bool(self.snapshot.get("enabled") and target.get("readable", self.snapshot.get("readable"))))
        self.assign.setEnabled(bool(self.snapshot.get("enabled")))
        self.add.setToolTip("新增记忆" if self.add.isEnabled() else "此会话未启用记忆或存储不可读，无法新增。")

    def set_status(self, text):
        self.status.setText(str(text or ""))
        self.status.setVisible(bool(text))

    def _toggle_details(self, expanded):
        self.details_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.details.setVisible(expanded and not self.details_toggle.isHidden())

    @staticmethod
    def _entry_title(entry):
        lines = [line.strip() for line in entry["text"].splitlines() if line.strip()]
        content = [line for line in lines if not re.match(r"^#{1,6}(?:\s|$)", line)]
        link = r"^(?:[-*+]\s+|\d+[.)]\s+)?\[[^\]]+\]\([^)]+\)\s*$"
        if entry.get("origin") == "legacy" and content and all(re.fullmatch(link, line) for line in content):
            return "旧版记忆目录"
        return (content[0] if content else lines[0].lstrip("# ") if lines else "空记录")

    def _open(self, item):
        if item is not None:
            self.opened.emit(self.scope.currentData(), item.data(Qt.ItemDataRole.UserRole))
