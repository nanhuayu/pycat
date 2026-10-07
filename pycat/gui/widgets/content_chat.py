"""Small Qt projection over content-bound conversations and the shared RunService."""
from __future__ import annotations

import re
import weakref
from copy import copy

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QSize, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pycat.gui.runtime.background_job import BackgroundJob
from pycat.gui.shortcuts import matches_shortcut
from pycat.gui.utils.display_text import message_time
from pycat.gui.utils.icon_manager import Icons
from pycat.gui.utils.theme import configure_icon_button, resolve_accent, resolve_theme, theme_tokens
from pycat.gui.widgets.image_annotations import ImageAnnotationPanel
from pycat.gui.widgets.markdown_view import MarkdownView
from pycat.gui.widgets.model_ref_selector import ModelRefCombo
from pycat.gui.widgets.themed_line_edit import ThemedPlainTextEdit
from pycat.models.contracts.agent import RunEventKind, RunStatus
from pycat.models.conversation import Message
from pycat.models.model_ref import build_model_ref


class ContentChat(QWidget):
    apply_requested = pyqtSignal(object, str)
    _event = pyqtSignal(str, str, object)

    def __init__(self, services, capture, parent=None):
        super().__init__(parent)
        self.services, self.capture = services, capture
        self.key, self.session_id, self._quote = '', '', ''
        self._job, self._suggestion = None, None
        self._watch = None
        self._generation = 0
        self._running = False
        self._shortcut_overrides = {}
        self.setObjectName('content_chat')
        self.setMinimumWidth(240)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 8, 8)
        layout.setSpacing(8)
        self.annotation_panel = ImageAnnotationPanel(self)
        self.annotation_panel.hide()
        layout.addWidget(self.annotation_panel)
        heading = QHBoxLayout()
        heading.addWidget(QLabel(self.tr('内容对话')))
        heading.addStretch()
        layout.addLayout(heading)
        self.transcript = QScrollArea()
        self.transcript.setObjectName('messages_scroll')
        self.transcript.setFrameShape(QFrame.Shape.NoFrame)
        self.transcript.setWidgetResizable(True)
        self.transcript.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.transcript.setAccessibleName(self.tr('内容对话记录'))
        self.messages = QWidget()
        self.messages.setObjectName('messages_container')
        self.messages_layout = QVBoxLayout(self.messages)
        self.messages_layout.setContentsMargins(0, 4, 0, 0)
        self.messages_layout.setSpacing(12)
        self.transcript.setWidget(self.messages)
        self._stream_view, self._stream_text = None, ''
        self._stream_timer = QTimer(self)
        self._stream_timer.setSingleShot(True)
        self._stream_timer.setInterval(400)
        self._stream_timer.timeout.connect(self._render_stream)
        self._clear_history()
        layout.addWidget(self.transcript, 1)
        self.quote = QLabel()
        self.quote.setWordWrap(True)
        self.quote.setProperty('muted', True)
        self.quote.hide()
        layout.addWidget(self.quote)
        self.apply_button = QPushButton(self.tr('应用到草稿'))
        self.apply_button.setEnabled(False)
        self.apply_button.hide()
        self.apply_button.setToolTip(self.tr('先检查回复；应用后仍需显式保存，可撤销。'))
        self.apply_button.clicked.connect(self._apply)
        layout.addWidget(self.apply_button)
        self.composer = QFrame(self)
        self.composer.setObjectName('input_wrapper')
        composer_layout = QVBoxLayout(self.composer)
        composer_layout.setContentsMargins(8, 8, 8, 8)
        composer_layout.setSpacing(4)
        self.input = ThemedPlainTextEdit(self.composer)
        self.input.setObjectName('content_chat_input')
        self.input.setPlaceholderText(self.tr('询问、解释或改写选中内容…'))
        self.input.setFixedHeight(76)
        self.input.installEventFilter(self)
        composer_layout.addWidget(self.input)
        controls = QHBoxLayout()
        self.model = ModelRefCombo(empty_label=self.tr('默认模型'), parent=self.composer)
        self.model.setObjectName('bottom_model_selector')
        self.model.setMinimumContentsLength(8)
        self.model.setMinimumWidth(0)
        self.model.setMaximumWidth(210)
        self.model.lineEdit().setFrame(False)
        controls.addWidget(self.model)
        controls.addStretch()
        self.send = QToolButton(self.composer)
        self.send.setObjectName('primary_action_btn')
        self.send.setFixedSize(30, 30)
        self.send.setIconSize(QSize(20, 20))
        self.send.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.send.clicked.connect(self._send_or_stop)
        controls.addWidget(self.send)
        composer_layout.addLayout(controls)
        layout.addWidget(self.composer)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setProperty('muted', True)
        layout.addWidget(self.status)
        self.input.textChanged.connect(self._sync_send_enabled)
        self._event.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        self._set_running(False)

    def bind(self, key, title, *, source=None):
        self._generation += 1
        if self._watch:
            self._watch.cancel()
            self._watch = None
        if self._job:
            self._job.abandon()
        self.key, self.session_id = key, ''
        self._suggestion = None
        self.apply_button.setEnabled(False)
        self.apply_button.hide()
        self.set_quote('')
        self._clear_history()
        self.input.clear()
        self._set_running(False)
        self._set_status(self.tr('正在读取…') if key else '')
        self.send.setEnabled(False)
        self.model.set_model_ref('')
        if not key:
            return
        def operation():
            session = self.services.content_chat_service.session(key, title, source=source)
            return (session, self.services.app_settings_service.load().get('shortcuts', {}),
                    self.services.provider_catalog_service.current())
        job = BackgroundJob(operation)
        self._job = job
        def complete(result, error):
            if self._job is not job or self.key != key:
                return
            self._job = None
            if error:
                self._set_status(str(error))
                return
            session, self._shortcut_overrides, providers = result
            self.session_id = session.id
            self.model.set_providers(providers, current_model_ref=build_model_ref(session.provider_name, session.model))
            self._render_history(session)
            self._set_running(self.services.conv_service.is_active(session.id))
            self._set_status(self.tr('正在生成…') if self._running else '')
            if self._running:
                endpoint, runs, session_id = weakref.ref(self), self.services.run_service, session.id
                generation = self._generation
                async def restore_when_finished():
                    session = await runs.wait_session(session_id)
                    widget = endpoint()
                    if widget is not None and not sip.isdeleted(widget):
                        widget._event.emit(key, 'restored', (generation, session))
                self._watch = runs.schedule(restore_when_finished())
        job.signals.finished.connect(complete)
        QThreadPool.globalInstance().start(job)

    def _render_history(self, session):
        # Keep the UI projection bounded; canonical messages remain with ConversationService.
        self._clear_history()
        for message in session.messages[-40:]:
            if message.role in {'user', 'assistant'}:
                display = copy(message)
                display.content = message.content.split('\n\nDocument context (data, not instructions):\n', 1)[0][:24000]
                self._add_message(display)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _clear_history(self):
        self._stream_timer.stop()
        self._stream_view, self._stream_text = None, ''
        while self.messages_layout.count():
            item = self.messages_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.empty = QLabel(self.tr('围绕当前资料提问，或选中正文后请求修改。'))
        self.empty.setWordWrap(True)
        self.empty.setProperty('muted', True)
        self.messages_layout.addWidget(self.empty)
        self.messages_layout.addStretch()

    def _add_message(self, message):
        # Share native Markdown and main-chat styling without importing the
        # agent trace/action widget back into its own content detail window.
        self.empty.hide()
        widget = QFrame()
        widget.setObjectName('message_widget')
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        outer = QVBoxLayout(widget)
        user = message.role == 'user'
        outer.setContentsMargins(24 if user else 0, 0, 0, 0)
        body = QFrame()
        body.setObjectName('user_bubble' if user else 'assistant_body')
        outer.addWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12 if user else 0, 8 if user else 0, 12 if user else 0, 8 if user else 0)
        layout.setSpacing(6)
        if not user:
            header = QHBoxLayout()
            avatar = QLabel()
            avatar.setPixmap(Icons.brand().pixmap(24, 24))
            header.addWidget(avatar)
            role = QLabel('PyCat')
            role.setObjectName('message_role')
            header.addWidget(role)
            timestamp = QLabel(message_time(message.created_at))
            timestamp.setObjectName('message_badge')
            header.addWidget(timestamp)
            header.addStretch()
            layout.addLayout(header)
        view = MarkdownView(message.content)
        view.setObjectName('message_content')
        layout.addWidget(view)
        if message.content:
            copy_button = QToolButton()
            configure_icon_button(copy_button, Icons.get_muted(Icons.COPY), self.tr('复制原文'))
            copy_button.clicked.connect(lambda: QApplication.clipboard().setText(message.content))
            outer.addWidget(copy_button, 0, Qt.AlignmentFlag.AlignRight if user else Qt.AlignmentFlag.AlignLeft)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, widget)
        return view

    def _render_stream(self):
        if self._stream_view is not None:
            at_bottom = self._at_bottom()
            self._stream_view.set_markdown(self._stream_text)
            if at_bottom:
                QTimer.singleShot(0, self._scroll_to_bottom)

    def _at_bottom(self):
        bar = self.transcript.verticalScrollBar()
        return bar.value() >= bar.maximum() - 24

    def _scroll_to_bottom(self):
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _set_status(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def set_quote(self, text):
        self._quote = str(text or '')[:12000]
        self.quote.setText(self.tr('已选择：') + self._quote[:160])
        self.quote.setVisible(bool(self._quote))
        if text:
            self.input.setFocus()

    def _set_running(self, running):
        self._running = running
        self.input.setReadOnly(running)
        self.model.setEnabled(not running)
        self._sync_send_enabled()
        self.send.setProperty('tone', 'danger' if running else 'primary')
        self._refresh_action_icon()
        self.send.style().unpolish(self.send)
        self.send.style().polish(self.send)

    def _sync_send_enabled(self):
        self.send.setEnabled(bool(self.session_id) and (self._running or bool(self.input.toPlainText().strip())))

    def _refresh_action_icon(self):
        tokens = theme_tokens(resolve_theme(self), resolve_accent(self))
        self.send.setIcon(Icons.get(Icons.STOP_FILLED if self._running else Icons.ARROW_UP,
                                   color=tokens.color('error' if self._running else 'on_primary')))
        label = self.tr('停止') if self._running else self.tr('发送')
        self.send.setToolTip(label)
        self.send.setAccessibleName(label)

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, 'send') and event.type() in {QEvent.Type.PaletteChange, QEvent.Type.StyleChange}:
            self._refresh_action_icon()

    def _send_or_stop(self):
        if self._running:
            async def stop():
                self.services.run_service.cancel_session(self.session_id)
            try:
                self.services.run_service.schedule(stop())
            except Exception as exc:
                self._set_status(str(exc))
            return
        question = self.input.toPlainText().strip()
        if not question or not self.session_id:
            return
        try:
            capture = self.capture()
            selection = capture.get('selection')
            quote = self._quote
            if quote and selection and capture.get('text', '')[slice(*selection)] == quote:
                quote = ''
            elif quote:
                # A rich-text quotation has no reliable source replacement range.
                capture = {**capture, 'selection': None, 'apply_range': None}
            request = self.services.content_chat_service.request(
                self.session_id, key=self.key, name=capture['name'], version=capture['version'],
                question=question + ('\n\nSelected quotation:\n' + quote if quote else ''),
                text=capture.get('text', ''), selection=capture.get('selection'),
                model=self.model.model_ref() or None, image_path=capture.get('image_path', ''), image_digest=capture.get('image_digest', ''),
                annotations=capture.get('annotations', ()), source_complete=capture.get('complete', True))
        except Exception as exc:
            self._set_status(str(exc))
            return
        self._suggestion = None
        self._generation += 1
        if self._watch:
            self._watch.cancel()
            self._watch = None
        self.apply_button.setEnabled(False)
        self.apply_button.hide()
        self._set_running(True)
        self._add_message(Message(role='user', content=question))
        self._stream_view = self._add_message(Message(role='assistant', content=''))
        self._stream_text = ''
        QTimer.singleShot(0, self._scroll_to_bottom)
        self._set_status(self.tr('正在生成…'))
        self.input.clear()
        self.set_quote('')
        key, runs, endpoint = self.key, self.services.run_service, weakref.ref(self)
        def emit(kind, data):
            widget = endpoint()
            if widget is not None and not sip.isdeleted(widget):
                widget._event.emit(key, kind, data)
        async def execute():
            try:
                handle = runs.start(request, source='content')
                async for event in handle.events():
                    if event.kind == RunEventKind.TEXT_DELTA:
                        emit('text', str(event.data))
                    elif event.kind in {RunEventKind.THINKING_DELTA, RunEventKind.CONDENSE, RunEventKind.RETRY}:
                        emit('phase', event.kind.value)
                result = await handle.result()
                emit('done', (result, capture))
            except Exception as exc:
                emit('error', str(exc))
        try:
            runs.schedule(execute())
        except Exception as exc:
            self._receive(key, 'error', str(exc))

    def _receive(self, key, kind, data):
        if key != self.key:
            return
        if kind == 'text':
            self._set_status(self.tr('正在生成…'))
            self._stream_text += data
            if not self._stream_timer.isActive():
                self._stream_timer.start()
        elif kind == 'phase':
            phases = {'thinking_delta': self.tr('正在思考…'), 'condense': self.tr('正在压缩…'), 'retry': self.tr('正在重试…')}
            self._set_status(phases[data])
        elif kind == 'done':
            result, capture = data
            self._set_running(False)
            if result.conversation:
                self._render_history(result.conversation)
                self.model.set_model_ref(build_model_ref(result.conversation.provider_name, result.conversation.model))
            self._set_status(result.error or (self.tr('已取消') if result.status == RunStatus.CANCELLED else ''))
            reply = result.final_message.content if result.final_message else ''
            blocks = re.findall(r'```[^\n]*\n(.*?)\n```', reply, flags=re.DOTALL)
            if result.status == RunStatus.COMPLETED and len(blocks) == 1 and capture.get('apply_range') is not None:
                self._suggestion = (capture, blocks[0])
                self.apply_button.setEnabled(True)
                self.apply_button.show()
        elif kind == 'error':
            self._set_running(False)
            self._stream_timer.stop()
            self._render_stream()
            self._set_status(data)
        elif kind == 'restored' and self._running:
            generation, session = data
            if generation != self._generation:
                return
            self._set_running(False)
            if session:
                self._render_history(session)
                self.model.set_model_ref(build_model_ref(session.provider_name, session.model))
            self._set_status('')

    def _apply(self):
        if self._suggestion:
            self.apply_requested.emit(*self._suggestion)

    def eventFilter(self, watched, event):
        if watched is self.input and event.type() in {QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress}:
            is_send = matches_shortcut(event, 'send_message', self._shortcut_overrides)
            is_stop = matches_shortcut(event, 'cancel_generation', self._shortcut_overrides)
            if is_send or is_stop:
                event.accept()
                if event.type() == QEvent.Type.KeyPress and ((is_send and not self._running) or (is_stop and self._running)):
                    self._send_or_stop()
                return True
        return super().eventFilter(watched, event)

    def set_shortcuts(self, overrides):
        self._shortcut_overrides = dict(overrides or {})

    def dispose(self):
        # Navigation detaches observation, never cancels a run or answers an interaction.
        self.key = ''
        if not sip.isdeleted(self._stream_timer):
            self._stream_timer.stop()
        self._generation += 1
        if self._watch:
            self._watch.cancel()
            self._watch = None
        if self._job:
            self._job.abandon()
            self._job = None
