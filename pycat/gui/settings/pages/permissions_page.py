"""Category-based tri-state permission editor backed by ToolDescriptor metadata."""
from __future__ import annotations

from PyQt6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pycat.core.tools.manager import ToolManager
from pycat.gui.settings.page_header import build_page_header
from pycat.gui.utils.combo_box import configure_combo_popup
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import configure_icon_button
from pycat.gui.view_models.tooling_labels import risk_level_label, tool_category_label, tool_display_name
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit
from pycat.models.contracts.capability import CapabilitiesConfig
from pycat.models.contracts.tooling import (
    TOOL_CATEGORIES,
    TOOL_CATEGORY_SORT_ORDER,
    ToolDescriptor,
    ToolPermissionConfig,
    ToolPolicy,
    default_tool_category_policies,
)


class PermissionsPage(QWidget):
    """Editor for the application-level *custom* permission rules.

    Session tool approval and file-tool range are chosen per conversation in
    the input area; this page only maintains the rules used by ``custom``.
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
        root.addWidget(build_page_header(QCoreApplication.translate('PermissionsPage', '工具规则（高级）'), QCoreApplication.translate('PermissionsPage', '这里维护“自定义规则”；会话级工具操作和文件范围在输入区选择，Mode 决定工具上限。')))

        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        for text, callback in (
            (QCoreApplication.translate('PermissionsPage', '安全默认'), self._fill_safe_defaults),
            (QCoreApplication.translate('PermissionsPage', '全部确认'), self._fill_all_ask),
            (QCoreApplication.translate('PermissionsPage', '全部允许工具'), self._fill_all_allow),
        ):
            button = QPushButton(text)
            button.setObjectName("settings_action_btn")
            button.clicked.connect(callback)
            preset_row.addWidget(button)
        preset_row.addStretch(1)
        root.addLayout(preset_row)

        self.warning_label = QLabel(
            QCoreApplication.translate('PermissionsPage', '“全部允许工具”只填充自定义工具规则，不会改变会话的工具操作或文件范围，也不授予工作区外路径。Channel 仍受无人值守安全上限约束；Mode、会话和父 Agent 的工具范围也不会被扩大。')
        )
        self.warning_label.setObjectName("permission_warning")
        self.warning_label.setProperty("warning", True)
        self.warning_label.setWordWrap(True)
        root.addWidget(self.warning_label)

        self.search_edit = ThemedLineEdit()
        self.search_edit.setPlaceholderText(QCoreApplication.translate('PermissionsPage', '搜索工具名称、技术 ID、类别或来源'))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._filter_rows)
        root.addWidget(self.search_edit)

        self.tree = QTreeWidget()
        self.tree.setObjectName("permission_tree")
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels([QCoreApplication.translate('PermissionsPage', '工具'), QCoreApplication.translate('PermissionsPage', '类别'), QCoreApplication.translate('PermissionsPage', '来源'), QCoreApplication.translate('PermissionsPage', '风险'), QCoreApplication.translate('PermissionsPage', '策略'), ""])
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
                tool_display_name(item).lower(),
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
                parent.setText(0, f"{tool_category_label(category)} ({len(children)})")
                parent.setText(1, tool_category_label(category))
                parent.setText(2, QCoreApplication.translate('PermissionsPage', '全局'))
                parent.setText(3, QCoreApplication.translate('PermissionsPage', '混合'))
                font = QFont(parent.font(0))
                font.setBold(True)
                parent.setFont(0, font)
                parent.setData(0, Qt.ItemDataRole.UserRole, category)

                action_combo = self._action_combo(policy.action, inherited=None)
                action_combo.currentIndexChanged.connect(self._rules_changed)
                self.tree.setItemWidget(parent, 4, action_combo)

                reset = QToolButton()
                configure_icon_button(reset, Icons.get_muted(Icons.REFRESH),
                                      QCoreApplication.translate('PermissionsPage', '恢复该类别默认值'))
                reset.clicked.connect(lambda _checked=False, category=category: self._reset_category(category))
                self.tree.setItemWidget(parent, 5, reset)
                self._category_rows[category] = {"item": parent, "action": action_combo}

                for descriptor in children:
                    child = QTreeWidgetItem(parent)
                    child.setText(0, tool_display_name(descriptor))
                    child.setText(1, tool_category_label(descriptor.category))
                    child.setText(2, descriptor.source)
                    child.setText(3, risk_level_label(descriptor.risk))
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
                    configure_icon_button(tool_reset, Icons.get_muted(Icons.REFRESH),
                                          QCoreApplication.translate('PermissionsPage', '重置为类别继承'))
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

    _ACTION_LABELS = {
        "allow": QT_TRANSLATE_NOOP("PermissionsPage", "放行"),
        "ask": QT_TRANSLATE_NOOP("PermissionsPage", "确认"),
        "deny": QT_TRANSLATE_NOOP("PermissionsPage", "禁用"),
    }

    @classmethod
    def _action_combo(cls, action: str, *, inherited: str | None) -> QComboBox:
        combo = QComboBox()
        if inherited is not None:
            label = QCoreApplication.translate("PermissionsPage", cls._ACTION_LABELS.get(inherited, inherited))
            combo.addItem(QCoreApplication.translate('PermissionsPage', '继承（{value}）').format(value=label), "inherit")
        for value, label in cls._ACTION_LABELS.items():
            combo.addItem(QCoreApplication.translate("PermissionsPage", label), value)
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
                (category, tool_category_label(category))
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
                        child.text(0),
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
