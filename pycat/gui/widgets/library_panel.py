"""Spaces: bounded library metadata, lazy topic navigation and the shared content workbench."""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QSize, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import COMPACT_CONTROL_HEIGHT, configure_icon_button, prepare_context_menu
from pycat.gui.widgets.navigation_header import NavigationHeader
from pycat.gui.widgets.themed_line_edit import ThemedLineEdit


class TopicTree(QTreeWidget):
    """Expand one metadata page at a time; depth is not fixed or preloaded."""
    failed = pyqtSignal(str)
    add_requested = pyqtSignal(str, object)

    def __init__(self, library, parent=None, *, restore=True, actions=True):
        super().__init__(parent)
        self.library, self._generation, self._jobs = library, 0, []
        self._restore, self._expanded_ids, self._state_loaded = restore, set(), False
        self._actions, self._selected_key = actions, None
        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(250)
        self._state_timer.timeout.connect(self._save_expanded)
        self.setHeaderHidden(True)
        self.setObjectName('library_topics')
        self.setFrameShape(self.Shape.NoFrame)
        self.setMinimumWidth(120)
        self.setUniformRowHeights(True)
        self.setSelectionBehavior(self.SelectionBehavior.SelectRows)
        if actions:
            self.setColumnCount(2)
            self.header().setStretchLastSection(False)
            self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            self.setColumnWidth(1, COMPACT_CONTROL_HEIGHT)
        self.itemExpanded.connect(self._expanded)
        self.itemClicked.connect(self._clicked)
        self.itemCollapsed.connect(self._collapsed)

    def refresh(self, *, shortcuts=True):
        current = self.currentItem()
        if current:
            self._selected_key = current.data(0, Qt.ItemDataRole.UserRole)
        elif shortcuts:
            self._selected_key = ('all', '')
        self._generation += 1
        for job in self._jobs:
            job.abandon()
        self._jobs.clear()
        self.clear()
        if shortcuts:
            for key, title, icon in (('all', self.tr('全部资料'), Icons.BOOKS), ('favorites', self.tr('收藏'), Icons.STAR)):
                item = QTreeWidgetItem([title])
                item.setData(0, Qt.ItemDataRole.UserRole, (key, ''))
                item.setIcon(0, Icons.get_muted(icon))
                item.setSizeHint(0, QSize(0, 36))
                if key == 'all':
                    self._add_action(item)
                self.addTopLevelItem(item)
                if item.data(0, Qt.ItemDataRole.UserRole) == self._selected_key:
                    self.setCurrentItem(item)
        self._load(None, '')

    def _add_action(self, item):
        if self._actions:
            item.setIcon(1, Icons.get_muted(Icons.PLUS))
            item.setToolTip(1, self.tr('添加资料或子主题'))

    def _expanded(self, item):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and data[0] == 'topic':
            self._expanded_ids.add(data[1])
            if self._restore:
                self._state_timer.start()
            if not item.data(0, Qt.ItemDataRole.UserRole + 1):
                self._load(item, data[1])

    def _collapsed(self, item):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and data[0] == 'topic':
            self._expanded_ids.discard(data[1])
            if self._restore:
                self._state_timer.start()

    def _save_expanded(self):
        if self._restore:
            value = sorted(self._expanded_ids)[:1000]
            job = BackgroundJob(lambda: self.library.save_view_state('expanded_topics', value))
            QThreadPool.globalInstance().start(job)

    def _clicked(self, item, column):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if self._actions and column == 1 and data and data[0] in {'all', 'topic'}:
            point = QPoint(self.header().sectionViewportPosition(1), self.visualItemRect(item).bottom())
            self.add_requested.emit(data[1], self.viewport().mapToGlobal(point))
            return
        if data and data[0] == 'more':
            parent = item.parent()
            self._load(parent, data[1], offset=data[2])
            if parent:
                parent.removeChild(item)
            else:
                self.takeTopLevelItem(self.indexOfTopLevelItem(item))

    def _load(self, parent, identifier, *, offset=0):
        generation = self._generation
        if parent:
            parent.setData(0, Qt.ItemDataRole.UserRole + 1, True)
        def operation():
            state = self.library.view_state('expanded_topics') if self._restore and not self._state_loaded else None
            return self.library.topics(identifier, offset=offset, limit=200), state
        job = BackgroundJob(operation)
        self._jobs.append(job)
        def complete(result, error):
            if generation != self._generation:
                return
            self._jobs.remove(job)
            if error:
                if parent:
                    parent.setData(0, Qt.ItemDataRole.UserRole + 1, False)
                self.failed.emit(str(error))
                return
            topics, state = result
            if not self._state_loaded:
                self._expanded_ids = set(state or [])
                self._state_loaded = True
            for topic in topics:
                item = QTreeWidgetItem([topic.title])
                item.setData(0, Qt.ItemDataRole.UserRole, ('topic', topic.id))
                item.setIcon(0, Icons.get_muted(Icons.FOLDER))
                item.setSizeHint(0, QSize(0, 36))
                self._add_action(item)
                if topic.has_children:
                    item.setChildIndicatorPolicy(item.ChildIndicatorPolicy.ShowIndicator)
                parent.addChild(item) if parent else self.addTopLevelItem(item)
                if item.data(0, Qt.ItemDataRole.UserRole) == self._selected_key:
                    self.setCurrentItem(item)
                if topic.id in self._expanded_ids:
                    item.setExpanded(True)
            if len(topics) == 200:
                more = QTreeWidgetItem([self.tr('加载更多…')])
                more.setData(0, Qt.ItemDataRole.UserRole, ('more', identifier, offset + 200))
                more.setSizeHint(0, QSize(0, 36))
                parent.addChild(more) if parent else self.addTopLevelItem(more)
        job.signals.finished.connect(complete)
        QThreadPool.globalInstance().start(job)

    def dispose(self):
        self._state_timer.stop()
        self._save_expanded()
        self._generation += 1
        for job in self._jobs:
            job.abandon()
        self._jobs.clear()


