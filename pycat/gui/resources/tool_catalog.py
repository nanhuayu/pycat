"""Read-only, searchable tool projection. Schemas are populated only on expansion."""
from __future__ import annotations

import json

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget


class ToolCatalog(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.count = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        self.heading = QToolButton()
        self.heading.setText("工具 · 0")
        self.heading.setCheckable(True)
        self.heading.setChecked(True)
        self.heading.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.heading.setArrowType(Qt.ArrowType.DownArrow)
        self.heading.toggled.connect(self._toggle)
        header.addWidget(self.heading)
        header.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索工具")
        self.search.setAccessibleName("搜索工具")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(220)
        self.search.textChanged.connect(self._filter)
        header.addWidget(self.search)
        layout.addLayout(header)
        self.tree = QTreeWidget()
        self.tree.setObjectName("resource_tool_list")
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(0)
        self.tree.setMinimumHeight(150)
        self.tree.setMaximumHeight(300)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.itemExpanded.connect(self._expand)
        layout.addWidget(self.tree)
        self.hint = QLabel("测试连接后显示工具说明和参数。")
        self.hint.setProperty("muted", True)
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

    def set_tools(self, names, schemas=()):
        by_name = {item.get("name"): item for item in schemas if isinstance(item, dict)}
        self.tree.clear()
        for name in names:
            item = QTreeWidgetItem([str(name)])
            schema = by_name.get(name)
            item.setData(0, Qt.ItemDataRole.UserRole, schema)
            item.setToolTip(0, str((schema or {}).get("description") or name))
            if schema:
                item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
            self.tree.addTopLevelItem(item)
        self.count = len(names)
        self.heading.setText(f"工具 · {self.count}")
        self.hint.setText("暂无工具，请测试连接。" if not names else "点击工具查看说明和参数。" if by_name else "缓存的工具名称；测试连接后可查看说明和参数。")
        self._filter()

    def _toggle(self, expanded):
        self.heading.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.tree.setVisible(expanded)
        self.search.setVisible(expanded)
        self.hint.setVisible(expanded)

    def _filter(self):
        query = self.search.text().strip().casefold()
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            item.setHidden(query not in (item.text(0) + " " + item.toolTip(0)).casefold())

    def visible_names(self):
        return [self.tree.topLevelItem(i).text(0) for i in range(self.tree.topLevelItemCount())
                if not self.tree.topLevelItem(i).isHidden()]

    def _expand(self, item):
        schema = item.data(0, Qt.ItemDataRole.UserRole)
        if not schema or item.childCount():
            return
        detail = QTreeWidgetItem()
        item.addChild(detail)
        label = QLabel(str(schema.get("description") or "暂无说明") + "\n\n参数\n" +
                       json.dumps(schema.get("inputSchema") or {}, ensure_ascii=False, indent=2))
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setContentsMargins(8, 8, 8, 12)
        self.tree.setItemWidget(detail, 0, label)
