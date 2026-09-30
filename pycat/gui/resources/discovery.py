"""Shared, ephemeral catalog/search state for the native resource lists."""
from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QPushButton, QToolButton

from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import configure_icon_button
from pycat.gui.view_models.extension_labels import extension_text


class ResourceDiscovery(QObject):
    changed = pyqtSignal()

    def __init__(self, *, kind, service, search, details, work_dir="", servers_provider=lambda: (), parent=None):
        super().__init__(parent)
        self.kind, self.service, self.search, self.details = kind, service, search, details
        self.work_dir, self.servers_provider = work_dir, servers_provider
        self._catalog = None
        self.market_rows = []
        self.next_cursor = ""
        self.searching = False
        self.enabled = True
        self.search_button = QToolButton()
        configure_icon_button(self.search_button, Icons.get(Icons.SEARCH), self.tr("搜索市场（回车）"))
        self.search_button.clicked.connect(self.search_market)
        self.next_button = QPushButton(self.tr("加载更多"))
        self.next_button.clicked.connect(lambda: self.search_market(next_page=True))
        self.next_button.hide()
        search.textChanged.connect(self._query_changed)
        search.returnPressed.connect(self.search_market)
        details.busy_changed.connect(self._set_busy)
        self._set_busy(False)

    def invalidate(self):
        self._catalog = None

    def catalog(self):
        if self._catalog is None:
            self._catalog = []
            try:
                if self.service:
                    self._catalog = [row for row in self.service.catalog(
                        work_dir=self.work_dir, servers=self.servers_provider()) if row["kind"] == self.kind]
            except Exception as exc:
                self.details.status.setText(str(exc))
        return self._catalog

    @staticmethod
    def matches(row, query):
        return query in " ".join(extension_text(row, key) for key in ("title", "description", "source")).casefold()

    def rows(self):
        query = self.search.text().strip().casefold()
        rows = [row for row in self.catalog() if self.matches(row, query)]
        known = {row["id"] for row in rows}
        return rows + [row for row in self.market_rows if row["id"] not in known]

    def _query_changed(self):
        if self.searching:
            self.cancel_pending()
        self.market_rows = []
        self.next_cursor = ""
        self.next_button.hide()
        self.changed.emit()

    def search_market(self, _checked=False, *, next_page=False):
        if not self.enabled or not self.service or self.details._job:
            return
        query = self.search.text().strip()
        if self.kind == "skill" and len(query) < 2:
            self.details.status.setText(self.tr("输入至少两个字符后，按回车搜索市场。"))
            return
        try:
            servers = self.servers_provider()
        except ValueError as exc:
            self.details.status.setText(str(exc))
            return
        cursor = self.next_cursor if next_page else ""
        self.searching = True
        def completed(result):
            if self.search.text().strip() != query:
                return
            rows = {row["id"]: row for row in self.market_rows} if next_page else {}
            rows.update((row["id"], row) for row in result["items"])
            self.market_rows = list(rows.values())
            self.next_cursor = result["next_cursor"]
            self.next_button.setVisible(bool(self.next_cursor))
            self.changed.emit()
            self.details.status.setText(self.tr("市场匹配 {count} 项。").format(count=len(self.market_rows)))
        self.details.run_operation(lambda cancel: self.service.search_market(self.kind, query,
            cursor=cursor, work_dir=self.work_dir, servers=servers, cancelled=cancel), completed,
            self.tr("正在搜索市场…"))

    def _set_busy(self, busy):
        if not busy:
            self.searching = False
        self.search.setEnabled(not busy or self.searching)
        self.search_button.setEnabled(self.service is not None and not busy)
        self.next_button.setEnabled(self.service is not None and not busy)

    def cancel_pending(self):
        self.details.cancel_pending()
        self.searching = False
