"""Lazy log and history reads through the browser's public keyboard interface."""

from pathlib import Path
import asyncio
import fcntl
import gc
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import weakref
from unittest.mock import patch

from rich.console import Console
from textual.widgets import DataTable, OptionList, Static

from fixtures.browser_workload import CapturedBrowser, captured_snapshot
from gwflow._browser_read import ReadOperation
from gwflow.browser import JobView, LogField, StatusBrowser, ValueField

from support import FIXTURES, LocalBackendTestCase
import test_browser_interaction
import test_graphs


class StatusLogInteractionTests(LocalBackendTestCase, unittest.IsolatedAsyncioTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow
    settle = test_graphs.TaskGraphTests.settle
    inject = test_graphs.TaskGraphTests.inject
    browser = test_browser_interaction.BrowserInteractionTests.browser
    shown = test_browser_interaction.BrowserInteractionTests.shown

    async def history_loaded(self, app, pilot):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            panel = app.screen.query_one('#history-panel')
            if panel.result and panel.operation is None:
                await pilot.pause()
                return panel
            await pilot.pause(0.05)
        self.fail('History did not finish loading')

    async def test_selected_job_opens_both_exact_streams_without_reinspection(self):
        self.configure_workflow(left_command="echo selected-stdout; echo selected-stderr >&2; printf left > same.txt")
        self.run_complete()
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            f"\nwith open({str(self.work / 'log-imports')!r}, 'a') as stream: stream.write('import\\n')\n")
        app = self.browser(selectors=('sample',), details=True)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, '5/5')
            inspected = app.snapshot['finished']
            await pilot.press('down', 'down', 'down', 'l')
            await self.shown(app, pilot, 'Choose log stream', timeout=3)
            await pilot.press('enter')
            await self.shown(app, pilot, 'selected-stdout')
            self.assertEqual(app.snapshot['finished'], inspected)
            await pilot.press('escape', 'l', 'down', 'enter')
            await self.shown(app, pilot, 'selected-stderr')
            await pilot.press('u')
            await self.shown(app, pilot, 'selected-stderr')
            self.assertEqual(app.snapshot['finished'], inspected)
            self.assertEqual((self.work / 'log-imports').read_text(), 'import\n')
            await pilot.press('q')

    async def test_task_history_distinguishes_retries_and_attempts_without_scheduler_observation(self):
        failed = self.work / 'fail-right'
        self.configure_workflow(right_command=(f'if test -f "{failed}"; then echo earlier-failed-log; exit 8; fi; '
                                               'echo successful-right; printf right > same.txt'))
        self.run_complete()
        first = self.cli('logs', 'sample', '--details')
        old_attempt = re.search(r'current attempt (\w+)', first).group(1)
        failed.touch()
        self.cli('run', '--force')
        self.settle()
        failed_index = self.cli('logs', 'sample', '--details')
        earlier = re.search(r'sample__right: failed; submission (\S+)', failed_index).group(1)
        failed.unlink()
        self.cli('run')
        self.settle()
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            f"\nwith open({str(self.work / 'history-imports')!r}, 'a') as stream: stream.write('import\\n')\n")
        app = StatusBrowser((['--file', str(workflow), '--backend', 'recovery_fixture'], ('sample',)),
                            selectors=('sample',))
        self.addCleanup(app.close_inspection)
        with patch.dict(os.environ, self.inject(record_status=True)):
            async with app.run_test(size=(120, 40)) as pilot:
                await self.shown(app, pilot, '5/5')
                observed = (self.work / 'backend-observations.jsonl').read_bytes()
                inspected = app.snapshot['finished']
                await pilot.press('enter')
                history = await self.history_loaded(app, pilot)
                current_submissions = {job['submission'] for job in app.snapshot['tasks'][0]['jobs']}
                self.assertTrue(current_submissions.isdisjoint(history.records))
                self.assertNotIn('History read at', test_browser_interaction.visible_text(app))
                task_view = app.screen
                jobs = task_view.query_one('#task-jobs', OptionList)
                self.assertIs(task_view.focused, jobs)
                await pilot.press('h')
                self.assertIs(app.screen, task_view)
                self.assertIs(task_view.focused, jobs)
                await pilot.press('enter', 'h')
                self.assertIsInstance(app.screen, JobView)
                await pilot.press('escape', 'tab')
                listing = app.screen.query_one('#history-jobs', DataTable)
                self.assertIs(app.screen.focused, listing)
                prompts = '\n'.join(str(cell) for row in range(listing.row_count) for cell in listing.get_row_at(row))
                self.assertNotIn('Current recorded attempt', prompts)
                self.assertIn('Earlier submission', prompts)
                self.assertIn('Previous attempt', prompts)
                self.assertIn(old_attempt, prompts)
                self.assertIn(earlier, prompts)
                self.assertIn('Recorded evidence', test_browser_interaction.visible_text(app))
                for _ in range(20):
                    if listing.get_row_at(listing.cursor_row)[-1].plain == earlier:
                        break
                    await pilot.press('down')
                self.assertEqual(listing.get_row_at(listing.cursor_row)[-1].plain, earlier)
                self.assertTrue(all(row.height == 1 for row in listing.ordered_rows))
                self.assertEqual(listing.render_line(1).text, '─' * listing.size.width)
                self.assertEqual(listing.get_row_at(listing.cursor_row)[1].style, 'bold red')
                await pilot.press('right', 'right', 'right')
                await pilot.pause()
                self.assertGreater(listing.scroll_x, 0)
                horizontal_position = listing.scroll_x
                await pilot.press('u')
                await self.history_loaded(app, pilot)
                self.assertEqual(listing.get_row_at(listing.cursor_row)[-1].plain, earlier)
                self.assertEqual(listing.scroll_x, horizontal_position)
                await pilot.press('home')
                history = task_view.query_one('#history-panel')
                jobs_panel = task_view.query_one('.jobs-panel')
                self.assertGreaterEqual(history.region.y, jobs_panel.region.bottom)
                self.assertGreater(history.size.height, jobs_panel.size.height)
                for size in ((80, 24), (40, 12), (120, 40)):
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    self.assertIs(task_view.focused, listing)
                    self.assertGreater(listing.content_size.height, 0)
                    self.assertIn('History', test_browser_interaction.visible_text(app))
                    self.assertEqual(listing.get_row_at(listing.cursor_row)[-1].plain, earlier)
                await pilot.press('enter')
                historical = app.screen
                self.assertEqual([panel.border_title for panel in historical.query('.detail-panel')], ['Job', 'Logs'])
                self.assertIn('Recorded state:', test_browser_interaction.visible_text(app))
                self.assertNotIn('Choose log stream', test_browser_interaction.visible_text(app))
                self.assertEqual(historical.selection()[1]['submission'], earlier)
                self.assertEqual(historical.focused.id, 'log-stdout')
                await pilot.press('down')
                self.assertEqual(historical.focused.id, 'log-stderr')
                await pilot.press('tab')
                self.assertEqual(historical.focused.id, 'log-stdout')
                await pilot.press('l')
                self.assertIs(app.screen, historical)
                await pilot.press('enter')
                await self.shown(app, pilot, 'earlier-failed-log')
                await pilot.press('escape')
                self.assertIs(app.screen, historical)
                self.assertEqual(historical.focused.id, 'log-stdout')
                await pilot.press('escape')
                self.assertIs(app.screen, task_view)
                self.assertIs(task_view.focused, listing)
                self.assertEqual(listing.get_row_at(listing.cursor_row)[-1].plain, earlier)
                await pilot.press('l')
                self.assertIsInstance(app.screen, type(historical))
                self.assertEqual(app.screen.selection()[1]['submission'], earlier)
                await pilot.press('escape')
                self.assertEqual(app.snapshot['finished'], inspected)
                self.assertEqual((self.work / 'history-imports').read_text(), 'import\n')
                self.assertEqual((self.work / 'backend-observations.jsonl').read_bytes(), observed)
                await pilot.press('q')

    async def test_history_reload_is_independent_of_refresh_and_survives_cleanup(self):
        app = self.browser()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('enter', 'enter')
            await self.shown(app, pilot, 'No earlier submissions.')
            panel = app.screen.query_one('#history-panel')
            read_before = panel.result
            self.run_complete()
            self.cli('run', '--force')
            self.finish()
            self.cli('clean', '--delete')
            with patch('gwflow.browser_evidence.ReadOperation', wraps=ReadOperation) as read:
                await pilot.press('r')
                await self.shown(app, pilot, 'reusable')
                self.assertIs(app.screen.query_one('#history-panel'), panel)
                self.assertIs(panel.result, read_before)
                read.assert_not_called()
                inspected = app.snapshot['finished']
                await pilot.press('u')
                await self.shown(app, pilot, 'Previous attempt')
                read.assert_called_once_with({'kind': 'history', 'storage': panel.storage, 'task': panel.task_name})
            self.assertEqual(app.snapshot['finished'], inspected)
            listing = panel.query_one(DataTable)
            prompts = [listing.get_row_at(row) for row in range(listing.row_count)]
            owner = self.work / '.gwf/gwflow/owner.json'
            saved = owner.read_bytes()
            owner.unlink()
            await pilot.press('u')
            await self.shown(app, pilot, 'Missing or malformed managed ownership')
            self.assertEqual([listing.get_row_at(row) for row in range(listing.row_count)], prompts)
            owner.write_bytes(saved)
            (self.work / 'workflow.py').write_text('from gwflow import Workflow\ngwf = Workflow()\n')
            await pilot.press('r')
            await self.shown(app, pilot, 'Showing 0 of 0 selected Tasks')
            self.assertFalse(app.screen.query('#history-panel'))
            await pilot.press('q')

    async def test_closing_and_quitting_during_a_blocked_history_read_reaps_its_process(self):
        app = self.browser(selectors=('sample',))
        children = Path(f'/proc/{os.getpid()}/task/{os.getpid()}/children')
        async with app.run_test(size=(80, 24)) as pilot:
            await self.shown(app, pilot, '0/5')
            baseline = set(children.read_text().split())
            with (self.work / '.gwf/gwflow-submission.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                for key in ('escape', 'q'):
                    await pilot.press('enter')
                    await self.shown(app, pilot, 'Reading history...')
                    self.assertGreater(len(set(children.read_text().split()) - baseline), 0)
                    await pilot.press(key)
                    await pilot.pause()
                    if key == 'escape':
                        await self.shown(app, pilot, '0/5')
                        self.assertEqual(set(children.read_text().split()), baseline)
        self.assertEqual(set(children.read_text().split()), baseline)
        self.assertEqual(app.return_value, 0)

    async def test_empty_previous_attempt_is_visible_without_any_recorded_submissions(self):
        interrupted = subprocess.run([sys.executable, str(FIXTURES / 'frontend_fault.py'), str(self.work),
                                      'after_ready', 'sample', 'run'], cwd=self.work,
                                     capture_output=True, text=True, timeout=30)
        self.assertEqual(interrupted.returncode, 93, interrupted.stdout + interrupted.stderr)
        previous = re.search(r'current attempt (\w+)', self.cli('logs', 'sample', '--details')).group(1)
        self.cli('run', '--force')
        self.finish()
        app = self.browser(selectors=('sample',))
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, '5/5')
            await pilot.press('enter')
            await self.history_loaded(app, pilot)
            table = app.screen.query_one('#history-jobs', DataTable)
            prompts = '\n'.join(str(cell) for row in range(table.row_count) for cell in table.get_row_at(row))
            self.assertIn('Previous attempt', prompts)
            self.assertIn(previous, prompts)
            self.assertIn('No recorded submissions', prompts)
            await pilot.press('q')


