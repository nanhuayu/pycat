"""Embedded provider connection and curated-model editor."""
from __future__ import annotations

import json
from collections.abc import Callable

from PyQt6.QtCore import Qt, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.core.app.services.provider import ProviderService
from pycat.core.app.services.provider_catalog import ProviderCatalogService
from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.settings.components import (
    RESOURCE_TRAILING_ICONS_ROLE,
    SettingsActionBar,
    SettingsStatusListItem,
    configure_settings_resource_list,
)
from pycat.gui.settings.model_catalog_dialog import ModelCatalogDialog
from pycat.gui.settings.model_profile_dialog import ModelProfileDialog
from pycat.gui.utils.combo_box import configure_combo_popup
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.settings_controls import SettingsFormLayout
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit, ThemedTextEdit
from pycat.models.image_api import IMAGE_PROTOCOLS, ImageAPI
from pycat.models.model_profile import ModelProfile
from pycat.models.provider import (
    ACCOUNT_AUTH_LABELS,
    ANTHROPIC_NATIVE,
    OLLAMA_CHAT,
    OPENAI_COMPATIBLE,
    OPENAI_RESPONSES,
    WORKBUDDY_ORIGIN,
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
        self._login_flows = []
        self._login_job = None
        flows = self._login_flows

        def abandon_jobs(_object=None, jobs=self._jobs) -> None:
            for flow in tuple(flows):
                flow.cancel()
            flows.clear()
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
        form = SettingsFormLayout(stacked_labels=True)
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
        self.auth_type_combo = QComboBox()
        self.auth_type_combo.addItem('API Key', 'api_key')
        self.auth_type_combo.addItem('ChatGPT / Codex 登录', 'chatgpt')
        self.auth_type_combo.addItem('WorkBuddy / CodeBuddy（国内 · 实验性）', 'workbuddy')
        configure_combo_popup(self.auth_type_combo)

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
        self._key_widget = key_widget

        self.headers_edit = ThemedTextEdit()
        self.headers_edit.setAcceptRichText(False)
        self.headers_edit.setPlaceholderText('{"Header": "value"}')
        self.headers_edit.setMinimumHeight(96)
        self.headers_edit.setMaximumHeight(120)

        self._name_label = QLabel("名称")
        self._auth_type_label = QLabel("登录方式")
        form.addRow(self._name_label, self.name_input)
        form.addRow(self._auth_type_label, self.auth_type_combo)
        self._api_type_label = QLabel('聊天接口类型')
        self._api_base_label = QLabel('API 地址')
        self._key_label = QLabel('API Key')
        form.addRow(self._key_label, key_widget)
        layout.addLayout(form)

        self.auth_row = QWidget()
        auth_layout = QHBoxLayout(self.auth_row)
        auth_layout.setContentsMargins(0, 0, 0, 0)
        self.login_btn = QPushButton('登录 ChatGPT')
        self.logout_btn = QPushButton('退出登录')
        self.cancel_login_btn = QPushButton('取消')
        self.sync_models_btn = QPushButton('同步模型')
        self.sync_models_btn.setIcon(Icons.get(Icons.REFRESH))
        self.auth_status = QLabel('未登录')
        self.auth_status.setObjectName('provider_status_label')
        self.auth_status.setTextFormat(Qt.TextFormat.PlainText)
        self.auth_status.setWordWrap(True)
        auth_layout.addWidget(self.auth_status, 1)
        for button in (self.login_btn, self.sync_models_btn, self.logout_btn, self.cancel_login_btn):
            button.setObjectName('settings_action_btn')
            auth_layout.addWidget(button)
        layout.addWidget(self.auth_row)
        self.login_btn.clicked.connect(self._login_account)
        self.logout_btn.clicked.connect(self._logout_account)
        self.cancel_login_btn.clicked.connect(self._cancel_login)
        self.sync_models_btn.clicked.connect(self.open_model_catalog)
        self.auth_type_combo.currentIndexChanged.connect(self._auth_type_changed)
        self.auth_row.hide()

        self.account_note = QLabel()
        self.account_note.setWordWrap(True)
        self.account_note.setProperty("muted", True)
        layout.addWidget(self.account_note)

        self.chat_connection = QWidget()
        chat_layout = QVBoxLayout(self.chat_connection)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_form = SettingsFormLayout(stacked_labels=True)
        chat_form.addRow(self._api_type_label, self.api_type_combo)
        chat_form.addRow(self._api_base_label, self.api_base_input)
        chat_layout.addLayout(chat_form)
        layout.addWidget(self.chat_connection)
        self.image_connection_toggle = QToolButton()
        self.image_connection_toggle.setObjectName("collapse_toggle")
        self.image_connection_toggle.setText("图像接口")
        self.image_connection_toggle.setCheckable(True)
        self.image_connection_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.image_connection_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.image_connection_toggle.toggled.connect(self._toggle_image_connection)
        layout.addWidget(self.image_connection_toggle)
        self.image_connection = self._build_image_connection()
        self.image_connection.hide()
        layout.addWidget(self.image_connection)

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
        self.test_btn.setToolTip("检查服务连接与模型目录，不发起图像生成。")
        self.test_btn.setObjectName("settings_action_btn")
        self.test_btn.setIcon(Icons.get(Icons.CHECK))
        self.test_btn.clicked.connect(self.test_connection)
        actions.addWidget(self.test_btn, 0, Qt.AlignmentFlag.AlignLeft)
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

    def _build_image_connection(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        form = SettingsFormLayout(stacked_labels=True)
        self.image_protocol_combo = QComboBox()
        for value, label in IMAGE_PROTOCOLS.items():
            self.image_protocol_combo.addItem(label, value)
        configure_combo_popup(self.image_protocol_combo)
        form.addRow("图像接口类型", self.image_protocol_combo)
        self.image_generation_input = ThemedLineEdit()
        self.image_edit_input = ThemedLineEdit()
        self.image_generation_preview = QLabel()
        self.image_edit_preview = QLabel()
        for label, control, preview in (
            ("图像生成地址", self.image_generation_input, self.image_generation_preview),
            ("图像编辑地址", self.image_edit_input, self.image_edit_preview),
        ):
            control.setPlaceholderText("留空跟随聊天 API 地址；可填基础地址或完整请求地址")
            control.setToolTip("生成和编辑分别配置。填写完整 HTTP(S) 地址，不要只填写 /v1/images/...。")
            preview.setWordWrap(True)
            preview.setTextFormat(Qt.TextFormat.PlainText)
            preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            preview.setProperty("muted", True)
            form.addRow(label, control)
            form.addRow(preview, info=True)
            control.textChanged.connect(self._refresh_image_endpoints)
        layout.addLayout(form)
        self.image_connection_note = QLabel()
        self.image_connection_note.setWordWrap(True)
        self.image_connection_note.setProperty("muted", True)
        layout.addWidget(self.image_connection_note)
        self.image_protocol_combo.currentIndexChanged.connect(self._refresh_image_endpoints)
        self.api_base_input.textChanged.connect(self._refresh_image_endpoints)
        return page

    def _image_api_draft(self) -> ImageAPI:
        return ImageAPI(protocol=str(self.image_protocol_combo.currentData() or 'openai_images'),
                        generation_url=self.image_generation_input.text(), edit_url=self.image_edit_input.text())

    def _toggle_image_connection(self, expanded: bool) -> None:
        self.image_connection_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.image_connection.setVisible(expanded and self.auth_type_combo.currentData() == 'api_key')

    def _refresh_image_endpoints(self, *_args) -> None:
        if self.auth_type_combo.currentData() != 'api_key':
            return
        api = self._image_api_draft()
        for edit, label in ((False, self.image_generation_preview), (True, self.image_edit_preview)):
            try:
                label.setText(api.endpoint(self.api_base_input.text(), edit=edit))
            except ValueError as exc:
                label.setText(str(exc))
        self.image_connection_note.setText("生成与编辑分别沿用上方 API 地址，也可设置独立地址。共用本服务 API Key。")

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
        self._cancel_login()
        self._provider = None
        self._active_model_id = ""
        self.clear_status()
        self.model_list.clear()
        self.setEnabled(False)

    def load_provider(self, provider: Provider) -> None:
        self._cancel_login()
        self._provider = Provider.from_dict(provider.to_dict())
        self.setEnabled(True)
        self.clear_status()
        self.name_input.setText(self._provider.name)
        self.name_input.setReadOnly(self._provider.is_builtin_account)
        self.auth_type_combo.setEnabled(not self._provider.is_builtin_account)
        index = self.api_type_combo.findData(self._provider.api_type)
        self.api_type_combo.setCurrentIndex(index if index >= 0 else 0)
        self.api_base_input.setText(self._provider.api_base)
        self.api_key_input.setText(self._provider.api_key)
        index = self.image_protocol_combo.findData(self._provider.image_api.protocol)
        self.image_protocol_combo.setCurrentIndex(max(0, index))
        self.image_generation_input.setText(self._provider.image_api.generation_url)
        self.image_edit_input.setText(self._provider.image_api.edit_url)
        self.auth_type_combo.blockSignals(True)
        self.auth_type_combo.setCurrentIndex(self.auth_type_combo.findData(self._provider.auth_type))
        self.auth_type_combo.blockSignals(False)
        self.image_connection_toggle.setChecked(bool(
            self._provider.auth_type == 'api_key' and (self._provider.image_api.generation_url
            or self._provider.image_api.edit_url or self._provider.image_api.protocol != 'openai_images'
            or any(model.model_type == 'image' for model in self._provider.models))))
        self._sync_auth_fields()
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
        provider.auth_type = str(self.auth_type_combo.currentData() or 'api_key')
        provider.image_api = self._image_api_draft()
        provider.custom_headers = self._json_object(self.headers_edit, "自定义请求头")
        provider.normalize_inplace()
        if validate_connection:
            valid, message = self._provider_service.validate_provider(provider)
            if provider.enabled and not valid:
                raise ValueError(message)
        self._provider = Provider.from_dict(provider.to_dict())
        return provider

    def _auth_type_changed(self):
        self._cancel_login()
        if self.auth_type_combo.currentData() == 'api_key' and self.api_base_input.text() in {'https://chatgpt.com/backend-api/codex', WORKBUDDY_ORIGIN + '/v2'}:
            self.api_base_input.setText('https://api.openai.com/v1')
        self._sync_auth_fields()

    def _sync_auth_fields(self):
        kind = self.auth_type_combo.currentData()
        account_login = kind in ACCOUNT_AUTH_LABELS
        fixed = bool(self._provider and self._provider.is_builtin_account)
        for widget in (self.name_input, self._name_label, self.auth_type_combo, self._auth_type_label):
            widget.setVisible(not fixed)
        for widget in (self._key_widget, self._key_label, self.chat_connection,
                       self.image_connection_toggle, self.headers_toggle):
            widget.setVisible(not account_login)
        self.auth_row.setVisible(account_login)
        self.api_type_combo.setEnabled(not account_login)
        self.api_base_input.setReadOnly(account_login)
        self._toggle_image_connection(self.image_connection_toggle.isChecked())
        self.headers_edit.setVisible(not account_login and self.headers_toggle.isChecked())
        self.account_note.setVisible(account_login)
        self.account_note.setText("聊天与图像能力共用此账号，无需配置接口。图像权限和额度由账号决定。"
                                if kind == 'chatgpt' else "国内账号 · 实验性。模型与可用额度由账号决定。")
        self.test_btn.setText("检查账号" if account_login else "测试连接")
        self.login_btn.setText('登录 WorkBuddy' if kind == 'workbuddy' else '登录 ChatGPT')
        self.auth_row.setToolTip('国内账号 · 实验性。使用浏览器授权所选身份；切换账号请退出后重新登录。' if kind == 'workbuddy' else '')
        connected = False
        label = '未登录'
        auth = self._provider_service.account_auth(str(self.auth_type_combo.currentData()))
        if auth is not None and self._provider is not None:
            try:
                state = auth.status(self._provider.id)
                connected = state['connected']
                label = ('已登录 · ' + str(state.get('label') or state.get('email') or '')).rstrip(' ·') if connected else '未登录'
            except RuntimeError as exc:
                label = str(exc)
        pending = bool(self._login_flows)
        self.login_btn.setVisible(not connected)
        self.login_btn.setEnabled(auth is not None and not pending)
        self.logout_btn.setVisible(connected)
        self.logout_btn.setEnabled(not pending)
        self.sync_models_btn.setEnabled(connected and not pending)
        self.cancel_login_btn.setVisible(pending)
        self.auth_status.setText(('在浏览器中完成登录…' if self._login_flows[0].authorization_url else '正在准备登录…') if pending else label)
        self.auth_status.setProperty('state', 'success' if connected else 'muted')
        self.auth_status.style().unpolish(self.auth_status)
        self.auth_status.style().polish(self.auth_status)
        self._refresh_image_endpoints()

    def _login_account(self):
        auth = self._provider_service.account_auth(str(self.auth_type_combo.currentData()))
        if auth is None or self._provider is None or self._login_flows:
            return
        try:
            flow = auth.begin_login(self._provider.id)
        except RuntimeError as exc:
            self.show_status(str(exc), state='error')
            return
        self._login_flows.append(flow)
        self._sync_auth_fields()
        self._start_login_step(auth, flow, prepare=True)

    def _start_login_step(self, auth, flow, *, prepare):
        operation = (lambda: auth.prepare_login(flow)) if prepare else (lambda: auth.finish_login(flow))
        job = BackgroundJob(operation, on_discard=lambda *_: flow.cancel())
        self._login_job = job
        self._jobs.add(job)

        def finished(result, error):
            self._jobs.discard(job)
            if flow not in self._login_flows:
                flow.cancel()
                return
            self._login_job = None
            if not error and prepare:
                if QDesktopServices.openUrl(QUrl(flow.authorization_url)):
                    self._sync_auth_fields()
                    self._start_login_step(auth, flow, prepare=False)
                    return
                error = RuntimeError('无法打开默认浏览器，请检查系统设置后重试。')
            if error:
                flow.cancel()
            self._login_flows.remove(flow)
            self._sync_auth_fields()
            self.show_status(str(error) if error else '已登录。同步可用模型后保存设置，即可在对话中选择。', state='error' if error else 'success')

        job.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(job)

    def _cancel_login(self, *, refresh_ui=True):
        if not self._login_flows and self._login_job is None:
            return
        for flow in self._login_flows:
            flow.cancel()
        self._login_flows.clear()
        if self._login_job is not None:
            self._login_job.abandon()
            self._jobs.discard(self._login_job)
            self._login_job = None
        if refresh_ui:
            self._sync_auth_fields()

    def _logout_account(self):
        self._cancel_login()
        auth = self._provider_service.account_auth(str(self.auth_type_combo.currentData()))
        if self._provider is not None and auth is not None:
            auth.logout(self._provider.id)
        self._sync_auth_fields()

    def hideEvent(self, event):
        if not event.spontaneous():
            # Hide can be delivered during native widget destruction; cancel
            # the owned work without traversing or changing child widgets.
            self._cancel_login(refresh_ui=False)
        super().hideEvent(event)

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
        try:
            self.build_provider(validate_connection=False)
        except ValueError as exc:
            self.show_status(str(exc), state="error")
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
        try:
            self.build_provider(validate_connection=False)
        except ValueError as exc:
            self.show_status(str(exc), state="error")
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
