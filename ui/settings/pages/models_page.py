from __future__ import annotations

from typing import List

from PyQt6.QtCore import pyqtSignal, Qt, QSize
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QLabel,
    QMessageBox,
    QGroupBox,
)

from models.provider import Provider
from services.provider_catalog_service import ProviderCatalogService
from services.provider_service import ProviderService
from ui.dialogs.provider_dialog import ProviderDialog
from ui.settings.page_header import build_page_header
from ui.widgets.model_ref_selector import ModelRefCombo, build_model_ref_options
from ui.utils.icon_manager import Icons


class ProviderListItem(QListWidgetItem):
    def __init__(self, provider: Provider):
        super().__init__()
        self.provider = provider
        self.update_display()

    @staticmethod
    def _api_type_label(provider: Provider) -> str:
        if getattr(provider, "is_anthropic_native", False):
            return "Anthropic 原生"
        return "OpenAI 兼容"

    def update_display(self) -> None:
        status_icon = Icons.get_success(Icons.CIRCLE_CHECK) if getattr(self.provider, "enabled", True) else Icons.get_muted(Icons.CIRCLE_INFO)
        api_type = self._api_type_label(self.provider)
        default_model = str(getattr(self.provider, "default_model", "") or "").strip()
        model_count = len(getattr(self.provider, "models", []) or [])
        default_label = default_model if default_model else "未设主模型"
        self.setIcon(status_icon)
        self.setText(f"  {self.provider.name} · {api_type} · {model_count} 个模型")
        self.setToolTip(
            f"接口: {api_type}\n主模型: {default_model or '未设置'}\nAPI: {self.provider.api_base}\n模型数: {len(self.provider.models)}"
        )
        self.setSizeHint(QSize(0, 40))


