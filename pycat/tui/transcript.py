"""Bounded, identity-preserving projections of canonical messages and live output."""
from __future__ import annotations

import json

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Label, Markdown, Static


class OpenContent(Message):
    def __init__(self, ref):
        super().__init__()
        self.ref = ref


class OpenText(Message):
    def __init__(self, title, value):
        super().__init__()
        self.title, self.value = title, value


class ContentButton(Button):
    def __init__(self, ref):
        super().__init__(Text('↗ ' + str(ref.get('name') or ref['ref'])), classes='content-ref')
        self.ref = ref

    def on_button_pressed(self, event):
        event.stop()
        self.post_message(OpenContent(self.ref))


class Fold(Vertical):
    """Render expensive Markdown only on expansion; keep full text in the reader."""
    def __init__(self, title, value, *, preview='', classes='', key=''):
        super().__init__(classes='fold ' + classes)
        self.title, self.value, self.preview, self.key = title, str(value or ''), preview, key
        self.expanded = False

    def compose(self):
        yield Button(Text('▸ ' + self.title), classes='fold-toggle')
        if self.preview:
            yield Static(Text(self.preview), classes='fold-preview')
        yield Vertical(classes='fold-body')

    def on_mount(self):
        self.query_one('.fold-body').display = False

    async def on_button_pressed(self, event):
        event.stop()
        if event.button.has_class('fold-full'):
            self.post_message(OpenText(self.title, self.value))
            return
        self.expanded = not self.expanded
        body = self.query_one('.fold-body', Vertical)
        if self.expanded and not body.children:
            await body.mount(Markdown(self.value[:32768]))
            if len(self.value) > 32768:
                await body.mount(Button('阅读完整内容（分页）', classes='fold-full'))
        body.display = self.expanded
        self.query_one('.fold-toggle', Button).label = Text(('▾ ' if self.expanded else '▸ ') + self.title)
        for preview in self.query('.fold-preview'):
            preview.display = not self.expanded


def tool_title(call):
    function, result = call.get('function') or {}, call.get('result')
    metadata = (result or {}).get('metadata') or {}
    failed = bool((result or {}).get('is_error') or metadata.get('is_error'))
    status = '失败' if failed else '取消' if metadata.get('subtask_status') == 'cancelled' else '完成' if result is not None else '结果未记录'
    name = function.get('name') or call.get('name') or '工具'
    detail = f" · exit {metadata['exit_code']}" if 'exit_code' in metadata else ''
    if 'duration_ms' in metadata:
        detail += f" · {metadata['duration_ms']} ms"
    return f'{status} · {name}{detail}'


class MessageView(Vertical):
    def __init__(self, record):
        super().__init__(classes='message-view ' + str(record['role']))
        self.record = record

    @property
    def tool_titles(self):
        return [tool_title(call) for call in self.record.get('tool_calls') or []]

    def compose(self):
        message = self.record
        if message['role'] == 'user':
            yield Label('你', classes='message-role')
        if message.get('thinking'):
            yield Fold('思考过程', message['thinking'], classes='thinking', key='thinking')
        text = str(message.get('content') or '')
        if message['role'] == 'tool':
            yield Fold(str(message.get('name') or '工具结果'), text, preview='\n'.join(text[:300].splitlines()[:3]))
        elif text:
            yield Markdown(text[:32768])
            if len(text) > 32768:
                yield Button('阅读完整消息（分页）', classes='message-full')
        refs = list(message.get('content_refs') or [])
        for call in message.get('tool_calls') or []:
            result, function = call.get('result') or {}, call.get('function') or {}
            metadata = result.get('metadata') or {}
            content = str(result.get('content') or '')
            preview = str(result.get('summary') or content)
            arguments = function.get('arguments') or {}
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False, indent=2)
            yield Fold(tool_title(call), '```json\n' + arguments + '\n```\n\n' + content,
                       preview='\n'.join(preview[:300].splitlines()[:3]), classes='tool-result', key=call.get('id', ''))
            if metadata.get('archive_ref'):
                yield ContentButton({'ref': metadata['archive_ref'], 'name': '完整工具结果', 'mime': 'text/plain'})
            if not (result.get('is_error') or metadata.get('is_error')):
                refs.extend(metadata.get('content_refs') or [])
        seen = set()
        for ref in refs:
            if ref.get('ref') and ref['ref'] not in seen:
                seen.add(ref['ref'])
                yield ContentButton(ref)

    def on_button_pressed(self, event):
        if event.button.has_class('message-full'):
            event.stop()
            self.post_message(OpenText('完整消息', self.record['content']))


