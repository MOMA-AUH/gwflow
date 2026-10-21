"""Lazy submission-log and recorded-history screens."""

import time

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ._browser_read import ReadOperation
from .browser import Breadcrumb, BrowserView, Shortcuts, ValueField


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


class StreamChooser(PinnedView):
    def __init__(self, task, job, breadcrumb):
        super().__init__(task, [*breadcrumb, 'Choose log stream'], job)

    def compose(self):
        yield self.heading()
        yield self.context()
        yield Static('Choose log stream')
        with VerticalScroll():
            yield Static(f"Task: {self.observation['name']}\nPublic job: {self.job['name']}\n"
                         f"Attempt: {self.observation.get('attempt') or 'not recorded'}\n"
                         f"Submission: {self.job['submission'] or 'not submitted'}\n"
                         f"{'Recorded' if 'recorded_state' in self.job else 'Captured'} state: "
                         f"{self.job.get('recorded_state', self.job['observed_state'])}\n"
                         f"Backend job: {self.job['backend_id'] if self.job['backend_id'] is not None else 'unavailable'}",
                         markup=False)
        yield OptionList('stdout', 'stderr', classes='jobs')
        yield self.footer()

    def on_mount(self):
        self.set_focus(self.query_one(OptionList))

    def on_option_list_option_selected(self, event):
        stream = ('stdout', 'stderr')[event.option_index]
        self.app.switch_screen(LogView(self.observation, self.job, stream, self.breadcrumb[:-1]))

    def footer(self):
        return Static(Shortcuts('↑↓ choose  Enter open', 'Esc back  ? help  q quit'), classes='shortcuts')


class LogPane(VerticalScroll):
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
        self.wrap = False

    def compose(self):
        yield self.heading()
        with VerticalScroll(id='log-info') as information:
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
        return Static(Shortcuts('PgUp/Dn Home/End Tab', 'F u r Esc ? q quit'),
                      classes='shortcuts')

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
        at_end = pane.is_vertical_scroll_end or pane.max_scroll_y == 0
        pane.anchor(False)
        self.wrap = not self.wrap
        pane.set_class(self.wrap, 'wrapped')
        pane.border_subtitle = 'w: wrap ' + ('on' if self.wrap else 'off')
        self.query_one('#log-text', Static).update(log_text(self.result, wrap=self.wrap))
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
        self.result = result
        self.query_one('#empty-log', Static).display = result['size'] == 0
        self.query_one('#log-text', Static).update(log_text(result, wrap=self.wrap))
        self.update_read_information()
        self.app.call_after_refresh(self.restore_reading_position)

    def restore_reading_position(self):
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


class HistoryView(PinnedView):
    BINDINGS = [Binding('u', 'reload', show=False)]

    def __init__(self, task, storage, breadcrumb):
        super().__init__(task, [*breadcrumb, 'Task history'])
        self.storage = storage
        self.operation = None
        self.result = {}
        self.records = {}

    def compose(self):
        yield self.heading()
        yield self.context()
        yield Static('Task: ' + self.observation['name'] + '\nRecorded evidence only; no scheduler queries.', markup=False)
        with VerticalScroll(classes='read-info'):
            yield Static('Reading history...', id='read-status')
        yield OptionList(classes='listing')
        yield self.footer()

    def footer(self):
        return Static(Shortcuts('↑↓ select  Enter/l logs  u reload', 'r refresh  Esc back  ? help  q quit'), classes='shortcuts')

    def on_mount(self):
        self.set_focus(self.query_one(OptionList))
        self.action_reload()
        self.read_timer = self.set_interval(0.05, self.read_progress)

    def action_reload(self):
        if self.operation is None:
            self.operation = ReadOperation({'kind': 'history', 'storage': self.storage, 'task': self.observation['name']})
            self.query_one('#read-status', Static).update('Reading history...')

    def read_progress(self):
        if self.operation is None or not self.operation.completed():
            return
        try:
            result = self.operation.outcome()
        finally:
            self.operation.close()
            self.operation = None
        if 'error' in result:
            prefix = ('Previously read ' + time.strftime('%H:%M:%S', time.localtime(self.result['finished'])) +
                      '; u: reload.\n') if self.result else ''
            self.query_one('#read-status', Static).update(Text(prefix + result['error']))
            return
        self.result = result
        self.observation = {**self.observation, 'attempt': result['current_attempt']}
        self.update_context()
        navigation = self.capture_navigation()
        self.records = {}
        listing = self.query_one(OptionList)
        listing.clear_options()
        for group in result['groups']:
            listing.add_option(Option(Text(group['label'], style='bold'), disabled=True))
            if not group['jobs']:
                listing.add_option(Option('No recorded submissions', disabled=True))
            for job in group['jobs']:
                self.records[job['submission']] = (dict(name=self.observation['name'], attempt=group['attempt']), job)
                listing.add_option(Option(Text(f"{job['name']}: {job['recorded_state']}\nSubmission: {job['submission']}"),
                                          id=job['submission']))
        if not result['groups']:
            listing.add_option(Option('No recorded work', disabled=True))
        self.query_one('#read-status', Static).update(
            'History read at ' + time.strftime('%H:%M:%S', time.localtime(result['finished'])))
        if navigation['rows'][0][0] is not None:
            self.app.call_after_refresh(self.restore_navigation, navigation)
        else:
            listing.action_first()

    def selection(self):
        option = self.query_one(OptionList).highlighted_option
        return self.records.get(option.id, (None, None)) if option else (None, None)

    def on_option_list_option_selected(self, event):
        self.app.action_logs()

    def on_unmount(self):
        self.read_timer.stop()
        if self.operation is not None:
            self.operation.close()
            self.operation = None