class ModelsPage(QWidget):
    providers_changed = pyqtSignal()

    page_title = "模型"

    def __init__(
        self,
        providers: List[Provider],
        default_chat_model: str = "",
        provider_service: ProviderService | None = None,
        provider_catalog_service: ProviderCatalogService | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.providers: List[Provider] = []
        self.provider_service = provider_service or ProviderService()
        if provider_catalog_service is None:
            raise ValueError("provider_catalog_service is required")
        self.provider_catalog_service = provider_catalog_service
        self.providers = self.provider_catalog_service.snapshot(providers or [])
        self._default_chat_model = str(default_chat_model or "").strip()
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(build_page_header("模型", "集中管理服务商、主模型，以及新建对话默认使用的模型池。"))

        pool_group = QGroupBox("新建对话默认模型")
        pool_layout = QVBoxLayout(pool_group)
        pool_layout.setContentsMargins(10, 8, 10, 10)
        pool_layout.setSpacing(6)

        self.model_pool_combo = ModelRefCombo(
            self.providers,
            current_model_ref=self._default_chat_model or "",
            allow_empty=False,
            empty_label="选择模型",
        )
        pool_layout.addWidget(self.model_pool_combo)
        self.model_pool_hint = QLabel("")
        self.model_pool_hint.setWordWrap(True)
        self.model_pool_hint.setProperty("muted", True)
        pool_layout.addWidget(self.model_pool_hint)
        layout.addWidget(pool_group)

        self.provider_list = QListWidget()
        self.provider_list.setObjectName("settings_list")
        self.provider_list.setSpacing(2)
        self.provider_list.setMinimumHeight(300)
        self.provider_list.itemDoubleClicked.connect(lambda _item: self._edit_provider())
        layout.addWidget(self.provider_list, 1)

        actions = QHBoxLayout()
        actions.setSpacing(6)

        btn_add = QPushButton()
        btn_add.setIcon(Icons.get(Icons.PLUS, scale_factor=1.0))
        btn_add.setText("添加")
        btn_add.clicked.connect(self._add_provider)
        actions.addWidget(btn_add)

        btn_default = QPushButton()
        btn_default.setIcon(Icons.get(Icons.DOWNLOAD, scale_factor=1.0))
        btn_default.setText("导入")
        btn_default.clicked.connect(self._add_default_providers)
        actions.addWidget(btn_default)

        btn_edit = QPushButton()
        btn_edit.setIcon(Icons.get(Icons.EDIT, scale_factor=1.0))
        btn_edit.setText("编辑")
        btn_edit.clicked.connect(self._edit_provider)
        actions.addWidget(btn_edit)

        btn_up = QPushButton()
        btn_up.setIcon(Icons.get(Icons.ARROW_UP, scale_factor=1.0))
        btn_up.setText("上移")
        btn_up.clicked.connect(lambda: self._move_provider(-1))
        actions.addWidget(btn_up)

        btn_down = QPushButton()
        btn_down.setIcon(Icons.get(Icons.ARROW_DOWN, scale_factor=1.0))
        btn_down.setText("下移")
        btn_down.clicked.connect(lambda: self._move_provider(1))
        actions.addWidget(btn_down)

        btn_del = QPushButton()
        btn_del.setIcon(Icons.get(Icons.TRASH, color=Icons.COLOR_ERROR, scale_factor=1.0))
        btn_del.setText("删除")
        btn_del.setProperty("danger", True)
        btn_del.clicked.connect(self._delete_provider)
        actions.addWidget(btn_del)

        actions.addStretch()
        layout.addLayout(actions)

        hint = QLabel("双击服务商可编辑；上方模型池会同步聚合所有已配置模型，供其他设置页搜索选择。")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._refresh_provider_list()

    def _refresh_provider_list(self) -> None:
        self.provider_list.clear()
        for p in self.providers:
            self.provider_list.addItem(ProviderListItem(p))
        try:
            current = self.model_pool_combo.model_ref() if hasattr(self, "model_pool_combo") else ""
            self.model_pool_combo.set_providers(self.providers, current_model_ref=current)
            option_count = len(build_model_ref_options(self.providers))
            selected = self.collect_default_chat_model() or "未设置"
            self.model_pool_hint.setText(
                f"默认模型：{selected} · 已汇总 {option_count} 个 provider|model 引用。"
                "新建对话、提示优化、上下文压缩等能力都会复用这一份模型池。"
            )
        except Exception:
            pass

    def _add_provider(self) -> None:
        dialog = ProviderDialog(parent=self, provider_service=self.provider_service)
        if dialog.exec():
            p = dialog.get_provider()
            self.providers = self.provider_catalog_service.upsert(self.providers, p)
            self._refresh_provider_list()
            self.providers_changed.emit()

    def _edit_provider(self) -> None:
        item = self.provider_list.currentItem()
        if not isinstance(item, ProviderListItem):
            return
        dialog = ProviderDialog(item.provider, provider_service=self.provider_service, parent=self)
        if dialog.exec():
            updated = dialog.get_provider()
            self.providers = self.provider_catalog_service.upsert(self.providers, updated)
            self._refresh_provider_list()
            self.providers_changed.emit()

    def _delete_provider(self) -> None:
        item = self.provider_list.currentItem()
        if not isinstance(item, ProviderListItem):
            return
        if QMessageBox.question(self, "删除", f'确定删除 "{item.provider.name}"？') == QMessageBox.StandardButton.Yes:
            self.providers = self.provider_catalog_service.remove(
                self.providers,
                str(getattr(item.provider, "id", "") or ""),
            )
            self._refresh_provider_list()
            self.providers_changed.emit()

    def _move_provider(self, delta: int) -> None:
        row = self.provider_list.currentRow()
        if row < 0:
            return
        provider_id = str(getattr(self.providers[row], "id", "") or "") if 0 <= row < len(self.providers) else ""
        new_row = row + int(delta)
        if 0 <= new_row < len(self.providers):
            self.providers = self.provider_catalog_service.move(self.providers, provider_id, delta)
            self._refresh_provider_list()
            self.provider_list.setCurrentRow(new_row)
            self.providers_changed.emit()

    def _add_default_providers(self) -> None:
        self.providers, added_any = self.provider_catalog_service.merge_defaults(self.providers)

        if added_any:
            self._refresh_provider_list()
            self.providers_changed.emit()

    def get_providers(self) -> List[Provider]:
        return list(self.providers)

    def collect_default_chat_model(self) -> str:
        return str(self.model_pool_combo.model_ref() or "").strip()
