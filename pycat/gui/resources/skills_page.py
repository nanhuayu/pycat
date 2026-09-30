"""Unified skill catalog, local editing and candidate-method management."""
from __future__ import annotations

import difflib
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QEvent, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.core.app.services.skill import SkillService
from pycat.core.config import get_global_subdir
from pycat.core.skills.usage import SkillUsageStore
from pycat.gui.dialogs.skill_evaluation_dialog import SkillEvaluationDialog
from pycat.gui.resources.discovery import ResourceDiscovery
from pycat.gui.resources.extension_details import ExtensionDetails
from pycat.gui.resources.skill_dialog import SkillAddDialog
from pycat.gui.settings.components import (
    RESOURCE_DESCRIPTION_ROLE,
    RESOURCE_DETAIL_META_ROLE,
    RESOURCE_SUBTITLE_ROLE,
    RESOURCE_TITLE_ROLE,
    RESOURCE_TOGGLE_ROLE,
    SettingsActionBar,
    SettingsListDetailLayout,
    SettingsStatusListItem,
    configure_settings_resource_list,
    set_resource_badge,
)
from pycat.gui.settings.page_header import build_page_header
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import configure_icon_button, configure_menu_button
from pycat.gui.view_models.extension_labels import extension_text, resource_badge
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


class SkillsPage(QWidget):
    page_title = "技能"

    def __init__(self, work_dir: str = ".", parent=None, *, skill_service: SkillService | None = None, candidate_evaluator=None, extension_service=None, show_header=True):
        super().__init__(parent)
        self._work_dir = str(work_dir or "")
        self._service = skill_service or SkillService()
        self._extensions = extension_service
        self._pending_skill = None
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
        self.search.setPlaceholderText(QCoreApplication.translate('SkillsPage', '搜索技能'))
        self.search.setAccessibleName(QCoreApplication.translate('SkillsPage', '搜索技能'))
        overview.addWidget(self.search, 1)
        self.back_to_skills_btn = QPushButton(self.tr("返回技能"))
        self.back_to_skills_btn.clicked.connect(lambda: self._set_view("active"))
        self.back_to_skills_btn.hide()
        overview.addWidget(self.back_to_skills_btn)
        self.add_btn = QPushButton(Icons.get(Icons.PLUS), QCoreApplication.translate('SkillsPage', '添加'))
        self.add_btn.clicked.connect(self._add_skill)
        overview.addWidget(self.add_btn)
        more = QToolButton()
        more.setObjectName("resource_more_button")
        menu = QMenu(more)
        menu.addAction(QCoreApplication.translate('SkillsPage', '重新扫描'), self._refresh_list)
        self.candidates_action = menu.addAction(QCoreApplication.translate('SkillsPage', '候选方法'))
        self.candidates_action.setCheckable(True)
        self.candidates_action.triggered.connect(lambda checked: self._set_view("candidates" if checked else "active"))
        configure_menu_button(more, menu, Icons.get_muted(Icons.MORE), QCoreApplication.translate('SkillsPage', '更多技能操作'))
        overview.addWidget(more)
        body.toolbar_layout.addLayout(overview)
        self.update_btn = QToolButton()
        self.update_btn.setObjectName("resource_update_button")
        configure_icon_button(self.update_btn, Icons.get_muted(Icons.CIRCLE_INFO), self.tr("版本与更新"))
        self.update_btn.setCheckable(True)
        self.update_btn.clicked.connect(self._show_update)
        self.update_btn.toggled.connect(self._update_action_label)
        self.detail_stack = QStackedWidget()
        body.detail_layout.addWidget(self.detail_stack, 1)
        self.local_detail = QWidget()
        right = QVBoxLayout(self.local_detail)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(8)
        self.detail_stack.addWidget(self.local_detail)
        self.extension_details = ExtensionDetails(kind="skill", service=self._extensions, work_dir=self._work_dir)
        self.detail_stack.addWidget(self.extension_details)
        self.extension_details.extension_prepared.connect(self._skill_prepared)
        self.extension_details.skills_changed.connect(self._skills_changed)
        self.extension_details.changed.connect(self._extension_changed)
        self.discovery = ResourceDiscovery(kind="skill", service=self._extensions,
            search=self.search, details=self.extension_details, work_dir=self._work_dir, parent=self)
        self.search_button = self.discovery.search_button
        overview.insertWidget(1, self.search_button)
        self.discovery.changed.connect(self._query_changed)
        self.extension_details.busy_changed.connect(self._set_busy)
        toolbar = self.action_bar = SettingsActionBar(spacing=4)
        self.edit_btn = toolbar.add_icon_action(QCoreApplication.translate('SkillsPage', '编辑技能'), Icons.get(Icons.EDIT), self._open_skill_file)
        self.toggle_btn = toolbar.add_icon_action(QCoreApplication.translate('SkillsPage', '停用技能'), Icons.get(Icons.PAUSE), self._toggle_skill_enabled)
        toolbar.add_widget(self.update_btn)
        self.delete_btn = toolbar.add_icon_action(
            QCoreApplication.translate('SkillsPage', '删除技能'),
            Icons.get(Icons.TRASH, color=Icons.COLOR_ERROR),
            self._delete_skill,
            danger=True,
        )
        toolbar.add_stretch()
        body.detail_layout.insertWidget(1, toolbar)
        left = body.list_layout
        self.skill_list = configure_settings_resource_list(
            SkillDropListWidget(self._on_skill_sources_dropped)
        )
        self.skill_list.currentItemChanged.connect(self._on_selection_changed)
        body.bind(self.skill_list, self._toggle_skill_enabled)
        left.addWidget(self.skill_list, 1)
        left.addWidget(self.discovery.next_button)

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
        right.addWidget(self.preview, 1)
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
        layout.addWidget(self.extension_details.status_bar)
        self._search_hint()
        self._sync_actions(None)

    def _set_view(self, view: str) -> None:
        self.cancel_pending()
        self._view = view
        self.discovery.enabled = view == "active"
        self.candidates_action.setChecked(view == "candidates")
        self.search.clear()
        label = QCoreApplication.translate('SkillsPage', '搜索候选方法') if view == "candidates" else QCoreApplication.translate('SkillsPage', '搜索技能')
        self.search.setPlaceholderText(label)
        self.search.setAccessibleName(label)
        self.list_body.show_list()
        self._refresh_list()

    def _refresh_list(self) -> None:
        for label in (self.source_label, self.provenance_label, self.description_label):
            label.setVisible(True)
        self.preview.clear()
        self.source_label.setText("")
        self.provenance_label.setText("")
        self.description_label.setText("")
        self._selected_candidate = None
        self.discovery.invalidate()
        candidates = self._view == "candidates"
        self.extension_details.status_bar.setVisible(not candidates)
        for button in (self.add_btn, self.search_button, self.edit_btn, self.toggle_btn, self.delete_btn):
            button.setVisible(not candidates)
        self.back_to_skills_btn.setVisible(candidates)
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
        current = self.skill_list.currentItem()
        selected = current.data(Qt.ItemDataRole.UserRole) if current else None
        query = self.search.text().strip().casefold()
        show_available = bool(query or self._pending_skill)

        self.skill_list.clear()
        self.skill_list.setToolTip(
            QCoreApplication.translate('SkillsPage', '已发现 {value} 个技能\n全局目录：{value_}\n项目目录：{value__}').format(value=len(self._skills), value_=get_global_subdir('skills'), value__=resolve_project_data_root(self._work_dir, data_dir=getattr(self._service, 'data_dir', None)) / 'skills')
        )
        catalog = self.discovery.rows() if show_available else []
        installed_hits = {row["id"] for row in catalog if row.get("installed") and self.discovery.matches(row, query)}
        installed_hits.update(row.get("skill_name") for row in self.discovery.market_rows if row.get("installed"))
        matches = [skill for skill in self._skills
                   if skill.name in installed_hits or query in f"{skill.name} {skill.description}".casefold()]
        for skill in matches:
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
            set_resource_badge(item, resource_badge(kind="skill", installed=True, enabled=enabled))
            item.setData(RESOURCE_DETAIL_META_ROLE, f"{self._source_scope(skill)} · {status}")
            self.skill_list.addItem(item)

        if show_available:
            available = [row for row in catalog if not row.get("installed")]
            if self._pending_skill:
                available = [row for row in available if row["id"] != self._pending_skill["id"]]
                available.insert(0, self._pending_skill)
            self._append_extensions(available)
        if self.skill_list.count() == 0:
            self.source_label.setText(self.tr("未找到匹配技能") if query else QCoreApplication.translate('SkillsPage', '暂无技能'))
            self.description_label.setText(self.tr("按回车搜索市场，或通过“添加”导入技能。") if query else
                QCoreApplication.translate('SkillsPage', '可以添加一个全局技能，或把目录型 SKILL.md 放入项目 .pycat/skills。'))
            self.preview.clear()
            self._sync_actions(None)
            return

        choices = [i for i in range(self.skill_list.count())
                   if self.skill_list.item(i).data(Qt.ItemDataRole.UserRole) is not None]
        restore_row = next((i for i in choices if self.skill_list.item(i).data(Qt.ItemDataRole.UserRole) == selected), choices[0])
        self.skill_list.setCurrentRow(restore_row)

    def _append_extensions(self, rows):
        for row in rows:
            item = QListWidgetItem(extension_text(row, "title"))
            item.setData(Qt.ItemDataRole.UserRole, row)
            for role, key in ((RESOURCE_TITLE_ROLE, "title"), (RESOURCE_DESCRIPTION_ROLE, "description"),
                              (RESOURCE_SUBTITLE_ROLE, "source")):
                item.setData(role, extension_text(row, key))
            set_resource_badge(item, resource_badge(kind="skill", directory=row.get("management") == "directory"))
            item.setToolTip(extension_text(row, "title") + "\n" + extension_text(row, "description"))
            self.skill_list.addItem(item)

    def _query_changed(self):
        if self._view == "candidates":
            query = self.search.text().casefold()
            for i in range(self.skill_list.count()):
                item = self.skill_list.item(i)
                item.setHidden(query not in item.text().casefold())
            return
        if not self.extension_details._job:
            self._search_hint()
        self._render_list()

    def _search_hint(self):
        self.extension_details.status.setText(self.tr("输入筛选已安装技能；回车搜索市场。也可拖入技能文件或目录。"))

    def _set_busy(self, busy):
        self.add_btn.setEnabled(not busy)
        self.skill_list.setEnabled(not busy or self.discovery.searching)
        self.update_btn.setEnabled(self._extensions is not None and not busy)
        if busy:
            for button in (self.edit_btn, self.toggle_btn, self.delete_btn):
                button.setEnabled(False)
        else:
            self._sync_actions(self._current_skill())

    def _skill_prepared(self, row):
        self._pending_skill = row
        self._set_view("active")
        self._render_list()
        for i in range(self.skill_list.count()):
            if self.skill_list.item(i).data(Qt.ItemDataRole.UserRole) == row:
                self.skill_list.setCurrentRow(i)
                self.list_body.show_detail()
                break

    def _extension_changed(self, row):
        for stored in self.discovery.market_rows:
            if stored["id"] == row["id"]:
                stored.update(row)

    def _skills_changed(self):
        row = self.extension_details._selected()
        self._pending_skill = None
        self._refresh_list()
        self._select_skill(row.get("skill_name") or row.get("id", ""))

    def _show_update(self, checked=True):
        skill = self._current_skill()
        if not checked:
            self.detail_stack.setCurrentWidget(self.local_detail)
            return
        if skill:
            self.show_version(skill.name)

    def _update_action_label(self, checked):
        label = self.tr("返回技能内容") if checked else self.tr("版本与更新")
        self.update_btn.setToolTip(label)
        self.update_btn.setAccessibleName(label)

    def show_version(self, name):
        row = next((row for row in self.discovery.catalog() if row["id"] == name), None)
        if row:
            self.extension_details.set_extension(row)
            self.detail_stack.setCurrentWidget(self.extension_details)
            self.update_btn.setChecked(True)
            self.list_body.show_detail()

    def cancel_pending(self):
        self.discovery.cancel_pending()

    def hideEvent(self, event):
        self.cancel_pending()
        super().hideEvent(event)

    def _on_selection_changed(self, current: QListWidgetItem | None, _prev) -> None:
        payload = current.data(Qt.ItemDataRole.UserRole) if current else None
        for label in (self.source_label, self.provenance_label, self.description_label):
            label.setVisible(True)
        self.action_bar.setVisible(self._view == "active" and isinstance(payload, str))
        self.update_btn.setChecked(False)
        self.update_btn.setVisible(self._view == "active" and isinstance(payload, str) and self._extensions is not None)
        if isinstance(payload, dict):
            self.extension_details.set_extension(payload)
            self.detail_stack.setCurrentWidget(self.extension_details)
            self._sync_actions(None)
            return
        self.detail_stack.setCurrentWidget(self.local_detail)
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
        if not payload:
            self.preview.clear()
            self.source_label.setText("")
            self.provenance_label.setText("")
            self.description_label.setText("")
            self._sync_actions(None)
            return
        # Filtering projects the already loaded catalog. Commands below resolve
        # a fresh skill through the service before changing its installation.
        skill = next((skill for skill in self._skills if skill.name == payload), None)
        if skill:
            usage_line = self._usage_summary(skill)
            self.source_label.setToolTip(QCoreApplication.translate('SkillsPage', '文件：{source}').format(source=skill.source))
            self.source_label.setText(usage_line)
            self.source_label.setVisible(bool(usage_line))
            self.provenance_label.setText(self._provenance_summary(skill))
            self.provenance_label.setVisible(bool(self.provenance_label.text()))
            self.description_label.setText(skill.description or "")
            self.description_label.setVisible(bool(skill.description))
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
        if self._view != "active" or item is None or not isinstance(item.data(Qt.ItemDataRole.UserRole), str):
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

    def _add_skill(self, _checked=False, *, source: Path | None = None) -> None:
        dialog = SkillAddDialog(has_project=bool(self._work_dir.strip()), allow_github=self._extensions is not None, parent=self)
        if source is not None:
            dialog.set_local_source(source)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        if values["kind"] == "github":
            if self._extensions:
                self.extension_details.prepare_skill(values["repository"], values["directory"], values["ref"], values["scope"])
            return
        if values["kind"] == "local":
            self._import_skill_source(Path(values["source"]), scope=values["scope"])
            return
        try:
            skill_file = self._service.create_managed(values["name"], description=values["description"],
                                                      scope=values["scope"], work_dir=self._work_dir)
        except Exception as exc:
            QMessageBox.warning(self, QCoreApplication.translate('SkillsPage', '创建技能失败'), str(exc))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(skill_file)))
        self._refresh_list()
        self._select_skill(values["name"])

    def _on_skill_sources_dropped(self, sources: list[Path]) -> None:
        if len(sources) > 1:
            QMessageBox.warning(self, QCoreApplication.translate('SkillsPage', '导入技能'), QCoreApplication.translate('SkillsPage', '一次只能导入一个技能文件或压缩包。'))
        elif sources:
            self._add_skill(source=sources[0])

    def _import_skill_source(self, source: Path, *, scope: str) -> None:
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
