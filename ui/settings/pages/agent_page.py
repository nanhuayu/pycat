from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QGroupBox, QCheckBox,
    QHBoxLayout, QSpinBox, QDoubleSpinBox, QComboBox,
)

from core.config.schema import PermissionsConfig, RetryConfig
from ui.settings.page_header import build_page_header
from ui.utils.combo_box import configure_combo_popup


class AgentPermissionsPage(QWidget):
    page_title = "Agent"

    def __init__(self, permissions: PermissionsConfig, retry: RetryConfig | None = None, parent=None):
        super().__init__(parent)
        self._setup_ui(permissions, retry or RetryConfig())

    def _setup_ui(self, permissions: PermissionsConfig, retry: RetryConfig) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("Agent", "集中控制自动授权与重试策略，不直接决定工具是否对模型可见。"))

        perm_group = QGroupBox("自动授权设置")
        perm_layout = QVBoxLayout(perm_group)

        self.auto_read_check = QCheckBox("自动允许读取类工具 (list_files, read_file, search_files 等)")
        self.auto_read_check.setToolTip("启用后，读取文件的操作将不再询问确认")
        self.auto_read_check.setChecked(bool(permissions.auto_approve_read))
        perm_layout.addWidget(self.auto_read_check)

        self.auto_edit_check = QCheckBox("自动允许编辑类工具 (write/edit/delete/patch)")
        self.auto_edit_check.setToolTip("启用后，修改文件的操作将不再询问确认 (请谨慎开启)")
        self.auto_edit_check.setChecked(bool(permissions.auto_approve_edit))
        perm_layout.addWidget(self.auto_edit_check)

        self.auto_cmd_check = QCheckBox("自动允许命令类工具 (execute_command/shell_*)")
        self.auto_cmd_check.setToolTip("启用后，执行 Shell 命令将不再询问确认 (极度危险!)")
        self.auto_cmd_check.setChecked(bool(permissions.auto_approve_command))
        perm_layout.addWidget(self.auto_cmd_check)

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

    def collect(self) -> PermissionsConfig:
        return PermissionsConfig(
            auto_approve_read=bool(self.auto_read_check.isChecked()),
            auto_approve_edit=bool(self.auto_edit_check.isChecked()),
            auto_approve_command=bool(self.auto_cmd_check.isChecked()),
        )

    def collect_retry(self) -> RetryConfig:
        return RetryConfig(
            max_retries=self.max_retries_spin.value(),
            base_delay=self.base_delay_spin.value(),
            backoff_factor=float(self.backoff_combo.currentText()),
        )
