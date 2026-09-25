from __future__ import annotations

from dataclasses import replace

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QSpinBox, QVBoxLayout, QWidget

from pycat.gui.settings.page_header import build_page_header
from pycat.gui.utils.form_builder import FormSection
from pycat.models.contracts.config import AgentRuntimeConfig, ContextConfig, RetryConfig


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
        layout.addWidget(build_page_header(QCoreApplication.translate('StrategyPage', '策略'), QCoreApplication.translate('StrategyPage', '配置运行上限、上下文压缩和重试。完成方式由各 Mode 单独决定。')))

        runtime = FormSection(QCoreApplication.translate('StrategyPage', '运行'))
        self.max_turns_spin = runtime.add_spin(
            QCoreApplication.translate('StrategyPage', '全局最大轮次'),
            value=int(agent.max_turns),
            range=(1, 1000),
            tooltip=QCoreApplication.translate('StrategyPage', '仅在 Mode 或子 Agent profile 未设置轮次时使用。'),
        )
        layout.addWidget(runtime.group)

        context_section = FormSection(QCoreApplication.translate('StrategyPage', '上下文压缩'))
        pol = context.compression_policy
        self.tight_replay_enabled = QCheckBox(QCoreApplication.translate('StrategyPage', '启用'))
        self.tight_replay_enabled.setChecked(bool(pol.tight_replay_enabled))
        compact_percent = max(11, min(95, round(float(pol.token_threshold_ratio) * 100)))
        tight_percent = max(
            10,
            min(compact_percent - 1, round(float(pol.tight_replay_threshold_ratio) * 100)),
        )
        tight_row = QWidget()
        tight_layout = QHBoxLayout(tight_row)
        tight_layout.setContentsMargins(0, 0, 0, 0)
        tight_layout.setSpacing(8)
        self.tight_replay_threshold_percent = QSpinBox()
        self.tight_replay_threshold_percent.setRange(10, compact_percent - 1)
        self.tight_replay_threshold_percent.setValue(tight_percent)
        self.tight_replay_threshold_percent.setSuffix("%")
        self.tight_replay_threshold_percent.setToolTip(
            QCoreApplication.translate('StrategyPage', '完整请求达到该比例后，用可恢复视图替换较旧工具结果；不调用模型，也不修改会话历史。')
        )
        tight_layout.addStretch(1)
        tight_layout.addWidget(self.tight_replay_enabled)
        tight_layout.addWidget(self.tight_replay_threshold_percent)
        context_section.form.addRow(QCoreApplication.translate('StrategyPage', '工具内容压缩'), tight_row)

        self.agent_auto_compress_enabled = QCheckBox(QCoreApplication.translate('StrategyPage', '启用'))
        self.agent_auto_compress_enabled.setChecked(bool(context.agent_auto_compress_enabled))

        compact_row = QWidget()
        compact_layout = QHBoxLayout(compact_row)
        compact_layout.setContentsMargins(0, 0, 0, 0)
        compact_layout.setSpacing(8)
        self.comp_token_threshold_percent = QSpinBox()
        self.comp_token_threshold_percent.setRange(tight_percent + 1, 95)
        self.comp_token_threshold_percent.setValue(compact_percent)
        self.comp_token_threshold_percent.setSuffix("%")
        self.comp_token_threshold_percent.setToolTip(
            QCoreApplication.translate('StrategyPage', '选定的完整请求达到可用输入预算的该比例后，自动生成历史摘要；原文保留。')
        )
        compact_layout.addStretch(1)
        compact_layout.addWidget(self.agent_auto_compress_enabled)
        compact_layout.addWidget(self.comp_token_threshold_percent)
        context_section.form.addRow(QCoreApplication.translate('StrategyPage', '上下文压缩'), compact_row)
        self.tight_replay_threshold_percent.valueChanged.connect(
            lambda value: self.comp_token_threshold_percent.setMinimum(min(95, value + 1))
        )
        self.comp_token_threshold_percent.valueChanged.connect(
            lambda value: self.tight_replay_threshold_percent.setMaximum(max(10, value - 1))
        )
        self.tight_replay_threshold_ratio = self.tight_replay_threshold_percent
        self.comp_token_threshold_ratio = self.comp_token_threshold_percent
        layout.addWidget(context_section.group)

        retry_section = FormSection(QCoreApplication.translate('StrategyPage', '重试'))
        self.max_retries_spin = retry_section.add_spin(
            QCoreApplication.translate('StrategyPage', '最大次数'), value=retry.max_retries, range=(0, 10)
        )
        self.base_delay_spin = retry_section.add_double_spin(
            QCoreApplication.translate('StrategyPage', '基础延迟（秒）'), value=retry.base_delay, range=(0.5, 30.0), step=0.5
        )
        self.backoff_combo = retry_section.add_combo(
            QCoreApplication.translate('StrategyPage', '退避因子'), items=["1.5", "2.0", "3.0"], current_text=str(retry.backoff_factor)
        )
        layout.addWidget(retry_section.group)

        self.agent_auto_compress_enabled.toggled.connect(self.comp_token_threshold_percent.setEnabled)
        self.comp_token_threshold_percent.setEnabled(bool(context.agent_auto_compress_enabled))
        self.tight_replay_enabled.toggled.connect(self.tight_replay_threshold_percent.setEnabled)
        self.tight_replay_threshold_percent.setEnabled(
            bool(getattr(pol, "tight_replay_enabled", True))
        )
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
        policy = replace(
            self._original_context.compression_policy,
            tight_replay_enabled=bool(self.tight_replay_enabled.isChecked()),
            tight_replay_threshold_ratio=float(self.tight_replay_threshold_percent.value()) / 100.0,
            token_threshold_ratio=float(self.comp_token_threshold_percent.value()) / 100.0,
        )
        return replace(
            self._original_context,
            agent_auto_compress_enabled=bool(self.agent_auto_compress_enabled.isChecked()),
            compression_policy=policy,
        )
