"""Direct editor for user-wide mode and subagent profiles."""
from __future__ import annotations

from dataclasses import replace
import logging
from typing import Iterable

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QSpinBox,
    QTabBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.app.services.mode_catalog import ModeCatalogService
from core.config import get_user_modes_json_path
from core.modes.defaults import get_required_mode_slugs
from gui.settings.components import (
    SettingsActionBar,
    SettingsListDetailLayout,
    SettingsStatusListItem,
    configure_settings_resource_list,
)
from gui.widgets.tool_category_selector import ToolCategorySelector
from gui.settings.page_header import build_page_header
from gui.utils.combo_box import configure_combo_popup
from gui.utils.icon_manager import Icons
from gui.widgets.model_ref_selector import ModelTargetCombo
from gui.widgets.themed_line_edit import ThemedLineEdit, ThemedTextEdit
from models.contracts.mode import ModeConfig
from models.contracts.model_target import ModelTarget
from models.provider import Provider


logger = logging.getLogger(__name__)


class ModesPage(QWidget):
    page_title = "模式"

    def __init__(
        self,
        _work_dir_unused: str | None = None,
        *,
        providers: Iterable[Provider] | None = None,
        mode_catalog: ModeCatalogService | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._providers = list(providers or ())
        self._catalog = mode_catalog or ModeCatalogService()
        self._modes: list[ModeConfig] = []
        self._current_slug = ""
        self._loading = False
        self._setup_ui()
        self.reload_from_disk()

    def set_providers(self, providers: Iterable[Provider]) -> None:
        self._providers = list(providers or ())
        self.model_target_combo.set_providers(self._providers)

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)
        root.addWidget(build_page_header("模式", "配置主模式与可委托的子 Agent profile。"))

        self.view_tabs = QTabBar()
        self.view_tabs.addTab("主模式")
        self.view_tabs.addTab("子 Agent")
        self.view_tabs.currentChanged.connect(self._on_view_changed)
        root.addWidget(self.view_tabs)

        body = SettingsListDetailLayout(list_stretch=2, detail_stretch=5)
        actions = SettingsActionBar(spacing=4)
        actions.add_icon_action("新增模式", Icons.get(Icons.PLUS), self._add_mode)
        self.mode_delete_btn = actions.add_icon_action(
            "删除模式",
            Icons.get(Icons.XMARK, color=Icons.COLOR_ERROR),
            self._delete_mode,
            danger=True,
        )
        actions.add_icon_action("重新读取模式", Icons.get(Icons.REFRESH), self.reload_from_disk)
        actions.add_icon_action("打开模式配置", Icons.get(Icons.FOLDER), self._open_config_dir)
        actions.add_stretch()
        body.list_layout.addWidget(actions)

        self.mode_list = configure_settings_resource_list(QListWidget())
        self.mode_list.currentRowChanged.connect(self._on_mode_selected)
        body.list_layout.addWidget(self.mode_list, 1)

        self.editor = QWidget()
        form = QFormLayout(self.editor)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)

        self.slug_edit = ThemedLineEdit()
        self.slug_edit.setReadOnly(True)
        self.name_edit = ThemedLineEdit()
        self.purpose_edit = ThemedLineEdit()
        self.tool_category_selector = ToolCategorySelector(
            columns=4,
            object_prefix="mode_tool_category",
            allow_inherit=False,
        )
        self.tool_categories_widget = self.tool_category_selector
        self.tool_category_checks = self.tool_category_selector.checks
        self.profile_kind_combo = QComboBox()
        self.profile_kind_combo.addItem("主模式", "primary")
        self.profile_kind_combo.addItem("委托专用子 Agent", "subagent")
        self.profile_kind_combo.addItem("主模式 + 子 Agent", "both")
        configure_combo_popup(self.profile_kind_combo)
        self.completion_policy_combo = QComboBox()
        self.completion_policy_combo.addItem("普通文本完成", "text")
        self.completion_policy_combo.addItem("必须显式调用 agent__complete", "explicit")
        configure_combo_popup(self.completion_policy_combo)
        self.model_target_combo = ModelTargetCombo(
            self._providers,
            current_target=ModelTarget(),
        )
        self.max_turns_spin = QSpinBox()
        self.max_turns_spin.setRange(0, 1000)
        self.max_turns_spin.setSpecialValueText("继承全局")
        self.shared_context_combo = QComboBox()
        self.shared_context_combo.addItem("仅共享索引", "indexes_only")
        self.shared_context_combo.addItem("共享选定产物", "selected_artifacts")
        self.shared_context_combo.addItem("只读完整会话", "full_session_readonly")
        configure_combo_popup(self.shared_context_combo)
        self.prompt_edit = ThemedTextEdit()
        self.prompt_edit.setAcceptRichText(False)
        self.prompt_edit.setMinimumHeight(130)

        form.addRow("标识", self.slug_edit)
        form.addRow("名称", self.name_edit)
        form.addRow("用途", self.purpose_edit)
        form.addRow("工具类别", self.tool_categories_widget)
        form.addRow("完成策略", self.completion_policy_combo)
        self.model_target_label = QLabel("委托模型")
        self.max_turns_label = QLabel("最大轮次")
        self.shared_context_label = QLabel("共享上下文")
        form.addRow(self.model_target_label, self.model_target_combo)
        form.addRow(self.max_turns_label, self.max_turns_spin)
        form.addRow(self.shared_context_label, self.shared_context_combo)
        form.addRow("指令", self.prompt_edit)
        body.add_detail_widget(self.editor, scrollable=True)
        root.addWidget(body, 1)

    def reload_from_disk(self) -> None:
        self._modes = list(self._catalog.load())
        self._current_slug = ""
        self._rebuild_list()

    @staticmethod
    def _kind_label(mode: ModeConfig) -> str:
        return {
            "primary": "主模式",
            "subagent": "子 Agent",
            "both": "主模式 + 子 Agent",
        }.get(mode.profile_kind, mode.profile_kind)

    def _rebuild_list(self, selected_slug: str = "") -> None:
        target = str(selected_slug or self._current_slug)
        self.mode_list.blockSignals(True)
        self.mode_list.clear()
        visible_indexes = self._visible_indexes()
        for mode_index in visible_indexes:
            mode = self._modes[mode_index]
            kind_label = self._kind_label(mode)
            item = SettingsStatusListItem(mode.slug)
            item.set_status(
                mode.name,
                enabled=True,
                detail=f"{kind_label} · {mode.slug}",
                tooltip=f"{mode.name}\n标识：{mode.slug}\n类型：{kind_label}",
            )
            item.setData(Qt.ItemDataRole.UserRole, mode.slug)
            self.mode_list.addItem(item)
        row = next(
            (
                index
                for index in range(self.mode_list.count())
                if str(self.mode_list.item(index).data(Qt.ItemDataRole.UserRole) or "") == target
            ),
            -1,
        )
        selected_row = row if row >= 0 else (0 if self.mode_list.count() else -1)
        self.mode_list.setCurrentRow(selected_row)
        self.mode_list.blockSignals(False)
        self._current_slug = (
            str(self.mode_list.item(selected_row).data(Qt.ItemDataRole.UserRole) or "")
            if selected_row >= 0
            else ""
        )
        self._load_current()

    def _on_mode_selected(self, row: int) -> None:
        if self._loading:
            return
        self._save_current()
        item = self.mode_list.item(row) if row >= 0 else None
        self._current_slug = str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""
        self._load_current()

    def _on_view_changed(self, _index: int) -> None:
        if self._loading:
            return
        self._save_current()
        self._current_slug = ""
        self._rebuild_list()

    def _visible_indexes(self) -> list[int]:
        subagents = self.view_tabs.currentIndex() == 1
        result: list[int] = []
        for index, mode in enumerate(self._modes):
            if subagents and mode.is_subagent_profile():
                result.append(index)
            elif not subagents and mode.is_primary_mode():
                result.append(index)
        return result

    def _load_current(self) -> None:
        mode = self._current_mode()
        self._loading = True
        try:
            self.editor.setEnabled(mode is not None)
            if mode is None:
                self.slug_edit.clear()
                self.name_edit.clear()
                self.purpose_edit.clear()
                for checkbox in self.tool_category_checks.values():
                    checkbox.setChecked(False)
                self.prompt_edit.clear()
                self.model_target_combo.set_model_target(ModelTarget())
                self.mode_delete_btn.setEnabled(False)
                return
            self.slug_edit.setText(mode.slug)
            self.name_edit.setText(mode.name)
            self.purpose_edit.setText(mode.purpose or "")
            self.tool_category_selector.set_categories(set(mode.allowed_tool_categories))
            index = self.profile_kind_combo.findData(mode.profile_kind)
            self.profile_kind_combo.setCurrentIndex(index if index >= 0 else 0)
            index = self.completion_policy_combo.findData(mode.completion_policy)
            self.completion_policy_combo.setCurrentIndex(index if index >= 0 else 0)
            self.model_target_combo.set_model_target(mode.model_target)
            self.max_turns_spin.setValue(int(mode.max_turns or 0))
            index = self.shared_context_combo.findData(mode.shared_context_policy)
            self.shared_context_combo.setCurrentIndex(index if index >= 0 else 0)
            self.prompt_edit.setPlainText(mode.prompt or "")
            self.mode_delete_btn.setEnabled(mode.slug not in set(get_required_mode_slugs()))
            self._sync_model_enabled()
        finally:
            self._loading = False

    def _current_mode(self) -> ModeConfig | None:
        return next((mode for mode in self._modes if mode.slug == self._current_slug), None)

    def _save_current(self) -> None:
        if self._loading:
            return
        current = self._current_mode()
        if current is None:
            return
        categories = sorted(self.tool_category_selector.selected_categories())
        turns = int(self.max_turns_spin.value())
        updated = replace(
            current,
            name=self.name_edit.text().strip() or current.slug,
            purpose=self.purpose_edit.text().strip(),
            prompt=self.prompt_edit.toPlainText().strip(),
            allowed_tool_categories=tuple(categories),
            profile_kind=current.profile_kind,
            completion_policy=str(self.completion_policy_combo.currentData() or "text"),
            model_target=self.model_target_combo.model_target(),
            max_turns=turns or None,
            shared_context_policy=str(
                self.shared_context_combo.currentData() or "indexes_only"
            ),
        )
        mode_index = next(
            (index for index, mode in enumerate(self._modes) if mode.slug == self._current_slug),
            -1,
        )
        if mode_index < 0:
            return
        self._modes[mode_index] = updated
        item = self._list_item(self._current_slug)
        if isinstance(item, SettingsStatusListItem):
            kind_label = self._kind_label(updated)
            item.set_status(
                updated.name,
                enabled=True,
                detail=f"{kind_label} · {updated.slug}",
                tooltip=f"{updated.name}\n标识：{updated.slug}\n类型：{kind_label}",
            )

    def _list_item(self, slug: str) -> SettingsStatusListItem | None:
        for row in range(self.mode_list.count()):
            item = self.mode_list.item(row)
            if str(item.data(Qt.ItemDataRole.UserRole) or "") == slug:
                return item if isinstance(item, SettingsStatusListItem) else None
        return None

    def _sync_model_enabled(self) -> None:
        mode = self._current_mode()
        enabled = bool(mode and mode.is_subagent_profile())
        for widget in (
            self.model_target_label,
            self.model_target_combo,
            self.max_turns_label,
            self.max_turns_spin,
            self.shared_context_label,
            self.shared_context_combo,
        ):
            widget.setVisible(enabled)
        self.model_target_combo.setEnabled(enabled)
        self.model_target_combo.setToolTip(
            "该 profile 被委托运行时使用。" if enabled else "主模式使用当前会话模型。"
        )

    def _add_mode(self) -> None:
        self._save_current()
        existing = {mode.slug for mode in self._modes}
        index = 1
        slug = "custom-agent"
        while slug in existing:
            index += 1
            slug = f"custom-agent-{index}"
        is_subagent = self.view_tabs.currentIndex() == 1
        self._modes.append(
            ModeConfig(
                slug=slug,
                name=f"自定义 Agent {index}",
                purpose="自定义委托 profile" if is_subagent else "自定义主模式",
                allowed_tool_categories=("read", "web", "state"),
                profile_kind="subagent" if is_subagent else "primary",
                completion_policy="explicit" if is_subagent else "text",
                source="global",
            )
        )
        self._current_slug = slug
        self._rebuild_list(slug)

    def _delete_mode(self) -> None:
        current = self._current_mode()
        if current is None:
            return
        if current.slug in set(get_required_mode_slugs()):
            QMessageBox.information(self, "不能删除", "Chat、Agent、Plan、Review 和 Channel 是核心模式。")
            return
        if (
            QMessageBox.question(self, "删除模式", f'确定删除“{current.name}”吗？')
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._modes = [mode for mode in self._modes if mode.slug != current.slug]
        self._current_slug = ""
        self._rebuild_list()

    def _open_config_dir(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(get_user_modes_json_path().parent)))

    def collect_modes(self) -> list[ModeConfig]:
        self._save_current()
        return list(self._modes)

    def save_to_disk(self) -> bool:
        try:
            return self._catalog.save(self.collect_modes())
        except Exception as exc:
            logger.debug("Failed to save modes: %s", exc)
            return False
