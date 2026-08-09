"""Embedded provider connection and curated-model editor."""
from __future__ import annotations

import json
from collections.abc import Callable

from PyQt6.QtCore import QThreadPool, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QFrame,
    QScrollArea,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.app.services.provider import ProviderService
from core.app.services.provider_catalog import ProviderCatalogService
from gui.runtime.background_job import BackgroundJob
from gui.settings.components import (
    RESOURCE_TRAILING_ICONS_ROLE,
    SettingsActionBar,
    SettingsStatusListItem,
    configure_settings_resource_list,
)
from gui.settings.model_catalog_dialog import ModelCatalogDialog
from gui.settings.model_profile_dialog import ModelProfileDialog
from gui.utils.combo_box import configure_combo_popup
from gui.utils.icon_manager import Icons
from gui.widgets.themed_line_edit import ThemedLineEdit, ThemedTextEdit
from models.model_profile import ModelProfile
from models.provider import (
    ANTHROPIC_NATIVE,
    OPENAI_COMPATIBLE,
    OPENAI_RESPONSES,
    OLLAMA_CHAT,
    Provider,
)


class _ModelListItem(SettingsStatusListItem):
    def __init__(self, profile: ModelProfile) -> None:
        super().__init__(profile.model_id)
        self.model_id = profile.model_id
        self.update_display(profile)

    def update_display(self, profile: ModelProfile) -> None:
        title = str(profile.display_name or profile.model_id or "未命名模型")
        subtitle = profile.model_id if profile.display_name and profile.display_name != profile.model_id else ""
        abilities = [
            label
            for enabled, label in (
                (profile.supports_tools, "工具"),
                (profile.supports_reasoning, "推理"),
                (profile.supports_input("image"), "图片输入"),
                (profile.supports_input("audio"), "音频输入"),
            )
            if enabled
        ]
        self.set_status(
            title,
            enabled=True,
            detail=subtitle,
            tooltip=(
                f"{title}\n模型 ID：{profile.model_id}\n"
                f"能力：{' / '.join(abilities) or '文本'}\n"
                f"上下文窗口：{profile.context_window or '未设置'}\n"
                f"最大输出：{profile.max_output_tokens or '未设置'}"
            ),
        )
        ability_icons = []
        for enabled, icon_name, color in (
            (profile.supports_reasoning, Icons.THINKING, Icons.COLOR_PRIMARY),
            (profile.supports_tools, Icons.WRENCH, Icons.COLOR_WARNING),
            (profile.supports_input("image"), Icons.IMAGE, Icons.COLOR_SUCCESS),
            (
                profile.supports_input("audio"),
                Icons.AUDIO,
                Icons.COLOR_MUTED,
            ),
        ):
            if enabled:
                ability_icons.append(Icons.get(icon_name, color=color, scale_factor=0.85))
        self.setData(RESOURCE_TRAILING_ICONS_ROLE, ability_icons)
        self.setData(Qt.ItemDataRole.UserRole, profile.model_id)