class StatusLogWindowTests(unittest.IsolatedAsyncioTestCase):
    shown = test_browser_interaction.BrowserInteractionTests.shown

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='gwflow-log-view-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stdout = self.root / 'exact.stdout'
        self.stderr = self.root / 'exact.stderr'

    def snapshot(self):
        snapshot = captured_snapshot()
        task = snapshot['tasks'][0]
        job = task['jobs'][0]
        job.update(local='compute', name='sample__compute', submission='sample__compute__' + '1' * 32 + '__' + '2' * 32,
                   logs={'stdout': str(self.stdout), 'stderr': str(self.stderr)})
        task.update(name='sample', attempt='3' * 32, jobs=[job], completed=1, total=1)
        snapshot.update(tasks=[task], total=1, notices='',
                        groups=[dict(key='sample@captured', label='sample', members=['sample'])])
        return snapshot

    def browser(self):
        return CapturedBrowser(selectors=('*',), details=True, snapshot_factory=self.snapshot)

    def content(self, app):
        return app.screen.query_one('#log-text', Static).content.plain

    async def test_closing_log_before_deferred_read_position_update_keeps_browser_usable(self):
        self.stdout.write_text('DEFERRED_LOG_CONTENT\n')
        app = self.browser()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'DEFERRED_LOG_CONTENT')
            view = app.screen
            deferred = []
            schedule = app.call_after_refresh

            def delay_position_update(callback, *args, **kwargs):
                if callback == view.restore_reading_position:
                    deferred.append((callback, args, kwargs))
                    return True
                return schedule(callback, *args, **kwargs)

            with patch.object(app, 'call_after_refresh', side_effect=delay_position_update):
                self.stdout.write_text('RELOADED_LOG_CONTENT\n')
                await pilot.press('u')
                await self.shown(app, pilot, 'RELOADED_LOG_CONTENT')
                self.assertTrue(deferred)
                await pilot.press('escape')
                await pilot.pause()
            self.assertFalse(view.is_attached)
            self.assertIsNone(view.operation)
            for callback, args, kwargs in deferred:
                schedule(callback, *args, **kwargs)
            await pilot.pause()
            await pilot.press('l', 'enter')
            await self.shown(app, pilot, 'RELOADED_LOG_CONTENT', timeout=3)
            await pilot.press('q')

    async def test_historical_job_details_keep_identity_and_stream_focus_across_refresh(self):
        from gwflow.browser_evidence import HistoricalJobView

        self.stderr.write_text('HISTORICAL_STDERR\n')
        app = self.browser()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('enter')
            task = dict(name='sample', attempt='historical-attempt')
            job = {**self.snapshot()['tasks'][0]['jobs'][0], 'submission': 'historical-submission',
                   'recorded_state': 'uncertain', 'observed_state': 'unknown'}
            view = HistoricalJobView(task, job, app.screen.breadcrumb)
            await app.push_screen(view)
            await pilot.pause()
            for size in ((100, 30), (40, 12), (120, 40)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                self.assertEqual(view.focus_chain, list(view.query(LogField)))
                self.assertTrue(all(field.size.height == 1 for field in view.query(ValueField)))
            self.assertIn('Recorded state: ? uncertain', test_browser_interaction.visible_text(app))
            self.assertEqual(next(field.value for field in view.query(ValueField) if field.label == 'Attempt:'),
                             'historical-attempt')
            await pilot.press('down')
            await app.accept_inspection(self.snapshot())
            self.assertIs(app.screen, view)
            self.assertEqual(view.selection(), (task, job))
            self.assertEqual(view.focused.id, 'log-stderr')
            await pilot.press('enter')
            await self.shown(app, pilot, 'HISTORICAL_STDERR')
            self.assertEqual(app.screen.job['submission'], 'historical-submission')
            await pilot.press('escape')
            self.assertIs(app.screen, view)
            self.assertEqual(view.focused.id, 'log-stderr')
            replacement = self.snapshot()
            replacement.update(tasks=[], groups=[], total=0)
            await app.accept_inspection(replacement)
            self.assertIs(app.screen, view)
            self.assertIn('Task no longer in the refreshed selection', test_browser_interaction.visible_text(app))
            await pilot.press('escape')
            await self.shown(app, pilot, 'Showing 0 of 0 selected Tasks')
            await pilot.press('q')

    async def test_log_information_and_content_have_separate_panels(self):
        self.stdout.write_text('PANEL_CONTENT\n')
        app = self.browser()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('down', 'enter', 'enter')
            await self.shown(app, pilot, 'PANEL_CONTENT')
            information = app.screen.query_one('#log-info')
            log = app.screen.query_one('#log-pane')
            self.assertEqual(information.border_title, 'Log information')
            self.assertEqual(log.border_title, 'stdout')
            self.assertEqual(len(information.query(ValueField)), 2)
            self.assertFalse(log.query(ValueField))
            self.assertIs(app.screen.focused, log)
            self.assertLessEqual(information.size.height, 5)
            visible = test_browser_interaction.visible_text(app)
            self.assertNotIn('(manual)', visible)
            self.assertNotIn('(live)', visible)
            self.assertIn('Attempt: ' + '3' * 32, visible)
            self.assertIn('Path:', visible)
            for label in ('Task:', 'Public job:', 'Submission:', 'Details', 'Portion:', 'Showing latest'):
                self.assertNotIn(label, visible)
            read_status = app.screen.query_one('#read-status', Static).content
            await pilot.press('tab')
            self.assertIs(app.screen.focused, information)
            self.assertEqual(app.screen.query_one('#read-status', Static).content, read_status)
            await pilot.press('tab')
            self.assertIs(app.screen.focused, log)
            for size in ((80, 24), (120, 40), (40, 12)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                self.assertLessEqual(information.region.bottom, log.region.y)
                self.assertGreaterEqual(log.content_size.height, 1)
                self.assertIn('PANEL_CONTENT', test_browser_interaction.visible_text(app))
                if size == (120, 40):
                    self.assertGreater(log.size.height, information.size.height)
            await pilot.press('tab')
            self.assertIs(app.screen.focused, information)
            await self.shown(app, pilot, 'Attempt:')
            self.assertEqual(self.content(app), 'PANEL_CONTENT\n')
            await pilot.press('tab')
            self.assertIs(app.screen.focused, log)
            self.stdout.write_text('')
            await pilot.press('u')
            await self.shown(app, pilot, 'Log is empty.')
            self.assertEqual(self.content(app), '')
            self.assertFalse(app.screen.query_one('#read-notice').display)
            self.stdout.write_text('PANEL_CONTENT_AGAIN\n')
            await pilot.press('u')
            await self.shown(app, pilot, 'PANEL_CONTENT_AGAIN')
            self.assertFalse(app.screen.query_one('#empty-log').display)
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')

    async def test_job_log_fields_open_selected_stream_and_restore_focus(self):
        self.stdout.write_text('DIRECT_STDOUT\n')
        self.stderr.write_text('DIRECT_STDERR\n')
        app = self.browser()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('down', 'enter')
            self.assertIsInstance(app.screen, JobView)
            job_view = app.screen
            self.assertEqual(next(field.value for field in job_view.query(ValueField) if field.label == 'Attempt:'),
                             '3' * 32)
            await pilot.press('l')
            self.assertIs(app.screen, job_view)
            for size in ((80, 24), (40, 12), (120, 40)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                fields = list(app.screen.query(LogField))
                self.assertEqual(app.screen.focus_chain, fields)
                self.assertIs(app.screen.focused, fields[0])
                await pilot.press('up')
                self.assertIs(app.screen.focused, fields[0])
                await pilot.press('down', 'down')
                self.assertIs(app.screen.focused, fields[1])
                await pilot.press('enter')
                await self.shown(app, pilot, 'DIRECT_STDERR')
                self.assertEqual(app.screen.stream, 'stderr')
                await pilot.press('escape')
                self.assertIs(app.screen.focused, fields[1])
                await pilot.press('up')
                self.assertIs(app.screen.focused, fields[0])
                await pilot.press('enter')
                await self.shown(app, pilot, 'DIRECT_STDOUT')
                self.assertEqual(app.screen.stream, 'stdout')
                await pilot.press('escape')
                self.assertIs(app.screen.focused, fields[0])
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')

    async def test_tail_paging_jumps_and_reload_keep_status_and_reading_position(self):
        self.stdout.write_text(''.join(f'row {index:06d} payload\n' for index in range(500000)))
        app = self.browser()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            inspected = app.snapshot['finished']
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'row 499999')
            self.assertEqual(len(self.content(app).splitlines()), 200)
            self.assertTrue(self.content(app).startswith('row 499800'))
            await pilot.press('home')
            await self.shown(app, pilot, 'row 000000', timeout=3)
            self.assertEqual(len(self.content(app).splitlines()), 200)
            pane = app.screen.query_one('#log-pane')
            for _ in range(200):
                if pane.is_vertical_scroll_end:
                    break
                await pilot.press('pagedown')
                await pilot.pause()
            self.assertTrue(pane.is_vertical_scroll_end)
            await pilot.press('pagedown')
            await self.shown(app, pilot, 'row 000200')
            self.assertTrue(self.content(app).startswith('row 000200'), self.content(app)[:100])
            await pilot.press('pageup')
            await self.shown(app, pilot, 'row 000199')
            await pilot.press('end')
            await self.shown(app, pilot, 'row 499999')
            with self.stdout.open('a') as stream:
                stream.write('row 500000 appended\n')
            await pilot.press('u')
            await self.shown(app, pilot, 'row 500000')
            await pilot.press('home')
            await self.shown(app, pilot, 'row 000000')
            await pilot.press('pagedown', 'pagedown')
            await pilot.wait_for_scheduled_animations()
            position = app.screen.query_one('#log-pane').scroll_y
            with self.stdout.open('a') as stream:
                stream.write('row 500001 appended\n')
            await pilot.press('u')
            await self.shown(app, pilot, 'Read at:')
            await pilot.wait_for_scheduled_animations()
            self.assertEqual(app.screen.query_one('#log-pane').scroll_y, position)
            self.assertTrue(self.content(app).startswith('row 000000'))
            self.assertEqual(app.snapshot['finished'], inspected)
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')

    async def test_refresh_keeps_exact_log_and_position_then_esc_finds_surviving_parent(self):
        self.stdout.write_text(''.join(f'pinned {index:04d}\n' for index in range(800)))
        app = CapturedBrowser(snapshot_factory=self.snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('enter', 'enter', 'enter', 'enter')
            await self.shown(app, pilot, 'pinned 0799')
            await pilot.press('home')
            await self.shown(app, pilot, 'pinned 0000')
            await pilot.press('pagedown')
            await pilot.wait_for_scheduled_animations()
            content = self.content(app)
            position = app.screen.query_one('#log-pane').scroll_y
            read_status = app.screen.query_one('#read-status', Static).content
            newer = self.snapshot()
            newer['tasks'][0]['attempt'] = '4' * 32
            newer['tasks'][0]['jobs'][0].update(submission='new-current-submission',
                                               logs={'stdout': str(self.root / 'new.stdout')})
            (self.root / 'new.stdout').write_text('NEW LOG MUST NOT REPLACE PINNED TEXT\n')
            await app.accept_inspection(newer)
            await self.shown(app, pilot, 'Current submission changed', timeout=3)
            self.assertEqual(self.content(app), content)
            self.assertEqual(app.screen.query_one('#log-pane').scroll_y, position)
            self.assertEqual(app.screen.query_one('#read-status', Static).content, read_status)
            removed = self.snapshot()
            removed.update(tasks=[], groups=[], total=0)
            await app.accept_inspection(removed)
            await self.shown(app, pilot, 'Task no longer in the refreshed selection', timeout=3)
            self.assertEqual(self.content(app), content)
            self.assertEqual(app.screen.query_one('#log-pane').scroll_y, position)
            await pilot.press('escape')
            await self.shown(app, pilot, 'Showing 0 of 0 selected Tasks', timeout=3)
            self.assertEqual(len(app.screen_stack), 2)
            await pilot.press('q')

    async def test_replacement_truncation_and_missing_file_keep_previous_portion_until_reload(self):
        self.stdout.write_text(''.join(f'original {index:04d}\n' for index in range(800)))
        app = self.browser()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'original 0799')
            original = self.content(app)
            replacement = self.root / 'replacement'
            replacement.write_text('replacement content\n' * 20)
            replacement.replace(self.stdout)
            await pilot.press('home')
            await self.shown(app, pilot, 'Log replaced', timeout=3)
            self.assertEqual(self.content(app), original)
            await pilot.press('u')
            await self.shown(app, pilot, 'replacement content')
            self.assertNotIn('original', self.content(app))
            previous = self.content(app)
            self.stdout.write_text('truncated content\n')
            await pilot.press('end')
            await self.shown(app, pilot, 'Log truncated', timeout=3)
            self.assertEqual(self.content(app), previous)
            await pilot.press('u')
            await self.shown(app, pilot, 'truncated content')
            self.stdout.unlink()
            await pilot.press('home')
            await self.shown(app, pilot, 'No stdout log available.', timeout=3)
            self.assertEqual(self.content(app), 'truncated content\n')
            self.assertIn('previously read', test_browser_interaction.visible_text(app).casefold())
            self.stdout.write_text('readable again\n')
            await pilot.press('u')
            await self.shown(app, pilot, 'readable again')
            self.assertNotIn('No stdout log available.', test_browser_interaction.visible_text(app))
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')

    async def test_log_formatting_preserves_styles_and_text_but_neutralizes_terminal_controls(self):
        original = (b'before\x1b[2J\x1b[Hafter\n\x1b[1;31mred bold\x1b[0m normal\n'
                    b'progress-one\rprogress-two\ninvalid: \xff\nnull: \x00\n'
                    b'\x1b]0;do not change title\x07visible end\n')
        self.stdout.write_bytes(original)
        app = self.browser()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'visible end')
            text = app.screen.query_one('#log-text', Static).content
            for expected in ('beforeafter', 'red bold normal', 'progress-one', 'progress-two', 'invalid: �', 'null: ␀'):
                self.assertIn(expected, text.plain)
            self.assertNotIn('do not change title', text.plain)
            style = text.get_style_at_offset(Console(), text.plain.index('red bold'))
            self.assertTrue(style.bold)
            self.assertEqual(style.color.number, 1)
            self.assertIn('Undecodable UTF-8', test_browser_interaction.visible_text(app))
            self.assertIn('Terminal controls', test_browser_interaction.visible_text(app))
            self.assertEqual(self.stdout.read_bytes(), original)
            await pilot.press('q')

    async def test_logs_open_unwrapped_and_toggle_without_rereading(self):
        original = 'START ' + 'x' * 3000 + ' FINISH\n'
        self.stdout.write_text(original)
        app = self.browser()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'START')
            pane = app.screen.query_one('#log-pane')
            text = app.screen.query_one('#log-text', Static)
            self.assertEqual(self.content(app), original)
            self.assertEqual(text.size.height, 2)
            self.assertGreater(pane.max_scroll_x, 0)
            self.assertNotIn('FINISH', test_browser_interaction.visible_text(app))
            await pilot.press('right')
            await pilot.wait_for_scheduled_animations()
            self.assertGreater(pane.scroll_x, 0)
            pane.scroll_to(x=pane.max_scroll_x, animate=False)
            await self.shown(app, pilot, 'FINISH')
            horizontal_position = pane.scroll_x
            await pilot.press('F')
            await pilot.pause(1.2)
            self.assertEqual(pane.scroll_x, horizontal_position)
            await pilot.press('F')
            status = app.screen.query_one('#read-status', Static).content
            self.stdout.unlink()  # Toggling only changes the already loaded portion.
            await pilot.press('w')
            await self.shown(app, pilot, 'wrap on')
            self.assertEqual(pane.max_scroll_x, 0)
            self.assertGreater(text.size.height, 2)
            await self.shown(app, pilot, 'FINISH')
            await pilot.press('w')
            await self.shown(app, pilot, 'wrap off')
            self.assertEqual(self.content(app), original)
            self.assertEqual(text.size.height, 2)
            self.assertGreater(pane.max_scroll_x, 0)
            self.assertEqual(app.screen.query_one('#read-status', Static).content, status)
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')

    async def test_huge_utf8_line_is_segmented_and_every_byte_remains_accessible(self):
        original = 'BEGIN ' + ''.join(f' checkpoint_{index:03d} ' + '🚀' * 2045 + f' checkpoint_{index:03d} '
                                    for index in range(80)) + ' END'
        self.stdout.write_text(original)
        self.assertGreater(self.stdout.stat().st_size, 650000)
        app = self.browser()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await pilot.press('w')
            await self.shown(app, pilot, 'END')
            self.assertIn('byte limit', test_browser_interaction.visible_text(app))
            self.assertIn('[continued line]', self.content(app))
            await pilot.press('home')
            await self.shown(app, pilot, 'BEGIN')
            portions = []
            for _ in range(20):
                content = self.content(app)
                self.assertLessEqual(len(content.encode('utf-8')), 200704)
                self.assertNotIn('�', content)
                portions.append(re.sub(r'\[(?:continued line|line continues in next portion)\]', '', content)
                                .replace('\n', ''))
                if ' END' in content:
                    break
                self.assertIn('[line continues in next portion]', content)
                await pilot.wait_for_scheduled_animations()
                # A completed read schedules viewport restoration after render.
                # Let that finish before positioning at the portion's end.
                await pilot.pause()
                pane = app.screen.query_one('#log-pane')
                pane.scroll_to(y=pane.max_scroll_y, animate=False, immediate=True)
                await pilot.pause()
                self.assertTrue(pane.is_vertical_scroll_end)
                await pilot.press('pagedown')
                deadline = time.monotonic() + 20
                while self.content(app) == content and time.monotonic() < deadline:
                    await pilot.pause(0.05)
                self.assertNotEqual(self.content(app), content, 'Paging must reach the adjacent portion')
            self.assertEqual(''.join(portions), original)
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')

    async def test_repeated_log_open_close_releases_views_descriptors_processes_and_timers(self):
        self.stdout.write_text('bounded reader\n')
        app = self.browser()
        children = Path(f'/proc/{os.getpid()}/task/{os.getpid()}/children')
        async with app.run_test(size=(80, 24)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'bounded reader')
            await pilot.press('escape')
            await pilot.pause()
            gc.collect()
            baseline_tasks = len(asyncio.all_tasks())
            baseline_fds = len(list(Path('/proc/self/fd').iterdir()))
            baseline_children = set(children.read_text().split())
            views = []
            for _ in range(12):
                await pilot.press('l', 'enter')
                await self.shown(app, pilot, 'bounded reader')
                views.append(weakref.ref(app.screen))
                await pilot.press('escape')
                await pilot.pause()
            gc.collect()
            self.assertTrue(all(view() is None for view in views))
            self.assertLessEqual(len(asyncio.all_tasks()), baseline_tasks + 2)
            self.assertLessEqual(len(list(Path('/proc/self/fd').iterdir())), baseline_fds + 1)
            self.assertEqual(set(children.read_text().split()), baseline_children)
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')


    async def test_minimum_terminal_keeps_log_content_and_read_failure_navigation_visible(self):
        self.stdout.write_text('wide-text-' * 10000 + '\nNARROW_LAST\n')
        app = self.browser()
        async with app.run_test(size=(40, 12)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'NARROW_LAST')
            replacement = self.snapshot()
            replacement['tasks'][0]['jobs'][0]['submission'] = 'different-submission'
            await app.accept_inspection(replacement)
            await self.shown(app, pilot, 'Current submission changed')
            await self.shown(app, pilot, 'NARROW_LAST', timeout=3)
            self.assertIn('q Quit', test_browser_interaction.visible_text(app))
            await pilot.press('escape', 'l', 'down', 'enter')
            await self.shown(app, pilot, 'No stderr log available.')
            self.assertIn('q Quit', test_browser_interaction.visible_text(app))
            await pilot.press('q')


    async def test_missing_logs_explain_absence_without_changing_job_state(self):
        for stream in ('stdout', 'stderr'):
            with self.subTest(stream=stream):
                app = self.browser()
                async with app.run_test(size=(120, 30)) as pilot:
                    await self.shown(app, pilot, 'sample')
                    await pilot.press('down', 'l')
                    if stream == 'stderr':
                        await pilot.press('down')
                    await pilot.press('enter')
                    state = app.screen.job['observed_state']
                    await self.shown(app, pilot, f'No {stream} log available.')
                    rendered = test_browser_interaction.visible_text(app)
                    self.assertIn('This submission may not have started, or its log may have been removed.', rendered)
                    self.assertIn('Attempt:', rendered)
                    self.assertIn('Path:', rendered)
                    self.assertIn(str(self.root / f'exact.{stream}'), rendered)
                    self.assertNotIn('[Errno', rendered)
                    self.assertNotIn('permissions', rendered)
                    self.assertEqual(app.screen.job['observed_state'], state)
                    self.assertEqual(app.acquisitions, 1)
                    await pilot.press('q')

    async def test_unsubmitted_and_nonregular_streams_show_read_errors_without_tracebacks(self):
        os.mkfifo(self.stdout)
        for submitted, expected in ((False, 'No recorded submission'), (True, 'not a regular file')):
            with self.subTest(submitted=submitted):
                def snapshot():
                    result = self.snapshot()
                    if not submitted:
                        result['tasks'][0]['jobs'][0].update(submission=None, logs={})
                    return result
                app = CapturedBrowser(selectors=('*',), details=True, snapshot_factory=snapshot)
                async with app.run_test(size=(100, 30)) as pilot:
                    await self.shown(app, pilot, 'sample')
                    await pilot.press('down', 'enter', 'enter')
                    await self.shown(app, pilot, expected, timeout=10)
                    self.assertNotIn('Traceback', str(app.screen.query_one('#read-status', Static).content))
                    self.assertEqual(app.acquisitions, 1)
                    await pilot.press('q')


