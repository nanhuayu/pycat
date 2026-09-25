"""Keyboard-first terminal workbench with cancellable runs and reusable forms."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, TextArea

from pycat.core.app.client import LocalClient
from pycat.core.content.mime import is_text_mime
from pycat.models.contracts.agent import MentionRef, RunRequest, TurnRevision
from pycat.tui.composer import InputPanel, single_line
from pycat.tui.panels import Form, Interaction, Picker, Reader
from pycat.tui.resume import ResumeScreen
from pycat.tui.transcript import Transcript


@dataclass
class Draft:
    text: str = ''
    attachments: list = field(default_factory=list)
    references: list = field(default_factory=list)
    mentions: list = field(default_factory=list)
    cursor: tuple[int, int] = (0, 0)
    scroll: float | None = None
    history_offset: int = -1


class WorkbenchApp(App):
    TITLE = 'PyCat'
    SUB_TITLE = 'Agent workbench'
    CSS_PATH = 'workbench.tcss'
    BINDINGS = [('ctrl+enter', 'send', '发送 / 引导'), ('escape', 'cancel', '停止'),
                ('ctrl+n', 'new', '新建'), ('ctrl+r', 'resume', '继续'), ('ctrl+p', 'commands', '操作'),
                ('f2', 'model', '模型'), ('f3', 'mode', '模式'), ('f4', 'permissions', '权限'),
                ('f5', 'revise', '修改消息'), ('alt+a', 'attach', '附件'), ('alt+m', 'mention', '引用'),
                ('f6', 'materials', '资料'), ('f8', 'config', '设置'), ('f1', 'help', '帮助'), ('ctrl+q', 'quit', '退出')]

    def __init__(self, *, services=None, client=None, session=None, work_dir='', initial_prompt='', open_sessions=False):
        super().__init__()
        self.body = Vertical(id='main')
        self.client = client or LocalClient(services)
        self.session, self.work_dir = session, work_dir
        self.initial_prompt, self.open_sessions = initial_prompt, open_sessions
        self.catalog = {}
        self.runs, self.stream_text, self.attachments, self.references, self.mentions = {}, {}, [], [], []
        self._interaction_screens = {}
        self._form_drafts = {}
        self._drafts = {}
        self._submitting = False
        self._selection_generation = 0
        self._selection_lock = asyncio.Lock()
        self.thinking_text, self.live_tools = {}, {}

    def compose(self) -> ComposeResult:
        with self.body:
            yield Label('PyCat', id='session-title')
            yield Transcript()
            yield Label('', id='activity')
            yield Label('', id='attachments')
            yield InputPanel(self.client, self.completion_context, self.initial_prompt)
            yield Label('', id='status-line')
            yield Label('', id='input-hints')

    def completion_context(self):
        return {'session': self.session['id'] if self.session else None, 'work_dir': self.work_dir}

    async def on_mount(self):
        initial = await self.client.bootstrap()
        self.catalog = initial['operations']
        for item in initial.get('runs', []):
            if not item['done']:
                run = item['run_id']
                self.runs[run] = item['session']
                self.stream_text[run] = ''
                self.run_worker(self.observe(run), group='observe-' + run)
        self.body.query_one('#activity').display = False
        self.refresh_attachments()
        if self.session:
            await self.select_session(self.session['id'])
        else:
            self.body.query_one(Transcript).welcome(self.work_dir)
            self.refresh_status()
        self.body.query_one('#composer', TextArea).focus()
        if self.open_sessions:
            self.action_resume()

    def on_resize(self, event):
        if self.body.is_mounted:
            self.refresh_status()

    def action_revise(self):
        self.run_worker(self.revise())

    async def revise(self):
        if not self.session or self.active_run():
            self.notify('请先选择一个空闲会话')
            return
        messages = [item for item in self.session['messages'] if item['role'] == 'user']
        identity = await self.push_screen_wait(Picker('选择消息', [(item['id'], item['content'][:100]) for item in messages]))
        if not identity:
            return
        message = next(item for item in messages if item['id'] == identity)
        action = await self.push_screen_wait(Picker('消息操作', [('edit', '编辑并重发'), ('retry', '重试'), ('delete', '删除此轮及后续消息')]))
        if not action:
            return
        try:
            if action == 'delete':
                confirm = await self.push_screen_wait(Picker('确认删除此轮及后续消息', [('cancel', '取消'), ('delete', '删除')]))
                if confirm == 'delete':
                    await self.client.operation('sessions.remove', {'session': self.session['id'], 'message': identity, 'expected_revision': self.session['revision']})
                    await self.select_session(self.session['id'])
                return
            text = ''
            if action == 'edit':
                edited = await self.push_screen_wait(Form('编辑消息', [{'name': 'text', 'type': 'text', 'required': True}], {'text': message['content']}))
                if edited is None:
                    return
                text = edited['text']
            result = await self.client.submit(RunRequest(text=text, conversation_id=self.session['id'],
                expected_revision=self.session['revision'], revision=TurnRevision(action, identity)), request_id=uuid.uuid4().hex)
            run = result['run_id']
            self.runs[run], self.stream_text[run] = self.session['id'], ''
            self.run_worker(self.observe(run), group='observe-' + run)
        except Exception as exc:
            self.notify(str(exc), severity='error')

    async def select_session(self, identity, *, offset=None):
        self._selection_generation += 1
        generation = self._selection_generation
        previous = self.session['id'] if self.session else None
        arguments = {'session': identity}
        if offset is not None:
            arguments['offset'] = offset
        elif previous != identity and identity in self._drafts:
            arguments['offset'] = self._drafts[identity].history_offset
        elif previous == identity and self.session.get('message_offset', 0) + len(self.session.get('messages', [])) < self.session.get('message_count', 0):
            arguments['offset'] = self.session['message_offset']
        session = await self.client.operation('sessions.read', arguments)
        async with self._selection_lock:
            if generation == self._selection_generation:
                await self.display_session(session, offset=offset)

    async def display_session(self, session, *, offset=None):
        # A page mount yields to Textual. Serialize that projection so a newer
        # session cannot be overwritten by a partially mounted older response.
        identity = session['id']
        previous = self.session['id'] if self.session else None
        transcript = self.body.query_one(Transcript)
        follow = previous != identity or transcript.is_vertical_scroll_end
        if previous != identity:
            editor = self.body.query_one('#composer', TextArea)
            old_offset = self.session.get('message_offset', 0) if self.session and self.session.get('message_offset', 0) + len(self.session.get('messages', [])) < self.session.get('message_count', 0) else -1
            self._drafts[previous] = Draft(editor.text, self.attachments, self.references, self.mentions,
                                          editor.cursor_location, transcript.scroll_y, old_offset)
            if len(self._drafts) > 100:
                del self._drafts[next(iter(self._drafts))]
            draft = self._drafts.pop(identity, Draft())
            self.attachments, self.references, self.mentions = draft.attachments, draft.references, draft.mentions
            scroll = draft.scroll
            editor.load_text(draft.text)
            editor.move_cursor(draft.cursor)
            self.body.query_one(InputPanel).invalidate()
            follow = scroll is None
        self.session = session
        self.work_dir = self.session.get('work_dir', '')
        self.refresh_attachments()
        await transcript.sync(self.session, reset=previous != identity, follow=follow and offset is None)
        if previous != identity and scroll is not None:
            transcript.call_after_refresh(transcript.scroll_to, y=scroll, animate=False)
        if offset is not None:
            transcript.call_after_refresh(transcript.scroll_home if offset >= 0 else transcript.scroll_end, animate=False)
        transcript.welcome(self.work_dir)
        self.refresh_status()
        self.flush_stream()

    def refresh_status(self):
        session = self.session or {}
        project = self.work_dir.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1] or '未选择项目'
        self.body.query_one('#session-title', Label).update(single_line('PyCat · ' + project + ' · ' + (session.get('title') or '新会话'), self.size.width - 2))
        label = f"{session.get('model') or 'F2 选择模型'} · {session.get('mode', 'chat')} · 权限 {session.get('settings', {}).get('tool_approval', 'default')}"
        self.body.query_one('#status-line', Label).update(single_line(label, self.size.width - 2))
        self.on_input_panel_candidates_changed()

    def refresh_attachments(self):
        label = self.body.query_one('#attachments', Label)
        values = [item.get('path', '') for item in self.attachments] + self.references
        label.update(Text('附件 / 引用：' + ' · '.join(values)))
        label.display = bool(values)

    def activity(self, text):
        self.body.query_one('#activity', Label).update(single_line(text, self.size.width - 2))
        self.body.query_one('#activity').display = bool(text)

    def on_input_panel_candidates_changed(self):
        # A queued child message can arrive while the screen is being torn down.
        if not self.body.query(InputPanel) or not self.body.query('#input-hints'):
            return
        text = '↑↓ 选择 · Tab 补全 · Enter 确认 · Esc 收起' if self.body.query_one(InputPanel).has_candidates else (
            'Enter 引导 · Esc 停止 · F1 帮助' if self.active_run() else 'Enter 发送 · Ctrl+J 换行 · / 命令 · @ 引用 · F1 帮助')
        self.body.query_one('#input-hints', Label).update(single_line(text, self.size.width - 2))

    def on_input_panel_submitted(self):
        self.action_send()

    def on_input_panel_cancelled(self):
        self.action_cancel()

    def on_input_panel_reference_selected(self, event):
        item = event.candidate
        if item['kind'] == 'file':
            reference = 'workspace:' + item['value']
            if reference not in self.references:
                self.references.append(reference)
        else:
            self.mentions = [old for old in self.mentions if (old['kind'], old['id']) != (item['kind'], item['value'])]
            self.mentions.append({'kind': item['kind'], 'id': item['value'], 'insert_text': item['insert_text']})
        self.refresh_attachments()

    def on_transcript_history_requested(self, event):
        if self.session:
            offset = max(0, self.session.get('message_offset', 0) - 100) if event.direction < 0 else -1
            self.run_worker(self.select_session(self.session['id'], offset=offset), group='history', exclusive=True)

    def on_open_text(self, event):
        self.push_screen(Reader(event.title, event.value))

    def on_open_content(self, event):
        if self.session:
            self.run_worker(self.open_content(self.session['id'], event.ref))

    async def open_content(self, identity, ref):
        mime = ref.get('mime') or ''
        if mime and not is_text_mime(mime):
            self.push_screen(Reader(ref.get('name', '资料'), {'ref': ref['ref'], 'mime': mime,
                '说明': '此内容可在桌面或 Web 资料视图中打开；终端不将二进制文件解码为文本。'}))
            return
        async def load(offset):
            return await self.client.operation('materials.read', {'session': identity, 'ref': ref['ref'], 'offset': offset})
        try:
            data = await load(0)
            self.push_screen(Reader(data.get('name') or '资料', data['text'], loader=load,
                has_more=data.get('has_more', False), next_offset=data.get('next_offset', 65536)))
        except Exception as exc:
            self.notify(str(exc), title='无法打开资料', severity='error')

    async def new_session(self):
        session = await self.client.operation('sessions.create', {'work_dir': self.work_dir})
        await self.select_session(session['id'])
        self.body.query_one('#composer', TextArea).focus()

    def active_run(self):
        identity = self.session['id'] if self.session else ''
        return next((run for run, session in self.runs.items() if session == identity), None)

    def action_send(self):
        self.run_worker(self.send_input(), group='submission')

    async def send_input(self):
        if self._submitting:
            return
        composer = self.body.query_one('#composer', TextArea)
        text = composer.text.strip()
        if not text and not self.attachments and not self.references:
            return
        self._submitting = True
        try:
            if (active := self.active_run()) and not text.startswith('/'):
                if self.attachments or self.references:
                    self.notify('运行中请发送文字引导；附件可在下一轮发送', severity='warning')
                    return
                result = await self.client.guidance(active, text)
                if result['accepted']:
                    composer.load_text('')
                return
            if self.session is None and not text.startswith('/'):
                self.session = await self.client.operation('sessions.create', {'work_dir': self.work_dir})
            request = RunRequest(text=text, conversation_id=self.session['id'] if self.session else None,
                work_dir=None if self.session else self.work_dir, attachments=tuple(self.attachments),
                references=tuple(self.references), mentions=tuple(MentionRef(item['kind'], item['id']) for item in self.mentions if item['insert_text'].strip() in text),
                expected_revision=self.session.get('revision') if self.session else None)
            result = await self.client.submit(request, request_id=uuid.uuid4().hex)
            self.body.query_one(InputPanel).invalidate()
            composer.load_text('')
            self.attachments, self.references, self.mentions = [], [], []
            self.refresh_attachments()
            if result['kind'] == 'run':
                run = result['run_id']
                self.runs[run] = self.session['id'] if self.session else ''
                self.stream_text[run] = ''
                self.run_worker(self.observe(run), group=run)
                self.activity('正在运行 · Esc 停止 · 输入文字补充引导')
                self.on_input_panel_candidates_changed()
                if self.session:
                    await self.select_session(self.session['id'])
            elif result['kind'] == 'exit':
                self.exit()
            elif result['kind'] == 'panel':
                await self.open_panel(result['panel'])
            else:
                if result.get('session'):
                    await self.select_session(result['session'])
                if result.get('message'):
                    self.push_screen(Reader('PyCat', result['message']))
        except Exception as exc:
            self.notify(str(exc), title='未能发送', severity='error', timeout=10)
        finally:
            self._submitting = False

    async def observe(self, run):
        try:
            async for event in self.client.events(run):
                if event.get('conversation_id'):
                    self.runs[run] = event['conversation_id']
                if event['type'] == 'event':
                    if event['kind'] == 'text_delta':
                        self.stream_text[run] = (self.stream_text[run] + str(event.get('data') or ''))[-32768:]
                    elif event['kind'] == 'thinking_delta':
                        self.thinking_text[run] = (self.thinking_text.get(run, '') + str(event.get('data') or ''))[-8192:]
                    elif event['kind'] in {'tool_start', 'tool_end'}:
                        data = event.get('data') or {}
                        tools = self.live_tools.setdefault(run, {})
                        key = data.get('tool_call_id') or event.get('root_tool_call_id') or str(event.get('sequence', ''))
                        status = '执行中' if event['kind'] == 'tool_start' else '失败' if data.get('is_error') else '已拒绝' if data.get('allowed') is False else '完成'
                        tools[key] = f"{status} · {event.get('tool_name') or data.get('tool_name') or '工具'} · {str(data.get('summary') or '')[:160]}"
                        if len(tools) > 32:
                            del tools[next(iter(tools))]
                elif event['type'] == 'interaction':
                    self.run_worker(self.ask(run, event), group='interaction-' + event['id'])
                elif event['type'] == 'interaction_closed':
                    screen = self._interaction_screens.pop(event['id'], None)
                    if screen and screen.is_mounted:
                        screen.dismiss(None)
                elif event['type'] == 'reset':
                    self.stream_text[run] = ''
                    self.thinking_text.pop(run, None)
                    self.live_tools.pop(run, None)
                    if self.session and event['session'] == self.session['id']:
                        await self.select_session(self.session['id'])
                    for pending in event['pending']:
                        self.run_worker(self.ask(run, pending), group='interaction-' + pending['id'])
                    if event['done']:
                        self.runs.pop(run, None)
                        self.stream_text.pop(run, None)
                        if event['final'].get('error'):
                            self.notify(event['final']['error'], severity='error')
                elif event['type'] == 'final':
                    if event.get('data') is not None:
                        self.push_screen(Reader('操作结果', event['data']))
                    identity = event.get('conversation_id') or self.runs.get(run)
                    self.runs.pop(run, None)
                    self.stream_text.pop(run, None)
                    self.thinking_text.pop(run, None)
                    self.live_tools.pop(run, None)
                    if identity and (self.session is None or self.session['id'] == identity):
                        await self.select_session(identity)
                        self.activity(event.get('error') or ('' if event['status'] == 'completed' else event['status']))
        except Exception as exc:
            self.notify(str(exc), severity='error', timeout=10)
        finally:
            self.runs.pop(run, None)
            self.stream_text.pop(run, None)
            self.thinking_text.pop(run, None)
            self.live_tools.pop(run, None)
            self.flush_stream()
            self.on_input_panel_candidates_changed()

    def flush_stream(self):
        run = self.active_run()
        self.body.query_one(Transcript).live(self.stream_text.get(run, ''), self.thinking_text.get(run, ''),
            '\n'.join(list(self.live_tools.get(run, {}).values())[-6:]), active=bool(run))

    async def ask(self, run, value):
        if value['id'] in self._interaction_screens:
            return
        screen = Interaction(value)
        self._interaction_screens[value['id']] = screen
        decision = await self.push_screen_wait(screen)
        if decision is not None:
            try:
                await self.client.respond(run, value['id'], decision)
            except Exception as exc:
                self.notify(str(exc), severity='error')
        self._interaction_screens.pop(value['id'], None)

    def action_cancel(self):
        if self.body.query_one(InputPanel).has_candidates:
            self.body.query_one(InputPanel).dismiss_candidates()
            return
        if run := self.active_run():
            self.run_worker(self.client.cancel(run))

    def action_new(self):
        self.run_worker(self.new_session())

    def action_resume(self):
        self.run_worker(self.open_panel('resume'))

    def action_model(self):
        self.run_worker(self.open_panel('model'))

    def action_mode(self):
        self.run_worker(self.open_panel('mode'))

    def action_permissions(self):
        self.run_worker(self.perform('sessions.permissions'))

    def action_materials(self):
        self.run_worker(self.perform('materials.list'))

    def action_config(self):
        self.run_worker(self.configure())

    def action_commands(self):
        self.run_worker(self.pick_operation())

    def action_attach(self):
        self.run_worker(self.attach())

    def action_mention(self):
        self.body.query_one(InputPanel).trigger_mention()

    def action_help(self):
        shortcuts = '\n'.join(f'- **{key}**：{description}' for key, _, description in self.BINDINGS)
        self.push_screen(Reader('终端帮助', 'Enter 发送 / 引导；Ctrl+J 换行；粘贴不会自动发送。\n\n'
            '输入 `/` 选择命令，`@` 添加带类型的引用。候选开启时 ↑↓ 选择、Tab 补全、Enter 确认、Esc 收起。\n\n' + shortcuts))

    async def pick_operation(self):
        choice = await self.push_screen_wait(Picker('所有操作', [(name, data['label'] + ' · ' + name) for name, data in self.catalog.items()]))
        if choice:
            await self.perform(choice)

    async def perform(self, name):
        if name not in self.catalog:
            self.notify('此操作尚不可用', severity='warning')
            return
        definition = self.catalog[name]
        preset = {'session': self.session['id'] if self.session else None, 'work_dir': self.work_dir,
                  'expected_revision': self.session.get('revision') if self.session else None}
        values = {**preset, **self._form_drafts.get(name, {})}
        fields = definition['parameters']
        data = await self.push_screen_wait(Form(definition['label'], fields, values)) if fields else {}
        if data is None:
            return
        self._form_drafts[name] = data
        try:
            result = await self.client.operation(name, data)
            if result and isinstance(result, dict) and result.get('run_id'):
                run = result['run_id']
                self.runs[run] = result.get('session') or (self.session['id'] if self.session else '')
                self.stream_text[run] = ''
                self.run_worker(self.observe(run), group='observe-' + run)
            else:
                self.push_screen(Reader(definition['label'], result))
            if self.session:
                await self.select_session(self.session['id'])
        except Exception as exc:
            self.notify(str(exc), title='操作失败；草稿已保留', severity='error', timeout=12)

    async def configure(self):
        view = await self.client.operation('config.read', {})
        labels = {'app_settings': '通用与运行设置', 'providers': '模型与服务商', 'mcp_servers': 'MCP', 'modes': '模式与 Agent', 'search_config': '搜索'}
        domain = await self.push_screen_wait(Picker('设置', list(labels.items())))
        if not domain:
            return
        field = {'name': 'value', 'type': 'list' if isinstance(view['values'][domain], list) else 'dict', 'required': True}
        draft = await self.push_screen_wait(Form(labels[domain], [field], {'value': view['values'][domain]}, submit='保存'))
        if draft:
            try:
                result = await self.client.operation('config.update', {'patch': {domain: draft['value']}, 'expected_revision': view['revision']})
                if not result['ok']:
                    self.push_screen(Reader('部分设置未保存', result))
                else:
                    self.notify('设置已保存')
            except Exception as exc:
                self._form_drafts['config.update'] = {'patch': {domain: draft['value']}, 'expected_revision': view['revision']}
                self.notify(str(exc), title='保存失败；草稿已保留', severity='error')

    async def open_panel(self, panel):
        self.body.query_one(InputPanel).dismiss_candidates()
        if panel == 'resume':
            if isinstance(self.screen, ResumeScreen):
                return
            choice = await self.push_screen_wait(ResumeScreen(self.client, self.work_dir, self.session['id'] if self.session else ''))
            if choice:
                try:
                    await self.select_session(choice)
                except Exception as exc:
                    self.notify(str(exc), title='无法恢复会话', severity='error')
            self.body.query_one('#composer', TextArea).focus()
            return
        if panel == 'config':
            await self.configure()
            return
        if panel in {'model', 'mode', 'agents'}:
            name = {'model': 'model.list', 'mode': 'mode.list', 'agents': 'mode.list'}[panel]
            rows = await self.client.operation(name, {'work_dir': self.work_dir} if panel in {'mode', 'agents'} else {})
            items = [(row.get('id') or row.get('ref') or row['slug'], row.get('title') or row.get('ref') or row['name'])
                     for row in rows if panel not in {'mode'} or row.get('profile_kind') in {'primary', 'both'} and row['slug'] != 'channel']
            choice = await self.push_screen_wait(Picker(panel.title(), items))
            if choice:
                if panel == 'agents':
                    self.push_screen(Reader('Agent', next(row for row in rows if row['slug'] == choice)))
                else:
                    if self.session is None:
                        await self.new_session()
                    await self.client.operation('sessions.select', {'session': self.session['id'], panel: choice, 'expected_revision': self.session['revision']})
                    await self.select_session(self.session['id'])
            return
        if panel in {'context', 'status'}:
            if self.session:
                try:
                    value = await self.client.operation('sessions.context' if panel == 'context' else 'sessions.read', {'session': self.session['id']})
                    self.push_screen(Reader('上下文预算' if panel == 'context' else '会话状态', value))
                except Exception as exc:
                    self.notify(str(exc), severity='error')
            else:
                self.notify('请先开始或恢复一个会话')
            return
        mapping = {'mcp': 'mcp.list', 'channels': 'channels.list',
                   'permissions': 'sessions.permissions', 'doctor': 'doctor', 'rename': 'sessions.rename', 'export': 'sessions.export'}
        if panel == 'copy' and self.session:
            self.copy_to_clipboard(next((m['content'] for m in reversed(self.session['messages']) if m['role'] == 'assistant'), ''))
            self.notify('已复制')
        elif panel in mapping:
            await self.perform(mapping[panel])

    async def attach(self):
        data = await self.push_screen_wait(Form('添加附件', [{'name': 'path', 'label': '本地文件路径', 'required': True}]))
        if data:
            self.attachments.append({'path': data['path']})
            self.refresh_attachments()


async def run_tui(services=None, **kwargs):
    await WorkbenchApp(services=services, **kwargs).run_async()
