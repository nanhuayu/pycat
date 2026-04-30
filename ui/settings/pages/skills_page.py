"""Skills management settings page."""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QTextEdit, QPushButton, QGroupBox, QLineEdit,
)
from PyQt6.QtCore import Qt

from core.config import get_global_subdir
from core.skills import SkillsManager
from ui.settings.page_header import build_page_header
from ui.utils.icon_manager import Icons


class SkillsPage(QWidget):
    page_title = "技能"

    def __init__(self, work_dir: str = ".", parent=None):
        super().__init__(parent)
        self._manager = SkillsManager(work_dir)
        self._skills = []
        self._setup_ui()
        self._refresh_list()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(build_page_header("技能", "统一查看全局与项目技能，便于快速核对来源、说明和原始内容。"))
        global_dir = str(get_global_subdir("skills"))
        location_label = QLabel(
            "技能支持 legacy 单文件 .md/.txt 与目录型 SKILL.md。\n"
            f"全局目录: {global_dir}  |  项目目录: .pycat/skills/"
        )
        location_label.setWordWrap(True)
        location_label.setProperty("muted", True)
        layout.addWidget(location_label)

        toolbar = QHBoxLayout()
        self.count_label = QLabel("")
        self.count_label.setProperty("muted", True)
        toolbar.addWidget(self.count_label)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索技能名称、来源或说明")
        self.search_input.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self.search_input, 1)
        toolbar.addStretch()

        reload_btn = QPushButton("重新扫描")
        reload_btn.setIcon(Icons.get(Icons.REFRESH, scale_factor=1.0))
        reload_btn.clicked.connect(self._refresh_list)
        toolbar.addWidget(reload_btn)
        layout.addLayout(toolbar)

        body = QHBoxLayout()
        body.setSpacing(12)

        left_group = QGroupBox("技能列表")
        left = QVBoxLayout(left_group)
        left.setContentsMargins(10, 10, 10, 10)
        left.setSpacing(8)
        self.skill_list = QListWidget()
        self.skill_list.setSpacing(2)
        self.skill_list.currentItemChanged.connect(self._on_selection_changed)
        left.addWidget(self.skill_list)

        body.addWidget(left_group, 1)

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

    def _refresh_list(self) -> None:
        self.preview.clear()
        self.source_label.setText("")
        self.description_label.setText("")
        self._manager.reload()
        self._skills = list(self._manager.list_skills())
        self._apply_filter()

    def _apply_filter(self) -> None:
        keyword = (self.search_input.text() or "").strip().lower()
        current_name = ""
        current = self.skill_list.currentItem()
        if current is not None:
            current_name = str(current.data(Qt.ItemDataRole.UserRole) or "")

        self.skill_list.clear()
        matched = []
        for skill in self._skills:
            haystack = "\n".join([
                str(getattr(skill, "name", "") or ""),
                str(getattr(skill, "source", "") or ""),
                str(getattr(skill, "description", "") or ""),
            ]).lower()
            if keyword and keyword not in haystack:
                continue
            matched.append(skill)

        self.count_label.setText(f"共 {len(self._skills)} 个技能，当前显示 {len(matched)} 个")
        for skill in matched:
            item = QListWidgetItem(skill.name)
            item.setData(Qt.ItemDataRole.UserRole, skill.name)
            item.setToolTip(f"来源：{skill.source}\n说明：{skill.description or '-'}")
            self.skill_list.addItem(item)

        if self.skill_list.count() == 0:
            self.source_label.setText("未找到匹配技能")
            self.description_label.setText("试试更短的关键字，或重新扫描技能目录。")
            self.preview.clear()
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
            return
        name = current.data(Qt.ItemDataRole.UserRole)
        skill = self._manager.get(name)
        if skill:
            self.source_label.setText(f"来源: {skill.source}")
            self.description_label.setText(skill.description or "")
            self.preview.setPlainText(skill.content)
        else:
            self.preview.clear()
            self.source_label.setText("")
            self.description_label.setText("")
