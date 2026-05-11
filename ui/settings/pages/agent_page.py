from __future__ import annotations

from typing import Iterable

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QGroupBox,
    QHBoxLayout, QSpinBox, QDoubleSpinBox, QComboBox,
)

from core.config.schema import (
    AgentRuntimeConfig,
    CompressionPolicyConfig,
    ContextConfig,
    RetryConfig,
)
from models.provider import Provider
from ui.settings.page_header import build_page_header
from ui.utils.combo_box import configure_combo_popup
from ui.utils.form_builder import FormSection


class AgentPage(QWidget):
    page_title = "Agent"

    def __init__(
        self,
        agent: AgentRuntimeConfig | None = None,
        retry: RetryConfig | None = None,
        context: ContextConfig | None = None,
        providers: Iterable[Provider] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._providers = list(providers or [])
        self._setup_ui(
            agent or AgentRuntimeConfig(),
            retry or RetryConfig(),
            context or ContextConfig(),
        )

    def _setup_ui(self, agent: AgentRuntimeConfig, retry: RetryConfig, context: ContextConfig) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("Agent", "配置 Agent 运行轮次、重试策略与上下文压缩。工具权限已移至独立权限面板。"))

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

        ctx = FormSection("上下文窗口")
        self.default_max_context_messages = ctx.add_spin(
            "默认上下文消息数", value=int(context.default_max_context_messages or 0),
            range=(0, 200), tooltip="默认值；0 表示不限制（由模型/服务商上下文上限决定）",
        )
        tip = QLabel("说明：一般保持 0 即可，让模型上下文窗口自行决定上限。")
        tip.setWordWrap(True)
        tip.setProperty("muted", True)
        layout.addWidget(tip)
        layout.addWidget(ctx.group)

        pol = context.compression_policy
        comp = FormSection("自动压缩")
        self.agent_auto_compress_enabled = comp.add_checkbox(
            "启用自动压缩", checked=bool(context.agent_auto_compress_enabled),
        )
        comp_hint = QLabel(
            "说明：自动压缩分两层配置。本页只控制何时触发自动压缩；"
            "压缩模型、系统提示词和工具详情选项由“能力 → 上下文压缩”统一配置。"
        )
        comp_hint.setWordWrap(True)
        comp_hint.setStyleSheet(
            "QLabel { padding: 8px 10px; border-radius: 6px; "
            "background: rgba(59, 130, 246, 0.10); color: palette(text); }"
        )
        comp.form.addRow(comp_hint)
        self.comp_max_active_messages = comp.add_spin(
            "活跃消息上限", value=int(pol.max_active_messages or 20), range=(5, 200),
        )
        self.comp_token_threshold_ratio = comp.add_double_spin(
            "Token 阈值比例", value=float(pol.token_threshold_ratio or 0.70),
            range=(0.10, 0.95), step=0.05,
        )
        self.comp_keep_last_n = comp.add_spin(
            "保留最后 N 条", value=int(pol.keep_last_n or 10), range=(2, 50),
        )
        layout.addWidget(comp.group)
        layout.addStretch()

    def collect_agent(self) -> AgentRuntimeConfig:
        return AgentRuntimeConfig(max_turns=int(self.max_turns_spin.value()))

    def collect_retry(self) -> RetryConfig:
        return RetryConfig(
            max_retries=self.max_retries_spin.value(),
            base_delay=self.base_delay_spin.value(),
            backoff_factor=float(self.backoff_combo.currentText()),
        )

    def collect_context(self) -> ContextConfig:
        pol = CompressionPolicyConfig.from_dict(
            {
                "max_active_messages": int(self.comp_max_active_messages.value()),
                "token_threshold_ratio": float(self.comp_token_threshold_ratio.value()),
                "keep_last_n": int(self.comp_keep_last_n.value()),
            }
        )
        default_max = int(self.default_max_context_messages.value())
        default_max = default_max if default_max > 0 else 0
        return ContextConfig(
            default_max_context_messages=default_max,
            agent_auto_compress_enabled=bool(self.agent_auto_compress_enabled.isChecked()),
            compression_policy=pol,
        )
