"""Lazy submission-log and recorded-history screens."""

import time

from rich.text import Text
from rich.segment import Segment
from textual.binding import Binding
from textual.containers import Vertical
from textual.strip import Strip
from textual.widgets import DataTable, Static

from ._browser_read import ReadOperation
from .browser import Breadcrumb, BrowserView, JobDetailsView, LogField, LogsPanel, ValueField, VimScroll
from .presentation import _VISUALS


def log_text(result, *, wrap=False):
    text = Text.from_ansi(result.get('content', ''))
    rendered = Text()
    if result.get('continued_before'):
        rendered.append('[continued line]\n', style='dim')
    for index, line in enumerate(text.split('\n', allow_blank=True)):
        if index:
            rendered.append('\n')
        segment_size = 2048 if wrap else max(1, len(line))
        for start in range(0, len(line), segment_size):
            if start:
                rendered.append('\n[continued line]\n', style='dim')
            rendered.append(line[start:start + segment_size])
    if result.get('continued_after'):
        rendered.append('\n[line continues in next portion]', style='dim')
    return rendered


class PinnedView(BrowserView):
    """Keep independently read evidence across changes to the status snapshot."""

    pinned = True

    def __init__(self, task, breadcrumb, job=None):
        super().__init__(breadcrumb)
        self.observation, self.job = task, job

    def context_notice(self):
        current = next((task for task in self.snapshot['tasks'] if task['name'] == self.observation['name']), None)
        if current is None:
            return 'Task no longer in the refreshed selection.'
        if self.job is not None:
            job = next((job for job in current['jobs'] if job['name'] == self.job['name']), None)
            if job is None:
                return 'Job no longer in the refreshed Task; viewing selected log.'
            if job['submission'] != self.job['submission']:
                return 'Current submission changed; viewing selected log.'
        if current.get('attempt') != self.observation.get('attempt'):
            return 'Current attempt changed; u: reload this view.'
        if not self.app.matches(current):
            return 'Task hidden by current view filters; keeping the selected evidence.'
        return ''

    def context(self):
        notice = self.context_notice()
        widget = Static(Text(notice, style='yellow'), id='pinned-context')
        widget.display = bool(notice)
        return widget

    async def refresh_contents(self, navigation=None):
        self.query_one('.breadcrumb', Static).update(Breadcrumb(self.breadcrumb))
        self.update_context()

    def update_context(self):
        notice = self.context_notice()
        context = self.query_one('#pinned-context', Static)
        context.update(Text(notice, style='yellow'))
        context.display = bool(notice)


class HistoricalJobView(PinnedView, JobDetailsView):
    """Use the common job layout while keeping the selected historical identity."""

    def __init__(self, task, job, breadcrumb):
        super().__init__(task, [*breadcrumb, job['local']], job)

    def compose(self):
        yield self.heading()
        yield self.context()
        yield from self.details()
        yield self.footer()

    def selection(self):
        return self.observation, self.job


class StreamChooser(PinnedView):
    def __init__(self, task, job, breadcrumb):
        super().__init__(task, [*breadcrumb, 'Choose log stream'], job)

    def compose(self):
        yield self.heading()
        yield self.context()
        yield Static('Choose log stream')
        with VimScroll():
            yield Static(f"Task: {self.observation['name']}\nPublic job: {self.job['name']}\n"
                         f"Attempt: {self.observation.get('attempt') or 'not recorded'}\n"
                         f"Submission: {self.job['submission'] or 'not submitted'}\n"
                         f"Captured state: {self.job['observed_state']}\n"
                         f"Backend job: {self.job['backend_id'] if self.job['backend_id'] is not None else 'unavailable'}",
                         markup=False)
        yield LogsPanel(self.job['logs'])
        yield self.footer()

    def on_mount(self):
        self.set_focus(self.query_one(LogField))

    def open_log(self, stream):
        self.app.switch_screen(LogView(self.observation, self.job, stream, self.breadcrumb[:-1]))

    def footer(self):
        return self.shortcut_bar(('Enter', 'Open'), ('↑↓/jk', 'Select stream'))


