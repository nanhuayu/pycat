"""Category-based permission editor backed by ToolDescriptor metadata."""
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
        self._approval_mode = permissions.approval_mode
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
        root.addWidget(build_page_header("权限", "Mode 决定工具上限；这里仅控制全局启用和逐次确认。"))

        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        for text, callback in (
            ("安全默认", self._apply_safe_defaults),
            ("开发信任", self._apply_developer_trust),
            ("全部放行", self._apply_allow_all),
        ):
            button = QPushButton(text)
            button.setObjectName("settings_action_btn")
            button.clicked.connect(callback)
            preset_row.addWidget(button)
        preset_row.addStretch(1)
        self.mode_label = QLabel()
        self.mode_label.setObjectName("permission_mode_label")
        preset_row.addWidget(self.mode_label)
        root.addLayout(preset_row)

        self.warning_label = QLabel("全部放行会跳过高风险工具确认，但仍不能突破 Mode、会话或父 Agent 的工具范围。")
        self.warning_label.setObjectName("permission_warning")
        self.warning_label.setProperty("warning", True)
        self.warning_label.setWordWrap(True)
        root.addWidget(self.warning_label)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索工具名称、技术 ID、类别或来源")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._filter_rows)
        root.addWidget(self.search_edit)

        self.tree = QTreeWidget()
        self.tree.setObjectName("permission_tree")
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels(["工具", "类别", "来源", "风险", "启用", "确认策略", ""])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(False)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3, 4, 5, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.tree, 1)

    def _descriptors(self) -> list[ToolDescriptor]:
        descriptors = list(self._tool_manager.list_tool_descriptors(include_dynamic=True).values())
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

                enabled_combo = self._enabled_combo(policy.enabled, inherited=False)
                confirm_combo = self._confirm_combo(policy.auto_approve, inherited=False)
                enabled_combo.currentIndexChanged.connect(
                    lambda _index, category=category: self._category_changed(category)
                )
                confirm_combo.currentIndexChanged.connect(
                    lambda _index, category=category: self._category_changed(category)
                )
                self.tree.setItemWidget(parent, 4, enabled_combo)
                self.tree.setItemWidget(parent, 5, confirm_combo)

                reset = QToolButton()
                reset.setIcon(Icons.get(Icons.REFRESH))
                reset.setAutoRaise(True)
                reset.setFixedSize(24, 24)
                reset.setToolTip("恢复该类别默认值")
                reset.clicked.connect(lambda _checked=False, category=category: self._reset_category(category))
                self.tree.setItemWidget(parent, 6, reset)
                self._category_rows[category] = {
                    "item": parent,
                    "enabled": enabled_combo,
                    "confirm": confirm_combo,
                }

                for descriptor in children:
                    child = QTreeWidgetItem(parent)
                    child.setText(0, descriptor.display_name or descriptor.name)
                    child.setText(1, TOOL_CATEGORY_LABELS.get(descriptor.category, descriptor.category))
                    child.setText(2, descriptor.source)
                    child.setText(3, RISK_LEVEL_LABELS.get(descriptor.risk, descriptor.risk))
                    child.setToolTip(0, f"{descriptor.name}\n{descriptor.description}")
                    child.setData(0, Qt.ItemDataRole.UserRole, descriptor.name)

                    override = self._overrides.get(descriptor.name)
                    tool_enabled = self._tool_enabled_combo(policy, override)
                    tool_enabled.currentIndexChanged.connect(self._tool_changed)
                    self.tree.setItemWidget(child, 4, tool_enabled)

                    confirm_widget: QComboBox | None = None
                    approval_label: QLabel | None = None
                    if descriptor.risk == "high":
                        approval_label = QLabel()
                        self.tree.setItemWidget(child, 5, approval_label)
                    else:
                        confirm_widget = self._tool_confirm_combo(policy, override)
                        confirm_widget.currentIndexChanged.connect(self._tool_changed)
                        self.tree.setItemWidget(child, 5, confirm_widget)

                    tool_reset = QToolButton()
                    tool_reset.setIcon(Icons.get(Icons.REFRESH))
                    tool_reset.setAutoRaise(True)
                    tool_reset.setFixedSize(24, 24)
                    tool_reset.setToolTip("重置为类别继承")
                    tool_reset.clicked.connect(
                        lambda _checked=False, tool_name=descriptor.name: self._reset_tool(tool_name)
                    )
                    self.tree.setItemWidget(child, 6, tool_reset)
                    self._rows[descriptor.name] = {
                        "item": child,
                        "descriptor": descriptor,
                        "enabled": tool_enabled,
                        "confirm": confirm_widget,
                        "approval_label": approval_label,
                    }
                parent.setExpanded(True)
            self._refresh_mode_ui()
            self._filter_rows(self.search_edit.text())
        finally:
            self._loading = False

    @staticmethod
    def _enabled_combo(enabled: bool, *, inherited: bool) -> QComboBox:
        combo = QComboBox()
        if inherited:
            combo.addItem("继承", "inherit")
        combo.addItem("启用", "enabled")
        combo.addItem("禁用", "disabled")
        combo.setCurrentIndex(combo.findData("enabled" if enabled else "disabled"))
        configure_combo_popup(combo)
        return combo

    @staticmethod
    def _confirm_combo(auto_approve: bool, *, inherited: bool) -> QComboBox:
        combo = QComboBox()
        if inherited:
            combo.addItem("继承", "inherit")
        combo.addItem("自动", "auto")
        combo.addItem("每次确认", "confirm")
        combo.setCurrentIndex(combo.findData("auto" if auto_approve else "confirm"))
        configure_combo_popup(combo)
        return combo

    def _tool_enabled_combo(self, category_policy: ToolPolicy, override: ToolPolicy | None) -> QComboBox:
        combo = self._enabled_combo(category_policy.enabled, inherited=True)
        combo.setItemText(0, f"继承（{'启用' if category_policy.enabled else '禁用'}）")
        if override is None:
            combo.setCurrentIndex(0)
        else:
            combo.setCurrentIndex(combo.findData("enabled" if override.enabled else "disabled"))
        return combo

    def _tool_confirm_combo(self, category_policy: ToolPolicy, override: ToolPolicy | None) -> QComboBox:
        combo = self._confirm_combo(category_policy.auto_approve, inherited=True)
        combo.setItemText(0, f"继承（{'自动' if category_policy.auto_approve else '确认'}）")
        if override is None:
            combo.setCurrentIndex(0)
        else:
            combo.setCurrentIndex(combo.findData("auto" if override.auto_approve else "confirm"))
        return combo

    def _category_changed(self, _category: str) -> None:
        if self._loading:
            return
        self._sync_from_controls()
        self._approval_mode = "custom"
        self._populate()

    def _tool_changed(self, _index: int) -> None:
        if self._loading:
            return
        self._approval_mode = "custom"
        self._refresh_mode_ui()

    def _sync_from_controls(self) -> None:
        for category, controls in self._category_rows.items():
            self._category_defaults[category] = ToolPolicy(
                enabled=str(controls["enabled"].currentData() or "enabled") == "enabled",
                auto_approve=str(controls["confirm"].currentData() or "confirm") == "auto",
            )

        overrides: dict[str, ToolPolicy] = {}
        for tool_name, controls in self._rows.items():
            enabled_value = str(controls["enabled"].currentData() or "inherit")
            confirm = controls["confirm"]
            confirm_value = str(confirm.currentData() or "inherit") if confirm is not None else "inherit"
            if enabled_value == "inherit" and confirm_value == "inherit":
                continue
            inherited = self._category_defaults.get(controls["descriptor"].category, ToolPolicy())
            overrides[tool_name] = ToolPolicy(
                enabled=inherited.enabled if enabled_value == "inherit" else enabled_value == "enabled",
                auto_approve=(
                    False
                    if controls["descriptor"].risk == "high"
                    else inherited.auto_approve if confirm_value == "inherit" else confirm_value == "auto"
                ),
            )
        self._overrides = overrides

    def _reset_tool(self, tool_name: str) -> None:
        controls = self._rows.get(tool_name)
        if not controls:
            return
        controls["enabled"].setCurrentIndex(0)
        if controls["confirm"] is not None:
            controls["confirm"].setCurrentIndex(0)
        self._approval_mode = "custom"
        self._refresh_mode_ui()

    def _reset_category(self, category: str) -> None:
        self._sync_from_controls()
        self._category_defaults[category] = default_tool_category_policies()[category]
        self._approval_mode = "custom"
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

    def _apply_safe_defaults(self) -> None:
        self._approval_mode = "standard"
        self._category_defaults = default_tool_category_policies()
        self._overrides.clear()
        self._populate()

    def _apply_developer_trust(self) -> None:
        self._approval_mode = "developer_trust"
        self._category_defaults = default_tool_category_policies()
        self._overrides.clear()
        self._populate()

    def _apply_allow_all(self) -> None:
        self._approval_mode = "allow_all"
        self._category_defaults = default_tool_category_policies()
        self._overrides.clear()
        self._populate()

    def _refresh_mode_ui(self) -> None:
        labels = {
            "standard": "当前：安全默认",
            "developer_trust": "当前：开发信任",
            "allow_all": "当前：全部放行",
            "custom": "当前：自定义",
        }
        self.mode_label.setText(labels.get(self._approval_mode, "当前：自定义"))
        self.warning_label.setVisible(self._approval_mode == "allow_all")
        policy_controls_enabled = self._approval_mode in {"standard", "custom"}
        for controls in self._category_rows.values():
            controls["confirm"].setEnabled(policy_controls_enabled)
        for controls in self._rows.values():
            confirm = controls.get("confirm")
            if confirm is not None:
                confirm.setEnabled(policy_controls_enabled)
            label = controls.get("approval_label")
            if label is not None:
                label.setText("全部放行" if self._approval_mode == "allow_all" else "每次确认")
                label.setToolTip("高风险工具仅在“全部放行”模式下跳过确认。")

    def collect(self) -> ToolPermissionConfig:
        self._sync_from_controls()
        return ToolPermissionConfig(
            approval_mode=self._approval_mode,
            category_defaults=dict(self._category_defaults),
            tools=dict(self._overrides),
        )

    def _reset_defaults(self) -> None:
        self._apply_safe_defaults()
