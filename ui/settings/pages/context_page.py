from __future__ import annotations

from typing import Iterable

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel

from core.config.schema import ContextConfig, CompressionPolicyConfig
from models.provider import Provider
from ui.settings.page_header import build_page_header
from ui.utils.form_builder import FormSection
from ui.widgets.model_ref_selector import ModelRefCombo


class ContextPage(QWidget):
    page_title = "上下文"

    def __init__(self, context: ContextConfig, providers: Iterable[Provider] | None = None, parent=None):
        super().__init__(parent)
        self._initial = context
        self._providers = list(providers or [])
        self._setup_ui(context)

    def _setup_ui(self, context: ContextConfig) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("上下文", "配置上下文窗口与自动压缩策略，减少长对话失控。"))

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
        agent = FormSection("自动压缩")
        self.agent_auto_compress_enabled = agent.add_checkbox(
            "启用自动压缩", checked=bool(context.agent_auto_compress_enabled),
        )
        self.summary_model = ModelRefCombo(
            self._providers,
            current_model_ref=context.summary_model or "",
            empty_label="跟随当前对话模型",
        )
        agent.form.addRow("压缩模型", self.summary_model)
        self.summary_include_tool_details = agent.add_checkbox(
            "压缩时包含工具详情", checked=bool(context.summary_include_tool_details),
        )
        self.summary_system_prompt = agent.add_text_edit(
            "压缩 System",
            text=(context.summary_system_prompt or ""),
            placeholder="留空时使用内置压缩模板",
            max_height=90,
        )
        self.comp_max_active_messages = agent.add_spin(
            "活跃消息上限", value=int(pol.max_active_messages or 20), range=(5, 200),
        )
        self.comp_token_threshold_ratio = agent.add_double_spin(
            "Token 阈值比例", value=float(pol.token_threshold_ratio or 0.70),
            range=(0.10, 0.95), step=0.05,
        )
        self.comp_keep_last_n = agent.add_spin(
            "保留最后 N 条", value=int(pol.keep_last_n or 10), range=(2, 50),
        )
        layout.addWidget(agent.group)
        layout.addStretch()

    def collect(self) -> ContextConfig:
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
            summary_model=self.summary_model.model_ref(),
            summary_system_prompt=(self.summary_system_prompt.toPlainText() or "").strip(),
            summary_include_tool_details=bool(self.summary_include_tool_details.isChecked()),
            compression_policy=pol,
        )
