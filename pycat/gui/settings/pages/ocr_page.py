"""One OCR configuration surface for the local and vision backends."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from pycat.core.capabilities.defaults import default_capabilities_config
from pycat.gui.settings.page_header import build_page_header
from pycat.gui.utils.form_builder import FormSection
from pycat.gui.widgets.model_ref_selector import ModelRefCombo, build_model_ref_options
from pycat.models.contracts.config import OcrConfig
from pycat.models.contracts.model_target import ModelTarget


def _local_status_text(status) -> str:
    if status is None:
        return QCoreApplication.translate("OcrPage", "本地组件将在使用时检查")
    if getattr(status, "missing", ()):
        return QCoreApplication.translate("OcrPage", "缺少：{files}").format(files=", ".join(status.missing))
    detail = str(getattr(status, "detail", "") or "")
    if detail == "本地 PP-OCRv6 Small 组件完整":
        return QCoreApplication.translate("OcrPage", "本地 PP-OCRv6 Small 组件完整")
    if detail == "OCR 引擎可用":
        return QCoreApplication.translate("OcrPage", "OCR 引擎可用")
    return detail or QCoreApplication.translate("OcrPage", "本地组件将在使用时检查")


class OcrPage(QWidget):
    page_title = "OCR"

    def __init__(self, config: OcrConfig | None = None, *, status: Any = None,
                 providers=(), capability=None, parent=None) -> None:
        super().__init__(parent)
        config = config or OcrConfig()
        self._capability = capability or default_capabilities_config().capability("ocr")
        self._providers = list(providers)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(build_page_header("OCR", QCoreApplication.translate('OcrPage', '从图片、截图和扫描 PDF 中提取文字。所有入口共用此配置。')))
        behavior = FormSection(QCoreApplication.translate('OcrPage', '识别方式'))
        self.enabled_check = behavior.add_checkbox(QCoreApplication.translate('OcrPage', '启用 OCR'), checked=config.enabled)
        self.backend_combo = behavior.add_combo(QCoreApplication.translate('OcrPage', '识别后端'))
        self.backend_combo.addItem(QCoreApplication.translate('OcrPage', '本地 · PP-OCRv6 Small'), "local")
        self.backend_combo.addItem(QCoreApplication.translate('OcrPage', '大模型 · 视觉识别'), "vision")
        self.backend_combo.setCurrentIndex(max(0, self.backend_combo.findData(config.backend)))
        self.pdf_batch_pages_spin = behavior.add_spin(QCoreApplication.translate('OcrPage', 'PDF 每批'), value=config.pdf_batch_pages, range=(1, 32))
        self.pdf_batch_pages_spin.setSuffix(QCoreApplication.translate('OcrPage', ' 页'))
        layout.addWidget(behavior.group)

        vision = FormSection(QCoreApplication.translate('OcrPage', '视觉模型'))
        self.model_combo = ModelRefCombo(providers, current_model_ref=self._capability.model_target.model_ref,
                                        require_image_input=True, allow_unlisted_current=True,
                                        empty_label=QCoreApplication.translate('OcrPage', '请选择支持图片输入的模型'))
        vision.form.addRow(QCoreApplication.translate('OcrPage', '模型'), self.model_combo)
        self.prompt_edit = vision.add_text_edit(QCoreApplication.translate('OcrPage', '识别提示词'), text=self._capability.prompt, max_height=200)
        self.prompt_edit.setMinimumHeight(130)
        reset = QPushButton(QCoreApplication.translate('OcrPage', '恢复默认提示词'))
        reset.clicked.connect(lambda: self.prompt_edit.setPlainText(default_capabilities_config().capability("ocr").prompt))
        vision.form.addRow(reset)
        self.vision_group = vision.group
        layout.addWidget(self.vision_group)

        runtime = FormSection(QCoreApplication.translate('OcrPage', '运行状态'))
        self.runtime_status_label = QLabel(_local_status_text(status))
        self.runtime_status_label.setWordWrap(True)
        runtime.form.addRow(self.runtime_status_label, info=True)
        self.runtime_group = runtime.group
        layout.addWidget(self.runtime_group)
        self.boundary_label = QLabel()
        self.boundary_label.setWordWrap(True)
        self.boundary_label.setProperty("muted", True)
        layout.addWidget(self.boundary_label)
        layout.addStretch()
        self.enabled_check.toggled.connect(self._sync_enabled_state)
        self.backend_combo.currentIndexChanged.connect(self._sync_enabled_state)
        self._sync_enabled_state()

    def set_providers(self, providers) -> None:
        self._providers = list(providers)
        self.model_combo.set_providers(self._providers)

    def _sync_enabled_state(self, *_args) -> None:
        enabled = self.enabled_check.isChecked()
        vision = self.backend_combo.currentData() == "vision"
        self.backend_combo.setEnabled(enabled)
        self.pdf_batch_pages_spin.setEnabled(enabled)
        self.vision_group.setVisible(vision)
        self.vision_group.setEnabled(enabled)
        self.runtime_group.setVisible(not vision)
        self.boundary_label.setText(
            QCoreApplication.translate('OcrPage', '图片和 PDF 页面将发送给所选模型服务。按页识别，不自动切换后端；模型需支持图片输入。')
            if vision else QCoreApplication.translate('OcrPage', '本地识别不上传文件；PP-OCR 组件只在识别时加载。'))

    def collect(self) -> OcrConfig:
        valid_models = {option.value for option in build_model_ref_options(self._providers, require_image_input=True)}
        if self.enabled_check.isChecked() and self.backend_combo.currentData() == "vision" and self.model_combo.model_ref() not in valid_models:
            raise ValueError(QCoreApplication.translate('OcrPage', '请为视觉 OCR 选择模型，并在模型设置中启用图片输入。'))
        return OcrConfig(enabled=self.enabled_check.isChecked(), pdf_batch_pages=self.pdf_batch_pages_spin.value(),
                         backend=self.backend_combo.currentData())

    def collect_capability(self):
        return replace(self._capability, enabled=True, operation="text", exposure="internal", runtime="single_turn",
                       model_target=ModelTarget.explicit(self.model_combo.model_ref()),
                       prompt=self.prompt_edit.toPlainText().strip() or default_capabilities_config().capability("ocr").prompt)
