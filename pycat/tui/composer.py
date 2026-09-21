"""Cursor-aware input and bounded completion over the shared workbench port."""
from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList, TextArea
from textual.widgets.option_list import Option


def single_line(value, width):
    text = Text(' '.join(str(value or '').split()), no_wrap=True, overflow='ellipsis')
    text.truncate(max(1, width), overflow='ellipsis', pad=True)
    return text


class PromptInput(TextArea):
    """Keep paste as text; route submit and popup keys before TextArea inserts."""

    BINDINGS = [Binding('f6', 'app.materials', show=False)]

    async def _on_key(self, event):
        panel = self.parent
        if event.key in {'enter', 'ctrl+enter'}:
            event.stop()
            event.prevent_default()
            if panel.has_candidates:
                panel.accept(submit=True)
            else:
                panel.post_message(InputPanel.Submitted())
        elif event.key in {'ctrl+j', 'shift+enter'}:
            event.stop()
            event.prevent_default()
            self.replace('\n', *self.selection, maintain_selection_offset=False)
        elif event.key == 'escape':
            event.stop()
            event.prevent_default()
            if panel.has_candidates:
                panel.dismiss_candidates()
            else:
                panel.post_message(InputPanel.Cancelled())
        elif event.key == 'tab' and panel.has_candidates:
            event.stop()
            event.prevent_default()
            panel.accept()
        else:
            await super()._on_key(event)

    async def _on_paste(self, event):
        event.stop()
        event.prevent_default()
        await super()._on_paste(event)

    def action_cursor_up(self, select=False):
        if self.parent.has_candidates and not select:
            self.parent.move_candidate(-1)
        else:
            super().action_cursor_up(select)

    def action_cursor_down(self, select=False):
        if self.parent.has_candidates and not select:
            self.parent.move_candidate(1)
        else:
            super().action_cursor_down(select)


