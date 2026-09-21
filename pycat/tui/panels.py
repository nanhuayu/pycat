from __future__ import annotations

import json

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Markdown, OptionList, TextArea
from textual.widgets.option_list import Option


class Picker(ModalScreen):
    BINDINGS = [Binding(key, action, show=False, priority=True) for key, action in (
        ('escape', 'dismiss(None)'), ('up', 'move(-1)'), ('down', 'move(1)'), ('enter', 'choose'))]

    def __init__(self, title, items):
        super().__init__()
        self.title, self.items = title, items

    def compose(self) -> ComposeResult:
        with Vertical(classes='dialog'):
            yield Label(self.title, classes='dialog-title')
            yield Input(placeholder='搜索…', id='filter')
            yield OptionList(*[Option(Text(label, no_wrap=True, overflow='ellipsis'), id=str(key)) for key, label in self.items], id='choices')
            yield Button('取消', id='dismiss')

    def on_input_changed(self, event: Input.Changed):
        choices = self.query_one('#choices', OptionList)
        choices.clear_options()
        choices.add_options([Option(Text(label, no_wrap=True, overflow='ellipsis'), id=str(key)) for key, label in self.items
                             if event.value.casefold() in label.casefold()])

    def action_move(self, direction):
        choices = self.query_one('#choices', OptionList)
        if choices.option_count:
            choices.highlighted = ((choices.highlighted or 0) + direction) % choices.option_count
            choices.scroll_to_highlight()

    def action_choose(self):
        choices = self.query_one('#choices', OptionList)
        if choices.option_count:
            self.dismiss(choices.get_option_at_index(choices.highlighted or 0).id)

    def on_option_list_option_selected(self, event):
        self.dismiss(event.option.id)

    def on_button_pressed(self, event):
        self.dismiss(None)


class Form(ModalScreen):
    BINDINGS = [('escape', 'dismiss(None)', '关闭')]

    def __init__(self, title, fields, values=None, *, submit='执行'):
        super().__init__()
        self.title, self.fields, self.values, self.submit = title, fields, values or {}, submit

    def compose(self):
        with Vertical(classes='dialog form-dialog'):
            yield Label(self.title, classes='dialog-title')
            with VerticalScroll():
                for index, field in enumerate(self.fields):
                    yield Label(field.get('label', field['name']) + (' *' if field.get('required') else ''))
                    value = self.values.get(field['name'], field.get('default'))
                    kind = field.get('type', 'str').split(' | ')[0]
                    if kind in {'dict', 'list', 'text'}:
                        text = str(value or '') if kind == 'text' else json.dumps(value, ensure_ascii=False, indent=2) if value is not None else ''
                        yield TextArea(text, id=f'field-{index}', classes='json-editor')
                    else:
                        text = '' if value is None else json.dumps(value) if isinstance(value, bool) else str(value)
                        yield Input(text, id=f'field-{index}', password=field['name'] in {'password', 'token', 'api_key'})
            yield Label('', id='form-error')
            with Horizontal(classes='dialog-actions'):
                yield Button('取消', id='dismiss')
                yield Button(self.submit, id='accept', variant='primary')

    def on_button_pressed(self, event):
        if event.button.id == 'dismiss':
            self.dismiss(None)
            return
        result = {}
        try:
            for index, field in enumerate(self.fields):
                widget = self.query_one(f'#field-{index}')
                text = widget.text if isinstance(widget, TextArea) else widget.value
                kind = field.get('type', 'str').split(' | ')[0]
                if not text and not field.get('required'):
                    result[field['name']] = field.get('default')
                elif kind in {'dict', 'list', 'bool'} or kind.startswith(('int', 'float')):
                    result[field['name']] = json.loads(text)
                else:
                    result[field['name']] = text
            self.dismiss(result)
        except ValueError as exc:
            self.query_one('#form-error', Label).update(f'请输入有效值：{exc}')


