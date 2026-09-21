"""Compact, reusable rows for files, artifacts, and delegated work."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy

from pycat.core.state.artifact import ArtifactService
from pycat.gui.dialogs.content_preview import show_content
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import COMPACT_CONTROL_HEIGHT
from pycat.gui.widgets.capsule import CapsuleLabel


logger = logging.getLogger(__name__)

PathResolver = Callable[[str, str], Path]


def _local_path(path: str, work_dir: str = "") -> Path:
    raw_work_dir = str(work_dir or "").strip()
    if raw_work_dir.startswith("ssh://"):
        raise ValueError("Remote files require a workspace content reference")
    candidate = Path(str(path or "").strip()).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    if not raw_work_dir:
        raise ValueError("relative file paths require an active workspace")
    root = Path(raw_work_dir).expanduser().resolve()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escaped workspace root: {path}") from exc
    return resolved


def _artifact_path(path: str, work_dir: str = "") -> Path:
    raw_work_dir = str(work_dir or "").strip()
    root = ArtifactService.storage_base(raw_work_dir)
    resolved = ArtifactService.resolve_content_path(path, work_dir=raw_work_dir).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"artifact path escaped workspace root: {path}") from exc
    return resolved


def _value(source: object, key: str, default: Any = "") -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def artifact_meta(artifact: object) -> str:
    """Return the short metadata shown in an artifact capsule."""
    kind = str(_value(artifact, "kind") or "").strip()
    status = str(_value(artifact, "status") or "").strip()
    return " · ".join(part for part in (kind, status) if part)


def artifact_tooltip(name: str, artifact: object, *, path: str = "", meta: str = "") -> str:
    """Build the single rich tooltip used by chat and the inspector."""
    title = str(name or _value(artifact, "name") or "未命名产物").strip() or "未命名产物"
    lines = [title]
    if meta:
        lines.append(meta)
    content = str(_value(artifact, "content") or "")
    chars = int(_value(artifact, "content_chars", 0) or len(content))
    if chars:
        lines.append(f"大小: {chars} 字符")
    if path:
        lines.append(f"路径: {path}")

    preview = str(_value(artifact, "abstract") or _value(artifact, "content") or "").strip()
    if preview:
        if len(preview) > 700:
            preview = preview[:699].rstrip() + "…"
        lines.extend(["", "简介:", preview])

    references = [str(item).strip() for item in (_value(artifact, "references", []) or []) if str(item).strip()]
    if references:
        lines.extend(["", "引用: " + "；".join(references[:5])])

    related = [str(item).strip() for item in (_value(artifact, "related", []) or []) if str(item).strip()]
    if related:
        lines.append("相关: " + "；".join(related[:5]))
    if path:
        lines.extend(["", "点击打开文件"])
    return "\n".join(lines)


class WorkflowCapsuleRow(QFrame):
    """One visual and interaction model for secondary workflow outputs."""

    clicked = pyqtSignal(object)

    def __init__(
        self,
        *,
        kind: str = "",
        status: str = "completed",
        payload: object = None,
        file_path: str = "",
        work_dir: str = "",
        path_resolver: PathResolver | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._payload = payload
        self._path = str(file_path or "").strip()
        self._work_dir = str(work_dir or "").strip()
        self._path_resolver = path_resolver or _local_path
        self._interactive = True
        self.setObjectName("tool_capsule_row")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(COMPACT_CONTROL_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self.icon_label = QLabel("")
        self.icon_label.setObjectName("tool_capsule_icon")
        self.icon_label.setFixedSize(16, 16)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self.title_label = CapsuleLabel(maximum=1)
        self.title_label.setObjectName("tool_capsule_title")
        self.title_label.setWordWrap(False)
        self.title_label.setMinimumWidth(0)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.title_label, 1)

        self.meta_label = QLabel("")
        self.meta_label.setObjectName("tool_capsule_meta")
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.meta_label.setWordWrap(False)
        self.meta_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.meta_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.meta_label)
        self.set_kind_status(kind=kind, status=status)

    def set_content(self, *, icon: str | QIcon, title: str, meta: str = "") -> None:
        if isinstance(icon, QIcon):
            self.icon_label.setText("")
            self.icon_label.setPixmap(icon.pixmap(16, 16))
        else:
            self.icon_label.clear()
            self.icon_label.setText(str(icon or ""))
        self.title_label.setText(str(title or ""))
        self.meta_label.setText(str(meta or ""))
        self.meta_label.setVisible(bool(str(meta or "")))
        self.setAccessibleName(" · ".join(part for part in (str(title or ""), str(meta or "")) if part))

    def set_kind_status(self, *, kind: str | None = None, status: str | None = None) -> None:
        if kind is not None:
            value = str(kind or "")
            self.setProperty("kind", value)
            self.icon_label.setProperty("kind", value)
        if status is not None:
            value = str(status or "")
            self.setProperty("status", value)
            self.icon_label.setProperty("status", value)
        for widget in (self, self.icon_label, self.title_label, self.meta_label):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def set_payload(self, payload: object) -> None:
        self._payload = payload

    def set_file_path(self, path: str) -> None:
        self._path = str(path or "").strip()

    def set_work_dir(self, work_dir: str) -> None:
        self._work_dir = str(work_dir or "").strip()

    def set_interactive(self, enabled: bool) -> None:
        self._interactive = bool(enabled)
        self.setCursor(Qt.CursorShape.PointingHandCursor if self._interactive else Qt.CursorShape.ArrowCursor)

    def resolved_path(self) -> Path:
        return self._path_resolver(self._path, self._work_dir)

    def open_file(self) -> None:
        if not self._path:
            return
        try:
            resolved = self.resolved_path()
        except Exception as exc:
            logger.debug("Failed to resolve workflow capsule path %s: %s", self._path, exc)
            show_content(self, error=str(exc))
            return
        # The shared detail validates and reports missing or unreadable files.
        show_content(self, path=resolved)

    def keyPressEvent(self, event):
        if self._interactive and event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self.clicked.emit(self._payload)
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        if self._interactive and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(pos):
            self.clicked.emit(self._payload)
            event.accept()
            return
        super().mouseReleaseEvent(event)


def create_artifact_capsule(
    name: str,
    artifact: object,
    *,
    title: str = "",
    work_dir: str = "",
    payload: object = None,
) -> WorkflowCapsuleRow:
    """Create the canonical clickable artifact row."""
    path = str(_value(artifact, "content_path") or _value(artifact, "path") or "").strip()
    status = str(_value(artifact, "status") or "completed").strip() or "completed"
    visual_status = {
        "failed": "failed",
        "error": "failed",
        "cancelled": "failed",
        "draft": "partial",
        "running": "running",
    }.get(status.lower(), "completed")
    meta = artifact_meta(artifact)
    row = WorkflowCapsuleRow(
        kind="artifact",
        status=visual_status,
        payload=path if payload is None else payload,
        file_path=path,
        work_dir=work_dir,
        path_resolver=_artifact_path,
    )
    row.set_content(
        icon=Icons.get_muted(Icons.FILE_LINES),
        title=str(title or name or _value(artifact, "name") or "未命名产物"),
        meta=meta,
    )
    row.setToolTip(artifact_tooltip(name, artifact, path=path, meta=meta))
    row.set_interactive(bool(path))
    return row
