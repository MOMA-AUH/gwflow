"""Lazy submission-log and recorded-history screens."""

import time

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ._browser_read import ReadOperation
from .browser import Breadcrumb, BrowserView, Shortcuts


def log_text(result):
    text = Text.from_ansi(result.get('content', ''))
    rendered = Text()
    if result.get('continued_before'):
        rendered.append('[continued line]\n', style='dim')
    for index, line in enumerate(text.split('\n', allow_blank=True)):
        if index:
            rendered.append('\n')
        for start in range(0, len(line), 2048):
            if start:
                rendered.append('\n[continued line]\n', style='dim')
            rendered.append(line[start:start + 2048])
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
        return Static(Text(self.context_notice(), style='yellow'), id='pinned-context')

    async def refresh_contents(self, navigation=None):
        self.query_one('.breadcrumb', Static).update(Breadcrumb(self.breadcrumb))
        self.query_one('#pinned-context', Static).update(Text(self.context_notice(), style='yellow'))


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
    BINDINGS = [Binding('u', 'reload', show=False), Binding('F', 'follow', show=False),
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

    def compose(self):
        yield self.heading()
        yield self.context()
        yield Static('Live log: off; F: follow/pause', id='follow-status')
        with VerticalScroll(classes='read-info'):
            yield Static('Reading log...', id='read-status')
        with LogPane(id='log-pane'):
            yield Static(f"Task: {self.observation['name']}\nPublic job: {self.job['name']}\n"
                         f"Attempt: {self.observation.get('attempt') or 'not recorded'}\n"
                         f"Submission: {self.job['submission'] or 'not submitted'}\n"
                         f"{self.stream}: {self.job['logs'].get(self.stream, 'unavailable')}", markup=False)
            yield Static(log_text(self.result), id='log-text')
        yield self.footer()

    def footer(self):
        return Static(Shortcuts('PgUp/PgDn Home/End Tab details', 'F follow u reload r Esc ? q quit'),
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
        self.query_one('#follow-status', Static).update('Live log: ' + self.follow_mode + '; F: follow/pause')
        self.follow_tick()

    def pause_follow(self):
        if self.follow_mode == 'following':
            self.follow_mode = 'paused'
            self.query_one('#follow-status', Static).update('Live log: paused; F: follow/pause')
            if self.operation is not None:
                self.operation.close()
                self.operation = None
                self.query_one('#read-status', Static).update(Text(self.previous_read_status()))
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
            previous = self.previous_read_status() + '\n' if self.read_error else ''
            self.query_one('#read-status', Static).update(Text(previous + 'Reading log...'))

    def previous_read_status(self):
        previous = ('Previously read ' + time.strftime('%H:%M:%S', time.localtime(self.result['finished']))
                    if self.result else 'No log content read')
        return previous + '; u: reload.' + ('\n' + self.read_error if self.read_error else '')

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
            self.query_one('#read-status', Static).update(Text(self.previous_read_status()))
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
        self.query_one('#log-text', Static).update(log_text(result))
        self.query_one('#read-status', Static).update(
            (self.follow_notice + '\n' if self.follow_notice else '') +
            'Log read at ' + time.strftime('%H:%M:%S', time.localtime(result['finished'])) +
            ('; 64 KiB byte limit' if any('byte limit' in notice for notice in result['notices']) else '') +
            f"; portion: bytes {result['start']}–{result['end']} of {result['size']}; up to 200 lines" +
            ('\n' + ' '.join(result['notices']) if result['notices'] else ''))
        self.app.call_after_refresh(self.restore_reading_position)

    def restore_reading_position(self):
        pane = self.query_one('#log-pane')
        if self.read_position == 'end':
            pane.anchor(pane.max_scroll_y > 0)
            pane.scroll_end(animate=False)
        elif self.read_position == 'start':
            pane.anchor(False)
            pane.scroll_home(animate=False)
        else:
            pane.anchor(False)
            pane.scroll_to(y=self.read_position, animate=False)

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
        self.query_one('#pinned-context', Static).update(Text(self.context_notice(), style='yellow'))
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
