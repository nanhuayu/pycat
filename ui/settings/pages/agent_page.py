from __future__ import annotations

from typing import Dict, List, Tuple

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QGroupBox, QCheckBox,
    QHBoxLayout, QSpinBox, QDoubleSpinBox, QComboBox,
    QPushButton, QGridLayout, QFrame, QScrollArea,
)
from PyQt6.QtCore import Qt

from core.capabilities import capability_tool_ids, default_capabilities_config
from core.capabilities.types import CapabilitiesConfig
from core.config.schema import ToolPermissionConfig, ToolPolicy, RetryConfig, AgentRuntimeConfig
from core.tools.mcp.naming import build_mcp_tool_name
from services.storage_service import StorageService
from ui.settings.page_header import build_page_header
from ui.utils.combo_box import configure_combo_popup


# ---------------------------------------------------------------------------
# Built-in tools exposed in the permission matrix
# ---------------------------------------------------------------------------
# Notes:
#   - "shell__process_query" is a virtual tool representing
#     shell_status + shell_logs + shell_wait (all read-only queries).
# ---------------------------------------------------------------------------

_TOOL_CATEGORIES: List[Tuple[str, str, List[Tuple[str, str]]]] = [
    (
        "读取类",
        "read",
        [
            ("list_directory", "浏览目录结构"),
            ("read_file", "读取文件内容"),
            ("search_code", "搜索代码文本"),
            ("skill__load", "加载 Skill"),
            ("skill__read_resource", "读取 Skill 资源"),
            ("ask_questions", "向用户提问"),
            ("manage_state", "管理会话状态与记忆"),
            ("manage_document", "管理会话文档"),
            ("attempt_completion", "标记任务完成"),
            ("switch_mode", "切换对话模式"),
            ("shell__process_query", "后台进程查询"),
        ],
    ),
    (
        "编辑类",
        "edit",
        [
            ("write_file", "写入/覆盖文件"),
            ("edit_file", "编辑文件内容"),
            ("delete_file", "删除文件或目录"),
            ("apply_patch", "应用 diff 补丁"),
        ],
    ),
    (
        "执行类",
        "command",
        [
            ("execute_command", "执行 Shell 命令"),
            ("python_exec", "执行 Python 代码"),
            ("shell_start", "启动后台进程"),
            ("shell_kill", "终止后台进程"),
        ],
    ),
    (
        "委托类",
        "delegate",
        [
            ("subagent__read_analyze", "只读分析子 Agent"),
            ("subagent__search", "搜索研究子 Agent"),
            ("subagent__custom", "通用委托子 Agent"),
        ],
    ),
]