class Reader(ModalScreen):
    BINDINGS = [('escape', 'dismiss(None)', '关闭')]

    PAGE_SIZE = 65536

    def __init__(self, title, value, *, loader=None, has_more=False, next_offset=65536):
        super().__init__()
        self.title, self.value = title, value
        self.loader, self.has_more, self.next_offset = loader, has_more, next_offset
        self._text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        self._pages, self._page = [0], 0
        self._busy = False

    def page_text(self):
        text = self._text if self.loader else self._text[self._pages[self._page]:self._pages[self._page] + self.PAGE_SIZE]
        return text if isinstance(self.value, str) else '```json\n' + text + '\n```'

    def compose(self):
        with Vertical(classes='dialog reader-dialog'):
            yield Label(self.title, classes='dialog-title')
            with VerticalScroll(classes='reader-body'):
                yield Markdown(self.page_text(), id='reader-content')
            yield Label('', id='reader-error')
            with Horizontal(classes='dialog-actions'):
                yield Button('上一页', id='reader-previous', disabled=True)
                yield Button('下一页', id='reader-next', disabled=not self.more())
                yield Button('关闭', id='dismiss', variant='primary')

    def more(self):
        return self.has_more if self.loader else self._pages[self._page] + self.PAGE_SIZE < len(self._text)

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'dismiss':
            self.dismiss(None)
        elif not self._busy:
            self.run_worker(self.change_page(1 if event.button.id == 'reader-next' else -1), group='reader-page')

    async def change_page(self, direction):
        if self._busy or direction > 0 and not self.more() or direction < 0 and not self._page:
            return
        self._busy = True
        target = self._page + direction
        start = (self.next_offset if self.loader else self._pages[self._page] + self.PAGE_SIZE) if direction > 0 else self._pages[target]
        try:
            if self.loader:
                data = await self.loader(start)
                if not self.is_mounted:
                    return
                self._text, self.has_more = data['text'], data.get('has_more', False)
                self.next_offset = data.get('next_offset', start + self.PAGE_SIZE)
            if target == len(self._pages):
                self._pages.append(start)
            self._page = target
            self.query_one('#reader-content', Markdown).update(self.page_text())
            self.query_one('.reader-body', VerticalScroll).scroll_home(animate=False)
            self.query_one('#reader-error', Label).update('')
            self.query_one('#reader-previous', Button).disabled = target == 0
            self.query_one('#reader-next', Button).disabled = not self.more()
        except Exception as exc:
            if self.is_mounted:
                self.query_one('#reader-error', Label).update(Text(str(exc)))
        finally:
            self._busy = False


class Interaction(ModalScreen):
    def __init__(self, interaction):
        super().__init__()
        self.interaction = interaction

    def compose(self):
        data = self.interaction['payload']
        with Vertical(classes='dialog'):
            yield Label('工具需要批准' if self.interaction['kind'] == 'approval' else '需要你的回答', classes='dialog-title')
            yield Label(data.get('message') or data.get('text') or data.get('tool_name', ''))
            if self.interaction['kind'] == 'approval':
                yield Label(f"{data.get('tool_name', '')} · {data.get('risk', '')}")
                yield TextArea(json.dumps(data.get('arguments', {}), ensure_ascii=False, indent=2), read_only=True, classes='json-editor')
                with Horizontal(classes='dialog-actions'):
                    yield Button('拒绝', id='deny')
                    yield Button('允许一次', id='allow', variant='primary')
                    if data.get('requires_path_approval') or data.get('external_path'):
                        yield Button('本次运行可读', id='run')
            else:
                for index, option in enumerate(data.get('options', []), 1):
                    yield Label(f"{index}. {option['label']}  {option.get('description', '')}")
                yield Input(placeholder='输入选项编号（多选用逗号）或文字', id='answer')
                with Horizontal(classes='dialog-actions'):
                    yield Button('跳过', id='skip')
                    yield Button('提交', id='answer-submit', variant='primary')

    def on_button_pressed(self, event):
        event.stop()
        action = event.button.id
        if self.interaction['kind'] == 'approval':
            self.dismiss({'approved': action != 'deny', 'read_scope': 'run' if action == 'run' else 'call' if action == 'allow' else ''})
            return
        self.submit_answer(skip=action == 'skip')

    def on_input_submitted(self, event):
        event.stop()
        self.submit_answer()

    def submit_answer(self, *, skip=False):
        text = self.query_one('#answer', Input).value.strip()
        if skip or not text:
            self.dismiss({'selected': [], 'freeText': None, 'skipped': True})
            return
        values = [part.strip() for part in text.split(',')]
        options = self.interaction['payload'].get('options', [])
        if all(value.isdigit() and 1 <= int(value) <= len(options) for value in values):
            selected = [options[int(value) - 1]['label'] for value in values]
            if len(selected) > 1 and not self.interaction['payload'].get('multiple'):
                self.notify('本题只能选择一项', severity='warning')
                return
            self.dismiss({'selected': selected, 'freeText': None, 'skipped': False})
        else:
            self.dismiss({'selected': [], 'freeText': text, 'skipped': False})
