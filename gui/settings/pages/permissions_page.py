"""Category-based tri-state permission editor backed by ToolDescriptor metadata."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.tools.manager import ToolManager
from gui.settings.page_header import build_page_header
from gui.utils.combo_box import configure_combo_popup
from gui.utils.icon_manager import Icons
from gui.widgets.themed_line_edit import ThemedLineEdit
from models.contracts.capability import CapabilitiesConfig
from models.contracts.tooling import (
    RISK_LEVEL_LABELS,
    TOOL_CATEGORIES,
    TOOL_CATEGORY_LABELS,
    TOOL_CATEGORY_SORT_ORDER,
    ToolDescriptor,
    ToolPermissionConfig,
    ToolPolicy,
    default_tool_category_policies,
)


class PermissionsPage(QWidget):
    """Editor for the application-level *custom* permission rules.

    Session presets (default/ask/deny/allow/custom) are chosen per conversation in
    the input area; this page only maintains the rules used by the "custom"
    preset.
    """

    page_title = "权限"

    def __init__(
        self,
        permissions: ToolPermissionConfig,
        capabilities: CapabilitiesConfig | None = None,
        tool_manager: ToolManager | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        del capabilities
        if tool_manager is None:
            raise ValueError("PermissionsPage requires an injected ToolManager")
        self._tool_manager = tool_manager
        self._category_defaults = dict(permissions.category_defaults)
        self._overrides = dict(permissions.tools)
        self._rows: dict[str, dict] = {}
        self._category_rows: dict[str, dict] = {}
        self._loading = False
        self._setup_ui()
        self._populate()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        root.addWidget(build_page_header("权限", "这里维护“自定义权限”预设的规则；会话级预设在输入区选择，Mode 决定工具上限。"))

        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        for text, callback in (
            ("安全默认", self._fill_safe_defaults),
            ("全部确认", self._fill_all_ask),
            ("全部放行", self._fill_all_allow),
        ):
            button = QPushButton(text)
            button.setObjectName("settings_action_btn")
            button.clicked.connect(callback)
            preset_row.addWidget(button)
        preset_row.addStretch(1)
        root.addLayout(preset_row)

        self.warning_label = QLabel(
            "“全部放行”规则会跳过所有工具确认（含高风险）；Channel 无人值守会话将直接执行高风险工具，"
            "但仍不能突破 Mode、会话或父 Agent 的工具范围。"
        )
        self.warning_label.setObjectName("permission_warning")
        self.warning_label.setProperty("warning", True)
        self.warning_label.setWordWrap(True)
        root.addWidget(self.warning_label)

        self.search_edit = ThemedLineEdit()
        self.search_edit.setPlaceholderText("搜索工具名称、技术 ID、类别或来源")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._filter_rows)
        root.addWidget(self.search_edit)

        self.tree = QTreeWidget()
        self.tree.setObjectName("permission_tree")
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(["工具", "类别", "来源", "风险", "策略", ""])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(False)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.tree, 1)

    def _descriptors(self) -> list[ToolDescriptor]:
        descriptors = [
            descriptor
            for descriptor in self._tool_manager.list_tool_descriptors(include_dynamic=True).values()
            if descriptor.name != "agent__complete"
        ]
        return sorted(
            descriptors,
            key=lambda item: (
                TOOL_CATEGORY_SORT_ORDER.get(item.category, 999),
                item.display_name.lower(),
                item.name,
            ),
        )

    def _populate(self) -> None:
        self._loading = True
        try:
            self.tree.clear()
            self._rows.clear()
            self._category_rows.clear()
            descriptors = self._descriptors()
            by_category = {category: [] for category in TOOL_CATEGORIES}
            for descriptor in descriptors:
                by_category.setdefault(descriptor.category, []).append(descriptor)

            for category in TOOL_CATEGORIES:
                children = by_category.get(category) or []
                if not children:
                    continue
                policy = self._category_defaults.get(category, ToolPolicy())
                parent = QTreeWidgetItem(self.tree)
                parent.setText(0, f"{TOOL_CATEGORY_LABELS.get(category, category)} ({len(children)})")
                parent.setText(1, TOOL_CATEGORY_LABELS.get(category, category))
                parent.setText(2, "全局")
                parent.setText(3, "混合")
                font = QFont(parent.font(0))
                font.setBold(True)
                parent.setFont(0, font)
                parent.setData(0, Qt.ItemDataRole.UserRole, category)

                action_combo = self._action_combo(policy.action, inherited=None)
                action_combo.currentIndexChanged.connect(self._rules_changed)
                self.tree.setItemWidget(parent, 4, action_combo)

                reset = QToolButton()
                reset.setIcon(Icons.get(Icons.REFRESH))
                reset.setAutoRaise(True)
                reset.setFixedSize(24, 24)
                reset.setToolTip("恢复该类别默认值")
                reset.clicked.connect(lambda _checked=False, category=category: self._reset_category(category))
                self.tree.setItemWidget(parent, 5, reset)
                self._category_rows[category] = {"item": parent, "action": action_combo}

                for descriptor in children:
                    child = QTreeWidgetItem(parent)
                    child.setText(0, descriptor.display_name or descriptor.name)
                    child.setText(1, TOOL_CATEGORY_LABELS.get(descriptor.category, descriptor.category))
                    child.setText(2, descriptor.source)
                    child.setText(3, RISK_LEVEL_LABELS.get(descriptor.risk, descriptor.risk))
                    child.setToolTip(0, f"{descriptor.name}\n{descriptor.description}")
                    child.setData(0, Qt.ItemDataRole.UserRole, descriptor.name)

                    override = self._overrides.get(descriptor.name)
                    tool_combo = self._action_combo(
                        override.action if override is not None else "inherit",
                        inherited=policy.action,
                    )
                    tool_combo.currentIndexChanged.connect(self._rules_changed)
                    self.tree.setItemWidget(child, 4, tool_combo)

                    tool_reset = QToolButton()
                    tool_reset.setIcon(Icons.get(Icons.REFRESH))
                    tool_reset.setAutoRaise(True)
                    tool_reset.setFixedSize(24, 24)
                    tool_reset.setToolTip("重置为类别继承")
                    tool_reset.clicked.connect(
                        lambda _checked=False, tool_name=descriptor.name: self._reset_tool(tool_name)
                    )
                    self.tree.setItemWidget(child, 5, tool_reset)
                    self._rows[descriptor.name] = {
                        "item": child,
                        "descriptor": descriptor,
                        "action": tool_combo,
                    }
                parent.setExpanded(True)
            self._filter_rows(self.search_edit.text())
        finally:
            self._loading = False

    _ACTION_ITEMS = (("放行", "allow"), ("确认", "ask"), ("禁用", "deny"))
    _ACTION_LABELS = {"allow": "放行", "ask": "确认", "deny": "禁用"}

    @classmethod
    def _action_combo(cls, action: str, *, inherited: str | None) -> QComboBox:
        combo = QComboBox()
        if inherited is not None:
            combo.addItem(f"继承（{cls._ACTION_LABELS.get(inherited, inherited)}）", "inherit")
        for label, value in cls._ACTION_ITEMS:
            combo.addItem(label, value)
        if inherited is not None and action == "inherit":
            combo.setCurrentIndex(0)
        else:
            combo.setCurrentIndex(combo.findData(action if action in cls._ACTION_LABELS else "ask"))
        configure_combo_popup(combo)
        return combo

    def _rules_changed(self, _index: int) -> None:
        if not self._loading:
            self._sync_from_controls()

    def _sync_from_controls(self) -> None:
        for category, controls in self._category_rows.items():
            self._category_defaults[category] = ToolPolicy(
                action=str(controls["action"].currentData() or "ask")
            )

        overrides: dict[str, ToolPolicy] = {}
        for tool_name, controls in self._rows.items():
            action_value = str(controls["action"].currentData() or "inherit")
            if action_value == "inherit":
                continue
            overrides[tool_name] = ToolPolicy(action=action_value)
        self._overrides = overrides

    def _reset_tool(self, tool_name: str) -> None:
        controls = self._rows.get(tool_name)
        if controls:
            controls["action"].setCurrentIndex(0)

    def _reset_category(self, category: str) -> None:
        self._sync_from_controls()
        self._category_defaults[category] = default_tool_category_policies()[category]
        self._populate()

    def _filter_rows(self, query: str) -> None:
        needle = str(query or "").strip().lower()
        for category, controls in self._category_rows.items():
            parent = controls["item"]
            category_match = needle in " ".join(
                (category, TOOL_CATEGORY_LABELS.get(category, category))
            ).lower()
            visible_children = 0
            for index in range(parent.childCount()):
                child = parent.child(index)
                tool_name = str(child.data(0, Qt.ItemDataRole.UserRole) or "")
                row = self._rows.get(tool_name) or {}
                descriptor = row.get("descriptor")
                haystack = " ".join(
                    (
                        getattr(descriptor, "name", ""),
                        getattr(descriptor, "display_name", ""),
                        getattr(descriptor, "category", ""),
                        getattr(descriptor, "source", ""),
                        getattr(descriptor, "description", ""),
                    )
                ).lower()
                hidden = bool(needle and not category_match and needle not in haystack)
                child.setHidden(hidden)
                visible_children += 0 if hidden else 1
            parent.setHidden(bool(needle and not category_match and visible_children == 0))

    def _fill_safe_defaults(self) -> None:
        self._category_defaults = default_tool_category_policies()
        self._overrides.clear()
        self._populate()

    def _fill_all_ask(self) -> None:
        self._category_defaults = {name: ToolPolicy(action="ask") for name in TOOL_CATEGORIES}
        self._overrides.clear()
        self._populate()

    def _fill_all_allow(self) -> None:
        self._category_defaults = {name: ToolPolicy(action="allow") for name in TOOL_CATEGORIES}
        self._overrides.clear()
        self._populate()

    def collect(self) -> ToolPermissionConfig:
        self._sync_from_controls()
        return ToolPermissionConfig(
            category_defaults=dict(self._category_defaults),
            tools=dict(self._overrides),
        )

    def _reset_defaults(self) -> None:
        self._fill_safe_defaults()
