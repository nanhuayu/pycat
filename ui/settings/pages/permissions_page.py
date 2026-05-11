from __future__ import annotations

from typing import Dict, List, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QCheckBox,
    QHBoxLayout, QPushButton, QGridLayout, QFrame,
)

from core.capabilities import default_capabilities_config
from core.capabilities.types import CapabilitiesConfig
from core.config.schema import ToolPermissionConfig, ToolPolicy
from core.tools.catalog import TOOL_CATEGORY_LABELS, TOOL_CATEGORIES, ToolDescriptor
from core.tools.manager import ToolManager
from services.storage_service import StorageService
from ui.settings.page_header import build_page_header


class PermissionsPage(QWidget):
    page_title = "权限"

    def __init__(
        self,
        permissions: ToolPermissionConfig,
        storage_service: StorageService | None = None,
        capabilities: CapabilitiesConfig | None = None,
        tool_manager: ToolManager | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._permissions = permissions
        self._storage = storage_service or StorageService()
        self._capabilities = capabilities or default_capabilities_config()
        self._tool_manager = tool_manager or ToolManager()
        self._category_tools: List[Tuple[str, str, List[Tuple[str, str]]]] = self._build_category_tools()
        self._setup_ui(permissions)

    def _build_category_tools(self) -> List[Tuple[str, str, List[Tuple[str, str]]]]:
        descriptors = self._tool_manager.list_tool_descriptors(include_dynamic=True)
        grouped: Dict[str, List[ToolDescriptor]] = {category: [] for category in TOOL_CATEGORIES}
        for descriptor in descriptors.values():
            grouped.setdefault(descriptor.category, []).append(descriptor)

        rows: List[Tuple[str, str, List[Tuple[str, str]]]] = []
        for category in TOOL_CATEGORIES:
            items = sorted(
                grouped.get(category, []),
                key=lambda d: (d.sort_order, d.display_name.lower(), d.name),
            )
            if not items:
                continue
            rows.append((
                TOOL_CATEGORY_LABELS.get(category, category),
                category,
                [(item.name, item.display_name) for item in items],
            ))
        return rows

    def _setup_ui(self, permissions: ToolPermissionConfig) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("权限", "统一控制内置工具、能力工具与 MCP 工具的启用和自动批准策略。"))

        scope_hint = QLabel(
            "说明：模式和请求级 tool_selection 决定模型能看见哪些工具；"
            "MCP 页决定外部 MCP Server 是否可用；本页只决定工具是否启用以及是否自动批准。"
        )
        scope_hint.setWordWrap(True)
        scope_hint.setProperty("muted", True)
        layout.addWidget(scope_hint)

        header = QHBoxLayout()
        header.addWidget(QLabel("工具名称"), 2)
        header.addWidget(QLabel("启用"), 1, alignment=Qt.AlignmentFlag.AlignCenter)
        header.addWidget(QLabel("自动批准"), 1, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setProperty("muted", True)
        layout.addWidget(sep)

        self._tool_checks: Dict[str, Tuple[QCheckBox, QCheckBox]] = {}
        self._cat_toggles: Dict[str, QCheckBox] = {}

        for category_label, category_key, tools in self._category_tools:
            cat_header = QHBoxLayout()
            cat_label = QLabel(f"<b>{category_label}</b>")
            cat_label.setProperty("muted", True)
            cat_header.addWidget(cat_label)

            cat_toggle = QCheckBox("启用全部")
            cat_toggle.setChecked(self._category_all_enabled(permissions, category_key, tools))
            cat_toggle.setToolTip(f"一键启用/禁用全部{category_label}工具")
            cat_toggle.stateChanged.connect(self._make_category_toggle(category_key, tools))
            cat_header.addWidget(cat_toggle)
            self._cat_toggles[category_key] = cat_toggle

            layout.addLayout(cat_header)

            grid = QGridLayout()
            grid.setColumnStretch(0, 2)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(2, 1)
            grid.setVerticalSpacing(4)
            grid.setHorizontalSpacing(8)

            for row, (tool_name, display_name) in enumerate(tools):
                policy = permissions.resolve(tool_name, category_key)
                self._add_tool_row(grid, row, tool_name, display_name, policy)

            layout.addLayout(grid)
            layout.addSpacing(8)

        reset_row = QHBoxLayout()
        reset_btn = QPushButton("恢复默认值")
        reset_btn.setToolTip("读取/搜索类工具启用并自动批准；编辑/命令/委托类工具启用但需确认；其余按类别默认。")
        reset_btn.clicked.connect(self._reset_defaults)
        reset_row.addStretch(1)
        reset_row.addWidget(reset_btn)
        layout.addLayout(reset_row)
        layout.addStretch()

    @staticmethod
    def _new_grid() -> QGridLayout:
        grid = QGridLayout()
        grid.setColumnStretch(0, 2)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        grid.setVerticalSpacing(4)
        grid.setHorizontalSpacing(8)
        return grid

    @staticmethod
    def _category_all_enabled(
        permissions: ToolPermissionConfig,
        category_key: str,
        tools: List[Tuple[str, str]],
    ) -> bool:
        if not tools:
            return True
        for tool_name, _display_name in tools:
            if not permissions.resolve(tool_name, category_key).enabled:
                return False
        return True

    def _add_tool_row(
        self,
        grid: QGridLayout,
        row: int,
        tool_name: str,
        display_name: str,
        policy: ToolPolicy,
    ) -> None:
        name_label = QLabel(display_name)
        name_label.setToolTip(tool_name)

        enabled_check = QCheckBox()
        enabled_check.setChecked(bool(policy.enabled))
        enabled_check.setToolTip(f"允许模型调用 {tool_name}")

        auto_check = QCheckBox()
        auto_check.setChecked(bool(policy.auto_approve))
        auto_check.setToolTip(f"调用 {tool_name} 时不再询问确认")

        grid.addWidget(name_label, row, 0)
        grid.addWidget(enabled_check, row, 1, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(auto_check, row, 2, alignment=Qt.AlignmentFlag.AlignCenter)
        self._tool_checks[tool_name] = (enabled_check, auto_check)

    def _make_category_toggle(
        self, category_key: str, tools: List[Tuple[str, str]]
    ):
        def _toggle(state: int):
            checked = state == Qt.CheckState.Checked.value
            for tool_name, _ in tools:
                if tool_name not in self._tool_checks:
                    continue
                enabled_check, _auto_check = self._tool_checks[tool_name]
                enabled_check.setChecked(checked)
        return _toggle

    def _reset_defaults(self) -> None:
        for _category_label, category_key, tools in self._category_tools:
            for tool_name, _display_name in tools:
                enabled_check, auto_check = self._tool_checks[tool_name]
                enabled_check.setChecked(True)
                auto_check.setChecked(category_key in ("read", "search"))
            if category_key in self._cat_toggles:
                self._cat_toggles[category_key].setChecked(True)
        for tool_name, (enabled_check, auto_check) in self._tool_checks.items():
            if tool_name.startswith("capability__") or tool_name.startswith("mcp__"):
                enabled_check.setChecked(True)
                auto_check.setChecked(False)

    def collect(self) -> ToolPermissionConfig:
        tools: Dict[str, ToolPolicy] = {}
        for tool_name, (enabled_check, auto_check) in self._tool_checks.items():
            policy = ToolPolicy(
                enabled=bool(enabled_check.isChecked()),
                auto_approve=bool(auto_check.isChecked()),
            )
            tools[tool_name] = policy

        category_defaults: Dict[str, ToolPolicy] = {}
        for _category_label, category_key, tool_list in self._category_tools:
            if not tool_list:
                continue
            first_tool = tool_list[0][0]
            category_enabled = bool(
                self._cat_toggles.get(category_key).isChecked()
                if category_key in self._cat_toggles else True
            )
            _enabled_check, auto_check = self._tool_checks[first_tool]
            category_defaults[category_key] = ToolPolicy(
                enabled=category_enabled,
                auto_approve=bool(auto_check.isChecked()),
            )

        return ToolPermissionConfig(category_defaults=category_defaults, tools=tools)
