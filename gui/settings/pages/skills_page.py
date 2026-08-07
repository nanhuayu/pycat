"""Skills management settings page."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QWidget,
    QVBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTextEdit,
    QMessageBox,
)

from core.config import get_global_subdir
from core.app.services.skill import SkillService
from gui.settings.page_header import build_page_header
from gui.settings.components import (
    SettingsActionBar,
    SettingsListDetailLayout,
    SettingsStatusListItem,
    configure_settings_resource_list,
)
from gui.utils.icon_manager import Icons
from gui.widgets.themed_line_edit import ThemedTextEdit
from gui.widgets.themed_line_edit import ThemedLineEdit


class SkillCreateDialog(QDialog):
    """Collect a local skill draft; no directory is created before confirmation."""

    def __init__(self, *, has_project: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("创建技能")
        self.setModal(True)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.name_edit = ThemedLineEdit()
        self.name_edit.setPlaceholderText("例如 review-code")
        self.description_edit = ThemedLineEdit()
        self.description_edit.setPlaceholderText("一句话说明用途")
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("全局", "global")
        if has_project:
            self.scope_combo.addItem("当前工作区", "project")
        form.addRow("名称", self.name_edit)
        form.addRow("说明", self.description_edit)
        form.addRow("范围", self.scope_combo)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("创建")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, str, str]:
        return (
            self.name_edit.text().strip(),
            self.description_edit.text().strip(),
            str(self.scope_combo.currentData() or "global"),
        )


class SkillsPage(QWidget):
    page_title = "技能"

    def __init__(self, work_dir: str = ".", parent=None, *, skill_service: SkillService | None = None):
        super().__init__(parent)
        self._work_dir = str(work_dir or "")
        self._service = skill_service or SkillService()
        self._skills = []
        self._setup_ui()
        self._refresh_list()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("技能", "加载、管理全局与项目技能。停用的技能不会进入运行时列表。"))

        body = SettingsListDetailLayout(list_stretch=1, detail_stretch=2)
        self.list_body = body
        toolbar = SettingsActionBar(spacing=4)
        self.add_btn = toolbar.add_icon_action("新增技能", Icons.get(Icons.PLUS), self._add_skill)
        self.edit_btn = toolbar.add_icon_action("编辑技能", Icons.get(Icons.EDIT), self._open_skill_file)
        self.toggle_btn = toolbar.add_icon_action("停用技能", Icons.get(Icons.PAUSE), self._toggle_skill_enabled)
        self.delete_btn = toolbar.add_icon_action(
            "删除技能",
            Icons.get(Icons.XMARK, color=Icons.COLOR_ERROR),
            self._delete_skill,
            danger=True,
        )
        toolbar.add_icon_action("重新扫描技能", Icons.get(Icons.REFRESH), self._refresh_list)
        toolbar.add_stretch()
        body.list_layout.addWidget(toolbar)
        left = body.list_layout
        self.skill_list = configure_settings_resource_list(QListWidget())
        self.skill_list.currentItemChanged.connect(self._on_selection_changed)
        self.skill_list.itemDoubleClicked.connect(self._open_selected_item)
        left.addWidget(self.skill_list, 1)
        right = body.detail_layout

        self.source_label = QLabel("")
        self.source_label.setWordWrap(True)
        self.source_label.setProperty("muted", True)
        right.addWidget(self.source_label)

        self.description_label = QLabel("")
        self.description_label.setWordWrap(True)
        self.description_label.setProperty("muted", True)
        right.addWidget(self.description_label)

        self.preview = ThemedTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("选择左侧技能查看内容")
        right.addWidget(self.preview)
        layout.addWidget(body, 1)
        self._sync_actions(None)

    def _refresh_list(self) -> None:
        self.preview.clear()
        self.source_label.setText("")
        self.description_label.setText("")
        self._skills = list(
            self._service.list_for_workdir(self._work_dir, include_disabled=True)
        )
        self._render_list()

    def _render_list(self) -> None:
        current_name = ""
        current = self.skill_list.currentItem()
        if current is not None:
            current_name = str(current.data(Qt.ItemDataRole.UserRole) or "")

        self.skill_list.clear()
        self.skill_list.setToolTip(
            f"已发现 {len(self._skills)} 个技能\n"
            f"全局目录：{get_global_subdir('skills')}\n"
            f"项目目录：{Path(self._work_dir) / '.pycat' / 'skills'}"
        )
        for skill in self._skills:
            enabled = bool(getattr(skill, "enabled", True))
            status = "启用" if enabled else "停用"
            item = SettingsStatusListItem(skill.name)
            item.set_status(
                skill.name,
                enabled=enabled,
                detail=self._source_scope(skill),
                tooltip=f"状态：{status}\n来源：{skill.source}\n说明：{skill.description or '-'}",
                two_lines=True,
            )
            self.skill_list.addItem(item)

        if self.skill_list.count() == 0:
            self.source_label.setText("暂无技能")
            self.description_label.setText("可以新增一个全局技能，或把目录型 SKILL.md 放入项目 .pycat/skills。")
            self.preview.clear()
            self._sync_actions(None)
            return

        restore_row = 0
        if current_name:
            for index in range(self.skill_list.count()):
                if str(self.skill_list.item(index).data(Qt.ItemDataRole.UserRole) or "") == current_name:
                    restore_row = index
                    break
        self.skill_list.setCurrentRow(restore_row)

    def _on_selection_changed(self, current: QListWidgetItem | None, _prev) -> None:
        if not current:
            self.preview.clear()
            self.source_label.setText("")
            self.description_label.setText("")
            self._sync_actions(None)
            return
        skill = self._service.get(
            current.data(Qt.ItemDataRole.UserRole),
            work_dir=self._work_dir,
            include_disabled=True,
        )
        if skill:
            status = "启用" if getattr(skill, "enabled", True) else "停用"
            self.source_label.setText(
                f"状态: {status}\n来源: {self._source_scope(skill)}\n文件: {skill.source}"
            )
            self.description_label.setText(skill.description or "")
            self.preview.setPlainText(skill.content)
            self._sync_actions(skill)
            return
        self.preview.clear()
        self.source_label.setText("")
        self.description_label.setText("")
        self._sync_actions(None)

    def _current_skill(self):
        item = self.skill_list.currentItem()
        if item is None:
            return None
        return self._service.get(
            str(item.data(Qt.ItemDataRole.UserRole) or ""),
            work_dir=self._work_dir,
            include_disabled=True,
        )

    def _sync_actions(self, skill) -> None:
        has_selection = skill is not None
        editable = has_selection and not bool(getattr(skill, "read_only", False))
        self.edit_btn.setEnabled(self._skill_file(skill) is not None)
        self.toggle_btn.setEnabled(editable)
        self.delete_btn.setEnabled(editable and self._skill_root(skill) is not None)
        enabled = bool(getattr(skill, "enabled", True)) if skill is not None else True
        action_label = "停用技能" if enabled else "启用技能"
        self.toggle_btn.setToolTip(action_label)
        self.toggle_btn.setAccessibleName(action_label)
        self.toggle_btn.setIcon(Icons.get(Icons.PAUSE if enabled else Icons.PLAY, scale_factor=1.0))

    def _open_selected_item(self, item: QListWidgetItem) -> None:
        self.skill_list.setCurrentItem(item)
        self._open_skill_file()

    def _open_skill_file(self) -> None:
        path = self._skill_file(self._current_skill())
        if path is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _add_skill(self) -> None:
        dialog = SkillCreateDialog(has_project=bool(str(self._work_dir or "").strip()), parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, description, scope = dialog.values()
        try:
            skill_file = self._service.create_managed(
                name,
                description=description,
                scope=scope,
                work_dir=self._work_dir,
            )
        except Exception as exc:
            QMessageBox.warning(self, "创建技能失败", str(exc))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(skill_file)))
        self._refresh_list()
        self._select_skill(name)

    def _toggle_skill_enabled(self) -> None:
        skill = self._current_skill()
        root = self._skill_root(skill)
        if skill is None or root is None or bool(getattr(skill, "read_only", False)):
            return
        try:
            self._service.set_managed_enabled(
                skill,
                not bool(getattr(skill, "enabled", True)),
                work_dir=self._work_dir,
            )
        except Exception as exc:
            QMessageBox.warning(self, "技能状态更新失败", str(exc))
            return
        name = skill.name
        self._refresh_list()
        self._select_skill(name)

    def _delete_skill(self) -> None:
        skill = self._current_skill()
        root = self._skill_root(skill)
        if skill is None or root is None or bool(getattr(skill, "read_only", False)):
            return
        if QMessageBox.question(
            self,
            "删除技能",
            f'确定删除技能 "{skill.name}" 吗？\n{root}',
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self._service.delete_managed(skill, work_dir=self._work_dir)
        except Exception as exc:
            QMessageBox.warning(self, "删除技能失败", str(exc))
            return
        self._refresh_list()

    def _select_skill(self, name: str) -> None:
        target = str(name or "").strip().lower()
        for row in range(self.skill_list.count()):
            item = self.skill_list.item(row)
            if str(item.data(Qt.ItemDataRole.UserRole) or "").strip().lower() == target:
                self.skill_list.setCurrentRow(row)
                return

    def _skill_root(self, skill) -> Path | None:
        if skill is None or bool(getattr(skill, "read_only", False)):
            return None
        try:
            source = Path(str(getattr(skill, "source", "") or "")).resolve()
            root = source.parent
            if not (root / "SKILL.md").is_file():
                return None
            allowed_roots = [
                get_global_subdir("skills").resolve(),
            ]
            if str(self._work_dir or "").strip():
                allowed_roots.append(
                    (Path(self._work_dir).resolve() / ".pycat" / "skills").resolve()
                )
            for allowed in allowed_roots:
                try:
                    root.relative_to(allowed)
                    return root
                except ValueError:
                    continue
        except Exception:
            return None
        return None

    @staticmethod
    def _skill_file(skill) -> Path | None:
        if skill is None:
            return None
        try:
            path = Path(str(getattr(skill, "source", "") or "")).expanduser().resolve()
        except Exception:
            return None
        return path if path.is_file() else None

    def _source_scope(self, skill) -> str:
        scope = str(getattr(skill, "source_scope", "") or "").strip().lower()
        if scope == "project":
            return "项目"
        if scope == "global":
            return "全局"
        if scope == "external":
            return "外部"
        root = self._skill_root(skill)
        if root is None:
            return "外部"
        try:
            if str(self._work_dir or "").strip():
                try:
                    root.relative_to((Path(self._work_dir).resolve() / ".pycat" / "skills").resolve())
                    return "项目"
                except ValueError:
                    pass
            root.relative_to(get_global_subdir("skills").resolve())
            return "全局"
        except ValueError:
            return "外部"
        return "外部"