class InputPanel(Vertical):
    class Submitted(Message):
        pass

    class Cancelled(Message):
        pass

    class CandidatesChanged(Message):
        pass

    class ReferenceSelected(Message):
        def __init__(self, candidate):
            super().__init__()
            self.candidate = candidate

    def __init__(self, client, context, text=''):
        super().__init__(id='input-panel')
        self.client, self.context, self.initial_text = client, context, text
        self.candidates, self.query = [], None
        self._generation, self._signature = 0, None
        self._timer = self._worker = None

    def compose(self):
        yield PromptInput(self.initial_text, id='composer')
        yield OptionList(id='completions')

    @property
    def editor(self):
        return self.query_one('#composer', TextArea)

    @property
    def has_candidates(self):
        return bool(self.candidates) and self.query_one('#completions').display

    def snapshot(self):
        editor = self.editor
        return (editor.text, editor.document.get_index_from_location(editor.cursor_location),
                editor.selection.is_empty, tuple(self.context().items()))

    def on_mount(self):
        self.query_one('#completions').display = False
        self.editor.move_cursor(self.editor.document.end)
        self.changed()

    def on_text_area_changed(self, event):
        event.stop()
        self.changed()

    def on_text_area_selection_changed(self, event):
        event.stop()
        self.changed()

    def on_resize(self):
        if self.is_mounted:
            self.resize_input()
            if self.has_candidates:
                self.render_candidates()

    def resize_input(self):
        # Wrapped lines count terminal cells, not Python characters or pixels.
        width = max(1, self.editor.content_size.width)
        lines = sum(max(1, (Text(line).cell_len + width - 1) // width) for line in self.editor.text.split('\n'))
        self.editor.styles.height = min(6, lines) + 2

    def invalidate(self):
        self._signature = None
        self.dismiss_candidates()

    def dismiss_candidates(self):
        self._generation += 1
        if self._timer:
            self._timer.stop()
        if self._worker:
            self._worker.cancel()
        self.candidates, self.query = [], None
        listing = self.query_one('#completions', OptionList)
        listing.highlighted = None
        listing.display = False
        self.post_message(self.CandidatesChanged())

    def changed(self):
        self.resize_input()
        signature = self.snapshot()
        if signature == self._signature:
            return
        self._signature = signature
        self.dismiss_candidates()
        text, cursor, empty, _ = signature
        # The registry remains authoritative. This cheap gate avoids RPCs for
        # ordinary prose and never scans files or defines a second parser.
        if not empty or not text or len(text) > 262144 or not any(ch in text[:cursor] for ch in '/@'):
            return
        generation = self._generation
        self._timer = self.set_timer(.12, lambda: self.start_query(signature, generation))

    def start_query(self, signature, generation):
        self._worker = self.run_worker(self.complete(signature, generation), group='completion', exclusive=True)

    async def complete(self, signature, generation):
        try:
            result = await self.client.operation('input.complete',
                {'text': signature[0], 'cursor': signature[1], **dict(signature[3])})
            if not self.is_mounted or generation != self._generation or signature != self.snapshot():
                return
            self.query = result.get('query')
            self.candidates = result.get('candidates', [])[:60] if self.query else []
            self.render_candidates()
        except Exception as exc:
            if generation == self._generation and self.is_mounted:
                self.dismiss_candidates()
                self.notify(str(exc), title='补全暂不可用', severity='warning')

    def render_candidates(self):
        listing = self.query_one('#completions', OptionList)
        selected = listing.highlighted or 0
        width = max(1, self.size.width - 2)
        labels = {'command': '命令', 'file': '文件', 'agent': 'Agent', 'run': '任务', 'channel': '频道', 'content': '资料'}
        rows = []
        for item in self.candidates:
            kind = '目录' if not item['terminal'] else labels.get(item['kind'], item['kind'])
            label, _, description = item['label'].partition(' - ')
            row = single_line(label, max(1, width - 9) if width < 65 else min(30, width // 3))
            if width >= 65:
                row.append('  ')
                detail = single_line(description, max(1, width - row.cell_len - 9))
                detail.stylize('dim')
                row.append(detail)
            row.append('  ' + kind, style='dim')
            rows.append(Option(row, id=item['key']))
        listing.clear_options().add_options(rows)
        listing.display = bool(rows)
        listing.styles.height = min(len(rows), 8, max(2, self.app.size.height // 3))
        listing.highlighted = min(selected, len(rows) - 1) if rows else None
        self.post_message(self.CandidatesChanged())

    def move_candidate(self, direction):
        listing = self.query_one('#completions', OptionList)
        listing.highlighted = ((listing.highlighted or 0) + direction) % len(self.candidates)
        listing.scroll_to_highlight()

    def on_option_list_option_selected(self, event):
        event.stop()
        self.accept(submit=True)

    def accept(self, *, submit=False):
        if not self.has_candidates or self.snapshot() != self._signature:
            return
        candidate = self.candidates[self.query_one('#completions', OptionList).highlighted or 0]
        query = self.query
        self.dismiss_candidates()
        editor = self.editor
        start = editor.document.get_location_from_index(query['start_pos'])
        end = editor.document.get_location_from_index(query['end_pos'])
        editor.replace(candidate.get('insert_text', ''), start, end, maintain_selection_offset=False)
        if candidate['terminal'] and candidate['kind'] != 'command':
            self.post_message(self.ReferenceSelected(candidate))
        continue_query = not candidate['terminal'] or (
            candidate['kind'] == 'command' and candidate.get('insert_text', '').endswith(' ') and not submit)
        # Finished entities stay closed; Tab on an argument-bearing command
        # immediately asks the shared resolver for its next parameter.
        self._signature = None if continue_query else self.snapshot()
        editor.focus()
        if continue_query:
            self.changed()
        elif submit and candidate.get('submit_on_accept'):
            self.post_message(self.Submitted())

    def trigger_mention(self):
        self.editor.focus()
        cursor = self.editor.document.get_index_from_location(self.editor.cursor_location)
        self.editor.insert((' ' if cursor and not self.editor.text[cursor - 1].isspace() else '') + '@',
                           maintain_selection_offset=False)
