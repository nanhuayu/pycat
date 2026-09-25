"""Unified, bounded Inspector navigation for two delegation lifetimes."""
from PyQt6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, QEvent, Qt, pyqtSignal
from PyQt6.QtWidgets import QListWidgetItem, QMenu, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import COMPACT_ICON_BUTTON_SIZE, configure_icon_button, prepare_context_menu
from pycat.gui.view_models.delegated_tasks import DelegatedTaskView
from pycat.gui.widgets.capsule import CapsuleDelegate, CapsuleList, capsule_height
from pycat.gui.widgets.collapsible_section import CollapsibleSection


class DelegatedTasksWidget(QWidget):
    operation_requested = pyqtSignal(str, object)
    PAGE_SIZE = 6
    STATUS = {'queued': QT_TRANSLATE_NOOP('DelegatedTasksWidget', '排队中'), 'running': QT_TRANSLATE_NOOP('DelegatedTasksWidget', '运行中'), 'completed': QT_TRANSLATE_NOOP('DelegatedTasksWidget', '已完成'),
              'interrupted': QT_TRANSLATE_NOOP('DelegatedTasksWidget', '待检查'), 'cancelled': QT_TRANSLATE_NOOP('DelegatedTasksWidget', '已停止'), 'failed': QT_TRANSLATE_NOOP('DelegatedTasksWidget', '执行失败'), 'failed_partial': QT_TRANSLATE_NOOP('DelegatedTasksWidget', '部分失败')}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.conversation_id = ''
        self._independent, self._subtasks = (), ()
        self._offsets = [0, 0]
        self._groups = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        for index, title in enumerate((QCoreApplication.translate('DelegatedTasksWidget', '协作任务'), QCoreApplication.translate('DelegatedTasksWidget', '已结束协作'))):
            section = CollapsibleSection(title, collapsed=bool(index))
            section.body_layout.setContentsMargins(0, 0, 0, 0)
            section.header.layout().setContentsMargins(0, 0, 0, 0)
            section.header.layout().setSpacing(4)
            section.toggle_btn.setFixedSize(COMPACT_ICON_BUTTON_SIZE, COMPACT_ICON_BUTTON_SIZE)
            section.toggle_btn.setAccessibleName(title)
            section.summary_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            more = QToolButton()
            configure_icon_button(more, Icons.get_muted(Icons.MORE), QCoreApplication.translate('DelegatedTasksWidget', '所选任务的更多操作'))
            section.header.layout().addWidget(more)
            items = CapsuleList()
            items.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            items.itemClicked.connect(lambda item: self._operate('open', item))
            items.itemActivated.connect(lambda item: self._operate('open', item))
            items.populate_menu = lambda menu, item: self._populate_menu(menu, item)
            menu = prepare_context_menu(QMenu(more), self)
            menu.aboutToShow.connect(lambda menu=menu, view=items: self._selected_menu(menu, view))
            more.setMenu(menu)
            more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            next_page = QToolButton()
            next_page.setAutoRaise(True)
            next_page.clicked.connect(lambda _=False, index=index: self._next_page(index))
            section.body_layout.addWidget(items)
            section.body_layout.addWidget(next_page)
            layout.addWidget(section)
            self._groups.append((section, items, next_page))
        self.hide()

    def set_conversation(self, conversation_id):
        if self.conversation_id != conversation_id:
            self.conversation_id = conversation_id
            self._independent, self._subtasks = (), ()
            self._offsets = [0, 0]
            self._groups[1][0].set_collapsed(True)
            self._render()

    def set_tasks(self, tasks, *, kind, conversation_id):
        if self.conversation_id != conversation_id:
            return
        tasks = tuple(tasks)
        if kind == 'independent':
            if tasks == self._independent:
                return
            self._independent = tasks
        else:
            if tasks == self._subtasks:
                return
            self._subtasks = tasks
        self._render()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, '_groups'):
            self._render()

    def _partition(self):
        tasks = (*self._independent, *self._subtasks)
        ended = {'completed', 'cancelled'}
        return ([task for task in tasks if task.status not in ended],
                [task for task in tasks if task.status in ended])

    def _render(self):
        groups = self._partition()
        for index, (section, items, next_page) in enumerate(self._groups):
            tasks = groups[index]
            offset = min(self._offsets[index], max(0, (len(tasks) - 1) // self.PAGE_SIZE * self.PAGE_SIZE))
            self._offsets[index] = offset
            visible = tasks[offset:offset + self.PAGE_SIZE]
            wanted = {task.key for task in visible}
            for row in reversed(range(items.count())):
                if items.item(row).data(Qt.ItemDataRole.UserRole).key not in wanted:
                    items.takeItem(row)
            existing = {items.item(row).data(Qt.ItemDataRole.UserRole).key: items.item(row) for row in range(items.count())}
            for row, task in enumerate(visible):
                item = existing.get(task.key)
                if item is None:
                    item = QListWidgetItem()
                    items.insertItem(row, item)
                elif items.row(item) != row:
                    items.insertItem(row, items.takeItem(items.row(item)))
                kind = QCoreApplication.translate('DelegatedTasksWidget', '独立任务') if task.kind == 'independent' else QCoreApplication.translate('DelegatedTasksWidget', '子任务')
                detail = f"{kind} · {QCoreApplication.translate('DelegatedTasksWidget', self.STATUS.get(task.status, task.status))}"
                item.setText(task.title)
                item.setIcon(Icons.get_muted(Icons.BOT))
                item.setData(CapsuleDelegate.DetailRole, detail)
                item.setData(Qt.ItemDataRole.UserRole, task)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, f'{task.title}，{detail}')
                lifetime = QCoreApplication.translate('DelegatedTasksWidget', '打开独立会话；可单独停止或继续。') if task.kind == 'independent' else QCoreApplication.translate('DelegatedTasksWidget', '打开父会话中的执行过程；停止父运行会结束子任务。')
                item.setToolTip(f'{task.title}\n{detail}\n{lifetime}')
            if items.currentRow() < 0 and items.count():
                items.setCurrentRow(0)
            items.setFixedHeight(len(visible) * (capsule_height(self.fontMetrics(), 2) + 4) + 2)
            section.set_summary(QCoreApplication.translate('DelegatedTasksWidget', '{value} 项').format(value=len(tasks)))
            section.setVisible(bool(tasks))
            next_page.setVisible(len(tasks) > self.PAGE_SIZE)
            next_page.setText(QCoreApplication.translate('DelegatedTasksWidget', '下一页 · {value}/{value_}').format(value=offset // self.PAGE_SIZE + 1, value_=max(1, (len(tasks) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)))
        self.setVisible(bool(groups[0] or groups[1]))

    def _next_page(self, index):
        count = len(self._partition()[index])
        offset = self._offsets[index] + self.PAGE_SIZE
        self._offsets[index] = offset if offset < count else 0
        self._render()

    def _operate(self, action, item):
        if item is not None:
            self._emit_current(action, item.data(Qt.ItemDataRole.UserRole))

    def _emit_current(self, action, target: DelegatedTaskView):
        # A menu opened before a refresh or conversation switch cannot redirect.
        if target in (*self._independent, *self._subtasks):
            self.operation_requested.emit(action, target)

    def _selected_menu(self, menu, view):
        menu.clear()
        item = view.currentItem()
        if item is not None:
            self._populate_menu(menu, item)

    def _populate_menu(self, menu, item):
        target = item.data(Qt.ItemDataRole.UserRole)
        source_id = self.conversation_id
        def emit(action):
            if self.conversation_id == source_id:
                self._emit_current(action, target)
        label = QCoreApplication.translate('DelegatedTasksWidget', '查看结果') if target.kind == 'independent' and target.message_id else QCoreApplication.translate('DelegatedTasksWidget', '打开独立会话') if target.kind == 'independent' else QCoreApplication.translate('DelegatedTasksWidget', '查看子任务过程')
        menu.addAction(label, lambda: emit('open'))
        if target.kind == 'independent' and target.status != 'completed':
            action = 'cancel' if target.status in {'queued', 'running'} else 'resume'
            menu.addAction(QCoreApplication.translate('DelegatedTasksWidget', '停止任务') if action == 'cancel' else QCoreApplication.translate('DelegatedTasksWidget', '继续任务'), lambda: emit(action))
