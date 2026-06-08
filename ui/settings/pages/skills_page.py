"""Skills management settings page."""
from __future__ import annotations

import shutil
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QTextEdit,
    QPushButton,
    QGroupBox,
    QMessageBox,
)

from core.config import get_global_subdir
from core.skills import SkillsManager
from ui.settings.page_header import build_page_header
from ui.utils.icon_manager import Icons


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
        global_dir = str(get_global_subdir("skills"))
        location_label = QLabel(
            "技能使用目录型 SKILL.md；停用状态写入技能目录内的 .disabled 标记。\n"
            f"全局目录: {global_dir}  |  项目目录: .pycat/skills/"
        )
        location_label.setWordWrap(True)
        location_label.setProperty("muted", True)
        layout.addWidget(location_label)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.add_btn = QPushButton("新增")
        self.add_btn.setObjectName("settings_action_btn")
        self.add_btn.setIcon(Icons.get(Icons.PLUS, scale_factor=1.0))
        self.add_btn.clicked.connect(self._add_skill)
        toolbar.addWidget(self.add_btn)

        self.toggle_btn = QPushButton("停用")
        self.toggle_btn.setObjectName("settings_action_btn")
        self.toggle_btn.setIcon(Icons.get(Icons.PAUSE, scale_factor=1.0))
        self.toggle_btn.clicked.connect(self._toggle_skill_enabled)
        toolbar.addWidget(self.toggle_btn)

        self.delete_btn = QPushButton("删除")
        self.delete_btn.setObjectName("settings_action_btn")
        self.delete_btn.setProperty("danger", True)
        self.delete_btn.setIcon(Icons.get(Icons.XMARK, color=Icons.COLOR_ERROR, scale_factor=1.0))
        self.delete_btn.clicked.connect(self._delete_skill)
        toolbar.addWidget(self.delete_btn)

        reload_btn = QPushButton("重新扫描")
        reload_btn.setObjectName("settings_action_btn")
        reload_btn.setIcon(Icons.get(Icons.REFRESH, scale_factor=1.0))
        reload_btn.clicked.connect(self._refresh_list)
        toolbar.addWidget(reload_btn)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        body = QHBoxLayout()
        body.setSpacing(12)

        self.left_group = QGroupBox("技能")
        left = QVBoxLayout(self.left_group)
        left.setContentsMargins(10, 10, 10, 10)
        left.setSpacing(8)
        self.skill_list = QListWidget()
        self.skill_list.setObjectName("settings_list")
        self.skill_list.setSpacing(2)
        self.skill_list.currentItemChanged.connect(self._on_selection_changed)
        left.addWidget(self.skill_list)
        body.addWidget(self.left_group, 1)

        right_group = QGroupBox("技能预览")
        right = QVBoxLayout(right_group)
        right.setContentsMargins(10, 10, 10, 10)
        right.setSpacing(8)

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
        body.addWidget(right_group, 2)

        layout.addLayout(body)
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
        self.left_group.setTitle(f"技能 ({len(self._skills)})")
        for skill in self._skills:
            enabled = bool(getattr(skill, "enabled", True))
            status = "启用" if enabled else "停用"
            item = QListWidgetItem(f"{skill.name}\n{status} · {self._source_scope(skill)}")
            item.setIcon(Icons.get(Icons.PLAY if enabled else Icons.PAUSE, scale_factor=0.9))
            item.setSizeHint(QSize(0, 40))
            item.setData(Qt.ItemDataRole.UserRole, skill.name)
            item.setToolTip(f"状态：{status}\n来源：{skill.source}\n说明：{skill.description or '-'}")
            if not enabled:
                item.setForeground(Qt.GlobalColor.gray)
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
            self.source_label.setText(f"状态: {status}\n来源: {skill.source}")
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
        self.toggle_btn.setEnabled(editable)
        self.delete_btn.setEnabled(editable and self._skill_root(skill) is not None)
        enabled = bool(getattr(skill, "enabled", True)) if skill is not None else True
        self.toggle_btn.setText("停用" if enabled else "启用")
        self.toggle_btn.setIcon(Icons.get(Icons.PAUSE if enabled else Icons.PLAY, scale_factor=1.0))

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