class Transcript(VerticalScroll):
    class HistoryRequested(Message):
        def __init__(self, direction):
            super().__init__()
            self.direction = direction

    def __init__(self):
        super().__init__(id='transcript')
        self.views = {}
        self._stream, self._thinking, self._tools = '', '', ''
        self._active = False

    def compose(self):
        yield Static('', id='welcome')
        yield Button('↑ 加载更早消息', id='history-older')
        yield Vertical(id='messages')
        yield Button('↓ 返回最新消息', id='history-newer')
        with Vertical(id='live-output'):
            yield Label('', id='live-thinking')
            yield Static('', id='live-tools')
            yield Markdown('', id='stream')

    def on_mount(self):
        for selector in ('#history-older', '#history-newer', '#live-output', '#live-thinking', '#live-tools', '#stream'):
            self.query_one(selector).display = False
        self.set_interval(.05, self.app.flush_stream)

    def welcome(self, work_dir):
        self.query_one('#welcome', Static).update(Text.assemble(
            ('今天想完成什么？\n', 'bold'), (work_dir or '未选择项目', 'dim'),
            '\n描述任务，随时补充文件与引导。\n\n',
            ('/resume', 'bold'), ' 继续会话   ', ('/model', 'bold'), ' 选择模型   ', ('@', 'bold'), ' 添加引用'))

    async def sync(self, session, *, reset=False, follow=False):
        records = session.get('messages', []) if session else []
        container = self.query_one('#messages', Vertical)
        wanted = {row['id']: row for row in records}
        if reset:
            await container.remove_children()
            self.views.clear()
        for identity in list(self.views):
            if identity not in wanted or self.views[identity].record != wanted[identity]:
                await self.views.pop(identity).remove()
        new = [MessageView(row) for row in records if row['id'] not in self.views]
        if new:
            await container.mount(*new)
            self.views.update({view.record['id']: view for view in new})
        # Only changed pages/snapshots reach here, never individual stream deltas.
        for index, row in enumerate(records):
            view = self.views[row['id']]
            if container.children[index] is not view:
                container.move_child(view, before=container.children[index])
        offset = session.get('message_offset', 0) if session else 0
        total = session.get('message_count', len(records)) if session else 0
        self.query_one('#history-older').display = offset > 0
        self.query_one('#history-newer').display = offset + len(records) < total
        self.query_one('#welcome').display = not records and not self._active and not self._stream and not self._thinking and not self._tools
        if follow:
            self.call_after_refresh(self.scroll_end, animate=False)

    def live(self, text, thinking, tools, *, active=False):
        text, thinking = text[-32768:], thinking[-8192:]
        if (text, thinking, tools, active) == (self._stream, self._thinking, self._tools, self._active):
            return
        self._active = active
        at_end = self.is_vertical_scroll_end
        if text != self._stream:
            self._stream = text
            self.query_one('#stream', Markdown).update(text)
            self.query_one('#stream').display = bool(text)
        if thinking != self._thinking:
            self._thinking = thinking
            self.query_one('#live-thinking', Label).update(Text('思考中 · ' + ' '.join(thinking[-150:].split()) if thinking else ''))
            self.query_one('#live-thinking').display = bool(thinking)
        if tools != self._tools:
            self._tools = tools
            self.query_one('#live-tools', Static).update(Text(tools))
            self.query_one('#live-tools').display = bool(tools)
        self.query_one('#live-output').display = bool(text or thinking or tools)
        self.query_one('#welcome').display = not self.views and not active and not (text or thinking or tools)
        if at_end and (text or thinking or tools):
            self.call_after_refresh(self.scroll_end, animate=False)

    def on_button_pressed(self, event):
        if event.button.id in {'history-older', 'history-newer'}:
            event.stop()
            self.post_message(self.HistoryRequested(-1 if event.button.id == 'history-older' else 1))