class AgentPermissionsPage(QWidget):
    page_title = "Agent"

    def __init__(
        self,
        permissions: ToolPermissionConfig,
        retry: RetryConfig | None = None,
        agent: AgentRuntimeConfig | None = None,
        storage_service: StorageService | None = None,
        capabilities: CapabilitiesConfig | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._permissions = permissions
        self._storage = storage_service or StorageService()
        self._capabilities = capabilities or default_capabilities_config()
        self._setup_ui(permissions, retry or RetryConfig(), agent or AgentRuntimeConfig())

    def _setup_ui(self, permissions: ToolPermissionConfig, retry: RetryConfig, agent: AgentRuntimeConfig) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("Agent", "集中控制运行轮次、自动授权与重试策略。每个工具可独立设置启用与自动批准。"))

        # --- Runtime strategy ---
        runtime_group = QGroupBox("运行策略")
        runtime_layout = QVBoxLayout(runtime_group)

        turns_row = QHBoxLayout()
        turns_row.addWidget(QLabel("Agent 最大轮次:"))
        self.max_turns_spin = QSpinBox()
        self.max_turns_spin.setRange(1, 100)
        self.max_turns_spin.setValue(int(agent.max_turns or 20))
        self.max_turns_spin.setToolTip("每次 Agent 调用最多允许的模型-工具循环轮次。")
        turns_row.addWidget(self.max_turns_spin)
        turns_row.addStretch(1)
        runtime_layout.addLayout(turns_row)

        hint = QLabel("该设置为全局默认值；能力或运行模式可按需提供更具体的轮次限制。")
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        runtime_layout.addWidget(hint)

        layout.addWidget(runtime_group)

        # --- Tool permission matrix ---
        perm_group = QGroupBox("工具权限矩阵")
        perm_layout = QVBoxLayout(perm_group)
        perm_layout.setSpacing(10)

        header = QHBoxLayout()
        header.addWidget(QLabel("工具名称"), 2)
        header.addWidget(QLabel("启用"), 1, alignment=Qt.AlignmentFlag.AlignCenter)
        header.addWidget(QLabel("自动批准"), 1, alignment=Qt.AlignmentFlag.AlignCenter)
        perm_layout.addLayout(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setProperty("muted", True)
        perm_layout.addWidget(sep)

        self._tool_checks: Dict[str, Tuple[QCheckBox, QCheckBox]] = {}
        self._cat_toggles: Dict[str, QCheckBox] = {}

        for category_label, category_key, tools in _TOOL_CATEGORIES:
            cat_header = QHBoxLayout()
            cat_label = QLabel(f"<b>{category_label}</b>")
            cat_label.setProperty("muted", True)
            cat_header.addWidget(cat_label)

            cat_toggle = QCheckBox("启用全部")
            cat_toggle.setChecked(True)
            cat_toggle.setToolTip(f"一键启用/禁用全部{category_label}工具")
            cat_toggle.stateChanged.connect(self._make_category_toggle(category_key, tools))
            cat_header.addWidget(cat_toggle)
            self._cat_toggles[category_key] = cat_toggle

            perm_layout.addLayout(cat_header)

            grid = QGridLayout()
            grid.setColumnStretch(0, 2)
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(2, 1)
            grid.setVerticalSpacing(4)
            grid.setHorizontalSpacing(8)

            for row, (tool_name, display_name) in enumerate(tools):
                # Virtual tool: shell__process_query -> resolve via read defaults
                if tool_name == "shell__process_query":
                    policy = permissions.resolve("shell_status", "read")
                else:
                    policy = permissions.resolve(tool_name, category_key)

                name_label = QLabel(display_name)
                name_label.setToolTip(tool_name)

                enabled_check = QCheckBox()
                enabled_check.setChecked(bool(policy.enabled))
                enabled_check.setToolTip(f"允许模型调用 {tool_name}")

                auto_check = QCheckBox()
                auto_check.setChecked(bool(policy.auto_approve))
                auto_check.setToolTip(f"调用 {tool_name} 时不再询问确认")

                # Read tools default to auto-approved; others require explicit enable
                if category_key == "read":
                    enabled_check.setChecked(True)
                    auto_check.setChecked(True)

                grid.addWidget(name_label, row, 0)
                grid.addWidget(enabled_check, row, 1, alignment=Qt.AlignmentFlag.AlignCenter)
                grid.addWidget(auto_check, row, 2, alignment=Qt.AlignmentFlag.AlignCenter)

                self._tool_checks[tool_name] = (enabled_check, auto_check)

            perm_layout.addLayout(grid)
            perm_layout.addSpacing(8)

        # --- Capability tools (merged into delegate category) ---
        cap_ids = capability_tool_ids(self._capabilities)
        if cap_ids:
            cap_label = QLabel("<b>扩展能力</b>")
            cap_label.setProperty("muted", True)
            perm_layout.addWidget(cap_label)

            cap_tools: List[Tuple[str, str]] = []
            for cap_id in cap_ids:
                tool_name = f"capability__{cap_id}"
                cap_cfg = self._capabilities.capability(cap_id)
                display_name = cap_cfg.name if cap_cfg else cap_id
                cap_tools.append((tool_name, display_name))

            # Wire capability tools to the delegate master toggle
            if "delegate" in self._cat_toggles:
                self._cat_toggles["delegate"].stateChanged.connect(
                    self._make_category_toggle("delegate", cap_tools)
                )

            cap_grid = QGridLayout()
            cap_grid.setColumnStretch(0, 2)
            cap_grid.setColumnStretch(1, 1)
            cap_grid.setColumnStretch(2, 1)
            cap_grid.setVerticalSpacing(4)
            cap_grid.setHorizontalSpacing(8)

            for row, (tool_name, display_name) in enumerate(cap_tools):
                policy = permissions.resolve(tool_name, "delegate")

                name_label = QLabel(display_name)
                name_label.setToolTip(tool_name)

                enabled_check = QCheckBox()
                enabled_check.setChecked(bool(policy.enabled))
                enabled_check.setToolTip(f"允许模型调用 {tool_name}")

                auto_check = QCheckBox()
                auto_check.setChecked(bool(policy.auto_approve))
                auto_check.setToolTip(f"调用 {tool_name} 时不再询问确认")

                cap_grid.addWidget(name_label, row, 0)
                cap_grid.addWidget(enabled_check, row, 1, alignment=Qt.AlignmentFlag.AlignCenter)
                cap_grid.addWidget(auto_check, row, 2, alignment=Qt.AlignmentFlag.AlignCenter)

                self._tool_checks[tool_name] = (enabled_check, auto_check)

            perm_layout.addLayout(cap_grid)
            perm_layout.addSpacing(8)

        # --- MCP tools (from cached schemas) ---
        mcp_servers = self._storage.load_mcp_servers()
        active_mcp_tools: List[Tuple[str, str, str]] = []  # (tool_name, display_name, server_name)
        for srv in mcp_servers:
            if not srv.enabled:
                continue
            for tname in srv.cached_tools or []:
                full_name = build_mcp_tool_name(srv.name, tname)
                active_mcp_tools.append((full_name, tname, srv.name))

        if active_mcp_tools:
            mcp_label = QLabel("<b>MCP 工具</b>")
            mcp_label.setProperty("muted", True)
            perm_layout.addWidget(mcp_label)

            mcp_grid = QGridLayout()
            mcp_grid.setColumnStretch(0, 2)
            mcp_grid.setColumnStretch(1, 1)
            mcp_grid.setColumnStretch(2, 1)
            mcp_grid.setVerticalSpacing(4)
            mcp_grid.setHorizontalSpacing(8)

            for row, (tool_name, display_name, server_name) in enumerate(active_mcp_tools):
                policy = permissions.resolve(tool_name, "misc")

                name_label = QLabel(f"{display_name} <span style='color:gray'>[{server_name}]</span>")
                name_label.setToolTip(tool_name)

                enabled_check = QCheckBox()
                enabled_check.setChecked(bool(policy.enabled))
                enabled_check.setToolTip(f"允许模型调用 {tool_name}")

                auto_check = QCheckBox()
                auto_check.setChecked(bool(policy.auto_approve))
                auto_check.setToolTip(f"调用 {tool_name} 时不再询问确认")

                mcp_grid.addWidget(name_label, row, 0)
                mcp_grid.addWidget(enabled_check, row, 1, alignment=Qt.AlignmentFlag.AlignCenter)
                mcp_grid.addWidget(auto_check, row, 2, alignment=Qt.AlignmentFlag.AlignCenter)

                self._tool_checks[tool_name] = (enabled_check, auto_check)

            perm_layout.addLayout(mcp_grid)
            perm_layout.addSpacing(8)

        # Reset button
        reset_row = QHBoxLayout()
        reset_btn = QPushButton("恢复默认值")
        reset_btn.setToolTip("读取类工具启用并自动批准；编辑/命令类工具启用但需确认；其余按类别默认。")
        reset_btn.clicked.connect(self._reset_defaults)
        reset_row.addStretch(1)
        reset_row.addWidget(reset_btn)
        perm_layout.addLayout(reset_row)

        layout.addWidget(perm_group)

        # --- Retry strategy ---
        retry_group = QGroupBox("重试策略")
        retry_layout = QVBoxLayout(retry_group)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("最大重试次数:"))
        self.max_retries_spin = QSpinBox()
        self.max_retries_spin.setRange(0, 10)
        self.max_retries_spin.setValue(retry.max_retries)
        self.max_retries_spin.setToolTip("LLM 调用失败后最多重试几次 (0 = 不重试)")
        row1.addWidget(self.max_retries_spin)
        retry_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("基础延迟 (秒):"))
        self.base_delay_spin = QDoubleSpinBox()
        self.base_delay_spin.setRange(0.5, 30.0)
        self.base_delay_spin.setSingleStep(0.5)
        self.base_delay_spin.setValue(retry.base_delay)
        self.base_delay_spin.setToolTip("首次重试前等待的秒数")
        row2.addWidget(self.base_delay_spin)
        retry_layout.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("退避因子:"))
        self.backoff_combo = QComboBox()
        configure_combo_popup(self.backoff_combo)
        self.backoff_combo.addItems(["1.5", "2.0", "3.0"])
        current_idx = self.backoff_combo.findText(str(retry.backoff_factor))
        self.backoff_combo.setCurrentIndex(current_idx if current_idx >= 0 else 1)
        self.backoff_combo.setToolTip("每次重试后延迟乘以该因子")
        row3.addWidget(self.backoff_combo)
        retry_layout.addLayout(row3)

        layout.addWidget(retry_group)

        layout.addStretch()

    def _make_category_toggle(
        self, category_key: str, tools: List[Tuple[str, str]]
    ):
        """Return a slot that batch-enables/disables tools in a category."""
        def _toggle(state: int):
            checked = state == Qt.CheckState.Checked.value
            for tool_name, _ in tools:
                enabled_check, _auto_check = self._tool_checks[tool_name]
                enabled_check.setChecked(checked)
        return _toggle

    def _reset_defaults(self) -> None:
        """Reset all tool checkboxes to their category defaults."""
        for category_label, category_key, tools in _TOOL_CATEGORIES:
            for tool_name, _display_name in tools:
                enabled_check, auto_check = self._tool_checks[tool_name]
                if category_key == "read":
                    enabled_check.setChecked(True)
                    auto_check.setChecked(True)
                else:
                    # edit, command, delegate — all default to enabled, not auto-approved
                    enabled_check.setChecked(True)
                    auto_check.setChecked(False)
            # Sync category master toggle
            if category_key in self._cat_toggles:
                self._cat_toggles[category_key].setChecked(True)
        # Capability & MCP tools default to enabled, not auto-approved
        for tool_name, (enabled_check, auto_check) in self._tool_checks.items():
            if tool_name.startswith("capability__") or tool_name.startswith("mcp__"):
                enabled_check.setChecked(True)
                auto_check.setChecked(False)

    def collect(self) -> ToolPermissionConfig:
        """Gather current settings into a ToolPermissionConfig."""
        tools: Dict[str, ToolPolicy] = {}
        for tool_name, (enabled_check, auto_check) in self._tool_checks.items():
            policy = ToolPolicy(
                enabled=bool(enabled_check.isChecked()),
                auto_approve=bool(auto_check.isChecked()),
            )
            if tool_name == "shell__process_query":
                # Expand virtual tool to the three real read-only shell tools
                for real_name in ("shell_status", "shell_logs", "shell_wait"):
                    tools[real_name] = policy
            else:
                tools[tool_name] = policy

        # Derive category defaults from the first real tool in each category.
        category_defaults: Dict[str, ToolPolicy] = {}
        for category_label, category_key, tool_list in _TOOL_CATEGORIES:
            if not tool_list:
                continue
            first_tool = tool_list[0][0]
            if first_tool == "shell__process_query":
                # Virtual tool maps to read defaults
                category_defaults[category_key] = ToolPolicy(
                    enabled=True, auto_approve=True
                )
            else:
                _enabled_check, auto_check = self._tool_checks[first_tool]
                category_defaults[category_key] = ToolPolicy(
                    enabled=True,
                    auto_approve=bool(auto_check.isChecked()),
                )

        return ToolPermissionConfig(
            category_defaults=category_defaults,
            tools=tools,
        )

    def collect_retry(self) -> RetryConfig:
        return RetryConfig(
            max_retries=self.max_retries_spin.value(),
            base_delay=self.base_delay_spin.value(),
            backoff_factor=float(self.backoff_combo.currentText()),
        )

    def collect_agent(self) -> AgentRuntimeConfig:
        return AgentRuntimeConfig(max_turns=int(self.max_turns_spin.value()))
