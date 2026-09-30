"""Resource settings compose their catalog and editor in a single page."""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from pycat.gui.resources.mcp_page import McpPage
from pycat.gui.resources.skills_page import SkillsPage


class ResourcePage(QWidget):
    changed = pyqtSignal()

    def __init__(self, *, kind, mcp_servers=(), skill_service=None, extension_service=None,
                 work_dir="", reload_provider=None, connection_tester=None,
                 candidate_evaluator=None, parent=None):
        super().__init__(parent)
        self.kind = kind
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        if kind == "skill":
            self.content = SkillsPage(work_dir=work_dir, skill_service=skill_service,
                extension_service=extension_service, candidate_evaluator=candidate_evaluator, show_header=False)
        else:
            self.content = McpPage(servers=mcp_servers, reload_provider=reload_provider,
                connection_tester=connection_tester, extension_service=extension_service,
                work_dir=work_dir, show_header=False)
            self.content.editor.changed.connect(self.changed)
        self.content.layout().setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.content, 1)

    def select_extension(self, extension_id):
        if self.kind == "skill":
            self.content.show_version(extension_id)
        else:
            self.content.editor.select_extension(extension_id)

    def cancel_pending(self):
        if self.kind == "skill":
            self.content.cancel_pending()
        else:
            self.content.editor.cancel_pending()
