"""Installed skills and candidate-method management."""
from __future__ import annotations

import difflib
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QEvent, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.core.app.services.skill import SkillService
from pycat.core.config import get_global_subdir
from pycat.core.skills.usage import SkillUsageStore
from pycat.gui.dialogs.skill_evaluation_dialog import SkillEvaluationDialog
from pycat.gui.settings.components import (
    RESOURCE_DESCRIPTION_ROLE,
    RESOURCE_TOGGLE_ROLE,
    SettingsActionBar,
    SettingsListDetailLayout,
    SettingsStatusListItem,
    build_dialog_button_box,
    configure_settings_resource_list,
)
from pycat.gui.settings.page_header import build_page_header
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.settings_controls import SettingsFormLayout
from pycat.gui.utils.theme import configure_menu_button
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit, ThemedTextEdit
from pycat.models.session_paths import resolve_project_data_root


class SkillDropListWidget(QListWidget):
    """Own drag/drop delivery for local Skill directory or zip sources."""

    def __init__(self, on_sources_dropped: Callable[[list[Path]], None], parent=None) -> None:
        super().__init__(parent)
        self._on_sources_dropped = on_sources_dropped
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DropOnly)
        viewport = self.viewport()
        viewport.setAcceptDrops(True)
        viewport.installEventFilter(self)

    @staticmethod
    def _local_paths(event) -> list[Path] | None:
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return None
        urls = list(mime.urls())
        if not urls or any(not url.isLocalFile() for url in urls):
            return None
        return [Path(url.toLocalFile()) for url in urls]

    def _handle_drag_event(self, event, *, drop: bool = False) -> bool:
        paths = self._local_paths(event)
        if paths is None:
            event.ignore()
            return True
        event.acceptProposedAction()
        if drop:
            self._on_sources_dropped(paths)
        return True

    def eventFilter(self, watched, event) -> bool:
        if watched is self.viewport() and event.type() in {
            QEvent.Type.DragEnter,
            QEvent.Type.DragMove,
            QEvent.Type.Drop,
        }:
            return self._handle_drag_event(event, drop=event.type() == QEvent.Type.Drop)
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event) -> None:
        self._handle_drag_event(event)

    def dragMoveEvent(self, event) -> None:
        self._handle_drag_event(event)

    def dropEvent(self, event) -> None:
        self._handle_drag_event(event, drop=True)


class SkillCreateDialog(QDialog):
    """Collect a local skill draft; no directory is created before confirmation."""

    def __init__(self, *, has_project: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle(QCoreApplication.translate('SkillsPage', '创建技能'))
        self.setModal(True)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        form = SettingsFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.name_edit = ThemedLineEdit()
        self.name_edit.setPlaceholderText(QCoreApplication.translate('SkillsPage', '例如 review-code'))
        self.description_edit = ThemedLineEdit()
        self.description_edit.setPlaceholderText(QCoreApplication.translate('SkillsPage', '一句话说明用途'))
        self.scope_combo = QComboBox()
        self.scope_combo.addItem(QCoreApplication.translate('SkillsPage', '全局'), "global")
        if has_project:
            self.scope_combo.addItem(QCoreApplication.translate('SkillsPage', '当前工作区'), "project")
        form.addRow(QCoreApplication.translate('SkillsPage', '名称'), self.name_edit)
        form.addRow(QCoreApplication.translate('SkillsPage', '说明'), self.description_edit)
        form.addRow(QCoreApplication.translate('SkillsPage', '范围'), self.scope_combo)
        layout.addLayout(form)
        buttons = build_dialog_button_box(self, accept_text=QCoreApplication.translate('SkillsPage', '创建'))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, str, str]:
        return (
            self.name_edit.text().strip(),
            self.description_edit.text().strip(),
            str(self.scope_combo.currentData() or "global"),
        )


