"""Opt-in following through public browser interactions and controlled files."""

import asyncio
import gc
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
import unittest
import weakref
from unittest.mock import patch

from rich.console import Console
from textual.widgets import Static

from test_browser_interaction import visible_text
import test_status_logs


class StatusFollowTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_status_logs.StatusLogWindowTests.setUp
    snapshot = test_status_logs.StatusLogWindowTests.snapshot
    browser = test_status_logs.StatusLogWindowTests.browser
    content = test_status_logs.StatusLogWindowTests.content
    shown = test_status_logs.StatusLogWindowTests.shown

    async def test_following_starts_off_and_uppercase_f_toggles_appended_reads(self):
        self.stdout.write_text('initial log\n')
        app = self.browser()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            inspected = app.snapshot['finished']
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'initial log')
            self.assertNotIn('(live)', visible_text(app))
            with self.stdout.open('a') as stream:
                stream.write('appended while off\n')
            await pilot.pause(1.3)
            self.assertNotIn('appended while off', self.content(app))
            await pilot.press('F')
            await self.shown(app, pilot, 'appended while off', timeout=4)
            self.assertIn('(live)', visible_text(app))
            status = app.screen.query_one('#read-status', Static).content
            self.assertEqual(status.get_style_at_offset(Console(), status.plain.index('(live)')).color.name, 'green')
            with self.stdout.open('a') as stream:
                stream.write('automatic appended read\n')
            await self.shown(app, pilot, 'automatic appended read', timeout=4)
            await pilot.wait_for_scheduled_animations()
            pane = app.screen.query_one('#log-pane')
            self.assertTrue(pane.is_vertical_scroll_end, (pane.scroll_y, pane.max_scroll_y, pane.scroll_target_y))
            await pilot.press('F')
            self.assertNotIn('(live)', visible_text(app))
            status = app.screen.query_one('#read-status', Static).content
            self.assertRegex(status.plain, r'^Read at:\s+\d{2}:\d{2}:\d{2}$')
            read_status = app.screen.query_one('#read-status', Static).content
            with self.stdout.open('a') as stream:
                stream.write('appended while paused\n')
            await pilot.pause(1.3)
            self.assertNotIn('appended while paused', self.content(app))
            self.assertEqual(app.screen.query_one('#read-status', Static).content, read_status)
            self.assertEqual(app.snapshot['finished'], inspected)
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')


    async def test_pausing_or_leaving_cancels_a_pending_follow_read_and_stops_retries(self):
        self.stdout.write_text(''.join(f'blocked reader {index:04d}\n' for index in range(500)))
        library = self.root / 'probe.so'
        compiled = subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-o', str(library),
                                   str(Path(__file__).parent / 'fixtures/log_read_probe.c'), '-ldl'],
                                  capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        gate, started = self.root / 'gate', self.root / 'started'
        environment = {'LD_PRELOAD': str(library) + ' ' + os.environ.get('LD_PRELOAD', ''),
                       'GWFLOW_LOG_READ_PATH': str(self.stdout), 'GWFLOW_LOG_READ_GATE': str(gate),
                       'GWFLOW_LOG_READ_STARTED': str(started)}
        with patch.dict(os.environ, environment):
            for key in ('F', 'up', 'question_mark', 'escape', 'q'):
                with self.subTest(key=key):
                    gate.unlink(missing_ok=True)
                    app = self.browser()
                    async with app.run_test(size=(80, 24)) as pilot:
                        await self.shown(app, pilot, 'sample')
                        await pilot.press('down', 'l', 'enter')
                        await self.shown(app, pilot, 'blocked reader 0499')
                        self.assertIn('Showing latest 200 lines.', visible_text(app))
                        self.assertNotIn('Portion:', visible_text(app))
                        await pilot.wait_for_scheduled_animations()
                        read_status = app.screen.query_one('#read-status', Static).content
                        info_region = app.screen.query_one('#log-info').region
                        log_region = app.screen.query_one('#log-pane').region
                        started.unlink(missing_ok=True)
                        gate.touch()
                        await pilot.press('F')
                        for _ in range(60):
                            if started.exists():
                                break
                            await pilot.pause(0.05)
                        self.assertTrue(started.exists(), visible_text(app))
                        self.assertEqual(str(app.screen.query_one('#read-status', Static).content),
                                         str(read_status) + ' (live)')
                        self.assertEqual(app.screen.query_one('#log-info').region, info_region)
                        self.assertEqual(app.screen.query_one('#log-pane').region, log_region)
                        pid = int(started.read_text())
                        self.assertTrue(Path(f'/proc/{pid}').exists())
                        await pilot.press(key)
                        await pilot.pause()
                        if key != 'q':
                            self.assertFalse(Path(f'/proc/{pid}').exists(), f'{key} must reap the pending reader')
                        if key == 'question_mark':
                            await pilot.press('escape')
                        if key not in ('escape', 'q'):
                            self.assertNotIn('(live)', visible_text(app))
                            self.assertIn('blocked reader', self.content(app))
                            self.assertNotIn('Reading log...', visible_text(app))
                        if key != 'q':
                            await pilot.pause(1.3)
                            self.assertEqual(int(started.read_text()), pid, 'Leaving or pausing must stop retries')
                        self.assertEqual(app.acquisitions, 1)
                        if key != 'q':
                            await pilot.press('q')
                    self.assertFalse(Path(f'/proc/{pid}').exists(), f'{key} must reap the pending reader')
                    gate.unlink()

    async def test_scrolling_up_or_home_pauses_and_end_does_not_enable_following(self):
        self.stdout.write_text(''.join(f'line {index:04d}\n' for index in range(500)))
        app = self.browser()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'line 0499')
            for key in ('up', 'pageup', 'home'):
                await pilot.press('F')
                await self.shown(app, pilot, 'Read at:')
                await pilot.wait_for_scheduled_animations()
                await pilot.press(key)
                self.assertNotIn('(live)', visible_text(app))
                if key == 'home':
                    await self.shown(app, pilot, 'line 0000')
                with self.stdout.open('a') as stream:
                    stream.write(f'paused after {key}\n')
                await pilot.pause(1.2)
                self.assertNotIn(f'paused after {key}', self.content(app))
                await pilot.press('end')
                await self.shown(app, pilot, f'paused after {key}')
                self.assertNotIn('(live)', visible_text(app))
                with self.stdout.open('a') as stream:
                    stream.write(f'end leaves paused {key}\n')
                await pilot.pause(1.2)
                self.assertNotIn(f'end leaves paused {key}', self.content(app))
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')

    async def test_following_recovers_from_replacement_truncation_and_read_failures(self):
        self.stdout.write_text('original content\n' * 500)
        app = self.browser()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            inspected = app.snapshot['finished']
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'original content')
            await pilot.press('F')
            await self.shown(app, pilot, 'Read at:')
            replacement = self.root / 'replacement'
            replacement.write_text('replacement content\n' * 40)
            replacement.replace(self.stdout)
            await self.shown(app, pilot, 'replacement content', timeout=4)
            self.assertIn('Log replaced', visible_text(app))
            self.assertNotIn('original content', self.content(app))
            await pilot.pause(1.3)
            self.assertIn('Log replaced', visible_text(app), 'File-change notice must survive unchanged checks')
            self.stdout.write_text('truncated content\n')
            await self.shown(app, pilot, 'truncated content', timeout=4)
            self.assertIn('Log truncated', visible_text(app))
            self.assertNotIn('replacement content', self.content(app))
            self.stdout.unlink()
            await self.shown(app, pilot, 'No such file', timeout=4)
            self.assertIn('Previously read', visible_text(app))
            self.assertEqual(self.content(app), 'truncated content\n')
            self.stdout.mkdir()
            await self.shown(app, pilot, 'not a regular file', timeout=4)
            self.assertEqual(self.content(app), 'truncated content\n')
            self.stdout.rmdir()
            self.stdout.write_text('readable again\n')
            await self.shown(app, pilot, 'readable again', timeout=4)
            self.assertIn('Log readable again', visible_text(app))
            self.assertNotIn('truncated content', self.content(app))
            self.assertIn('(live)', visible_text(app))
            self.assertEqual(app.snapshot['finished'], inspected)
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')

    async def test_pending_and_canceled_retries_keep_the_last_failure_until_recovery(self):
        self.stdout.write_text('retained after failure\n')
        library = self.root / 'probe.so'
        compiled = subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-o', str(library),
                                   str(Path(__file__).parent / 'fixtures/log_read_probe.c'), '-ldl'],
                                  capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        gate, started = self.root / 'gate', self.root / 'started'
        environment = {'LD_PRELOAD': str(library) + ' ' + os.environ.get('LD_PRELOAD', ''),
                       'GWFLOW_LOG_READ_PATH': str(self.stdout), 'GWFLOW_LOG_READ_GATE': str(gate),
                       'GWFLOW_LOG_READ_STARTED': str(started)}
        with patch.dict(os.environ, environment):
            app = self.browser()
            async with app.run_test(size=(100, 30)) as pilot:
                await self.shown(app, pilot, 'sample')
                await pilot.press('down', 'l', 'enter')
                await self.shown(app, pilot, 'retained after failure')
                await pilot.press('F')
                await self.shown(app, pilot, 'Read at:')
                self.stdout.unlink()
                await self.shown(app, pilot, 'No such file', timeout=4)
                failed = str(app.screen.query_one('#read-notice', Static).content)
                started.unlink(missing_ok=True)
                gate.touch()
                self.stdout.write_text('recovered content\n')
                for _ in range(80):
                    if started.exists():
                        break
                    await pilot.pause(0.05)
                self.assertTrue(started.exists())
                self.assertIn(failed, str(app.screen.query_one('#read-notice', Static).content))
                self.assertIn('retained after failure', self.content(app))
                await pilot.press('F')
                self.assertNotIn('(live)', visible_text(app))
                self.assertEqual(str(app.screen.query_one('#read-notice', Static).content), failed)
                gate.unlink()
                await pilot.press('F')
                await self.shown(app, pilot, 'recovered content', timeout=4)
                self.assertIn('Log readable again', visible_text(app))
                self.assertNotIn('No such file', str(app.screen.query_one('#read-notice', Static).content))
                self.assertEqual(app.acquisitions, 1)
                await pilot.press('q')

    async def test_manual_recovery_preserves_older_portion_without_claiming_latest_content(self):
        contents = ''.join(f'line {index:04d}\n' for index in range(500))
        self.stdout.write_text(contents)
        app = self.browser()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'line 0499')
            await pilot.press('home')
            await self.shown(app, pilot, 'line 0000')
            self.stdout.unlink()
            await pilot.press('u')
            await self.shown(app, pilot, 'No such file')
            self.stdout.write_text(contents)
            await pilot.press('u')
            await self.shown(app, pilot, 'Log readable again')
            self.assertIn('line 0000', self.content(app))
            self.assertNotIn('line 0499', self.content(app))
            self.assertNotIn('latest content', str(app.screen.query_one('#read-notice', Static).content))
            self.assertNotIn('(live)', visible_text(app))
            await pilot.press('q')

    async def test_scrolling_up_with_the_scrollbar_pauses_following(self):
        self.stdout.write_text(''.join(f'scrollbar {index:04d}\n' for index in range(500)))
        app = self.browser()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'scrollbar 0499')
            await pilot.press('F')
            await self.shown(app, pilot, 'Read at:')
            await pilot.wait_for_scheduled_animations()
            pane = app.screen.query_one('#log-pane')
            await pilot.click(pane.vertical_scrollbar, offset=(0, 1))
            self.assertNotIn('(live)', visible_text(app))
            await pilot.wait_for_scheduled_animations()
            self.assertLess(pane.scroll_y, pane.max_scroll_y)
            await pilot.press('F')
            await self.shown(app, pilot, 'Read at:')
            await pilot.wait_for_scheduled_animations()
            bar = pane.vertical_scrollbar
            await pilot.mouse_down(bar, offset=(0, bar.size.height - 1))
            await pilot.hover(bar, offset=(0, 1))
            await pilot.mouse_up(bar, offset=(0, 1))
            self.assertNotIn('(live)', visible_text(app))
            await pilot.wait_for_scheduled_animations()
            self.assertLess(pane.scroll_y, pane.max_scroll_y)
            await pilot.press('q')

    async def test_rapid_append_replacement_and_incomplete_utf8_keep_bounded_portions(self):
        self.stdout.write_text('🚀' * 100000 + '\nINITIAL_END\n')
        app = self.browser()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'INITIAL_END')
            await pilot.press('F')
            await self.shown(app, pilot, 'Read at:')
            for index in range(5):
                replacement = self.root / 'replacement'
                replacement.write_text('🚀' * 40000 + f'\nREPLACEMENT_END_{index}\n')
                replacement.replace(self.stdout)
                await self.shown(app, pilot, f'REPLACEMENT_END_{index}', timeout=4)
                self.assertIn('Log replaced', visible_text(app))
                self.assertIn('byte limit', visible_text(app))
                self.assertIn('[continued line]', self.content(app))
                self.assertLessEqual(len(self.content(app).encode('utf-8')), 200704)
                self.assertNotIn('�', self.content(app))
            for _ in range(30):
                with self.stdout.open('ab') as stream:
                    stream.write(b'x' * 8192)
                await asyncio.sleep(0.01)
            with self.stdout.open('ab') as stream:
                stream.write(b'\nRAPID_DONE ' + b'\xf0\x9f')
            await self.shown(app, pilot, 'RAPID_DONE', timeout=4)
            self.assertIn('Output advanced beyond the previous portion', visible_text(app))
            self.assertIn('Undecodable UTF-8', visible_text(app))
            self.assertIn('�', self.content(app))
            self.assertLessEqual(len(self.content(app).encode('utf-8')), 200704)
            with self.stdout.open('ab') as stream:
                stream.write(b'\x9a\x80\nCOMPLETED_UTF8\n')
            await self.shown(app, pilot, 'COMPLETED_UTF8', timeout=4)
            self.assertIn('🚀', self.content(app))
            self.assertNotIn('�', self.content(app))
            self.assertLessEqual(len(self.content(app).encode('utf-8')), 200704)
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')

    async def test_refresh_keeps_following_the_selected_submission_after_retry_and_task_removal(self):
        self.stdout.write_text('selected submission\n')
        app = self.browser()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'selected submission')
            await pilot.press('F')
            await self.shown(app, pilot, 'Read at:')
            new_path = self.root / 'new.stdout'
            new_path.write_text('NEW SUBMISSION MUST NOT REPLACE SELECTED LOG\n')
            newer = self.snapshot()
            newer['tasks'][0]['attempt'] = '4' * 32
            newer['tasks'][0]['jobs'][0].update(submission='new-submission', logs={'stdout': str(new_path)})
            app.snapshot_factory = lambda: newer
            await pilot.press('r')
            await self.shown(app, pilot, 'Current submission changed')
            self.assertIn('(live)', visible_text(app))
            with self.stdout.open('a') as stream:
                stream.write('retired submission appended\n')
            await self.shown(app, pilot, 'retired submission appended', timeout=4)
            self.assertNotIn('NEW SUBMISSION', self.content(app))
            removed = self.snapshot()
            removed.update(tasks=[], groups=[], total=0)
            app.snapshot_factory = lambda: removed
            await pilot.press('r')
            await self.shown(app, pilot, 'Task no longer in the refreshed selection')
            self.assertIn('(live)', visible_text(app))
            with self.stdout.open('a') as stream:
                stream.write('removed Task appended\n')
            await self.shown(app, pilot, 'removed Task appended', timeout=4)
            self.assertEqual(app.acquisitions, 3)
            await pilot.press('escape')
            await self.shown(app, pilot, 'Showing 0 of 0 selected Tasks')
            await pilot.press('q')

    async def test_repeated_follow_pause_and_close_cycles_release_owned_resources(self):
        self.stdout.write_text('cycle reader\n')
        app = self.browser()
        children = Path(f'/proc/{os.getpid()}/task/{os.getpid()}/children')
        async with app.run_test(size=(80, 24)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'cycle reader')
            await pilot.press('escape')
            await pilot.pause()
            gc.collect()
            baseline_tasks = len(asyncio.all_tasks())
            baseline_fds = len(list(Path('/proc/self/fd').iterdir()))
            baseline_children = set(children.read_text().split())
            views = []
            for index in range(12):
                await pilot.press('l', 'enter')
                await self.shown(app, pilot, 'cycle reader')
                await pilot.press('F')
                await self.shown(app, pilot, 'Read at:')
                await pilot.press('up')
                self.assertNotIn('(live)', visible_text(app))
                await pilot.press('F')
                await self.shown(app, pilot, 'Read at:')
                views.append(weakref.ref(app.screen))
                await pilot.press('escape')
                await pilot.pause()
                self.assertEqual(set(children.read_text().split()), baseline_children)
            gc.collect()
            self.assertTrue(all(view() is None for view in views))
            self.assertLessEqual(len(asyncio.all_tasks()), baseline_tasks + 2)
            self.assertLessEqual(len(list(Path('/proc/self/fd').iterdir())), baseline_fds + 1)
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')


    async def test_minimum_terminal_keeps_follow_state_latest_text_and_failure_controls_visible(self):
        self.stdout.write_text('line\n' * 500 + 'MINIMUM_TAIL\n')
        app = self.browser()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('down', 'l', 'enter')
            await self.shown(app, pilot, 'MINIMUM_TAIL')
            await pilot.press('F')
            await self.shown(app, pilot, 'Read at:')
            await pilot.resize_terminal(40, 12)
            await self.shown(app, pilot, 'MINIMUM_TAIL', timeout=4)
            self.assertIn('(live)', visible_text(app))
            replacement = self.snapshot()
            replacement['tasks'][0]['jobs'][0]['submission'] = 'newer-submission'
            app.snapshot_factory = lambda: replacement
            await pilot.press('r')
            await self.shown(app, pilot, 'Current submission changed', timeout=4)
            await self.shown(app, pilot, 'MINIMUM_TAIL', timeout=4)
            self.stdout.unlink()
            await self.shown(app, pilot, 'Previously read', timeout=4)
            self.assertIn('No such file', str(app.screen.query_one('#read-notice', Static).content))
            self.assertIn('(live)', visible_text(app))
            self.assertIn('q quit', visible_text(app))
            self.assertIn('MINIMUM_TAIL', self.content(app))
            await pilot.press('q')


    async def test_utf8_completion_during_a_follow_read_keeps_the_displayed_byte_range_consistent(self):
        self.stdout.write_bytes(b'tail start \xf0\x9f')
        library = self.root / 'probe.so'
        compiled = subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-o', str(library),
                                   str(Path(__file__).parent / 'fixtures/log_read_probe.c'), '-ldl'],
                                  capture_output=True, text=True)
        self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
        gate, started = self.root / 'gate', self.root / 'started'
        environment = {'LD_PRELOAD': str(library) + ' ' + os.environ.get('LD_PRELOAD', ''),
                       'GWFLOW_LOG_READ_PATH': str(self.stdout), 'GWFLOW_LOG_READ_GATE': str(gate),
                       'GWFLOW_LOG_READ_STARTED': str(started)}
        with patch.dict(os.environ, environment):
            app = self.browser()
            async with app.run_test(size=(100, 30)) as pilot:
                await self.shown(app, pilot, 'sample')
                await pilot.press('down', 'l', 'enter')
                await self.shown(app, pilot, 'tail start')
                started.unlink(missing_ok=True)
                gate.touch()
                await pilot.press('F')
                for _ in range(60):
                    if started.exists():
                        break
                    await pilot.pause(0.05)
                self.assertTrue(started.exists())
                with self.stdout.open('ab') as stream:
                    stream.write(b'\x9a\x80')
                gate.unlink()
                await self.shown(app, pilot, 'tail start 🚀', timeout=4)
                result = app.screen.result
                self.assertLessEqual(result['end'], result['size'])
                await pilot.press('q')