class LibraryPanel(QWidget):
    _changed = pyqtSignal()
    return_to_chat = pyqtSignal()
    about_requested = pyqtSignal()
    content_requested = pyqtSignal(str)
    file_created = pyqtSignal(str)

    def __init__(self, services, parent=None):
        super().__init__(parent)
        self.services, self.library = services, services.library_service
        self.topic_id, self.favorites, self.offset = '', False, 0
        self._job, self._generation = None, 0
        self._mutation_jobs = []
        self._seen_revision = -1
        self.setObjectName('library_panel')
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter()
        self.splitter.setHandleWidth(1)
        root.addWidget(self.splitter)
        self.topic_navigation = QWidget()
        self.topic_navigation.setObjectName('library_navigation')
        self.topic_navigation.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.topic_navigation.setMinimumWidth(180)
        self.topic_navigation.setMaximumWidth(320)
        navigation = QVBoxLayout(self.topic_navigation)
        navigation.setContentsMargins(10, 8, 10, 10)
        navigation.setSpacing(8)
        self.header = NavigationHeader(Icons.ARROW_LEFT, self.tr('返回会话'), self.return_to_chat, self.about_requested)
        navigation.addWidget(self.header)
        self.splitter.addWidget(self.topic_navigation)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(12, 10, 12, 10)
        heading = QHBoxLayout()
        title = QLabel(self.tr('资料库'))
        title.setObjectName('page_title')
        heading.addWidget(title)
        heading.addStretch()
        self.search = ThemedLineEdit()
        self.search.setPlaceholderText(self.tr('搜索资料名称'))
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(400)
        heading.addWidget(self.search, 1)
        self.add = QToolButton()
        configure_icon_button(self.add, Icons.get(Icons.PLUS), self.tr('添加资料或主题'))
        menu = self.topic_add_menu(None)
        self.add.setMenu(menu)
        self.add.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        heading.addWidget(self.add)
        self.topic_toggle = QToolButton()
        configure_icon_button(self.topic_toggle, Icons.get(Icons.PANEL_LEFT), self.tr('显示或收起主题'))
        self.topic_toggle.clicked.connect(lambda: self.topic_navigation.setVisible(not self.topic_navigation.isVisible()))
        heading.addWidget(self.topic_toggle)
        body.addLayout(heading)
        self.tree = TopicTree(self.library)
        self.tree.failed.connect(lambda message: self.status.setText(message))
        self.tree.itemClicked.connect(self._select_topic)
        self.tree.add_requested.connect(self.show_topic_add)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.topic_menu)
        navigation.addWidget(self.tree, 1)
        listing = QVBoxLayout()
        listing.setContentsMargins(8, 0, 0, 0)
        self.table = QTableWidget(0, 4)
        self.table.setObjectName('library_items')
        self.table.setFrameShape(self.table.Shape.NoFrame)
        self.table.setShowGrid(False)
        self.table.setSelectionMode(self.table.SelectionMode.SingleSelection)
        self.table.setHorizontalHeaderLabels(['', self.tr('名称'), self.tr('来源'), self.tr('最近修改')])
        self.table.setSelectionBehavior(self.table.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(self.table.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 38)
        self.table.setColumnWidth(2, 160)
        self.table.setColumnWidth(3, 130)
        self.table.cellDoubleClicked.connect(lambda row, column: self.open_item(row) if column else None)
        self.table.cellClicked.connect(self._cell_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.item_menu)
        listing.addWidget(self.table, 1)
        pages = QHBoxLayout()
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setProperty('muted', True)
        pages.addWidget(self.status, 1)
        self.previous, self.next = QPushButton(self.tr('上一页')), QPushButton(self.tr('下一页'))
        self.previous.setFixedHeight(COMPACT_CONTROL_HEIGHT)
        self.next.setFixedHeight(COMPACT_CONTROL_HEIGHT)
        self.previous.clicked.connect(lambda: self.change_page(-100))
        self.next.clicked.connect(lambda: self.change_page(100))
        pages.addWidget(self.previous)
        pages.addWidget(self.next)
        listing.addLayout(pages)
        body.addLayout(listing, 1)
        self.splitter.addWidget(content)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([232, 1000])
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(self._search)
        self.search.textEdited.connect(lambda _: self.search_timer.start())
        self._changed.connect(self._refresh_on_change)
        self._unsubscribe = services.app_coordinator.store.subscribe(self._changed.emit)
        self.tree.refresh()
        self.refresh()

    def _refresh_on_change(self):
        state = self.services.app_coordinator.store.get_state()
        if state.content_revision != self._seen_revision and 'library' in state.changed_domains:
            self._seen_revision = state.content_revision
            self.refresh()

    def _search(self):
        self.offset = 0
        self.refresh()

    def show_favorites(self):
        self.tree.setCurrentItem(self.tree.topLevelItem(1))
        self._select_scope(('favorites', ''))

    def _select_topic(self, item, column):
        if column != 0:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] == 'more':
            return
        self._select_scope(data)

    def _select_scope(self, data):
        self.topic_id = data[1] if data[0] == 'topic' else ''
        self.favorites = data[0] == 'favorites'
        self.offset = 0
        self.refresh()

    def refresh(self):
        if self._job:
            self._job.abandon()
        self._generation += 1
        generation = self._generation
        query = dict(topic_id=self.topic_id, favorites=self.favorites,
                     query=self.search.text().strip(), offset=self.offset, limit=100)
        job = BackgroundJob(lambda: self.library.items(**query))
        self._job = job
        def complete(page, error):
            if generation != self._generation:
                return
            self._job = None
            if error:
                self.status.setText(str(error))
                return
            self.table.setRowCount(len(page.items))
            for row, record in enumerate(page.items):
                star = QTableWidgetItem()
                star.setIcon(Icons.get(Icons.STAR, color=Icons.COLOR_PRIMARY if record['favorite'] else Icons.COLOR_MUTED))
                star.setToolTip(self.tr('取消收藏') if record['favorite'] else self.tr('收藏'))
                self.table.setItem(row, 0, star)
                name = QTableWidgetItem(record['title'])
                name.setIcon(Icons.get_muted(Icons.FILE))
                name.setData(Qt.ItemDataRole.UserRole, record)
                name.setToolTip(record['ref'].get('ref', ''))
                self.table.setItem(row, 1, name)
                source = self.tr('资料库文件') if record['owned'] else {
                    'file': self.tr('本机文件'), 'workspace': self.tr('项目文件'), 'wiki': self.tr('项目知识'),
                    'artifact': self.tr('会话产物'), 'input': self.tr('会话附件'), 'archive': self.tr('归档内容'),
                    'library': self.tr('资料库')}.get(record['ref']['kind'], self.tr('引用'))
                self.table.setItem(row, 2, QTableWidgetItem(source))
                self.table.setItem(row, 3, QTableWidgetItem(record['updated_at'][:16].replace('T', ' ')))
            self.status.setText(self.tr('共 {count} 项').format(count=page.total))
            self.previous.setEnabled(page.offset > 0)
            self.next.setEnabled(page.has_next)
            self.previous.setVisible(page.offset > 0 or page.has_next)
            self.next.setVisible(page.offset > 0 or page.has_next)
        job.signals.finished.connect(complete)
        QThreadPool.globalInstance().start(job)

    def change_page(self, delta):
        self.offset = max(0, self.offset + delta)
        self.refresh()

    def _mutation(self, operation, *, topics=False, on_success=None):
        job = BackgroundJob(operation)
        self._mutation_jobs.append(job)
        def complete(result, error):
            if job not in self._mutation_jobs:
                return
            self._mutation_jobs.remove(job)
            if error:
                self.status.setText(str(error))
            else:
                if on_success:
                    on_success(result)
                if topics:
                    self.tree.refresh()
                self.refresh()
        job.signals.finished.connect(complete)
        QThreadPool.globalInstance().start(job)

    def add_files(self, copy, *, topic_id=None):
        topic = self.topic_id if topic_id is None else topic_id
        paths, _ = QFileDialog.getOpenFileNames(self, self.tr('添加资料'))
        if paths:
            operation = self.library.import_file if copy else self.library.add_file
            self._mutation(lambda: [operation(path, topic_id=topic) for path in paths])

    def create_topic(self, parent_id=None):
        parent_id = self.topic_id if parent_id is None else parent_id
        title, accepted = QInputDialog.getText(self, self.tr('新建主题'), self.tr('主题名称'))
        if accepted and title.strip():
            self._mutation(lambda: self.library.create_topic(title, parent_id), topics=True)

    def create_file(self, topic_id=None):
        topic = self.topic_id if topic_id is None else topic_id
        favorite = self.favorites if topic_id is None else False
        name, accepted = QInputDialog.getText(self, self.tr('新建文件'),
            self.tr('文件名（默认 .md，可用 .txt、.csv 等）'), text=self.tr('未命名.md'))
        if accepted and name.strip():
            self._mutation(lambda: self.library.create_file(name, topic_id=topic, favorite=favorite),
                           on_success=lambda item: self.file_created.emit(item.id))

    def topic_add_menu(self, parent_id):
        menu = prepare_context_menu(QMenu(self), self)
        menu.addAction(self.tr('新建文件…'), lambda: self.create_file(parent_id))
        menu.addAction(self.tr('链接文件…'), lambda: self.add_files(False, topic_id=parent_id))
        menu.addAction(self.tr('导入副本…'), lambda: self.add_files(True, topic_id=parent_id))
        menu.addSeparator()
        menu.addAction(self.tr('新建子主题…') if parent_id else self.tr('新建主题…'), lambda: self.create_topic(parent_id))
        return menu

    def show_topic_add(self, parent_id, position):
        self._select_scope(('topic', parent_id) if parent_id else ('all', ''))
        menu = self.topic_add_menu(parent_id)
        menu.exec(position)
        menu.deleteLater()

    def choose_topic(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(self.tr('选择主题'))
        dialog.resize(420, 480)
        layout = QVBoxLayout(dialog)
        tree = TopicTree(self.library, dialog, restore=False, actions=False)
        tree.refresh(shortcuts=False)
        root = QTreeWidgetItem([self.tr('无主题（根目录）')])
        root.setData(0, Qt.ItemDataRole.UserRole, ('topic', ''))
        tree.insertTopLevelItem(0, root)
        tree.setCurrentItem(root)
        layout.addWidget(tree)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        item = tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        tree.dispose()
        return data[1] if accepted and data and data[0] == 'topic' else None

    def topic_menu(self, position):
        item = self.tree.itemAt(position)
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        parent_id = data[1] if data and data[0] == 'topic' else ''
        title = item.text(0) if item else ''
        menu = self.topic_add_menu(parent_id)
        if parent_id:
            menu.addSeparator()
            def rename():
                name, accepted = QInputDialog.getText(self, self.tr('重命名主题'), self.tr('主题名称'), text=title)
                if accepted:
                    self._mutation(lambda: self.library.rename_topic(parent_id, name), topics=True)
            def move():
                destination = self.choose_topic()
                if destination is not None:
                    self._mutation(lambda: self.library.move_topic(parent_id, destination), topics=True)
            def remove():
                if QMessageBox.question(self, self.tr('删除主题'), self.tr('仅删除分类；子主题和资料会移到上一层，源文件保留。')) == QMessageBox.StandardButton.Yes:
                    # A removed active node must not keep filtering by its old ID.
                    self._mutation(lambda: self.library.delete_topic(parent_id), topics=True,
                                   on_success=lambda _result: setattr(self, 'topic_id', ''))
            menu.addAction(self.tr('重命名…'), rename)
            menu.addAction(self.tr('移动到…'), move)
            menu.addAction(self.tr('删除主题'), remove)
        menu.exec(self.tree.viewport().mapToGlobal(position))
        menu.deleteLater()

    def _cell_clicked(self, row, column):
        if column == 0:
            record = self.table.item(row, 1).data(Qt.ItemDataRole.UserRole)
            self._mutation(lambda: self.library.set_favorite(record['id'], not record['favorite']))

    def open_item(self, row):
        item = self.table.item(row, 1)
        if item is None:
            return
        record = item.data(Qt.ItemDataRole.UserRole)
        self.content_requested.emit(record['id'])

    def item_menu(self, position):
        row = self.table.rowAt(position.y())
        if row < 0:
            return
        record = self.table.item(row, 1).data(Qt.ItemDataRole.UserRole)
        menu = prepare_context_menu(QMenu(self), self)
        menu.addAction(self.tr('打开'), lambda: self.open_item(row))
        menu.addAction(self.tr('取消收藏') if record['favorite'] else self.tr('收藏'),
                       lambda: self._mutation(lambda: self.library.set_favorite(record['id'], not record['favorite'])))
        def move():
            destination = self.choose_topic()
            if destination is not None:
                self._mutation(lambda: self.library.move_item(record['id'], destination))
        def remove():
            prompt = self.tr('删除导入副本？源文件保留。') if record['owned'] else self.tr('移除此引用？源文件保留。')
            if QMessageBox.question(self, self.tr('移除资料'), prompt) == QMessageBox.StandardButton.Yes:
                self._mutation(lambda: self.library.remove(record['id']))
        menu.addAction(self.tr('移动到主题…'), move)
        menu.addAction(self.tr('移除资料'), remove)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def dispose(self):
        self._generation += 1
        self._unsubscribe()
        for job in self._mutation_jobs:
            job.abandon()
        self._mutation_jobs.clear()
        if self._job:
            self._job.abandon()
        self.tree.dispose()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'topic_navigation') and self.width() < 700:
            self.topic_navigation.hide()
