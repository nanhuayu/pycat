from __future__ import annotations

from dataclasses import replace

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSpinBox, QVBoxLayout, QWidget

from gui.settings.page_header import build_page_header
from gui.utils.form_builder import FormSection
from models.contracts.config import AgentRuntimeConfig, ContextConfig, RetryConfig


class StrategyPage(QWidget):
    page_title = "策略"

    def __init__(
        self,
        agent: AgentRuntimeConfig | None = None,
        retry: RetryConfig | None = None,
        context: ContextConfig | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._original_context = context or ContextConfig()
        self._setup_ui(agent or AgentRuntimeConfig(), retry or RetryConfig(), self._original_context)

    def _setup_ui(self, agent: AgentRuntimeConfig, retry: RetryConfig, context: ContextConfig) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(build_page_header("策略", "配置运行上限、上下文压缩和重试。完成方式由各 Mode 单独决定。"))

        runtime = FormSection("运行")
        self.max_turns_spin = runtime.add_spin(
            "全局最大轮次",
            value=int(agent.max_turns or 20),
            range=(1, 100),
            tooltip="仅在 Mode 或子 Agent profile 未设置轮次时使用。",
        )
        layout.addWidget(runtime.group)

        context_section = FormSection("上下文")
        self.agent_auto_compress_enabled = context_section.add_checkbox(
            "自动压缩", checked=bool(context.agent_auto_compress_enabled)
        )
        layout.addWidget(context_section.group)

        pol = context.compression_policy
        compression = FormSection("压缩阈值")
        threshold_row = QWidget()
        threshold_layout = QHBoxLayout(threshold_row)
        threshold_layout.setContentsMargins(0, 0, 0, 0)
        threshold_layout.setSpacing(8)
        threshold_layout.addWidget(QLabel("预整理"))
        self.comp_preflight_threshold_percent = QSpinBox()
        self.comp_preflight_threshold_percent.setRange(10, 90)
        self.comp_preflight_threshold_percent.setValue(round(float(pol.preflight_threshold_ratio) * 100))
        self.comp_preflight_threshold_percent.setSuffix("%")
        threshold_layout.addWidget(self.comp_preflight_threshold_percent)
        threshold_layout.addWidget(QLabel("压缩"))
        self.comp_token_threshold_percent = QSpinBox()
        self.comp_token_threshold_percent.setRange(15, 95)
        self.comp_token_threshold_percent.setValue(round(float(pol.token_threshold_ratio) * 100))
        self.comp_token_threshold_percent.setSuffix("%")
        threshold_layout.addWidget(self.comp_token_threshold_percent)
        threshold_layout.addStretch(1)
        compression.form.addRow("触发比例", threshold_row)
        self.comp_history_keep_last_turns = compression.add_spin(
            "保留最近轮次", value=int(pol.history_keep_last_turns or 3), range=(1, 20)
        )
        self.comp_token_threshold_ratio = self.comp_token_threshold_percent
        layout.addWidget(compression.group)

        retry_section = FormSection("重试")
        self.max_retries_spin = retry_section.add_spin(
            "最大次数", value=retry.max_retries, range=(0, 10)
        )
        self.base_delay_spin = retry_section.add_double_spin(
            "基础延迟（秒）", value=retry.base_delay, range=(0.5, 30.0), step=0.5
        )
        self.backoff_combo = retry_section.add_combo(
            "退避因子", items=["1.5", "2.0", "3.0"], current_text=str(retry.backoff_factor)
        )
        layout.addWidget(retry_section.group)

        for control in (
            self.comp_preflight_threshold_percent,
            self.comp_token_threshold_percent,
            self.comp_history_keep_last_turns,
        ):
            self.agent_auto_compress_enabled.toggled.connect(control.setEnabled)
            control.setEnabled(bool(context.agent_auto_compress_enabled))
        layout.addStretch(1)

    def collect_agent(self) -> AgentRuntimeConfig:
        return AgentRuntimeConfig(max_turns=int(self.max_turns_spin.value()))

    def collect_retry(self) -> RetryConfig:
        return RetryConfig(
            max_retries=self.max_retries_spin.value(),
            base_delay=self.base_delay_spin.value(),
            backoff_factor=float(self.backoff_combo.currentText()),
        )

    def collect_context(self) -> ContextConfig:
        low = float(self.comp_preflight_threshold_percent.value()) / 100.0
        high = float(self.comp_token_threshold_percent.value()) / 100.0
        if low >= high:
            raise ValueError("预整理阈值必须低于压缩阈值")
        policy = replace(
            self._original_context.compression_policy,
            preflight_threshold_ratio=low,
            token_threshold_ratio=high,
            history_keep_last_turns=int(self.comp_history_keep_last_turns.value()),
        )
        return replace(
            self._original_context,
            agent_auto_compress_enabled=bool(self.agent_auto_compress_enabled.isChecked()),
            compression_policy=policy,
        )
