"""Full-width, paged session navigation; never loads conversation bodies."""
from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Input, Label, OptionList
from textual.widgets.option_list import Option

from pycat.tui.composer import single_line
from pycat.tui.panels import Form, Reader


def relative_time(value):
    try:
        time = datetime.fromisoformat(value)
        seconds = max(0, (datetime.now(time.tzinfo) - time).total_seconds())
        if seconds < 60:
            return '刚刚'
        if seconds < 3600:
            return f'{int(seconds // 60)} 分钟前'
        if seconds < 86400:
            return f'{int(seconds // 3600)} 小时前'
        return f'{int(seconds // 86400)} 天前' if seconds < 86400 * 30 else time.strftime('%Y-%m-%d')
    except (TypeError, ValueError):
        return '时间未知'


class ResumeScreen(Screen):
    PAGE_SIZE = 100
    BINDINGS = [Binding(key, action, show=False, priority=True) for key, action in (
        ('escape', 'dismiss(None)'), ('up', 'move(-1)'), ('down', 'move(1)'),
        ('enter', 'choose'), ('f2', 'rename'), ('f3', 'details'),
        ('ctrl+pagedown', 'page(1)'), ('ctrl+pageup', 'page(-1)'))]

    def __init__(self, client, work_dir, selected=''):
        super().__init__(id='resume-screen')
        self.client, self.work_dir, self.selected = client, work_dir, selected
        self.all_projects, self.page_offset, self.has_more = False, 0, False
        self.rows, self._generation, self._timer = [], 0, None
        self._loading = False

    def compose(self):
        with Horizontal(id='resume-heading'):
            yield Label('恢复会话', classes='section-title')
            yield Button('当前项目 · 切换全部', id='resume-scope')
        yield Input(placeholder='搜索标题、项目或会话 ID', id='resume-search')
        yield Label('最近更新      会话', id='resume-columns')
        yield OptionList(id='resume-list')
        yield Label('', id='resume-detail')
        yield Label('', id='resume-error')
        with Horizontal(id='resume-actions'):
            yield Button('↑ 上一页', id='resume-previous')
            yield Button('↓ 下一页', id='resume-next')
            yield Button('F2 重命名', id='resume-rename')
            yield Button('F3 详情', id='resume-details')
            yield Button('Enter 继续', id='resume-choose')
            yield Button('Esc 返回', id='resume-close')

    def on_mount(self):
        self.query_one('#resume-search', Input).focus()
        self.reload()

    def reload(self, *, debounce=False):
        self._generation += 1
        self._loading = True
        if self._timer:
            self._timer.stop()
        generation = self._generation
        def callback():
            self.run_worker(self.load_rows(generation), group='sessions', exclusive=True)
        if debounce:
            self._timer = self.set_timer(.12, callback)
        else:
            callback()

    async def load_rows(self, generation):
        try:
            rows = await self.client.operation('sessions.list', {
                'query': self.query_one('#resume-search', Input).value,
                'work_dir': None if self.all_projects else self.work_dir,
                'offset': self.page_offset, 'limit': self.PAGE_SIZE + 1})
            if generation != self._generation or not self.is_mounted:
                return
            self.has_more = len(rows) > self.PAGE_SIZE
            self.rows = rows[:self.PAGE_SIZE]
            self.render_rows()
            self.query_one('#resume-error', Label).update('' if rows else '没有匹配会话；可切换全部项目或修改搜索')
        except Exception as exc:
            if self.is_mounted and generation == self._generation:
                self.query_one('#resume-error', Label).update(Text(str(exc)))
        finally:
            if generation == self._generation:
                self._loading = False

    def render_rows(self):
        listing = self.query_one('#resume-list', OptionList)
        width = max(1, listing.content_size.width - 2)
        wide = width >= 100
        options = []
        for row in self.rows:
            title_width = max(1, width - (40 if wide else 14))
            text = single_line(relative_time(row.get('updated_at')), 12)
            text.stylize('dim')
            text.append('  ')
            text.append(single_line(('● ' if row.get('pinned') else '') + (row.get('title') or '未命名会话'), title_width))
            if wide:
                project = str(row.get('work_dir') or '未选择项目').replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
                text.append('  ')
                detail = single_line(project, 17)
                detail.stylize('dim')
                text.append(detail)
                text.append(f" {row.get('message_count', 0):>5}", style='dim')
            options.append(Option(text, id=row['id']))
        listing.clear_options().add_options(options)
        listing.highlighted = next((i for i, row in enumerate(self.rows) if row['id'] == self.selected), 0) if options else None
        listing.scroll_to_highlight()
        self.query_one('#resume-previous', Button).disabled = self.page_offset == 0
        self.query_one('#resume-next', Button).disabled = not self.has_more
        self.update_detail()

    def current(self):
        index = self.query_one('#resume-list', OptionList).highlighted
        return self.rows[index] if index is not None and index < len(self.rows) else None

    def update_detail(self):
        row = self.current()
        self.selected = row['id'] if row else ''
        self.query_one('#resume-detail', Label).update(Text(
            f"{row.get('title') or '未命名会话'}\n{row.get('work_dir') or '未选择项目'}\n"
            f"{row['id']} · {row.get('message_count', 0)} 条消息 · 创建于 {row.get('created_at') or '未知'}"
            if row else ''))

    def on_resize(self):
        if self.is_mounted:
            self.render_rows()

    def on_input_changed(self, event):
        if event.input.id == 'resume-search':
            event.stop()
            self.page_offset = 0
            self.reload(debounce=True)

    def on_option_list_option_highlighted(self, event):
        event.stop()
        self.update_detail()

    def on_option_list_option_selected(self, event):
        event.stop()
        self.action_choose()

    def action_move(self, direction):
        listing = self.query_one('#resume-list', OptionList)
        if self.rows:
            listing.highlighted = max(0, min(len(self.rows) - 1, (listing.highlighted or 0) + direction))
            listing.scroll_to_highlight()

    def action_choose(self):
        if not self._loading and (row := self.current()):
            self.dismiss(row['id'])

    def action_page(self, direction):
        if self._loading or (direction > 0 and not self.has_more) or (direction < 0 and self.page_offset == 0):
            return
        self.page_offset = max(0, self.page_offset + direction * self.PAGE_SIZE)
        self.selected = ''
        self.reload()

    def action_rename(self):
        if not self._loading and (row := self.current()):
            self.run_worker(self.rename(row), group='rename')

    def action_details(self):
        if not self._loading and (row := self.current()):
            self.app.push_screen(Reader('会话详情', {
                '标题': row.get('title') or '未命名会话', '项目': row.get('work_dir') or '未选择项目',
                '会话 ID': row['id'], '消息数': row.get('message_count', 0),
                '最近更新': row.get('updated_at'), '创建时间': row.get('created_at')}))

    async def rename(self, row):
        values = await self.app.push_screen_wait(Form('重命名会话', [{'name': 'title', 'label': '名称', 'required': True}],
                                                       {'title': row.get('title', '')}, submit='保存'))
        if values and values['title'].strip():
            try:
                await self.client.operation('sessions.rename', {'session': row['id'], 'title': values['title'].strip()})
                self.reload()
            except Exception as exc:
                self.notify(str(exc), severity='error')

    def on_button_pressed(self, event):
        event.stop()
        action = event.button.id
        if action == 'resume-scope':
            self.all_projects = not self.all_projects
            self.page_offset = 0
            event.button.label = '全部项目 · 切换当前' if self.all_projects else '当前项目 · 切换全部'
            self.reload()
        elif action == 'resume-choose':
            self.action_choose()
        elif action == 'resume-close':
            self.dismiss(None)
        elif action == 'resume-rename':
            self.action_rename()
        elif action == 'resume-details':
            self.action_details()
        elif action in {'resume-next', 'resume-previous'}:
            self.action_page(1 if action == 'resume-next' else -1)
