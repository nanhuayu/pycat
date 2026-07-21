"""Skills management settings page."""
from __future__ import annotations

import shutil
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTextEdit,
    QMessageBox,
)

from core.config import get_global_subdir
from core.skills import SkillsManager
from gui.settings.page_header import build_page_header
from gui.settings.components import (
    SettingsActionBar,
    SettingsListDetailLayout,
    SettingsStatusListItem,
    configure_settings_resource_list,
)
from gui.utils.icon_manager import Icons


class SkillsPage(QWidget):
    page_title = "技能"

    def __init__(self, work_dir: str = ".", parent=None):
        super().__init__(parent)
        self._work_dir = str(work_dir or ".")
        self._manager = SkillsManager(self._work_dir, include_disabled=True)
        self._skills = []
        self._setup_ui()
        self._refresh_list()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("技能", "加载、管理全局与项目技能。停用的技能不会进入运行时列表。"))

        body = SettingsListDetailLayout("技能", "技能预览", list_stretch=1, detail_stretch=2)
        self.list_body = body
        body.list_title_label.setToolTip(
            f"全局目录：{get_global_subdir('skills')}\n项目目录：{Path(self._work_dir) / '.pycat' / 'skills'}"
        )
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

        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("选择左侧技能查看内容")
        right.addWidget(self.preview)
        layout.addWidget(body, 1)
        self._sync_actions(None)

    def _refresh_list(self) -> None:
        self.preview.clear()
        self.source_label.setText("")
        self.description_label.setText("")
        self._manager.reload()
        self._skills = list(self._manager.list_skills())
        self._render_list()

    def _render_list(self) -> None:
        current_name = ""
        current = self.skill_list.currentItem()
        if current is not None:
            current_name = str(current.data(Qt.ItemDataRole.UserRole) or "")

        self.skill_list.clear()
        self.list_body.set_list_title(f"技能 ({len(self._skills)})")
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
        skill = self._manager.get(current.data(Qt.ItemDataRole.UserRole))
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
        return self._manager.get(str(item.data(Qt.ItemDataRole.UserRole) or ""))

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
        root = get_global_subdir("skills")
        root.mkdir(parents=True, exist_ok=True)
        index = 1
        while (root / f"custom-skill-{index}").exists():
            index += 1
        skill_dir = root / f"custom-skill-{index}"
        skill_dir.mkdir(parents=True, exist_ok=False)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            f"name: custom-skill-{index}\n"
            "description: 简短说明这个技能适合处理什么。\n"
            "mode: agent\n"
            "---\n\n"
            "# 使用说明\n\n"
            "写下触发场景、处理步骤和必要约束。\n",
            encoding="utf-8",
        )
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(skill_dir)))
        self._refresh_list()
        self._select_skill(f"custom-skill-{index}")

    def _toggle_skill_enabled(self) -> None:
        skill = self._current_skill()
        root = self._skill_root(skill)
        if skill is None or root is None or bool(getattr(skill, "read_only", False)):
            return
        marker = root / ".disabled"
        if bool(getattr(skill, "enabled", True)):
            marker.write_text("disabled by PyCat settings\n", encoding="utf-8")
        else:
            try:
                marker.unlink()
            except FileNotFoundError:
                pass
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
        shutil.rmtree(root)
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
                (Path(self._work_dir).resolve() / ".pycat" / "skills").resolve(),
                get_global_subdir("skills").resolve(),
            ]
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
            root.relative_to((Path(self._work_dir).resolve() / ".pycat" / "skills").resolve())
            return "项目"
        except ValueError:
            return "全局"
