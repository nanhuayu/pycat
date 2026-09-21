"""Provider catalog and application model defaults."""
from __future__ import annotations

from typing import List

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QMessageBox, QVBoxLayout, QWidget

from pycat.core.app.services.provider import ProviderService
from pycat.core.app.services.provider_catalog import ProviderCatalogService
from pycat.gui.settings.components import (
    SettingsActionBar,
    SettingsListDetailLayout,
    SettingsStatusListItem,
    configure_settings_resource_list,
)
from pycat.gui.settings.page_header import build_page_header
from pycat.gui.settings.provider_editor import ProviderEditor
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.widgets.model_ref_selector import ModelRefCombo
from pycat.models.provider import Provider, api_type_label


class ProviderListItem(SettingsStatusListItem):
    def __init__(self, provider: Provider) -> None:
        super().__init__(provider)
        self.provider = provider
        self.update_display()

    def update_display(self) -> None:
        enabled = bool(self.provider.enabled)
        self.set_status(
            self.provider.account_label if self.provider.is_builtin_account else self.provider.name,
            enabled=enabled,
            tooltip=(
                f"{self.provider.name}\n"
                f"状态：{'启用' if enabled else '停用'}\n"
                f"接口：{api_type_label(self.provider.api_type)}\n"
                f"API：{self.provider.api_base or '-'}"
            ),
        )