class LogPane(VimScroll):
    def action_scroll_up(self):
        # Mirror the screen-level Up binding: pause live following, then scroll.
        self.screen.pause_follow()
        self.scroll_up(animate=False)

    def on_scroll_up(self):
        self.screen.pause_follow()

    def on_mouse_scroll_up(self, event):
        if not event.ctrl and not event.shift:
            self.screen.pause_follow()

    def on_scroll_to(self, message):
        if message.y is not None and message.y < self.scroll_y:
            self.screen.pause_follow()


class LogView(PinnedView):
    DEFAULT_CSS = """
    LogView #log-info {
        height: auto; max-height: 45%; min-height: 4;
        border: round $foreground 25%; padding: 0 1; margin-bottom: 1;
    }
    LogView #log-pane {
        height: 1fr; min-height: 3; border: round $foreground 25%; padding: 0 1;
        overflow-x: auto;
    }
    LogView #log-text { width: auto; text-wrap: nowrap; }
    LogView #log-pane.wrapped #log-text { width: 1fr; text-wrap: wrap; }
    LogView #read-status { height: 1; text-wrap: nowrap; }
    LogView #log-info:focus, LogView #log-pane:focus { border: round $accent; }
    LogView #pinned-context { text-wrap: nowrap; text-overflow: ellipsis; }
    LogView.compact #log-info { height: 5; min-height: 5; max-height: 5; margin-bottom: 0; }
    LogView.compact #log-pane { scrollbar-size-horizontal: 0; }
    """

    BINDINGS = [Binding('u', 'reload', show=False), Binding('F', 'follow', show=False),
                Binding('w', 'toggle_wrap', show=False),
                Binding('home', 'beginning', show=False, priority=True),
                Binding('end', 'latest', show=False, priority=True),
                Binding('up', 'scroll_up', show=False, priority=True),
                Binding('pageup', 'older', show=False, priority=True),
                Binding('pagedown', 'newer', show=False, priority=True)]

    def __init__(self, task, job, stream, breadcrumb):
        super().__init__(task, [*breadcrumb, stream], job)
        self.stream = stream
        self.operation = None
        self.result = {}
        self.follow_mode = 'off'
        self.next_follow = 0.0
        self.follow_notice = ''
        self.read_error = ''
        self.read_error_kind = ''
        self.wrap = False

    def compose(self):
        yield self.heading()
        with VimScroll(id='log-info') as information:
            information.border_title = 'Log information'
            yield self.context()
            yield Static(self.read_status(), id='read-status')
            yield Static('Reading log...', id='read-notice')
            yield ValueField('Attempt:', self.observation.get('attempt') or 'not recorded',
                             label_width=len('Attempt:'))
            yield ValueField('Path:', self.job['logs'].get(self.stream, 'unavailable'), label_width=len('Attempt:'))
        with LogPane(id='log-pane') as content:
            content.border_title = self.stream
            content.border_subtitle = 'w: wrap off'
            empty = Static('Log is empty.', id='empty-log')
            empty.display = bool(self.result) and self.result['size'] == 0
            yield empty
            yield Static(log_text(self.result), id='log-text')
        yield self.footer()

    def read_status(self):
        timestamp = time.strftime('%H:%M:%S', time.localtime(self.result['finished'])) if self.result else '—'
        status = Text.assemble(('Read at:', 'bold'), f' {timestamp}')
        if self.follow_mode == 'following':
            status.append(' (live)', style='green')
        return status

    def update_read_information(self):
        self.query_one('#read-status', Static).update(self.read_status())
        notices = []
        if self.read_error:
            if self.read_error_kind == 'missing':
                notices = [f'No {self.stream} log available.',
                           'This submission may not have started, or its log may have been removed.']
                if self.result:
                    notices.insert(0, 'Previously read content; u: reload.')
            else:
                notices = [('Previously read content' if self.result else 'No log content read') + '; u: reload.',
                           self.read_error]
        elif self.result:
            result = self.result
            if self.follow_notice:
                notices.append(self.follow_notice)
            if result['start'] > 0 or result['end'] < result['size']:
                location = 'latest' if result['end'] == result['size'] else 'first' if result['start'] == 0 else 'middle'
                if any('byte limit' in notice for notice in result['notices']):
                    notices.append(f'Showing {location} portion (64 KiB byte limit).')
                else:
                    notices.append(f"Showing {location} {len(result['content'].splitlines())} lines.")
            notices.extend(notice for notice in result['notices'] if 'byte limit' not in notice)
        else:
            notices.append('Reading log...' if self.operation else 'No log content read; u: reload.')
        notice = self.query_one('#read-notice', Static)
        notice.update(Text('\n'.join(notices), style='yellow' if self.read_error else ''))
        notice.display = bool(notices)

    def footer(self):
        return self.shortcut_bar(('↑↓/jk', 'Scroll'), ('F', 'Live/manual'), ('u', 'Reload'), ('w', 'Wrap'),
                                 ('Tab', 'Switch panel'), ('←→', 'Scroll sideways'), ('PgUp/PgDn', 'Page'),
                                 ('Home', 'Beginning'), ('End', 'Latest'), ('r', 'Refresh'))

    def check_action(self, action, parameters):
        if action in ('beginning', 'latest', 'older', 'newer', 'scroll_up'):
            return self.focused is self.query_one('#log-pane')
        return True

    def on_mount(self):
        self.set_focus(self.query_one('#log-pane'))
        self.action_reload()
        self.read_timer = self.set_interval(0.05, self.read_progress)

    def action_follow(self):
        if self.follow_mode == 'following':
            self.pause_follow()
            return
        self.follow_mode = 'following'
        self.update_read_information()
        self.follow_tick()

    def action_toggle_wrap(self):
        pane = self.query_one('#log-pane')
        text = self.query_one('#log-text', Static)
        # Scrollbar layout can report overflow even when all text fits vertically.
        at_end = pane.is_vertical_scroll_end or text.size.height <= pane.scrollable_content_region.height
        pane.anchor(False)
        self.wrap = not self.wrap
        pane.set_class(self.wrap, 'wrapped')
        pane.border_subtitle = 'w: wrap ' + ('on' if self.wrap else 'off')
        text.update(log_text(self.result, wrap=self.wrap))
        if at_end:
            self.call_after_refresh(self.scroll_to_latest_display)

    def pause_follow(self):
        if self.follow_mode == 'following':
            self.follow_mode = 'off'
            if self.operation is not None:
                self.operation.close()
                self.operation = None
            self.update_read_information()
        pane = self.query_one('#log-pane')
        pane.anchor(False)
        self.read_position = pane.scroll_y

    def on_screen_suspend(self):
        if self.follow_mode == 'following':
            self.pause_follow()

    def follow_tick(self):
        if self.follow_mode == 'following' and self.operation is None:
            self.next_follow = time.monotonic() + 1
            self.read('backward', None, 'end', following=True)

    def action_reload(self):
        if self.follow_mode == 'following':
            self.follow_tick()
            return
        pane = self.query_one('#log-pane')
        if not self.result or self.result['end'] == self.result['size'] and pane.is_vertical_scroll_end:
            self.read('backward', None, 'end', reload=True)
        else:
            self.read('forward', self.result['start'], pane.scroll_y, reload=True)

    def action_beginning(self):
        self.pause_follow()
        self.read('forward', 0, 'start')

    def action_latest(self):
        self.read('backward', None, 'end')

    def action_older(self):
        self.pause_follow()
        pane = self.query_one('#log-pane')
        if pane.scroll_y > 0:
            pane.scroll_page_up(animate=False)
        elif self.result and self.result['start'] > 0:
            self.read('backward', self.result['start'], 'end')

    def action_scroll_up(self):
        self.pause_follow()
        self.query_one('#log-pane').scroll_up(animate=False)

    def action_newer(self):
        pane = self.query_one('#log-pane')
        if not pane.is_vertical_scroll_end:
            pane.scroll_page_down(animate=False)
        elif self.result and self.result['end'] < self.result['size']:
            self.read('forward', self.result['end'], 'start')

    def read(self, direction, offset, position, *, reload=False, following=False):
        if self.operation is None:
            pane = self.query_one('#log-pane')
            self.previous_position = 'end' if pane.is_vertical_scroll_end else pane.scroll_y
            self.read_position = position
            previous = ({key: self.result[key] for key in ('start', 'end', 'size', 'identity', 'digest')}
                        if self.result and not reload else None)
            self.operation = ReadOperation({'path': self.job['logs'].get(self.stream),
                                            'direction': direction, 'offset': offset, 'previous': previous,
                                            'following': following})
            if not self.result and not self.read_error:
                self.update_read_information()

    def read_progress(self):
        if self.operation is None and self.follow_mode == 'following' and time.monotonic() >= self.next_follow:
            self.follow_tick()
        if self.operation is None or not self.operation.completed():
            return
        try:
            result = self.operation.outcome()
        finally:
            self.operation.close()
            self.operation = None
        if 'error' in result:
            self.read_error = result['error']
            self.read_error_kind = result.get('error_kind', '')
            self.update_read_information()
            self.read_position = self.previous_position
            self.app.call_after_refresh(self.restore_reading_position)
            return
        notices = []
        if self.read_error:
            notices.append('Log readable again.')
        if result.get('change'):
            notices.append(result['change'] + '; reloaded latest content.')
        if result.get('gap'):
            notices.append('Output advanced beyond the previous portion; Home/PgUp access earlier content.')
        if notices:
            self.follow_notice = ' '.join(notices)
        self.read_error = ''
        self.read_error_kind = ''
        self.result = result
        self.query_one('#empty-log', Static).display = result['size'] == 0
        self.query_one('#log-text', Static).update(log_text(result, wrap=self.wrap))
        self.update_read_information()
        self.app.call_after_refresh(self.restore_reading_position)

    def restore_reading_position(self):
        # An app-level refresh callback may run after this log view is closed.
        if not self.is_attached:
            return
        pane = self.query_one('#log-pane')
        if self.read_position == 'end':
            self.scroll_to_latest_display()
        elif self.read_position == 'start':
            pane.anchor(False)
            pane.scroll_home(animate=False, x_axis=False)
        else:
            pane.anchor(False)
            pane.scroll_to(y=self.read_position, animate=False)

    def scroll_to_latest_display(self):
        pane = self.query_one('#log-pane')
        horizontal_position = pane.scroll_x
        # Anchor only overflowing text: scrollbar layout can temporarily report
        # overflow even for short content. Anchoring also resets the X position.
        pane.anchor(self.query_one('#log-text').size.height > pane.scrollable_content_region.height)
        pane.scroll_to(x=horizontal_position, animate=False, release_anchor=False)
        pane.scroll_end(animate=False, x_axis=False)

    def on_unmount(self):
        self.read_timer.stop()
        if self.operation is not None:
            self.operation.close()
            self.operation = None


