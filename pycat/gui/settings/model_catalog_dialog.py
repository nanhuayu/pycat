"""Searchable remote model catalog for curating one provider's model list."""
from __future__ import annotations

from PyQt6.QtCore import QCoreApplication, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QToolButton,
    QVBoxLayout,
)

from pycat.gui.settings.components import build_dialog_button_box
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit
from pycat.models.model_profile import BUNDLED_MODEL_TAG, ModelProfile
from pycat.models.provider import Provider


class ModelCatalogDialog(QDialog):
    """Stage additions and removals without mutating the provider draft."""

    refresh_requested = pyqtSignal()

    def __init__(self, provider: Provider, parent=None) -> None:
        super().__init__(parent)
        self._provider = Provider.from_dict(provider.to_dict())
        self._profiles = {profile.model_id: profile for profile in self._provider.get_models()}
        self._catalog_order = [profile.model_id for profile in self._provider.models]
        self._selected_ids = set(self._catalog_order)
        self._remote_profiles: dict[str, ModelProfile] = {}
        self._remote_ids: list[str] = []
        self._status_message = ""
        self._setup_ui()
        self._rebuild_list()

    def _setup_ui(self) -> None:
        self.setWindowTitle(QCoreApplication.translate('ModelCatalogDialog', '{name} 模型目录').format(name=self._provider.name))
        self.setObjectName("model_catalog_dialog")
        self.setMinimumSize(560, 500)
        self.resize(640, 580)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self.search_input = ThemedLineEdit()
        self.search_input.setPlaceholderText(QCoreApplication.translate('ModelCatalogDialog', '搜索模型 ID 或名称'))
        self.search_input.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.search_input, 1)
        self.refresh_btn = QToolButton()
        self.refresh_btn.setObjectName("toolbar_btn")
        self.refresh_btn.setIcon(Icons.get(Icons.REFRESH))
        self.refresh_btn.setToolTip(QCoreApplication.translate('ModelCatalogDialog', '重新获取远端模型'))
        self.refresh_btn.clicked.connect(self.refresh_requested)
        search_row.addWidget(self.refresh_btn)
        root.addLayout(search_row)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("model_catalog_list")
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.list_widget, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setProperty("muted", True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        buttons = build_dialog_button_box(self, accept_text=QCoreApplication.translate('ModelCatalogDialog', '应用选择'), accept_button=QDialogButtonBox.StandardButton.Ok)
        self.apply_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        root.addLayout(footer)

    def set_loading(self, loading: bool) -> None:
        self.refresh_btn.setEnabled(not loading)
        if loading:
            self._status_message = QCoreApplication.translate('ModelCatalogDialog', '正在获取远端模型...')
        self._update_count()

    def set_remote_models(self, profiles: list[ModelProfile]) -> None:
        self._remote_profiles = {
            profile.model_id: ModelProfile.from_dict(profile.to_dict())
            for profile in profiles or []
            if isinstance(profile, ModelProfile) and profile.model_id
        }
        self._remote_ids = sorted(self._remote_profiles)
        for model_id, profile in self._remote_profiles.items():
            existing = self._profiles.get(model_id)
            if existing is None or BUNDLED_MODEL_TAG in existing.tags:
                self._profiles[model_id] = profile
        self._status_message = QCoreApplication.translate('ModelCatalogDialog', '未找到远端模型') if not self._remote_ids else ""
        self._rebuild_list()

    def set_error(self, message: str) -> None:
        self._status_message = QCoreApplication.translate('ModelCatalogDialog', '获取失败：{value}').format(value=str(message or QCoreApplication.translate('ModelCatalogDialog', '未知错误')))
        self._update_count()

    def _checked_ids(self) -> set[str]:
        return {
            str(self.list_widget.item(row).data(Qt.ItemDataRole.UserRole) or "")
            for row in range(self.list_widget.count())
            if self.list_widget.item(row).checkState() == Qt.CheckState.Checked
        }

    def _rebuild_list(self) -> None:
        current_id = ""
        current = self.list_widget.currentItem()
        if current is not None:
            current_id = str(current.data(Qt.ItemDataRole.UserRole) or "")
        catalog_ids = self._catalog_order + [
            model_id for model_id in self._remote_ids if model_id not in self._catalog_order
        ]

        self.list_widget.blockSignals(True)
        try:
            self.list_widget.clear()
            for model_id in catalog_ids:
                profile = self._profiles.get(model_id)
                label = str(getattr(profile, "display_name", "") or model_id)
                if profile is not None and label != model_id:
                    label = f"{label}  ·  {model_id}"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, model_id)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if model_id in self._selected_ids else Qt.CheckState.Unchecked
                )
                source = QCoreApplication.translate('ModelCatalogDialog', '远端与本地') if model_id in self._remote_ids and model_id in self._catalog_order else QCoreApplication.translate('ModelCatalogDialog', '远端模型') if model_id in self._remote_ids else QCoreApplication.translate('ModelCatalogDialog', '本地模型')
                details = [model_id, source]
                if profile is not None and profile.context_window:
                    details.append(QCoreApplication.translate('ModelCatalogDialog', '上下文 {context_window:,}').format(context_window=profile.context_window))
                if profile is not None and profile.reasoning_codec != "none":
                    details.append(QCoreApplication.translate('ModelCatalogDialog', '推理 {reasoning_codec}').format(reasoning_codec=profile.reasoning_codec))
                item.setToolTip("\n".join(details))
                self.list_widget.addItem(item)
                if model_id == current_id:
                    self.list_widget.setCurrentItem(item)
        finally:
            self.list_widget.blockSignals(False)
        self._apply_filter(self.search_input.text())
        self._update_count()

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        model_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if item.checkState() == Qt.CheckState.Checked:
            self._selected_ids.add(model_id)
        else:
            self._selected_ids.discard(model_id)
        self._update_count()

    def _apply_filter(self, text: str) -> None:
        query = str(text or "").strip().lower()
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            haystack = f"{item.text()} {item.data(Qt.ItemDataRole.UserRole) or ''}".lower()
            item.setHidden(bool(query and query not in haystack))

    def _update_count(self) -> None:
        selected = len(self._checked_ids())
        total = self.list_widget.count()
        self.status_label.setText(self._status_message or QCoreApplication.translate('ModelCatalogDialog', '已选 {selected} / {total}').format(selected=selected, total=total))

    def selected_profiles(self) -> list[ModelProfile]:
        selected = self._checked_ids()
        profiles: list[ModelProfile] = []
        for row in range(self.list_widget.count()):
            model_id = str(self.list_widget.item(row).data(Qt.ItemDataRole.UserRole) or "")
            if model_id not in selected:
                continue
            profile = self._profiles.get(model_id) or self._provider.effective_model_profile(model_id)
            profiles.append(ModelProfile.from_dict(profile.to_dict()))
        return profiles
