"""One compact brand/action row shared by the secondary navigation panels."""
from PyQt6.QtCore import QEvent, QSize
from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QToolButton, QWidget

from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import COMPACT_CONTROL_HEIGHT, configure_icon_button


class NavigationHeader(QWidget):
    def __init__(self, icon, label, on_action, on_brand, parent=None):
        super().__init__(parent)
        self.setObjectName('navigation_header')
        self.setFixedHeight(40)
        self._action_icon = icon
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.brand = QPushButton('PyCat')
        self.brand.setObjectName('brand_btn')
        self.brand.setIcon(Icons.brand())
        self.brand.setIconSize(QSize(28, 28))
        self.brand.setToolTip(self.tr('关于 PyCat'))
        self.brand.setAccessibleName(self.tr('关于 PyCat'))
        self.brand.clicked.connect(on_brand)
        row.addWidget(self.brand)
        row.addStretch()
        self.action = QToolButton()
        configure_icon_button(self.action, Icons.get_muted(icon), label)
        # This row follows the 30px navigation controls; opt out of the
        # generic 28px compact-command QSS maximum before pinning geometry.
        self.action.setProperty('compactCommand', False)
        self.action.setFixedSize(COMPACT_CONTROL_HEIGHT, COMPACT_CONTROL_HEIGHT)
        self.action.clicked.connect(on_action)
        row.addWidget(self.action)

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, 'action') and event.type() in {QEvent.Type.PaletteChange, QEvent.Type.StyleChange}:
            self.action.setIcon(Icons.get_muted(self._action_icon))