class HistoryTable(DataTable):
    """Keep a thin separator below the headings while either axis scrolls."""

    BINDINGS = [Binding("j", "cursor_down", show=False), Binding("k", "cursor_up", show=False)]

    COMPONENT_CLASSES = {'history-header-rule'}
    DEFAULT_CSS = """
    HistoryTable > .history-header-rule { color: $foreground 20%; }
    """

    def render_line(self, y):
        if self.show_header and y == self.header_height - 1:
            return Strip([Segment('─' * self.size.width, self.get_component_rich_style('history-header-rule'))])
        return super().render_line(y)


class HistoryPanel(Vertical):
    """Read earlier submissions independently of the Task's status snapshot."""

    DEFAULT_CSS = """
    HistoryPanel { height: 1fr; min-height: 6; margin-top: 1; border: round $foreground 25%; }
    HistoryPanel:focus-within { border: round $accent; }
    HistoryPanel #history-status { height: auto; max-height: 3; margin: 0 1; }
    HistoryPanel #history-jobs { height: 1fr; border: none; padding: 0 1; scrollbar-size-horizontal: 1; }
    .compact HistoryPanel { min-height: 6; margin-top: 0; }
    .compact HistoryPanel #history-status { display: none; }
    """

    def __init__(self, task, storage):
        super().__init__(id='history-panel')
        self.task_name = task['name']
        self.storage = storage
        self.operation = None
        self.result = {}
        self.records = {}
        self.border_title = 'History'
        self.border_subtitle = 'Recorded evidence · ←→ scroll'

    def compose(self):
        yield Static('Reading history...', id='history-status')
        table = HistoryTable(id='history-jobs', cursor_type='row', zebra_stripes=True, header_height=2,
                             cursor_foreground_priority='renderable')
        table.add_columns('Job', 'Recorded state', 'History', 'Attempt', 'Submission')
        table.add_row(Text('Reading history...'), '', '', '', '')
        yield table

    def on_mount(self):
        self.reload()
        self.read_timer = self.set_interval(0.05, self.read_progress)

    def reload(self):
        if self.storage is None:
            self.query_one('#history-status', Static).update('History unavailable.')
            self.query_one(DataTable).clear().add_row(Text('History unavailable.'), '', '', '', '')
        elif self.operation is None:
            self.operation = ReadOperation({'kind': 'history', 'storage': self.storage, 'task': self.task_name})
            status = self.query_one('#history-status', Static)
            status.update('Reading history...')
            status.display = True

    def read_progress(self):
        if self.operation is None or not self.operation.completed():
            return
        try:
            result = self.operation.outcome()
        finally:
            self.operation.close()
            self.operation = None
        table = self.query_one(DataTable)
        if 'error' in result:
            status = self.query_one('#history-status', Static)
            status.update(Text(result['error'], style='yellow'))
            status.display = True
            if not self.result:
                table.clear().add_row(Text(result['error'], style='yellow'), '', '', '', '')
            return
        selected = table.ordered_rows[table.cursor_row].key.value if table.row_count else None
        position = table.scroll_offset
        self.result = result
        self.records = {}
        table.clear()
        for group in result['groups']:
            category = Text('Earlier submission', style='cyan') if group['kind'] == 'retries' else Text('Previous attempt', style='magenta')
            if not group['jobs']:
                table.add_row(Text('No recorded submissions'), '', category, Text(group['attempt']), '',
                              key='empty:' + group['attempt'])
            for job in group['jobs']:
                self.records[job['submission']] = (dict(name=self.task_name, attempt=group['attempt']), job)
                symbol, style = _VISUALS.get(job['state'], ('?', 'yellow'))
                state = Text(f"{symbol} {job['recorded_state']}", style=style)
                table.add_row(Text(job['local']), state, category, Text(group['attempt']), Text(job['submission']),
                              key=job['submission'])
        if not table.row_count:
            table.add_row(Text('No earlier submissions.'), '', '', '', '')
        self.query_one('#history-status', Static).display = False
        indices = {row.key.value: index for index, row in enumerate(table.ordered_rows)}
        if selected is not None and selected in indices:
            table.move_cursor(row=indices[selected], scroll=False)
            self.call_after_refresh(table.scroll_to, position.x, position.y, animate=False)
        else:
            table.move_cursor(row=0)

    def selection(self):
        table = self.query_one(DataTable)
        key = table.ordered_rows[table.cursor_row].key.value if table.row_count else None
        return self.records.get(key, (None, None))

    def on_data_table_row_selected(self, event):
        event.stop()
        task, job = self.selection()
        if task is not None and job is not None:
            self.app.push_screen(HistoricalJobView(task, job, self.screen.breadcrumb))

    def on_unmount(self):
        self.read_timer.stop()
        if self.operation is not None:
            self.operation.close()
            self.operation = None
