from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QMessageBox,
    QGroupBox,
    QFormLayout,
    QLineEdit,
    QComboBox,
)

from core.config import get_user_modes_json_path, load_user_modes_dict, save_user_modes_dict
from core.modes.defaults import get_primary_mode_slugs
from ui.settings.page_header import build_page_header
from ui.utils.combo_box import configure_combo_popup
from ui.utils.icon_manager import Icons


logger = logging.getLogger(__name__)
from core.modes.manager import ModeManager
from core.modes.types import ModeConfig, ToolCategoryOptions
from core.tools.catalog import normalize_tool_category


def _mode_to_json(mode: ModeConfig) -> Dict[str, Any]:
    allowed_tool_categories: List[Any] = []
    for category in mode.allowed_tool_categories or []:
        if isinstance(category, tuple) and len(category) == 2:
            name = str(category[0])
            opts = category[1]
            if isinstance(opts, ToolCategoryOptions):
                allowed_tool_categories.append(
                    [
                        name,
                        {
                            "fileRegex": (opts.file_regex or "") if getattr(opts, "file_regex", None) else "",
                            "description": (opts.description or "") if getattr(opts, "description", None) else "",
                        },
                    ]
                )
            else:
                allowed_tool_categories.append(name)
        else:
            allowed_tool_categories.append(str(category))

    payload = {
        "slug": mode.slug,
        "name": mode.name,
        "roleDefinition": (mode.role_definition or ""),
        "whenToUse": mode.when_to_use or "",
        "description": mode.description or "",
        "customInstructions": mode.custom_instructions or "",
        "allowed_tool_categories": allowed_tool_categories,
    }
    if getattr(mode, "tool_allowlist", None):
        payload["toolAllowlist"] = list(mode.tool_allowlist or [])
    if getattr(mode, "tool_denylist", None):
        payload["toolDenylist"] = list(mode.tool_denylist or [])
    if getattr(mode, "max_turns", None):
        payload["maxTurns"] = int(mode.max_turns)
    if getattr(mode, "context_window_limit", None):
        payload["contextWindowLimit"] = int(mode.context_window_limit)
    if getattr(mode, "auto_compress_enabled", None) is not None:
        payload["autoCompressEnabled"] = bool(mode.auto_compress_enabled)
    return payload