@unittest.skipUnless(sys.platform == 'linux', 'resource acceptance uses Linux file descriptors')
class StatusLogReadBoundsTests(unittest.TestCase):
    def test_large_file_and_long_line_reads_stay_within_each_request_budget(self):
        with tempfile.TemporaryDirectory(prefix='gwflow-log-trace-') as temporary:
            root = Path(temporary)
            compiler = shutil.which('cc')
            self.assertIsNotNone(compiler, 'A C compiler is required for the real file-I/O resource probe')
            compiled = subprocess.run([compiler, '-shared', '-fPIC', '-O2', '-Wall', '-Wextra',
                                       '-o', str(root / 'probe.so'),
                                       str(Path(__file__).parent / 'fixtures/log_read_probe.c'), '-ldl'],
                                      capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run(
                [sys.executable, '-m', 'unittest',
                 'test_status_logs.StatusLogWindowTests.test_tail_paging_jumps_and_reload_keep_status_and_reading_position',
                 'test_status_logs.StatusLogWindowTests.test_huge_utf8_line_is_segmented_and_every_byte_remains_accessible'],
                cwd=root, env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parent),
                               'LD_PRELOAD': str(root / 'probe.so') + ' ' + os.environ.get('LD_PRELOAD', ''),
                               'GWFLOW_LOG_READ_REPORT': str(root / 'reads')},
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            read_bytes = [int(line.split()[1]) for line in (root / 'reads').read_text().splitlines()]
            self.assertGreaterEqual(len(read_bytes), 15, read_bytes)
            # Every file read is a separate owned process. This includes the
            # previous-window consistency check, UTF-8 context, and new window.
            self.assertLessEqual(max(read_bytes), 131088, read_bytes)

