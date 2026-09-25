"""Scope and record projection for short memory; editing belongs to shared detail."""
import re

from PyQt6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import INSPECTOR_MARGIN, configure_icon_button, prepare_context_menu
from pycat.gui.widgets.capsule import CapsuleDelegate, CapsuleList
from pycat.models.session_paths import has_active_workspace

_STATUS_LABELS = frozenset((
    QT_TRANSLATE_NOOP("MemoryPanel", "未启用"),
    QT_TRANSLATE_NOOP("MemoryPanel", "存储不可读"),
    QT_TRANSLATE_NOOP("MemoryPanel", "正在整理"),
    QT_TRANSLATE_NOOP("MemoryPanel", "等待整理"),
    QT_TRANSLATE_NOOP("MemoryPanel", "最近整理无新增"),
    QT_TRANSLATE_NOOP("MemoryPanel", "暂无记忆"),
))


class MemoryPanel(QWidget):
    opened = pyqtSignal(str, str)
    retry_requested = pyqtSignal()
    assign_requested = pyqtSignal()
    forget_requested = pyqtSignal(str, str, str)

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
        self.scope.setAccessibleName(QCoreApplication.translate('MemoryPanel', '记忆范围'))
        self.scope.currentIndexChanged.connect(self.render)
        header.addWidget(self.scope, 1)
        self.add = QToolButton()
        configure_icon_button(self.add, Icons.get_muted(Icons.PLUS), QCoreApplication.translate('MemoryPanel', '添加记忆'))
        self.add.clicked.connect(lambda: self.opened.emit(self.scope.currentData(), ""))
        header.addWidget(self.add)
        self.more = QToolButton()
        configure_icon_button(self.more, Icons.get_muted(Icons.MORE), QCoreApplication.translate('MemoryPanel', '所选记忆的更多操作'))
        self.more.clicked.connect(self._show_menu)
        header.addWidget(self.more)
        layout.addLayout(header)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setProperty("muted", True)
        layout.addWidget(self.status)
        self.details_toggle = QToolButton()
        self.details_toggle.setText(QCoreApplication.translate('MemoryPanel', '整理详情'))
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
        self.entries.populate_menu = self.populate_menu
        self.entries.itemSelectionChanged.connect(lambda: self.more.setEnabled(self.entries.currentItem() is not None))
        self.entries.setAccessibleName(QCoreApplication.translate('MemoryPanel', '记忆条目'))
        self.entries.itemClicked.connect(self._open)
        self.entries.itemActivated.connect(self._open)
        layout.addWidget(self.entries, 1)
        self.counter = QLabel()
        self.counter.setProperty("muted", True)
        layout.addWidget(self.counter)
        self.retry = QToolButton()
        self.retry.setText(QCoreApplication.translate('MemoryPanel', '重试整理'))
        self.retry.clicked.connect(self.retry_requested)
        layout.addWidget(self.retry)
        self.assign = QToolButton()
        self.assign.setText(QCoreApplication.translate('MemoryPanel', '导入待归属的旧记忆'))
        self.assign.clicked.connect(self.assign_requested)
        layout.addWidget(self.assign)
        self.set_workspace("")

    def set_workspace(self, workspace):
        if workspace != self._workspace:
            self._workspace = workspace
            self.scope.blockSignals(True)
            self.scope.clear()
            if has_active_workspace(workspace):
                self.scope.addItem(QCoreApplication.translate('MemoryPanel', '项目记忆'), "memory")
            self.scope.addItem(QCoreApplication.translate('MemoryPanel', '用户偏好'), "user")
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
        status = str(self.snapshot.get("status", QCoreApplication.translate('MemoryPanel', '正在读取…')))
        scope_hint = QCoreApplication.translate('MemoryPanel', '本项目的会话共用') if self.scope.currentData() == "memory" else QCoreApplication.translate('MemoryPanel', '跨项目使用的用户偏好')
        self.scope.setToolTip(scope_hint)
        self.scope.setAccessibleDescription(scope_hint)
        if status.startswith("已记住"):
            status = "" if records else QCoreApplication.translate('MemoryPanel', '暂无记忆；任务结束后会在后台整理有用事实。')
        evolution = self.snapshot.get("evolution") or {}
        if status in _STATUS_LABELS:
            status = QCoreApplication.translate("MemoryPanel", status)
        elif evolution.get("failure_count") and self.snapshot.get("enabled") and self.snapshot.get("readable") and not evolution.get("inflight"):
            prefix = QCoreApplication.translate("MemoryPanel", "部分已保存") if evolution.get("published_job_count") else QCoreApplication.translate("MemoryPanel", "整理待处理")
            status = QCoreApplication.translate("MemoryPanel", "{prefix} · {count} 项待处理").format(prefix=prefix, count=evolution.get("attention_count", 0))
        project_jobs = self.scope.currentData() == "memory" or not has_active_workspace(self._workspace)
        if not project_jobs:
            status = QCoreApplication.translate('MemoryPanel', '未启用') if not self.snapshot.get("enabled", True) else "" if records else QCoreApplication.translate('MemoryPanel', '暂无用户偏好')
        error = str(target.get("error") or self.snapshot.get("error") or "")
        self.set_status("\n".join(part for part in (status, error if error not in status else "") if part))
        attention = int(evolution.get("attention_count", evolution.get("failure_count", 0)) or 0)
        details_visible = project_jobs and bool(attention or not evolution.get("available", True))
        self.details_toggle.setVisible(details_visible)
        labels = {"version_conflict": QCoreApplication.translate('MemoryPanel', '版本冲突'), "capacity": QCoreApplication.translate('MemoryPanel', '容量不足'), "invalid_plan": QCoreApplication.translate('MemoryPanel', '整理格式需修正'), "transient": QCoreApplication.translate('MemoryPanel', '暂时无法处理')}
        detail = [QCoreApplication.translate('MemoryPanel', '项目后台整理（包含记忆、知识和技能）') if has_active_workspace(self._workspace) else QCoreApplication.translate('MemoryPanel', '后台整理')]
        detail += [QCoreApplication.translate('MemoryPanel', '{value}：{count} 项').format(value=labels.get(code, code), count=count) for code, count in evolution.get("reason_counts", {}).items()]
        if evolution.get("last_error"):
            detail.append(str(evolution["last_error"]))
        self.details.setText("\n".join(detail))
        self.details.setVisible(details_visible and self.details_toggle.isChecked())
        self.status.setToolTip(detail[0] if project_jobs else scope_hint)
        self.counter.setText(QCoreApplication.translate('MemoryPanel', '{value} 条记录').format(value=len(records)))
        self.counter.setVisible(bool(records))
        self.entries.clear()
        for entry in records:
            title = self._entry_title(entry)
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, entry["id"])
            item.setIcon(Icons.get_muted(Icons.BRAIN))
            note = QCoreApplication.translate('MemoryPanel', '旧版记录') if entry.get("origin") == "legacy" else ""
            item.setData(CapsuleDelegate.DetailRole, note)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, "\n".join(filter(None, [title, note, entry["text"]])))
            item.setToolTip(entry["text"])
            self.entries.addItem(item)
            if entry["id"] == entry_id:
                self.entries.setCurrentItem(item)
        self.entries.verticalScrollBar().setValue(scroll)
        self.more.setEnabled(self.entries.currentItem() is not None)
        self.retry.setVisible(project_jobs and bool(attention))
        self.retry.setEnabled(bool(self.snapshot.get("enabled") and not evolution.get("inflight")))
        self.assign.setVisible(bool(self.snapshot.get("unassigned_count")))
        self.add.setEnabled(bool(self.snapshot.get("enabled") and target.get("readable", self.snapshot.get("readable"))))
        self.assign.setEnabled(bool(self.snapshot.get("enabled")))
        self.add.setToolTip(QCoreApplication.translate('MemoryPanel', '添加记忆') if self.add.isEnabled() else QCoreApplication.translate('MemoryPanel', '此会话未启用记忆或存储不可读，无法添加。'))

    def set_status(self, text):
        self.status.setText(str(text or ""))
        self.status.setVisible(bool(text))

    def populate_menu(self, menu, item):
        if item is None:
            return
        scope, entry_id = self.scope.currentData(), item.data(Qt.ItemDataRole.UserRole)
        snapshot = self.snapshot
        target = next((row for row in self.snapshot.get("targets", []) if row["target"] == scope), {})
        entry = next((row for row in target.get("records", []) if row["id"] == entry_id), None)
        if entry is None:
            return
        def current():
            return self.snapshot is snapshot and self.scope.currentData() == scope
        menu.addAction(QCoreApplication.translate('MemoryPanel', '查看 / 编辑'), lambda: self.opened.emit(scope, entry_id) if current() else None)
        menu.addAction(QCoreApplication.translate('MemoryPanel', '复制内容'), lambda: QApplication.clipboard().setText(entry["text"]) if current() else None)
        menu.addSeparator()
        menu.addAction(QCoreApplication.translate('MemoryPanel', '忘记这条记忆'), lambda: self.forget_requested.emit(scope, entry_id, target.get("digest", "")) if current() else None).setEnabled(
            bool(self.snapshot.get("enabled") and target.get("readable", self.snapshot.get("readable"))))

    def _show_menu(self):
        menu = prepare_context_menu(QMenu(self), self)
        self.populate_menu(menu, self.entries.currentItem())
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.more.mapToGlobal(self.more.rect().bottomLeft()))

    def _toggle_details(self, expanded):
        self.details_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.details.setVisible(expanded and not self.details_toggle.isHidden())

    @staticmethod
    def _entry_title(entry):
        lines = [line.strip() for line in entry["text"].splitlines() if line.strip()]
        content = [line for line in lines if not re.match(r"^#{1,6}(?:\s|$)", line)]
        link = r"^(?:[-*+]\s+|\d+[.)]\s+)?\[[^\]]+\]\([^)]+\)\s*$"
        if entry.get("origin") == "legacy" and content and all(re.fullmatch(link, line) for line in content):
            return QCoreApplication.translate('MemoryPanel', '旧版记忆目录')
        return (content[0] if content else lines[0].lstrip("# ") if lines else QCoreApplication.translate('MemoryPanel', '空记录'))

    def _open(self, item):
        if item is not None:
            self.opened.emit(self.scope.currentData(), item.data(Qt.ItemDataRole.UserRole))