class StatusFollowReadBoundsTests(unittest.TestCase):
    def test_follow_reads_and_resources_remain_bounded_under_file_changes_and_view_cycles(self):
        with tempfile.TemporaryDirectory(prefix='gwflow-follow-probe-') as temporary:
            root = Path(temporary)
            compiled = subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra',
                                       '-o', str(root / 'probe.so'),
                                       str(Path(__file__).parent / 'fixtures/log_read_probe.c'), '-ldl'],
                                      capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            started = time.perf_counter()
            result = subprocess.run(
                [sys.executable, '-m', 'unittest',
                 'test_status_follow.StatusFollowTests.test_rapid_append_replacement_and_incomplete_utf8_keep_bounded_portions',
                 'test_status_follow.StatusFollowTests.test_repeated_follow_pause_and_close_cycles_release_owned_resources'],
                cwd=root, env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parent),
                               'LD_PRELOAD': str(root / 'probe.so') + ' ' + os.environ.get('LD_PRELOAD', ''),
                               'GWFLOW_LOG_READ_REPORT': str(root / 'reads')},
                capture_output=True, text=True, timeout=120)
            elapsed = time.perf_counter() - started
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            read_bytes = [int(line.split()[1]) for line in (root / 'reads').read_text().splitlines()]
            self.assertGreaterEqual(len(read_bytes), 30, read_bytes)
            self.assertLessEqual(max(read_bytes), 131088, read_bytes)
            self.measurements = dict(elapsed_seconds=elapsed, read_requests=len(read_bytes),
                                     bytes_per_request=read_bytes, maximum_read_bytes=max(read_bytes),
                                     child_maximum_rss_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)

