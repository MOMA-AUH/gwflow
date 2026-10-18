"""Captured 100-Task/1,000-job layout and bounded-resource acceptance checks."""

import asyncio
import gc
from pathlib import Path
import unittest
import weakref

from fixtures.browser_workload import CapturedBrowser, captured_snapshot
from gwflow.diagnostics import error_message
from test_browser_interaction import visible_text


class BrowserWorkloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_filter_error_control_opens_details_without_applying_pending_choices(self):
        app = CapturedBrowser(statuses=('failed',), status_interval=60)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.accept_inspection(None, 'refresh failure\nTraceback:\ncomplete traceback')
            await pilot.press('f', 'c', 'tab')
            self.assertIn('Press Enter for error details', visible_text(app))
            await pilot.press('shift+tab')
            self.assertIn('Tab then Enter: error details', visible_text(app))
            self.assertNotIn('Press Enter for error details', visible_text(app))
            await pilot.press('tab')
            self.assertIn('Press Enter for error details', visible_text(app))
            await pilot.press('enter', 'enter')
            self.assertIn('complete traceback', visible_text(app))
            await pilot.press('escape')
            self.assertIn('Showing 6 of 100', visible_text(app))
            await pilot.press('q')
        self.assertEqual(app.return_value, 1)

    async def test_task_and_group_that_stop_matching_filter_return_to_surviving_parent(self):
        app = CapturedBrowser(statuses=('failed',), status_interval=60)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('enter', 'enter', 'enter')
            replacement = captured_snapshot()
            replacement['tasks'][18]['state'] = 'reusable'
            await app.accept_inspection(replacement)
            await pilot.pause()
            text = visible_text(app)
            self.assertIn('Tasks in mapping', text)
            self.assertIn('no longer available', text)
            self.assertIn('sample_019', text)
            replacement = captured_snapshot()
            for task in replacement['tasks']:
                if task['state'] == 'failed':
                    task['state'] = 'reusable'
            await app.accept_inspection(replacement)
            await pilot.pause()
            text = visible_text(app)
            self.assertIn('returned to Groups', text)
            self.assertIn('Showing 0 of 100', text)
            await pilot.press('q')

    async def test_latest_failure_replaces_open_expanded_error_details(self):
        app = CapturedBrowser()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await app.accept_inspection(None, 'first failure\nTraceback:\nfirst traceback')
            await pilot.press('tab', 'enter', 'enter')
            self.assertIn('first traceback', visible_text(app))
            await app.accept_inspection(None, 'second failure\nTraceback:\nsecond traceback', 'second output')
            await pilot.pause()
            text = visible_text(app)
            self.assertIn('second failure', text)
            self.assertIn('second traceback', text)
            self.assertNotIn('first traceback', text)
            await pilot.press('q')
        self.assertEqual(app.return_value, 1)

    async def test_switching_help_and_filter_keeps_transient_menus_and_tasks_bounded(self):
        app = CapturedBrowser()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('f', 'question_mark')
            baseline = len(asyncio.all_tasks())
            for _ in range(8):
                await pilot.press('f', 'question_mark')
            self.assertEqual(len(app.screen_stack), 3)
            self.assertLessEqual(len(asyncio.all_tasks()), baseline + 2)
            await pilot.press('escape')
            self.assertIn('Showing 100 of 100', visible_text(app))
            self.assertEqual(len(app.screen_stack), 2)
            await pilot.press('q')

    async def test_removed_task_and_group_return_to_nearest_surviving_parent(self):
        # Layout must show the fallback immediately, without a later age tick
        # repairing a notice formatted before the new heading acquired a width.
        app = CapturedBrowser(status_interval=60)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('enter', 'enter', 'enter')
            replacement = captured_snapshot()
            removed = replacement['tasks'].pop(0)['name']
            replacement['groups'][0]['members'].remove(removed)
            replacement['total'] -= 1
            await app.accept_inspection(replacement)
            await pilot.pause()
            text = visible_text(app)
            self.assertIn('Tasks in mapping', text)
            self.assertIn('no longer available', text)
            self.assertIn('sample_001', text)
            await pilot.resize_terminal(40, 24)
            await pilot.pause()
            self.assertIn('returned to mapping.', visible_text(app))
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertIn('no longer available', visible_text(app))
            self.assertEqual(app.acquisitions, 1)
            replacement = captured_snapshot()
            replacement['groups'].pop(0)
            await app.accept_inspection(replacement)
            await pilot.pause()
            text = visible_text(app)
            self.assertIn('returned to Groups', text)
            self.assertIn('large_group', text)
            await pilot.press('q')

    async def test_failed_refresh_keeps_snapshot_age_filters_and_browsing_at_minimum_size(self):
        app = CapturedBrowser(statuses=('failed',))
        async with app.run_test(size=(40, 12)) as pilot:
            await pilot.pause()
            original_time = app.snapshot['finished']
            try:
                raise RuntimeError('captured refresh failure')
            except RuntimeError as error:
                failure = error_message(error, 'Status inspection')
            await app.accept_inspection(None, failure)
            await pilot.pause()
            text = visible_text(app)
            self.assertIn('previous snapshot', text)
            self.assertIn('Refresh failed', text)
            self.assertIn('14/24', text)
            self.assertEqual(app.snapshot['finished'], original_time)
            await pilot.press('tab', 'enter')
            self.assertIn('Error details', visible_text(app))
            await pilot.press('escape', 'tab', 'tab')
            await pilot.press('enter')
            self.assertIn('sample_018', visible_text(app))
            await pilot.press('f', 'c', 'enter')
            self.assertIn('Showing 100/100 selected', visible_text(app))
            self.assertEqual(app.snapshot['finished'], original_time)
            await pilot.press('r')
            self.assertNotIn('Refresh failed', visible_text(app))
            await pilot.press('q')
        self.assertEqual(app.return_value, 0)

    async def test_refresh_retains_expanded_diagnostics_control_focus_and_scroll(self):
        app = CapturedBrowser(selectors=('*',))
        async with app.run_test(size=(40, 12)) as pilot:
            await pilot.pause()
            await pilot.press('enter', 'tab', 'enter')
            self.assertIn('Captured attempt and paths', visible_text(app))
            await pilot.press('tab')
            await pilot.wait_for_scheduled_animations()
            self.assertIn('Copy run preview', visible_text(app))
            await pilot.press('r')
            await pilot.wait_for_scheduled_animations()
            self.assertIn('Copy run preview', visible_text(app))
            await pilot.press('enter')
            self.assertTrue(app.clipboard.endswith('--dry-run'))
            await pilot.press('shift+tab')
            self.assertIn('Captured attempt and paths', visible_text(app))
            await pilot.press('q')

    async def test_full_group_counts_and_required_notices_survive_state_and_name_filters(self):
        app = CapturedBrowser(statuses=('failed',))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            text = visible_text(app)
            for expected in ('14/24', '6 Tasks match the filter',
                             'Showing 6 of 100', 'Workflow blocked:'):
                self.assertIn(expected, text)
            self.assertRegex(text, r'active\s+4')
            self.assertRegex(text, r'failed\s+6')
            await pilot.press('enter')
            text = visible_text(app)
            self.assertIn('sample_018', text)
            self.assertNotIn('sample_000', text)
            await pilot.press('slash', *'sample_023', 'enter')
            self.assertIn('Showing 1 of 100', visible_text(app))
            await pilot.press('f', 'c', 'enter')
            self.assertIn('Showing 1 of 100', visible_text(app))
            await pilot.press('slash', 'ctrl+shift+a', 'backspace', 'enter', 'escape')
            self.assertIn('Showing 100 of 100', visible_text(app))
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')

    async def test_expanded_large_selection_is_navigable_at_all_supported_sizes(self):
        for size in ((40, 12), (80, 24), (120, 40)):
            with self.subTest(size=size):
                app = CapturedBrowser(selectors=('*',), details=True)
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    text = visible_text(app)
                    self.assertIn('sample_000', text)
                    self.assertIn('10/10', text)
                    await pilot.press('end')
                    self.assertIn('job_09', visible_text(app))
                    await pilot.press('enter')
                    self.assertIn('sample_099', visible_text(app))
                    await pilot.press('escape', 'home', 'pagedown', 'pageup')
                    self.assertIn('sample_000', visible_text(app))
                    self.assertEqual(app.acquisitions, 1)
                    await pilot.press('q')

    async def test_repeated_refresh_and_navigation_release_old_snapshots_and_ui_tasks(self):
        app = CapturedBrowser()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('enter', 'enter', 'enter', 'escape', 'escape', 'escape', 'r')
            await pilot.pause()
            gc.collect()
            baseline_tasks = len(asyncio.all_tasks())
            baseline_fds = len(list(Path('/proc/self/fd').iterdir()))
            old_snapshots = []
            for _ in range(12):
                old_snapshots.append(weakref.ref(app.snapshot))
                await pilot.press('enter', 'enter', 'enter', 'r', 'escape', 'escape', 'escape')
                await pilot.pause()
            gc.collect()
            self.assertTrue(all(snapshot() is None for snapshot in old_snapshots))
            self.assertLessEqual(len(asyncio.all_tasks()), baseline_tasks + 2)
            self.assertLessEqual(len(list(Path('/proc/self/fd').iterdir())), baseline_fds + 1)
            self.assertEqual(app.acquisitions, 14)
            await pilot.press('q')