class ProviderEditor(QWidget):
    """Edit one provider snapshot without owning persistence."""

    model_catalog_changed = pyqtSignal(object)

    def __init__(
        self,
        provider_service: ProviderService,
        provider_catalog_service: ProviderCatalogService | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._provider_service = provider_service
        self._provider_catalog_service = provider_catalog_service
        self._provider: Provider | None = None
        self._active_model_id = ""
        self._jobs: set[BackgroundJob] = set()

        def abandon_jobs(_object=None, jobs=self._jobs) -> None:
            for job in tuple(jobs):
                job.abandon()
            jobs.clear()

        self.destroyed.connect(abandon_jobs)
        self._setup_ui()
        self.setEnabled(False)

    @staticmethod
    def _api_type_options() -> list[tuple[str, str]]:
        return [
            ("OpenAI 兼容 / Chat Completions", OPENAI_COMPATIBLE),
            ("OpenAI Responses API", OPENAI_RESPONSES),
            ("Anthropic 原生 / Messages API", ANTHROPIC_NATIVE),
            ("Ollama 本地 / Chat API", OLLAMA_CHAT),
        ]

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.tabs = QTabWidget()
        self.connection_tab = self._build_connection_tab()
        self.tabs.addTab(self.connection_tab, "连接")
        self.tabs.addTab(self._build_model_tab(), "模型")
        root.addWidget(self.tabs, 1)

    def _build_connection_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("provider_connection_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)

        self.name_input = ThemedLineEdit()
        self.api_type_combo = QComboBox()
        configure_combo_popup(self.api_type_combo, popup_minimum_width=300)
        for label, value in self._api_type_options():
            self.api_type_combo.addItem(label, value)
        self.api_base_input = ThemedLineEdit()
        self.api_key_input = ThemedLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)

        key_widget = QWidget()
        key_layout = QHBoxLayout(key_widget)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        key_layout.addWidget(self.api_key_input, 1)
        self.show_key_btn = QPushButton()
        self.show_key_btn.setObjectName("settings_action_btn")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.setIcon(Icons.get(Icons.EYE))
        self.show_key_btn.setToolTip("显示 API Key")
        self.show_key_btn.setFixedWidth(32)
        self.show_key_btn.toggled.connect(self._toggle_key_visibility)
        key_layout.addWidget(self.show_key_btn)

        self.headers_edit = ThemedTextEdit()
        self.headers_edit.setAcceptRichText(False)
        self.headers_edit.setPlaceholderText('{"Header": "value"}')
        self.headers_edit.setMinimumHeight(96)
        self.headers_edit.setMaximumHeight(120)

        form.addRow("名称", self.name_input)
        form.addRow("接口类型", self.api_type_combo)
        form.addRow("API 地址", self.api_base_input)
        form.addRow("API Key", key_widget)
        layout.addLayout(form)

        self.headers_toggle = QToolButton()
        self.headers_toggle.setObjectName("collapse_toggle")
        self.headers_toggle.setText("自定义请求头")
        self.headers_toggle.setCheckable(True)
        self.headers_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.headers_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.headers_toggle.toggled.connect(self._toggle_headers)
        layout.addWidget(self.headers_toggle)
        self.headers_edit.setVisible(False)
        layout.addWidget(self.headers_edit)

        actions = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.setObjectName("settings_action_btn")
        self.test_btn.setIcon(Icons.get(Icons.CHECK))
        self.test_btn.clicked.connect(self.test_connection)
        actions.addWidget(self.test_btn)
        self.status_label = QLabel("")
        self.status_label.setObjectName("provider_status_label")
        self.status_label.setProperty("state", "muted")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        actions.addWidget(self.status_label, 1)
        layout.addLayout(actions)
        layout.addStretch(1)
        scroll.setWidget(tab)
        return scroll

    def _build_model_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        actions = SettingsActionBar()
        self.catalog_btn = actions.add_action("模型目录", Icons.get(Icons.REFRESH), self.open_model_catalog)
        self.add_model_btn = actions.add_action("自定义模型", Icons.get(Icons.PLUS), self._add_custom_model)
        self.edit_model_btn = actions.add_action("编辑", Icons.get(Icons.EDIT), self._edit_model)
        self.remove_model_btn = actions.add_action(
            "删除", Icons.get(Icons.XMARK, color=Icons.COLOR_ERROR), self._remove_model, danger=True,
        )
        actions.add_stretch()
        layout.addWidget(actions)

        self.model_list = configure_settings_resource_list(QListWidget())
        self.model_list.currentItemChanged.connect(self._on_model_selected)
        self.model_list.itemDoubleClicked.connect(lambda _item: self._edit_model())
        layout.addWidget(self.model_list, 1)
        return tab

    def clear(self) -> None:
        self._provider = None
        self._active_model_id = ""
        self.clear_status()
        self.model_list.clear()
        self.setEnabled(False)

    def load_provider(self, provider: Provider) -> None:
        self._provider = Provider.from_dict(provider.to_dict())
        self.setEnabled(True)
        self.clear_status()
        self.name_input.setText(self._provider.name)
        index = self.api_type_combo.findData(self._provider.api_type)
        self.api_type_combo.setCurrentIndex(index if index >= 0 else 0)
        self.api_base_input.setText(self._provider.api_base)
        self.api_key_input.setText(self._provider.api_key)
        self.headers_edit.setPlainText(self._json_text(self._provider.custom_headers))
        self.headers_toggle.setChecked(False)
        self._refresh_model_list()

    def build_provider(
        self,
        *,
        enabled: bool | None = None,
        validate_connection: bool = True,
    ) -> Provider:
        if self._provider is None:
            raise ValueError("未选择服务商")
        provider = Provider.from_dict(self._provider.to_dict())
        if enabled is not None:
            provider.enabled = bool(enabled)
        provider.name = self.name_input.text().strip()
        provider.api_type = str(self.api_type_combo.currentData() or OPENAI_COMPATIBLE)
        provider.api_base = self.api_base_input.text().strip()
        provider.api_key = self.api_key_input.text().strip()
        provider.custom_headers = self._json_object(self.headers_edit, "自定义请求头")
        provider.normalize_inplace()
        if validate_connection:
            valid, message = self._provider_service.validate_provider(provider)
            if provider.enabled and not valid:
                raise ValueError(message)
        self._provider = Provider.from_dict(provider.to_dict())
        return provider

    def show_status(self, message: str, *, state: str = "muted", reveal_connection: bool = True) -> None:
        text = str(message or "").strip()
        self.status_label.setText(text)
        self.status_label.setProperty("state", str(state or "muted"))
        self.status_label.setVisible(bool(text))
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        if text and reveal_connection:
            self.tabs.setCurrentWidget(self.connection_tab)

    def clear_status(self) -> None:
        self.show_status("", reveal_connection=False)

    @staticmethod
    def _json_text(value: dict) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2) if value else ""

    @staticmethod
    def _json_object(editor: QTextEdit, label: str) -> dict:
        text = editor.toPlainText().strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except Exception as exc:
            raise ValueError(f"{label} JSON 无效：{exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{label}必须是 JSON 对象")
        return value

    def _toggle_key_visibility(self, checked: bool) -> None:
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)
        self.show_key_btn.setIcon(Icons.get(Icons.EYE_SLASH if checked else Icons.EYE))

    def _toggle_headers(self, checked: bool) -> None:
        self.headers_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self.headers_edit.setVisible(bool(checked))

    def _refresh_model_list(self, *, preferred_id: str = "") -> None:
        if self._provider is None:
            self.model_list.clear()
            return
        target = str(preferred_id or self._active_model_id or "").strip()
        self.model_list.blockSignals(True)
        try:
            self.model_list.clear()
            for profile in self._provider.models:
                self.model_list.addItem(_ModelListItem(profile))
            row = next(
                (
                    index
                    for index in range(self.model_list.count())
                    if str(self.model_list.item(index).data(Qt.ItemDataRole.UserRole) or "") == target
                ),
                0 if self.model_list.count() else -1,
            )
            self.model_list.setCurrentRow(row)
        finally:
            self.model_list.blockSignals(False)
        item = self.model_list.currentItem()
        self._active_model_id = str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""
        self._sync_model_actions()

    def _on_model_selected(self, current, _previous) -> None:
        self._active_model_id = str(current.data(Qt.ItemDataRole.UserRole) or "") if current is not None else ""
        self._sync_model_actions()

    def _add_custom_model(self) -> None:
        if self._provider is None:
            return
        dialog = ModelProfileDialog(self._provider, parent=self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        profile = dialog.accepted_profile()
        if self._provider.find_model_profile(profile.model_id) is not None:
            QMessageBox.warning(self, "模型已存在", f'“{profile.model_id}”已在常用模型目录中。')
            return
        self._provider.upsert_model(profile)
        self._refresh_model_list(preferred_id=profile.model_id)
        self._emit_model_catalog_changed()

    def _edit_model(self) -> None:
        if self._provider is None or not self._active_model_id:
            return
        profile = self._provider.find_model_profile(self._active_model_id)
        if profile is None:
            return
        dialog = ModelProfileDialog(
            self._provider,
            model_id=self._active_model_id,
            profile=profile,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        self._provider.upsert_model(dialog.accepted_profile())
        self._refresh_model_list(preferred_id=self._active_model_id)
        self._emit_model_catalog_changed()

    def _remove_model(self) -> None:
        if self._provider is None or not self._active_model_id:
            return
        model_id = self._active_model_id
        if QMessageBox.question(self, "删除模型", f'确定从常用目录删除“{model_id}”吗？') != QMessageBox.StandardButton.Yes:
            return
        self._provider.remove_model(model_id)
        self._active_model_id = ""
        self._refresh_model_list()
        self._emit_model_catalog_changed()

    def _emit_model_catalog_changed(self) -> None:
        if self._provider is not None:
            self.model_catalog_changed.emit(Provider.from_dict(self._provider.to_dict()))

    def _sync_model_actions(self) -> None:
        has_model = bool(self._active_model_id)
        self.edit_model_btn.setEnabled(has_model)
        self.remove_model_btn.setEnabled(has_model)

    def _start_job(self, operation: Callable[[], object], callback: Callable[[object, object], None]) -> None:
        job = BackgroundJob(operation)
        self._jobs.add(job)

        def finished(result, error) -> None:
            self._jobs.discard(job)
            callback(result, error)

        job.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(job)

    def test_connection(self) -> None:
        try:
            provider = self.build_provider()
        except ValueError as exc:
            self.show_status(str(exc), state="error")
            return
        self.test_btn.setEnabled(False)
        self.show_status("正在测试连接...", state="muted")

        async def operation():
            return await self._provider_service.test_connection(provider)

        def done(result, error) -> None:
            self.test_btn.setEnabled(True)
            if error is not None:
                self.show_status(f"连接失败：{error}", state="error")
            else:
                success, message = result
                self.show_status(
                    "连接成功" if success else f"连接失败：{message}",
                    state="success" if success else "error",
                )

        self._start_job(operation, done)

    def open_model_catalog(self) -> None:
        try:
            provider = self.build_provider(validate_connection=False)
        except ValueError as exc:
            self.show_status(str(exc), state="error")
            return

        dialog = ModelCatalogDialog(provider, parent=self)
        dialog.refresh_requested.connect(lambda: self._refresh_remote_catalog(dialog))
        QTimer.singleShot(0, dialog.refresh_requested.emit)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        self._apply_model_profiles(dialog.selected_profiles())

    def _apply_model_profiles(self, profiles: list[ModelProfile]) -> None:
        if self._provider is None:
            return
        self._provider.models = [ModelProfile.from_dict(profile.to_dict()) for profile in profiles]
        self._refresh_model_list()
        self._emit_model_catalog_changed()

    def _refresh_remote_catalog(self, dialog: ModelCatalogDialog) -> None:
        if self._provider is None:
            return
        try:
            provider = self.build_provider(validate_connection=True)
        except ValueError as exc:
            dialog.set_error(str(exc))
            return
        dialog.set_loading(True)

        async def operation():
            profiles = await self._provider_service.fetch_models(provider)
            if self._provider_catalog_service is not None:
                profiles = self._provider_catalog_service.enrich_discovered_models(provider, profiles)
            return profiles

        def done(result, error) -> None:
            dialog.set_loading(False)
            if error is not None:
                dialog.set_error(str(error))
                return
            dialog.set_remote_models(list(result or []))

        self._start_job(operation, done)
