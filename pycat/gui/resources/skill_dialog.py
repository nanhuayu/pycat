"""One entry point for creating or importing a skill; services own publication."""
from pathlib import Path

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.core.skills.manage import SKILL_NAME_RE
from pycat.gui.settings.components import build_dialog_button_box, settings_dialog_layout
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.settings_controls import SettingsFormLayout
from pycat.gui.utils.theme import configure_icon_button
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit


class SkillAddDialog(QDialog):
    def __init__(self, *, has_project: bool, allow_github: bool = True, parent=None):
        super().__init__(parent)
        layout = settings_dialog_layout(self, self.tr("添加技能"))
        self.setAcceptDrops(True)
        form = SettingsFormLayout()
        self.source_combo = QComboBox()
        for label, kind in ((self.tr("新建技能"), "create"), (self.tr("GitHub 仓库"), "github"),
                            (self.tr("本地文件或目录"), "local")):
            self.source_combo.addItem(label, kind)
        self.source_combo.model().item(self.source_combo.findData("github")).setEnabled(allow_github)
        form.addRow(self.tr("来源"), self.source_combo)
        layout.addLayout(form)
        self.fields = QWidget()
        field_layout = QVBoxLayout(self.fields)
        field_layout.setContentsMargins(0, 0, 0, 0)
        self.source_pages = []
        layout.addWidget(self.fields)
        self.name_edit = ThemedLineEdit()
        self.name_edit.setPlaceholderText(self.tr("例如 review-code"))
        self.description_edit = ThemedLineEdit()
        self.description_edit.setPlaceholderText(self.tr("一句话说明用途"))
        self.repository_edit = ThemedLineEdit()
        self.repository_edit.setPlaceholderText("owner/repo")
        self.directory_edit = ThemedLineEdit()
        self.directory_edit.setPlaceholderText("skills/example-skill")
        self.ref_edit = ThemedLineEdit("HEAD")
        self.path_edit = ThemedLineEdit()
        self.path_edit.setPlaceholderText(self.tr("选择或拖入 SKILL.md、ZIP 或技能目录"))
        local_source = QWidget()
        local_layout = QHBoxLayout(local_source)
        local_layout.setContentsMargins(0, 0, 0, 0)
        local_layout.setSpacing(4)
        local_layout.addWidget(self.path_edit, 1)
        for icon, label, callback in ((Icons.FILE, self.tr("选择文件"), self._choose_file),
                                      (Icons.FOLDER, self.tr("选择目录"), self._choose_directory)):
            button = QToolButton()
            configure_icon_button(button, Icons.get(icon), label)
            button.clicked.connect(callback)
            local_layout.addWidget(button)
        for rows in (
            ((self.tr("名称"), self.name_edit), (self.tr("说明"), self.description_edit)),
            ((self.tr("仓库"), self.repository_edit), (self.tr("技能目录"), self.directory_edit),
             (self.tr("分支 / 标签 / 提交"), self.ref_edit)),
            ((self.tr("路径"), local_source),),
        ):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            fields = SettingsFormLayout()
            for label, field in rows:
                fields.addRow(label, field)
            page_layout.addLayout(fields)
            field_layout.addWidget(page)
            self.source_pages.append(page)
        scope_form = SettingsFormLayout()
        self.scope_combo = QComboBox()
        self.scope_combo.addItem(self.tr("全局"), "global")
        if has_project:
            self.scope_combo.addItem(self.tr("当前工作区"), "project")
        scope_form.addRow(self.tr("范围"), self.scope_combo)
        layout.addLayout(scope_form)
        self.validation_label = QLabel()
        self.validation_label.setWordWrap(True)
        self.validation_label.setObjectName("validation_error_label")
        self.validation_label.hide()
        layout.addWidget(self.validation_label)
        self.buttons = build_dialog_button_box(self, accept_text=self.tr("创建"))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        self._source_changed(0)

    def _source_changed(self, index):
        for i, page in enumerate(self.source_pages):
            page.setVisible(i == index)
        self.fields.layout().activate()
        self.layout().activate()
        self.validation_label.hide()
        label = (self.tr("创建"), self.tr("准备安装"), self.tr("导入"))[index]
        primary = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        primary.setText(label)
        primary.setAccessibleName(label)
        self.adjustSize()

    def set_local_source(self, path: Path):
        self.source_combo.setCurrentIndex(self.source_combo.findData("local"))
        self.path_edit.setText(str(path))

    def values(self) -> dict:
        kind = self.source_combo.currentData()
        values = {"kind": kind, "scope": self.scope_combo.currentData()}
        if kind == "create":
            values.update(name=self.name_edit.text().strip().lower(), description=self.description_edit.text().strip())
        elif kind == "github":
            values.update(repository=self.repository_edit.text().strip(), directory=self.directory_edit.text().strip(),
                          ref=self.ref_edit.text().strip() or "HEAD")
        else:
            values["source"] = self.path_edit.text().strip()
        return values

    def accept(self):
        values = self.values()
        if values["kind"] == "create" and not SKILL_NAME_RE.fullmatch(values["name"]):
            self._invalid(self.tr("名称需为 3–64 个小写字母、数字或短横线，并以字母或数字开头。"), self.name_edit)
            return
        if values["kind"] == "github":
            if not values["repository"]:
                self._invalid(self.tr("请填写 GitHub 仓库，例如 owner/repo。"), self.repository_edit)
                return
            if not values["directory"]:
                self._invalid(self.tr("请填写仓库内包含 SKILL.md 的技能目录。"), self.directory_edit)
                return
        if values["kind"] == "local":
            source = Path(values["source"])
            if not values["source"] or not source.exists():
                self._invalid(self.tr("请选择存在的技能文件或目录。"), self.path_edit)
                return
            if not source.is_dir() and source.suffix.lower() not in {".md", ".zip"}:
                self._invalid(self.tr("支持 Markdown 技能文件、ZIP 或包含 SKILL.md 的目录。"), self.path_edit)
                return
        super().accept()

    def _invalid(self, message, field=None):
        self.validation_label.setText(message)
        self.validation_label.show()
        if field is not None:
            field.setFocus()

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr("选择技能文件"), "", self.tr("技能文件 (*.md *.zip)"))
        if path:
            self.set_local_source(Path(path))

    def _choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, self.tr("选择技能目录"))
        if path:
            self.set_local_source(Path(path))

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            self.set_local_source(Path(urls[0].toLocalFile()))
            event.acceptProposedAction()
        else:
            event.ignore()
