"""Paged material navigation; no file access and no detail window ownership."""
from collections import Counter

from PyQt6.QtCore import QCoreApplication, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QListWidgetItem, QToolButton, QVBoxLayout, QWidget

from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import INSPECTOR_MARGIN, configure_icon_button
from pycat.gui.widgets.capsule import CapsuleDelegate, CapsuleList
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit
from pycat.models.session_paths import has_active_workspace


class MaterialsPanel(QWidget):
    refresh_requested = pyqtSignal(bool)
    opened = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.offset = 0
        self.dirty = True
        self.conversation = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(INSPECTOR_MARGIN, INSPECTOR_MARGIN, INSPECTOR_MARGIN, INSPECTOR_MARGIN)
        layout.setSpacing(4)
        filters = QHBoxLayout()
        filters.setSpacing(4)
        self.search = ThemedLineEdit()
        self.search.setMinimumWidth(0)
        self.search.setPlaceholderText(QCoreApplication.translate('MaterialsPanel', '搜索资料…'))
        self.search.setAccessibleName(QCoreApplication.translate('MaterialsPanel', '搜索资料'))
        self.search.setClearButtonEnabled(True)
        filters.addWidget(self.search, 1)
        self.kind = QComboBox()
        for title, kind in ((QCoreApplication.translate('MaterialsPanel', '全部'), "all"), (QCoreApplication.translate('MaterialsPanel', '成果'), "artifact"), (QCoreApplication.translate('MaterialsPanel', '知识'), "wiki"), (QCoreApplication.translate('MaterialsPanel', '文件'), "file")):
            self.kind.addItem(title, kind)
        self.kind.setAccessibleName(QCoreApplication.translate('MaterialsPanel', '资料类型'))
        self.kind.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        filters.addWidget(self.kind)
        refresh = QToolButton()
        configure_icon_button(refresh, Icons.get_muted(Icons.REFRESH), QCoreApplication.translate('MaterialsPanel', '刷新资料'))
        refresh.clicked.connect(lambda: self.refresh_requested.emit(True))
        filters.addWidget(refresh)
        layout.addLayout(filters)
        self.status = QLabel(QCoreApplication.translate('MaterialsPanel', '选择会话后查看资料'))
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setProperty("muted", True)
        layout.addWidget(self.status)
        self.items = CapsuleList()
        self.items.setAccessibleName(QCoreApplication.translate('MaterialsPanel', '资料列表'))
        self.items.itemClicked.connect(self._open)
        self.items.itemActivated.connect(self._open)
        layout.addWidget(self.items, 1)
        paging = QHBoxLayout()
        paging.setSpacing(4)
        self.counter = QLabel()
        self.counter.setProperty("muted", True)
        paging.addWidget(self.counter, 1)
        self.previous = QToolButton()
        configure_icon_button(self.previous, Icons.get_muted(Icons.CHEVRON_LEFT), QCoreApplication.translate('MaterialsPanel', '上一页资料'))
        self.previous.clicked.connect(lambda: self._page(-50))
        paging.addWidget(self.previous)
        self.next = QToolButton()
        configure_icon_button(self.next, Icons.get_muted(Icons.CHEVRON_RIGHT), QCoreApplication.translate('MaterialsPanel', '下一页资料'))
        self.next.clicked.connect(lambda: self._page(50))
        paging.addWidget(self.next)
        layout.addLayout(paging)
        self.previous.hide()
        self.next.hide()
        self.counter.hide()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(lambda: self.refresh_requested.emit(False))
        self.search.textChanged.connect(self._filter)
        self.kind.currentIndexChanged.connect(self._filter)

    def _filter(self, _value):
        self.offset = 0
        self.timer.start()

    def _page(self, delta):
        self.offset = max(0, self.offset + delta)
        self.refresh_requested.emit(False)

    def _open(self, item):
        if item is not None:
            self.opened.emit(item.data(Qt.ItemDataRole.UserRole))

    def apply(self, page):
        selected = self.items.currentItem()
        key = selected.data(Qt.ItemDataRole.UserRole)["key"] if selected else None
        scroll = self.items.verticalScrollBar().value()
        self.items.clear()
        names = Counter(row["title"] for row in page.items)
        for row in page.items:
            label = {"wiki": QCoreApplication.translate('MaterialsPanel', '知识'), "artifact": QCoreApplication.translate('MaterialsPanel', '成果'), "file": QCoreApplication.translate('MaterialsPanel', '文件')}[row["kind"]]
            scope = row["scope"].replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            roles = [role for role in row["roles"] if role not in {"成果", "知识"}]
            meta = " · ".join([label, scope, *roles])
            title = row["title"]
            item = QListWidgetItem(title)
            item.setIcon(Icons.get_muted({"wiki": Icons.BOOK, "artifact": Icons.FILE_LINES, "file": Icons.FILE}[row["kind"]]))
            item.setData(Qt.ItemDataRole.AccessibleTextRole, "\n".join([title, meta]))
            item.setData(Qt.ItemDataRole.UserRole, row)
            details = list(roles)
            if names[title] > 1:
                ref = row.get("ref") or {}
                details.append(str(ref.get("ref") or row["scope"]))
                if ref.get("digest"):
                    details.append(ref["digest"][:8])
            item.setData(CapsuleDelegate.DetailRole, " · ".join(details))
            item.setToolTip("\n".join([title, meta, row["scope"], row.get("summary", ""), str((row["ref"] or {}).get("ref", ""))]))
            self.items.addItem(item)
            if row["key"] == key:
                self.items.setCurrentItem(item)
        self.items.verticalScrollBar().setValue(scroll)
        self.previous.setEnabled(page.offset > 0)
        self.next.setEnabled(page.has_next)
        paged = page.total > 50
        self.previous.setVisible(paged)
        self.next.setVisible(paged)
        self.counter.setVisible(page.total > 0)
        self.counter.setText(QCoreApplication.translate('MaterialsPanel', '{total} 项 · {value}/{value_}').format(total=page.total, value=page.offset // 50 + 1, value_=(page.total + 49) // 50) if paged else QCoreApplication.translate('MaterialsPanel', '{total} 项').format(total=page.total))
        empty = QCoreApplication.translate('MaterialsPanel', '没有匹配项') if self.search.text().strip() else {
            "all": QCoreApplication.translate('MaterialsPanel', '本会话暂无资料'), "artifact": QCoreApplication.translate('MaterialsPanel', '本会话暂无成果'),
            "wiki": QCoreApplication.translate('MaterialsPanel', '此项目暂无知识'), "file": QCoreApplication.translate('MaterialsPanel', '本会话暂无文件')}[self.kind.currentData()]
        if self.kind.currentData() == "wiki" and not has_active_workspace(getattr(self.conversation, "work_dir", "")):
            empty = QCoreApplication.translate('MaterialsPanel', '选择工作区后查看项目知识')
        self.set_status("" if page.total else empty)
        self.dirty = False

    def set_status(self, text):
        self.status.setText(str(text or ""))
        self.status.setVisible(bool(text))

    def showEvent(self, event):
        super().showEvent(event)
        if self.dirty:
            self.refresh_requested.emit(False)
