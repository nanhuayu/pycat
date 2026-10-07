"""One compact primary navigation rail; secondary panels keep their own content."""
from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import QButtonGroup, QToolButton, QVBoxLayout, QWidget

from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import NAVIGATION_RAIL_WIDTH


class AppNavigation(QWidget):
    selected = pyqtSignal(str)
    new_conversation_requested = pyqtSignal()
    about_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('app_navigation')
        self.setFixedWidth(NAVIGATION_RAIL_WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 8, 7, 8)
        layout.setSpacing(8)
        self.group = QButtonGroup(self)
        self.buttons = {}
        self.brand = self._button(self.tr('PyCat · 主页'), Icons.brand())
        self.brand.clicked.connect(lambda: self.selected.emit('home'))
        layout.addWidget(self.brand)
        self.new_chat = self._button(self.tr('新建对话'), Icons.get_muted(Icons.PLUS))
        self.new_chat.clicked.connect(self.new_conversation_requested.emit)
        layout.addWidget(self.new_chat)
        layout.addSpacing(4)
        for key, title, icon in (('home', self.tr('主页'), Icons.HOME), ('spaces', self.tr('空间'), Icons.BOOKS),
                                 ('tools', self.tr('工具'), Icons.TOOLS), ('settings', self.tr('设置'), Icons.SETTINGS)):
            if key == 'settings':
                layout.addStretch()
            button = self._button(title, Icons.get_muted(icon))
            button.setCheckable(True)
            self.group.addButton(button)
            self.buttons[key] = button
            button.clicked.connect(lambda _checked, name=key: self.selected.emit(name))
            layout.addWidget(button)
        self.about = self._button(self.tr('关于 PyCat'), Icons.get_muted(Icons.CIRCLE_INFO))
        self.about.clicked.connect(self.about_requested.emit)
        layout.addWidget(self.about)
        self.set_current('home')

    def _button(self, title, icon):
        button = QToolButton(self)
        button.setText(title)
        button.setToolTip(title)
        button.setAccessibleName(title)
        button.setIcon(icon)
        button.setIconSize(QSize(20, 20))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setFixedSize(32, 32)
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return button

    def set_current(self, key):
        if key in self.buttons:
            self.buttons[key].setChecked(True)
