"""Manual capture and entry points into the shared MCP configuration."""
from __future__ import annotations

from PyQt6.QtCore import QCoreApplication, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from pycat.gui.utils.form_builder import FormSection
from pycat.gui.utils.icon_manager import Icons


class AutomationPage(QWidget):
    page_title = '电脑与浏览器'
    capture_requested = pyqtSignal()
    configure_requested = pyqtSignal(str)
    shortcuts_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(16)
        capture = FormSection(QCoreApplication.translate('AutomationPage', '屏幕截图'))
        actions = QWidget()
        row = QHBoxLayout(actions)
        row.setContentsMargins(0, 0, 0, 0)
        self.preview_button = QPushButton(QCoreApplication.translate('AutomationPage', '截图并预览'))
        self.preview_button.setIcon(Icons.get(Icons.IMAGE))
        self.preview_button.clicked.connect(self.capture_requested.emit)
        row.addWidget(self.preview_button)
        shortcuts = QPushButton(QCoreApplication.translate('AutomationPage', '设置快捷键'))
        shortcuts.clicked.connect(self.shortcuts_requested.emit)
        row.addWidget(shortcuts)
        row.addStretch()
        capture.form.addRow(actions, info=True)
        self.notice = QLabel(QCoreApplication.translate('AutomationPage', '拖动框选，在选区旁调整尺寸或精确坐标。确认后可复制、保存、贴图、提取文字或加入对话。'))
        self.notice.setWordWrap(True)
        self.notice.setProperty('muted', True)
        capture.form.addRow(self.notice, info=True)
        root.addWidget(capture.group)

        automation = FormSection(QCoreApplication.translate('AutomationPage', 'Agent 操作'))
        for preset, title, detail in (
            ('browser', QCoreApplication.translate('AutomationPage', '浏览器操作'), QCoreApplication.translate('AutomationPage', '在 MCP 的“发现”中安装原生浏览器驱动，或配置其他浏览器 MCP。')),
            ('desktop', QCoreApplication.translate('AutomationPage', '桌面操作'), QCoreApplication.translate('AutomationPage', '查看跨平台桌面驱动及平台要求，再通过 MCP 配置连接。')),
        ):
            row_widget = QWidget()
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            info = QLabel(detail)
            info.setWordWrap(True)
            info.setProperty('muted', True)
            row.addWidget(info, 1)
            button = QPushButton(QCoreApplication.translate('AutomationPage', '配置'))
            button.setAccessibleName(QCoreApplication.translate('AutomationPage', '配置{title}').format(title=title))
            button.clicked.connect(lambda _checked=False, key=preset: self.configure_requested.emit(key))
            setattr(self, preset + '_button', button)
            row.addWidget(button)
            automation.form.addRow(title, row_widget)
        note = QLabel(QCoreApplication.translate('AutomationPage', '连接和启停在 MCP 中管理，工具授权在“运行与权限”中设置。手动截图不改变 Agent 的屏幕访问权限。'))
        note.setWordWrap(True)
        note.setProperty('muted', True)
        automation.form.addRow(note, info=True)
        root.addWidget(automation.group)
        root.addStretch()