class ModelsPage(QWidget):
    providers_changed = pyqtSignal()
    page_title = "模型"

    def __init__(
        self,
        providers: List[Provider],
        default_chat_model: str = "",
        default_auxiliary_model: str = "",
        provider_service: ProviderService | None = None,
        provider_catalog_service: ProviderCatalogService | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        if provider_catalog_service is None:
            raise ValueError("provider_catalog_service is required")
        self.provider_service = provider_service or ProviderService()
        self.provider_catalog_service = provider_catalog_service
        self.providers = self.provider_catalog_service.snapshot(providers or [])
        self._default_chat_model = str(default_chat_model or "").strip()
        self._default_auxiliary_model = str(default_auxiliary_model or "").strip()
        self._active_provider_id = ""
        self._changing_selection = False
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)
        root.addWidget(build_page_header("模型与服务", "管理默认模型、服务商连接和单模型能力参数。"))

        route_bar = QWidget()
        route_bar.setObjectName("model_route_bar")
        route_layout = QHBoxLayout(route_bar)
        route_layout.setContentsMargins(0, 0, 0, 0)
        route_layout.setSpacing(8)
        self.model_pool_combo = ModelRefCombo(
            self.providers,
            current_model_ref=self._default_chat_model,
            allow_empty=False,
            empty_label="选择模型",
        )
        self.auxiliary_model_combo = ModelRefCombo(
            self.providers,
            current_model_ref=self._default_auxiliary_model,
            allow_empty=True,
            empty_label="跟随会话模型",
        )
        self.model_pool_combo.setMinimumWidth(180)
        self.auxiliary_model_combo.setMinimumWidth(180)
        self.model_pool_combo.setToolTip("新建桌面对话和频道会话默认使用该模型。")
        self.auxiliary_model_combo.setToolTip("能力和子 Agent 未指定模型时使用；留空则跟随当前会话主模型。")
        route_layout.addWidget(QLabel("默认模型"))
        route_layout.addWidget(self.model_pool_combo, 1)
        route_layout.addSpacing(8)
        route_layout.addWidget(QLabel("辅助模型"))
        route_layout.addWidget(self.auxiliary_model_combo, 1)
        root.addWidget(route_bar)

        split = SettingsListDetailLayout()
        provider_actions = SettingsActionBar(spacing=4)
        self.btn_add_provider = provider_actions.add_icon_action(
            "新增 API 服务商", Icons.get(Icons.PLUS), self._add_provider,
        )
        self.btn_toggle_enabled = provider_actions.add_icon_action(
            "停用服务商", Icons.get(Icons.PAUSE), self._toggle_provider_enabled,
        )
        provider_actions.add_stretch()
        self.btn_move_up = provider_actions.add_icon_action(
            "上移服务商", Icons.get(Icons.ARROW_UP), lambda: self._move_provider(-1),
        )
        self.btn_move_down = provider_actions.add_icon_action(
            "下移服务商", Icons.get(Icons.ARROW_DOWN), lambda: self._move_provider(1),
        )
        self.btn_delete_provider = provider_actions.add_icon_action(
            "删除服务商",
            Icons.get(Icons.XMARK, color=Icons.COLOR_ERROR),
            self._delete_provider,
            danger=True,
        )
        split.list_layout.addWidget(provider_actions)
        self.provider_list = configure_settings_resource_list(QListWidget())
        self.provider_list.currentItemChanged.connect(self._on_provider_selected)
        split.list_layout.addWidget(self.provider_list, 1)
        split.bind(self.provider_list)
        self.provider_editor = ProviderEditor(
            self.provider_service,
            self.provider_catalog_service,
        )
        self.provider_editor.model_catalog_changed.connect(self._on_model_catalog_changed)
        split.add_detail_widget(self.provider_editor)
        root.addWidget(split, 1)

        self._refresh_provider_list()

    def _current_item(self) -> ProviderListItem | None:
        item = self.provider_list.currentItem()
        return item if isinstance(item, ProviderListItem) else None

    def _commit_active_editor(self) -> Provider | None:
        return self._commit_active_editor_with_validation(validate_connection=True)

    def _commit_active_editor_with_validation(self, *, validate_connection: bool) -> Provider | None:
        if not self._active_provider_id or not self.provider_editor.isEnabled():
            return None
        updated = self.provider_editor.build_provider(validate_connection=validate_connection)
        self.providers = self.provider_catalog_service.upsert(self.providers, updated)
        for row in range(self.provider_list.count()):
            item = self.provider_list.item(row)
            if isinstance(item, ProviderListItem) and item.provider.id == updated.id:
                item.provider = updated
                item.update_display()
                break
        return updated

    def _try_commit_active_editor(self, *, validate_connection: bool = True) -> bool:
        try:
            self._commit_active_editor_with_validation(validate_connection=validate_connection)
            return True
        except ValueError as exc:
            self.provider_editor.show_status(str(exc), state="error")
            return False

    def _on_provider_selected(self, current, previous) -> None:
        if self._changing_selection:
            return
        if self._active_provider_id:
            if not self._try_commit_active_editor():
                self._changing_selection = True
                try:
                    self.provider_list.setCurrentItem(previous)
                finally:
                    self._changing_selection = False
                return
            self._refresh_model_options()
            self.providers_changed.emit()
        item = current if isinstance(current, ProviderListItem) else None
        if item is None:
            self._active_provider_id = ""
            self.provider_editor.clear()
        else:
            provider, _index = self.provider_catalog_service.find(self.providers, item.provider.id)
            self._active_provider_id = str(item.provider.id or "")
            self.provider_editor.load_provider(provider or item.provider)
        self._sync_actions()

    def _refresh_provider_list(self, preferred_id: str = "") -> None:
        target = str(preferred_id or self._active_provider_id or "").strip()
        self._changing_selection = True
        selected_row = -1
        try:
            self.provider_list.clear()
            for row, provider in enumerate(self.providers):
                self.provider_list.addItem(ProviderListItem(provider))
                if str(provider.id or "") == target:
                    selected_row = row
            if self.provider_list.count():
                self.provider_list.setCurrentRow(selected_row if selected_row >= 0 else 0)
        finally:
            self._changing_selection = False

        self._refresh_model_options()
        self._active_provider_id = ""
        self.provider_editor.clear()
        if self.provider_list.count():
            self._on_provider_selected(self.provider_list.currentItem(), None)
        else:
            self._sync_actions()

    def _refresh_model_options(self) -> None:
        primary = self.model_pool_combo.model_ref() if hasattr(self, "model_pool_combo") else self._default_chat_model
        auxiliary = self.auxiliary_model_combo.model_ref() if hasattr(self, "auxiliary_model_combo") else self._default_auxiliary_model
        self.model_pool_combo.set_providers(self.providers, current_model_ref=primary)
        self.auxiliary_model_combo.set_providers(self.providers, current_model_ref=auxiliary)

    def _on_model_catalog_changed(self, provider: Provider) -> None:
        self.providers = self.provider_catalog_service.upsert(self.providers, provider)
        for row in range(self.provider_list.count()):
            item = self.provider_list.item(row)
            if isinstance(item, ProviderListItem) and item.provider.id == provider.id:
                item.provider = provider
                item.update_display()
                break
        self._refresh_model_options()
        self.providers_changed.emit()

    def _sync_actions(self) -> None:
        item = self._current_item()
        has_item = item is not None
        fixed = bool(item and item.provider.is_builtin_account)
        row = self.provider_list.currentRow()
        count = self.provider_list.count()
        self.btn_toggle_enabled.setEnabled(has_item)
        self.btn_delete_provider.setEnabled(has_item and not fixed)
        self.btn_delete_provider.setToolTip('固定登录入口，可停用或退出登录' if fixed else '删除服务商')
        self.btn_move_up.setEnabled(has_item and not fixed and row > 0 and not self.provider_list.item(row - 1).provider.is_builtin_account)
        self.btn_move_down.setEnabled(has_item and not fixed and 0 <= row < count - 1 and not self.provider_list.item(row + 1).provider.is_builtin_account)
        enabled = bool(item.provider.enabled) if item else True
        action_label = "停用服务商" if enabled else "启用服务商"
        self.btn_toggle_enabled.setText(action_label)
        self.btn_toggle_enabled.setToolTip(action_label)
        self.btn_toggle_enabled.setAccessibleName(action_label)
        self.btn_toggle_enabled.setIcon(Icons.get(Icons.PAUSE if enabled else Icons.PLAY))

    def _add_provider(self) -> None:
        if not self._try_commit_active_editor(validate_connection=False):
            return
        names = {provider.name for provider in self.providers}
        provider = Provider(name="new-provider", enabled=True)
        name = provider.name
        index = 1
        while name in names:
            index += 1
            name = f"{provider.name}-{index}"
        provider.name = name
        self.providers = self.provider_catalog_service.upsert(self.providers, provider)
        self._active_provider_id = provider.id
        self._refresh_provider_list(provider.id)
        self.provider_editor.name_input.selectAll()
        self.provider_editor.name_input.setFocus()
        self.providers_changed.emit()

    def _toggle_provider_enabled(self) -> None:
        item = self._current_item()
        if item is None:
            return
        next_enabled = not bool(item.provider.enabled)
        try:
            updated = self.provider_editor.build_provider(enabled=next_enabled)
        except ValueError as exc:
            self.provider_editor.show_status(str(exc), state="error")
            return
        provider_id = item.provider.id
        self.providers = self.provider_catalog_service.upsert(self.providers, updated)
        self._refresh_provider_list(provider_id)
        self.providers_changed.emit()

    def _delete_provider(self) -> None:
        item = self._current_item()
        if item is None or item.provider.is_builtin_account:
            return
        if QMessageBox.question(self, "删除服务商", f'确定删除“{item.provider.name}”吗？') != QMessageBox.StandardButton.Yes:
            return
        self.providers = self.provider_catalog_service.remove(self.providers, item.provider.id)
        self._active_provider_id = ""
        self._refresh_provider_list()
        self.providers_changed.emit()

    def _move_provider(self, delta: int) -> None:
        item = self._current_item()
        if item is None:
            return
        if not self._try_commit_active_editor():
            return
        provider_id = item.provider.id
        self.providers = self.provider_catalog_service.move(self.providers, provider_id, delta)
        self._refresh_provider_list(provider_id)
        self.providers_changed.emit()

    def select_provider(self, provider_id: str) -> None:
        target = str(provider_id or "")
        for row in range(self.provider_list.count()):
            item = self.provider_list.item(row)
            if isinstance(item, ProviderListItem) and str(item.provider.id or "") == target:
                self.provider_list.setCurrentRow(row)
                break

    def get_providers(self) -> List[Provider]:
        self._commit_active_editor()
        return self.provider_catalog_service.snapshot(self.providers)

    def collect_default_chat_model(self) -> str:
        return str(self.model_pool_combo.model_ref() or "").strip()

    def collect_default_auxiliary_model(self) -> str:
        return str(self.auxiliary_model_combo.model_ref() or "").strip()