def _normalize_modes_payload(obj: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    modes = obj.get("modes") if isinstance(obj, dict) else obj
    if not isinstance(modes, list):
        return None, "modes.json 需要是 {\"modes\": [...]} 或者直接是数组"

    out: List[Dict[str, Any]] = []
    for it in modes:
        if not isinstance(it, dict):
            continue
        slug = str(it.get("slug") or "").strip().lower()
        if not slug:
            continue
        out.append(dict(it))
    return out, None


def _core_modes_only(modes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_slug = {
        str(item.get("slug") or "").strip().lower(): dict(item)
        for item in modes
        if isinstance(item, dict) and str(item.get("slug") or "").strip()
    }
    ordered: List[Dict[str, Any]] = []
    for slug in get_primary_mode_slugs():
        mode = by_slug.get(slug)
        if mode is not None:
            ordered.append(mode)
    return ordered


class ModesPage(QWidget):
    """Global modes editor.

    Stores configuration in APPDATA/PyCat/modes.json (user-wide), not per-workspace.
    """

    page_title = "模式"

    def __init__(self, _work_dir_unused: str | None = None, parent=None):
        super().__init__(parent)
        self._modes: List[Dict[str, Any]] = []
        self._current_index: int = -1
        self._setup_ui()
        self.reload_from_disk()

    # ---------------- UI ----------------
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(build_page_header("模式", "管理核心工作模式：Chat、Channel、Agent、Explore、Plan。"))

        path = get_user_modes_json_path()
        self.path_label = QLabel(f"全局配置文件：{path}")
        self.path_label.setWordWrap(True)
        self.path_label.setProperty("muted", True)
        layout.addWidget(self.path_label)

        row = QHBoxLayout()
        btn_open_dir = QPushButton("打开目录")
        btn_open_dir.setIcon(Icons.get(Icons.FOLDER, scale_factor=1.0))
        btn_open_dir.clicked.connect(self._open_config_dir)
        row.addWidget(btn_open_dir)

        btn_reload = QPushButton("重新加载")
        btn_reload.setIcon(Icons.get(Icons.REFRESH, scale_factor=1.0))
        btn_reload.clicked.connect(self.reload_from_disk)
        row.addWidget(btn_reload)

        btn_save = QPushButton("保存")
        btn_save.setProperty("primary", True)
        btn_save.clicked.connect(self._save_clicked)
        row.addWidget(btn_save)

        btn_load_json = QPushButton("载入")
        btn_load_json.setIcon(Icons.get(Icons.IMPORT, scale_factor=1.0))
        btn_load_json.setToolTip("从全局 modes.json 重新载入")
        btn_load_json.clicked.connect(self.reload_from_disk)
        row.addWidget(btn_load_json)

        row.addStretch()
        layout.addLayout(row)

        # ---- Visual tab
        visual = QWidget()
        visual_layout = QVBoxLayout(visual)
        visual_layout.setContentsMargins(0, 0, 0, 0)
        visual_layout.setSpacing(8)
        layout.addWidget(visual, 1)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        header_row.addWidget(QLabel("核心/自定义模式"))

        self.mode_combo = QComboBox()
        configure_combo_popup(self.mode_combo, popup_minimum_width=240)
        self.mode_combo.setObjectName("settings_modes_combo")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_selected)
        self.mode_combo.setMinimumWidth(220)
        header_row.addWidget(self.mode_combo, 1)

        btn_add = QPushButton("新增")
        btn_add.setIcon(Icons.get(Icons.PLUS, scale_factor=1.0))
        btn_add.clicked.connect(self._add_mode)
        header_row.addWidget(btn_add)

        btn_del = QPushButton("删除")
        btn_del.setIcon(Icons.get(Icons.TRASH, color=Icons.COLOR_ERROR, scale_factor=1.0))
        btn_del.setProperty("danger", True)
        btn_del.clicked.connect(self._delete_mode)
        header_row.addWidget(btn_del)

        visual_layout.addLayout(header_row)

        self.mode_summary_label = QLabel("")
        self.mode_summary_label.setWordWrap(True)
        self.mode_summary_label.setProperty("muted", True)
        visual_layout.addWidget(self.mode_summary_label)

        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        form_group = QGroupBox("基础信息")
        form = QFormLayout(form_group)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)

        self.slug_edit = QLineEdit()
        self.slug_edit.setReadOnly(True)
        form.addRow("标识", self.slug_edit)

        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._apply_current_edits)
        form.addRow("显示名称", self.name_edit)

        self.description_edit = QLineEdit()
        self.description_edit.setPlaceholderText("简短说明该模式负责什么")
        self.description_edit.textChanged.connect(self._apply_current_edits)
        form.addRow("简介", self.description_edit)

        self.when_to_use_edit = QTextEdit()
        self.when_to_use_edit.setAcceptRichText(False)
        self.when_to_use_edit.setPlaceholderText("描述什么时候适合切换到该模式")
        self.when_to_use_edit.setMaximumHeight(90)
        self.when_to_use_edit.textChanged.connect(self._apply_current_edits)
        form.addRow("适用场景", self.when_to_use_edit)

        self.allowed_tool_categories_edit = QLineEdit()
        self.allowed_tool_categories_edit.setPlaceholderText("例如：read, search, edit, execute, manage, delegate, extension, mcp")
        self.allowed_tool_categories_edit.setToolTip(
            "可用工具类别: read (文件读取), search (搜索/获取), edit (文件编辑), execute (Shell/Python 执行), "
            "manage (状态/待办/产物管理), delegate (子任务委托), extension (扩展能力), mcp (MCP 服务)"
        )
        self.allowed_tool_categories_edit.textChanged.connect(self._apply_current_edits)
        form.addRow("允许工具类别", self.allowed_tool_categories_edit)

        self.role_edit = QTextEdit()
        self.role_edit.setAcceptRichText(False)
        self.role_edit.setPlaceholderText("角色定义 / System Prompt（高级项，通常只需调整简介和适用场景）")
        self.role_edit.textChanged.connect(self._apply_current_edits)
        self.role_edit.setMinimumHeight(160)
        form.addRow("角色定义", self.role_edit)

        self.custom_edit = QTextEdit()
        self.custom_edit.setAcceptRichText(False)
        self.custom_edit.setPlaceholderText("附加指令（可选，会附加在 system prompt 的 Custom Instructions 区域）")
        self.custom_edit.textChanged.connect(self._apply_current_edits)
        self.custom_edit.setMinimumHeight(90)
        form.addRow("附加指令", self.custom_edit)

        right_layout.addWidget(form_group)

        hint = QLabel(
            "建议只保留 Chat / Channel / Agent / Explore / Plan 五个核心模式；不同模式只声明允许工具类别。\n"
            "Code / Debug / Orchestrator 等旧模式仍可作为高级配置存在，但不会默认占用主界面。"
        )
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        right_layout.addWidget(hint)
        right_layout.addStretch(1)

        visual_layout.addLayout(right_layout, 1)

        self.json_edit = QTextEdit()
        self.json_edit.setObjectName("settings_modes_json")
        self.json_edit.setAcceptRichText(False)
        self.json_edit.setVisible(False)

    # ---------------- Data ----------------
    def _open_config_dir(self) -> None:
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(get_user_modes_json_path().parent)))
        except Exception as exc:
            logger.debug("Failed to open modes config directory: %s", exc)

    def reload_from_disk(self) -> None:
        try:
            mm = ModeManager(None)
            modes = [_mode_to_json(m) for m in mm.list_ui_modes()]
        except Exception:
            data = load_user_modes_dict()
            modes, _err = _normalize_modes_payload(data) if data else (None, None)
            modes = list(modes or [])

        self._modes = _core_modes_only(list(modes or []))
        self._rebuild_combo()
        self._sync_json_from_visual()

        if self.mode_combo.count() > 0:
            self.mode_combo.setCurrentIndex(0)
            self._current_index = self.mode_combo.currentIndex()
            self._load_current_into_form()

    def _rebuild_combo(self) -> None:
        cur_slug = ""
        if 0 <= self._current_index < len(self._modes):
            cur_slug = str(self._modes[self._current_index].get("slug") or "")

        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        for m in self._modes:
            slug = str(m.get("slug") or "")
            name = str(m.get("name") or slug)
            self.mode_combo.addItem(f"{name} ({slug})", slug)
        self.mode_combo.blockSignals(False)

        # restore selection
        if cur_slug:
            idx = self.mode_combo.findData(cur_slug)
            if idx >= 0:
                self.mode_combo.setCurrentIndex(idx)

    def _on_mode_selected(self, row: int) -> None:
        self._current_index = int(row)
        self._load_current_into_form()

    def _load_current_into_form(self) -> None:
        if self._current_index < 0 or self._current_index >= len(self._modes):
            self.slug_edit.setText("")
            self.name_edit.setText("")
            self.description_edit.setText("")
            self.when_to_use_edit.setPlainText("")
            self.allowed_tool_categories_edit.setText("")
            self.role_edit.setPlainText("")
            self.custom_edit.setPlainText("")
            self.mode_summary_label.setText("")
            return

        m = self._modes[self._current_index]
        self.slug_edit.setText(str(m.get("slug") or ""))
        self.name_edit.blockSignals(True)
        self.description_edit.blockSignals(True)
        self.when_to_use_edit.blockSignals(True)
        self.allowed_tool_categories_edit.blockSignals(True)
        self.role_edit.blockSignals(True)
        self.custom_edit.blockSignals(True)
        try:
            self.name_edit.setText(str(m.get("name") or ""))
            self.description_edit.setText(str(m.get("description") or ""))
            self.when_to_use_edit.setPlainText(str(m.get("whenToUse") or ""))

            allowed_tool_categories = m.get("allowed_tool_categories")
            if isinstance(allowed_tool_categories, list):
                flat: List[str] = []
                for category in allowed_tool_categories:
                    if isinstance(category, str):
                        flat.append(category)
                    elif isinstance(category, list) and category:
                        flat.append(str(category[0]))
                self.allowed_tool_categories_edit.setText(", ".join([x for x in flat if x]))
            else:
                self.allowed_tool_categories_edit.setText("")

            self.role_edit.setPlainText(str(m.get("roleDefinition") or ""))
            self.custom_edit.setPlainText(str(m.get("customInstructions") or ""))
        finally:
            self.name_edit.blockSignals(False)
            self.description_edit.blockSignals(False)
            self.when_to_use_edit.blockSignals(False)
            self.allowed_tool_categories_edit.blockSignals(False)
            self.role_edit.blockSignals(False)
            self.custom_edit.blockSignals(False)

        summary_name = str(m.get("name") or m.get("slug") or "未命名模式")
        summary_slug = str(m.get("slug") or "")
        summary_desc = str(m.get("description") or "")
        allowed_tool_categories = m.get("allowed_tool_categories") if isinstance(m.get("allowed_tool_categories"), list) else []
        category_count = len(allowed_tool_categories)
        summary = f"当前模式：{summary_name} ({summary_slug}) · {category_count} 个允许工具类别"
        if summary_desc:
            summary += f"\n{summary_desc}"
        self.mode_summary_label.setText(summary)

    def _apply_current_edits(self) -> None:
        if self._current_index < 0 or self._current_index >= len(self._modes):
            return

        m = self._modes[self._current_index]
        m["name"] = (self.name_edit.text() or "").strip()
        m["description"] = (self.description_edit.text() or "").strip()
        m["whenToUse"] = (self.when_to_use_edit.toPlainText() or "").strip()

        categories_raw = (self.allowed_tool_categories_edit.text() or "").strip()
        allowed_tool_categories: List[Any] = []
        if categories_raw:
            for part in categories_raw.split(","):
                category = normalize_tool_category(part.strip())
                if category and category not in allowed_tool_categories:
                    allowed_tool_categories.append(category)
        m["allowed_tool_categories"] = allowed_tool_categories

        m["roleDefinition"] = (self.role_edit.toPlainText() or "").strip()
        m["customInstructions"] = (self.custom_edit.toPlainText() or "").strip()

        # Update combo display text
        slug = str(m.get("slug") or "")
        name = str(m.get("name") or slug)
        if 0 <= self._current_index < self.mode_combo.count():
            self.mode_combo.blockSignals(True)
            self.mode_combo.setItemText(self._current_index, f"{name} ({slug})")
            self.mode_combo.blockSignals(False)
        desc = str(m.get("description") or "")
        self.mode_summary_label.setText(
            f"当前模式：{name or slug} ({slug}) · {len(allowed_tool_categories)} 个允许工具类别"
            + (f"\n{desc}" if desc else "")
        )

    def _add_mode(self) -> None:
        QMessageBox.information(self, "已精简", "当前仅保留 Chat / Channel / Agent / Explore / Plan 五个核心模式。")

    def _delete_mode(self) -> None:
        if self._current_index < 0 or self._current_index >= len(self._modes):
            return
        slug = str(self._modes[self._current_index].get("slug") or "")
        if slug in set(get_primary_mode_slugs()):
            QMessageBox.information(self, "禁止删除", "核心模式固定保留：Chat / Channel / Agent / Explore / Plan。")
            return
        if QMessageBox.question(self, "删除", f"确定删除 {slug}？") != QMessageBox.StandardButton.Yes:
            return
        del self._modes[self._current_index]
        self._current_index = -1
        self._rebuild_combo()
        if self.mode_combo.count() > 0:
            self.mode_combo.setCurrentIndex(0)
        self._sync_json_from_visual()

    def _sync_json_from_visual(self) -> None:
        payload = {"modes": list(self._modes or [])}
        self.json_edit.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))

    def _apply_json_to_visual(self) -> None:
        raw = (self.json_edit.toPlainText() or "").strip()
        if not raw:
            QMessageBox.warning(self, "错误", "JSON 不能为空")
            return
        try:
            obj = json.loads(raw)
        except Exception as e:
            QMessageBox.warning(self, "错误", f"JSON 解析失败：{e}")
            return

        modes, err = _normalize_modes_payload(obj)
        if err:
            QMessageBox.warning(self, "错误", err)
            return

        self._modes = list(modes or [])
        self._modes = _core_modes_only(self._modes)
        self._rebuild_combo()
        if self.mode_combo.count() > 0:
            self.mode_combo.setCurrentIndex(0)

    # ---------------- Public API ----------------
    def validate(self, raw_json: str) -> bool:
        raw = (raw_json or "").strip()
        if not raw:
            QMessageBox.warning(self, "错误", "modes.json 内容不能为空")
            return False
        try:
            obj = json.loads(raw)
        except Exception as e:
            QMessageBox.warning(self, "错误", f"modes.json 不是合法 JSON：{e}")
            return False

        modes, err = _normalize_modes_payload(obj)
        if err:
            QMessageBox.warning(self, "错误", err)
            return False

        # ensure base modes exist
        slugs = {str(m.get("slug") or "").strip().lower() for m in (modes or [])}
        required = set(get_primary_mode_slugs())
        missing = [slug for slug in get_primary_mode_slugs() if slug not in slugs]
        if missing:
            QMessageBox.warning(self, "错误", "modes.json 必须包含核心模式：" + ", ".join(missing))
            return False
        return True

    def save_to_disk(self) -> bool:
        self._sync_json_from_visual()
        raw = (self.json_edit.toPlainText() or "").strip()

        if not self.validate(raw):
            return False

        try:
            obj = json.loads(raw)
        except Exception:
            return False

        modes, err = _normalize_modes_payload(obj)
        if err:
            QMessageBox.warning(self, "错误", err)
            return False
        core_payload = {"modes": _core_modes_only(list(modes or []))}
        if len(core_payload["modes"]) < len(get_primary_mode_slugs()):
            QMessageBox.warning(self, "错误", "保存失败：核心模式不完整。")
            return False

        # If saved from JSON, refresh the visual model so UI stays consistent.
        self._modes = list(core_payload["modes"])
        self._rebuild_combo()
        self._sync_json_from_visual()
        if self.mode_combo.count() > 0:
            self.mode_combo.setCurrentIndex(0)

        ok = save_user_modes_dict(core_payload)
        if not ok:
            QMessageBox.warning(self, "错误", "写入全局 modes.json 失败（请检查权限）")
        return ok

    def _save_clicked(self) -> None:
        if self.save_to_disk():
            QMessageBox.information(self, "已保存", "全局模式配置已写入。")