class SkillImportDialog(QDialog):
    """Confirm the target scope for one imported skill source."""

    def __init__(self, source_name: str, *, has_project: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle(QCoreApplication.translate('SkillsPage', '导入技能'))
        self.setModal(True)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        label = QLabel(QCoreApplication.translate('SkillsPage', '导入“{source_name}”到哪个范围？').format(source_name=source_name))
        label.setWordWrap(True)
        layout.addWidget(label)
        form = SettingsFormLayout()
        self.scope_combo = QComboBox()
        self.scope_combo.addItem(QCoreApplication.translate('SkillsPage', '全局'), "global")
        if has_project:
            self.scope_combo.addItem(QCoreApplication.translate('SkillsPage', '当前工作区'), "project")
        form.addRow(QCoreApplication.translate('SkillsPage', '范围'), self.scope_combo)
        layout.addLayout(form)
        buttons = build_dialog_button_box(self, accept_text=QCoreApplication.translate('SkillsPage', '导入'))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def scope(self) -> str:
        return str(self.scope_combo.currentData() or "global")


class SkillsPage(QWidget):
    page_title = "技能"

    def __init__(self, work_dir: str = ".", parent=None, *, skill_service: SkillService | None = None, candidate_evaluator=None, show_header=True):
        super().__init__(parent)
        self._work_dir = str(work_dir or "")
        self._service = skill_service or SkillService()
        self._skills = []
        self._candidates = []
        self._selected_candidate = None
        self._candidate_evaluator = candidate_evaluator
        self._show_header = show_header
        self._setup_ui()
        self._refresh_list()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        if self._show_header:
            layout.addWidget(build_page_header(QCoreApplication.translate('SkillsPage', '技能'), QCoreApplication.translate('SkillsPage', '加载、管理全局与项目技能。停用的技能不会进入运行时列表。')))

        body = SettingsListDetailLayout()
        self._view = "active"
        self.list_body = body
        overview = QHBoxLayout()
        self.search = ThemedLineEdit()
        self.search.setPlaceholderText(QCoreApplication.translate('SkillsPage', '搜索已安装技能'))
        self.search.setAccessibleName(QCoreApplication.translate('SkillsPage', '搜索已安装技能'))
        self.search.textChanged.connect(self._filter_list)
        overview.addWidget(self.search, 1)
        self.add_btn = QPushButton()
        add_menu = QMenu(self.add_btn)
        add_menu.addAction(QCoreApplication.translate('SkillsPage', '创建技能'), self._add_skill)
        self.import_btn = add_menu.addAction(QCoreApplication.translate('SkillsPage', '导入 SKILL.md / ZIP'), self._import_skill_picker)
        configure_menu_button(self.add_btn, add_menu, Icons.get(Icons.PLUS), QCoreApplication.translate('SkillsPage', '添加'))
        overview.addWidget(self.add_btn)
        more = QToolButton()
        more.setObjectName("resource_more_button")
        menu = QMenu(more)
        menu.addAction(QCoreApplication.translate('SkillsPage', '重新扫描'), self._refresh_list)
        menu.addAction(QCoreApplication.translate('SkillsPage', '查看技能'), lambda: self._set_view("active"))
        menu.addAction(QCoreApplication.translate('SkillsPage', '候选方法'), lambda: self._set_view("candidates"))
        configure_menu_button(more, menu, Icons.get_muted(Icons.MORE), QCoreApplication.translate('SkillsPage', '更多技能操作'))
        overview.addWidget(more)
        body.toolbar_layout.addLayout(overview)
        toolbar = SettingsActionBar(spacing=4)
        self.edit_btn = toolbar.add_icon_action(QCoreApplication.translate('SkillsPage', '编辑技能'), Icons.get(Icons.EDIT), self._open_skill_file)
        self.toggle_btn = toolbar.add_icon_action(QCoreApplication.translate('SkillsPage', '停用技能'), Icons.get(Icons.PAUSE), self._toggle_skill_enabled)
        self.delete_btn = toolbar.add_icon_action(
            QCoreApplication.translate('SkillsPage', '删除技能'),
            Icons.get(Icons.TRASH, color=Icons.COLOR_ERROR),
            self._delete_skill,
            danger=True,
        )
        toolbar.add_stretch()
        body.detail_layout.addWidget(toolbar)
        left = body.list_layout
        self.skill_list = configure_settings_resource_list(
            SkillDropListWidget(self._on_skill_sources_dropped)
        )
        self.skill_list.currentItemChanged.connect(self._on_selection_changed)
        body.bind(self.skill_list, self._toggle_skill_enabled)
        left.addWidget(self.skill_list, 1)
        right = body.detail_layout

        self.source_label = QLabel("")
        self.source_label.setWordWrap(True)
        self.source_label.setProperty("muted", True)
        right.addWidget(self.source_label)

        self.provenance_label = QLabel("")
        self.provenance_label.setWordWrap(True)
        self.provenance_label.setProperty("muted", True)
        right.addWidget(self.provenance_label)

        self.description_label = QLabel("")
        self.description_label.setWordWrap(True)
        self.description_label.setProperty("muted", True)
        right.addWidget(self.description_label)

        self.preview = ThemedTextEdit()
        self.preview.setObjectName("resource_preview")
        self.preview.setFrameShape(ThemedTextEdit.Shape.NoFrame)
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText(QCoreApplication.translate('SkillsPage', '选择技能查看内容'))
        right.addWidget(self.preview)
        candidate_actions = QHBoxLayout()
        self.evaluate_btn = QPushButton(QCoreApplication.translate('SkillsPage', '对照试验'))
        self.evaluate_btn.clicked.connect(self._evaluate_candidate)
        candidate_actions.addWidget(self.evaluate_btn)
        self.publish_btn = QPushButton(QCoreApplication.translate('SkillsPage', '发布'))
        self.publish_btn.clicked.connect(lambda: self._publish_candidate())
        candidate_actions.addWidget(self.publish_btn)
        self.rollback_btn = QPushButton(QCoreApplication.translate('SkillsPage', '回滚'))
        self.rollback_btn.clicked.connect(lambda: self._publish_candidate(rollback=True))
        candidate_actions.addWidget(self.rollback_btn)
        for button in (self.evaluate_btn, self.publish_btn, self.rollback_btn):
            button.hide()
        right.addLayout(candidate_actions)
        layout.addWidget(body, 1)
        self._sync_actions(None)

    def _set_view(self, view: str) -> None:
        self._view = view
        self.search.clear()
        label = QCoreApplication.translate('SkillsPage', '搜索候选方法') if view == "candidates" else QCoreApplication.translate('SkillsPage', '搜索已安装技能')
        self.search.setPlaceholderText(label)
        self.search.setAccessibleName(label)
        self.list_body.show_list()
        self._refresh_list()

    def _refresh_list(self) -> None:
        self.preview.clear()
        self.source_label.setText("")
        self.provenance_label.setText("")
        self.description_label.setText("")
        self._selected_candidate = None
        candidates = self._view == "candidates"
        for button in (self.add_btn, self.import_btn, self.edit_btn, self.toggle_btn, self.delete_btn):
            button.setVisible(not candidates)
        for button in (self.evaluate_btn, self.publish_btn, self.rollback_btn):
            button.setVisible(candidates)
        if candidates:
            self._candidates = self._service.list_candidates(self._work_dir)
            self.skill_list.clear()
            for candidate in self._candidates:
                item = QListWidgetItem(candidate["name"])
                item.setData(Qt.ItemDataRole.UserRole, candidate["id"])
                item.setToolTip(candidate.get("reason") or QCoreApplication.translate('SkillsPage', '待试验的方法'))
                self.skill_list.addItem(item)
            self._sync_actions(None)
            if self.skill_list.count():
                self.skill_list.setCurrentRow(0)
            else:
                self.description_label.setText(QCoreApplication.translate('SkillsPage', '暂无候选方法。自动整理提出的方法会先保留在这里，通过对照试验后再发布。'))
            return
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
            QCoreApplication.translate('SkillsPage', '已发现 {value} 个技能\n全局目录：{value_}\n项目目录：{value__}').format(value=len(self._skills), value_=get_global_subdir('skills'), value__=resolve_project_data_root(self._work_dir, data_dir=getattr(self._service, 'data_dir', None)) / 'skills')
        )
        for skill in self._skills:
            enabled = bool(getattr(skill, "enabled", True))
            status = QCoreApplication.translate('SkillsPage', '已启用') if enabled else QCoreApplication.translate('SkillsPage', '已停用')
            item = SettingsStatusListItem(skill.name)
            item.set_status(
                skill.name,
                enabled=enabled,
                detail=self._source_scope(skill),
                tooltip=QCoreApplication.translate('SkillsPage', '状态：{status}\n来源：{source}\n说明：{value}').format(status=status, source=skill.source, value=skill.description or '-'),
                two_lines=True,
            )
            item.setData(RESOURCE_DESCRIPTION_ROLE, skill.description or QCoreApplication.translate('SkillsPage', '暂无说明'))
            item.setData(RESOURCE_TOGGLE_ROLE, "skill" if not skill.read_only or skill.source_scope == "bundled" else "")
            self.skill_list.addItem(item)

        if self.skill_list.count() == 0:
            self.source_label.setText(QCoreApplication.translate('SkillsPage', '暂无技能'))
            self.description_label.setText(QCoreApplication.translate('SkillsPage', '可以添加一个全局技能，或把目录型 SKILL.md 放入项目 .pycat/skills。'))
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
        self._filter_list()

    def _filter_list(self):
        query = self.search.text().casefold()
        for index in range(self.skill_list.count()):
            item = self.skill_list.item(index)
            text = item.text() + " " + str(item.data(RESOURCE_DESCRIPTION_ROLE) or "")
            item.setHidden(query not in text.casefold())

    def _on_selection_changed(self, current: QListWidgetItem | None, _prev) -> None:
        if self._view == "candidates":
            candidate = next((item for item in self._candidates if current is not None and item["id"] == current.data(Qt.ItemDataRole.UserRole)), None)
            self._selected_candidate = candidate
            self.evaluate_btn.setEnabled(bool(candidate) and self._candidate_evaluator is not None)
            self.publish_btn.setEnabled(bool(candidate) and candidate["status"] in {"passed", "publishing"})
            self.rollback_btn.setEnabled(bool(candidate) and candidate["status"] in {"published", "rolling_back"})
            if candidate:
                try:
                    detail = self._service.candidate_detail(candidate["id"], work_dir=self._work_dir, scope=candidate["scope"])
                    status = {"draft": QCoreApplication.translate('SkillsPage', '待试验'), "passed": QCoreApplication.translate('SkillsPage', '试验通过'), "failed": QCoreApplication.translate('SkillsPage', '试验未通过'), "published": QCoreApplication.translate('SkillsPage', '已发布'), "rolled_back": QCoreApplication.translate('SkillsPage', '已回滚'),
                              "publishing": QCoreApplication.translate('SkillsPage', '发布中断，可继续发布'), "rolling_back": QCoreApplication.translate('SkillsPage', '回退中断，可继续回退')}.get(detail["status"], QCoreApplication.translate('SkillsPage', '存储不可读'))
                    self.source_label.setText(status)
                    self.description_label.setText(detail["description"])
                    self.preview.setPlainText("\n".join(difflib.unified_diff(detail["before"].splitlines(), detail["after"].splitlines(), fromfile=QCoreApplication.translate('SkillsPage', '原方法'), tofile=QCoreApplication.translate('SkillsPage', '候选方法'), lineterm="")))
                except (OSError, ValueError, KeyError) as exc:
                    self.source_label.setText(QCoreApplication.translate('SkillsPage', '存储不可读：{exc}').format(exc=exc))
            return
        if not current:
            self.preview.clear()
            self.source_label.setText("")
            self.provenance_label.setText("")
            self.description_label.setText("")
            self._sync_actions(None)
            return
        skill = self._service.get(
            current.data(Qt.ItemDataRole.UserRole),
            work_dir=self._work_dir,
            include_disabled=True,
        )
        if skill:
            status = QCoreApplication.translate('SkillsPage', '已启用') if getattr(skill, "enabled", True) else QCoreApplication.translate('SkillsPage', '已停用')
            usage_line = self._usage_summary(skill)
            source_text = f"{self._source_scope(skill)} · {status}"
            self.source_label.setToolTip(QCoreApplication.translate('SkillsPage', '文件：{source}').format(source=skill.source))
            if usage_line:
                source_text += f"\n{usage_line}"
            self.source_label.setText(source_text)
            self.provenance_label.setText(self._provenance_summary(skill))
            self.description_label.setText(skill.description or "")
            self.preview.setMarkdown(skill.content)
            self._sync_actions(skill)
            return
        self.preview.clear()
        self.source_label.setText("")
        self.provenance_label.setText("")
        self.description_label.setText("")
        self._sync_actions(None)

    def _evaluate_candidate(self):
        if self._selected_candidate and self._candidate_evaluator:
            dialog = SkillEvaluationDialog(self._selected_candidate, evaluator=self._candidate_evaluator, parent=self)
            dialog.exec()
            self._refresh_list()

    def _publish_candidate(self, *, rollback=False):
        if not self._selected_candidate:
            return
        candidate = self._selected_candidate
        ok, message = self._service.publish_candidate(candidate["id"], work_dir=self._work_dir, scope=candidate["scope"], rollback=rollback)
        self._refresh_list()
        if not ok:
            self.source_label.setText(message)

    def _provenance_summary(self, skill) -> str:
        metadata = getattr(skill, "metadata", {}) or {}
        provenance = metadata.get("provenance") if isinstance(metadata, dict) else None
        if not isinstance(provenance, dict) or not provenance:
            return ""
        parts: list[str] = []
        version = str(provenance.get("version") or "").strip()
        if version:
            parts.append(f"v{version}" if not version.lower().startswith("v") else version)
        repository = str(provenance.get("repository") or "").strip()
        if repository:
            parts.append(repository)
        license_name = str(provenance.get("license") or "").strip()
        if license_name:
            parts.append(license_name)
        author = str(provenance.get("author") or "").strip()
        if author:
            parts.append(f"by {author}")
        return " · ".join(parts)

    def _usage_summary(self, skill) -> str:
        root = self._skill_root(skill)
        if root is None:
            return ""
        try:
            store = SkillUsageStore(root=root.parent)
            record = store.get(skill.name)
        except Exception:
            return ""
        if not record:
            return ""
        created_by = str(record.get("created_by") or "user")
        creator_label = {"user": QCoreApplication.translate('SkillsPage', '用户'), "agent": "agent", "import": QCoreApplication.translate('SkillsPage', '导入')}.get(created_by, created_by)
        loads = record.get("load_count")
        parts = [QCoreApplication.translate('SkillsPage', '创建者: {creator_label}').format(creator_label=creator_label)]
        if isinstance(loads, int):
            parts.append(QCoreApplication.translate('SkillsPage', '加载 {loads} 次').format(loads=loads))
        last_used = str(record.get("last_used_at") or "").strip()
        if last_used:
            parts.append(QCoreApplication.translate('SkillsPage', '最近使用 {value}').format(value=last_used[:10]))
        return " · ".join(parts)

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
        self.toggle_btn.setEnabled(editable or getattr(skill, "source_scope", "") == "bundled")
        edit_label = QCoreApplication.translate('SkillsPage', '复制为用户技能后编辑') if has_selection and skill.read_only else QCoreApplication.translate('SkillsPage', '编辑技能')
        self.edit_btn.setToolTip(edit_label)
        self.edit_btn.setAccessibleName(edit_label)
        self.delete_btn.setEnabled(editable and self._skill_root(skill) is not None)
        enabled = bool(getattr(skill, "enabled", True)) if skill is not None else True
        action_label = QCoreApplication.translate('SkillsPage', '停用技能') if enabled else QCoreApplication.translate('SkillsPage', '启用技能')
        self.toggle_btn.setToolTip(action_label)
        self.toggle_btn.setAccessibleName(action_label)
        self.toggle_btn.setIcon(Icons.get(Icons.PAUSE if enabled else Icons.PLAY, scale_factor=1.0))

    def _open_skill_file(self) -> None:
        skill = self._current_skill()
        if skill is not None and skill.read_only:
            try:
                self._service.copy_to_managed(skill.name, work_dir=self._work_dir)
            except Exception as exc:
                QMessageBox.warning(self, QCoreApplication.translate('SkillsPage', '复制技能失败'), str(exc))
                return
            self._refresh_list()
            self._select_skill(skill.name)
            return
        path = self._skill_file(skill)
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
            QMessageBox.warning(self, QCoreApplication.translate('SkillsPage', '创建技能失败'), str(exc))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(skill_file)))
        self._refresh_list()
        self._select_skill(name)

    def _import_skill_picker(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self,
            QCoreApplication.translate('SkillsPage', '选择技能文件'),
            "",
            QCoreApplication.translate('SkillsPage', '技能文件 (*.md *.zip);;所有文件 (*)'),
        )
        if not path:
            return
        self._import_skill_source(Path(path))

    def _on_skill_sources_dropped(self, sources: list[Path]) -> None:
        if len(sources) > 1:
            QMessageBox.warning(self, QCoreApplication.translate('SkillsPage', '导入技能'), QCoreApplication.translate('SkillsPage', '一次只能导入一个技能文件或压缩包。'))
            return
        if not sources:
            return
        self._import_skill_source(sources[0])

    def _import_skill_source(self, source: Path) -> None:
        source = Path(source)
        if not source.exists():
            QMessageBox.warning(self, QCoreApplication.translate('SkillsPage', '导入技能'), QCoreApplication.translate('SkillsPage', '导入来源不存在。'))
            return
        is_zip = source.is_file() and source.suffix.lower() == ".zip"
        is_md_file = source.is_file() and source.suffix.lower() == ".md"
        if not is_zip and not is_md_file and not source.is_dir():
            QMessageBox.warning(
                self,
                QCoreApplication.translate('SkillsPage', '导入技能'),
                QCoreApplication.translate('SkillsPage', '只支持 .md 技能文件、包含 SKILL.md 的目录或 .zip 压缩包。'),
            )
            return
        dialog = SkillImportDialog(
            source.stem if is_zip or is_md_file else source.name,
            has_project=bool(str(self._work_dir or "").strip()),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        scope = dialog.scope()
        overwrite = False
        try:
            skill_file = self._service.import_managed(
                source,
                scope=scope,
                work_dir=self._work_dir,
                overwrite=overwrite,
            )
        except FileExistsError:
            answer = QMessageBox.question(
                self,
                QCoreApplication.translate('SkillsPage', '技能已存在'),
                QCoreApplication.translate('SkillsPage', '同名技能已存在，是否覆盖？'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                skill_file = self._service.import_managed(
                    source,
                    scope=scope,
                    work_dir=self._work_dir,
                    overwrite=True,
                )
            except Exception as exc:
                QMessageBox.warning(self, QCoreApplication.translate('SkillsPage', '导入技能失败'), str(exc))
                return
        except Exception as exc:
            QMessageBox.warning(self, QCoreApplication.translate('SkillsPage', '导入技能失败'), str(exc))
            return
        QMessageBox.information(self, QCoreApplication.translate('SkillsPage', '导入技能'), QCoreApplication.translate('SkillsPage', '已导入技能：\n{skill_file}').format(skill_file=skill_file))
        self._refresh_list()
        self._select_skill(skill_file.parent.name)

    def _toggle_skill_enabled(self) -> None:
        skill = self._current_skill()
        root = self._skill_root(skill)
        if skill is None or (skill.source_scope != "bundled" and (root is None or skill.read_only)):
            return
        try:
            self._service.set_managed_enabled(
                skill,
                not bool(getattr(skill, "enabled", True)),
                work_dir=self._work_dir,
            )
        except Exception as exc:
            QMessageBox.warning(self, QCoreApplication.translate('SkillsPage', '技能状态更新失败'), str(exc))
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
            QCoreApplication.translate("SkillsPage", "删除技能"),
            QCoreApplication.translate("SkillsPage", '确定删除技能 "{name}" 吗？\n{root}').format(name=skill.name, root=root),
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self._service.delete_managed(skill, work_dir=self._work_dir)
        except Exception as exc:
            QMessageBox.warning(self, QCoreApplication.translate('SkillsPage', '删除技能失败'), str(exc))
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
                get_global_subdir("skills", data_dir=self._service.data_dir).resolve(),
            ]
            if str(self._work_dir or "").strip():
                allowed_roots.append(
                    (resolve_project_data_root(self._work_dir, data_dir=getattr(self._service, "data_dir", None)) / "skills").resolve()
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
            return QCoreApplication.translate('SkillsPage', '项目')
        if scope == "global":
            return QCoreApplication.translate('SkillsPage', '全局')
        if scope == "external":
            return QCoreApplication.translate('SkillsPage', '外部')
        if scope == "bundled":
            return QCoreApplication.translate('SkillsPage', '内置')
        root = self._skill_root(skill)
        if root is None:
            return QCoreApplication.translate('SkillsPage', '外部')
        try:
            if str(self._work_dir or "").strip():
                try:
                    root.relative_to((resolve_project_data_root(self._work_dir, data_dir=getattr(self._service, "data_dir", None)) / "skills").resolve())
                    return QCoreApplication.translate('SkillsPage', '项目')
                except ValueError:
                    pass
            root.relative_to(get_global_subdir("skills").resolve())
            return QCoreApplication.translate('SkillsPage', '全局')
        except ValueError:
            return QCoreApplication.translate('SkillsPage', '外部')
        return QCoreApplication.translate('SkillsPage', '外部')
