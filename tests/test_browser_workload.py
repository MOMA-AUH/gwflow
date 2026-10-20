"""Captured 100-Task/1,000-job layout and bounded-resource acceptance checks."""

import asyncio
import gc
from pathlib import Path
import unittest
import weakref
import xml.etree.ElementTree as ET

from textual.widgets import OptionList

from fixtures.browser_workload import CapturedBrowser, captured_snapshot
from gwflow.diagnostics import error_message
from test_browser_interaction import visible_text


class BrowserWorkloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_instance_summaries_share_rows_and_wrap_on_narrow_screens(self):
        def snapshot():
            result = captured_snapshot()
            result['notices'] = ''
            for task in result['tasks']:
                task['detail'] = '1 job running; 12 jobs queued'
            result['tasks'][1]['detail'] = 'work present'
            result['tasks'][2]['detail'] = 'progress unavailable: unresolved submission'
            return result

        app = CapturedBrowser(snapshot_factory=snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('enter')
            nodes = list(ET.fromstring(app.export_screenshot()).iter('{http://www.w3.org/2000/svg}text'))
            first = next(node for node in nodes if ''.join(node.itertext()).startswith('sample_000'))
            second = next(node for node in nodes if ''.join(node.itertext()).startswith('sample_001'))
            detail = next(node for node in nodes if '1\xa0running' in ''.join(node.itertext()))
            self.assertEqual(first.get('y'), detail.get('y'))
            work = next(node for node in nodes if 'work\xa0present' in ''.join(node.itertext()))
            self.assertEqual(second.get('y'), work.get('y'))
            self.assertEqual(detail.get('x'), work.get('x'))
            header = next(node for node in nodes if 'Jobs' in ''.join(node.itertext()))
            heading = ''.join(header.itertext()).replace('\xa0', ' ')
            cell_width = float(header.get('textLength')) / len(heading)
            detail_text = ''.join(detail.itertext()).replace('\xa0', ' ')
            leading = len(detail_text) - len(detail_text.lstrip())
            self.assertAlmostEqual(float(header.get('x')) + heading.index('Jobs') * cell_width,
                                   float(detail.get('x')) + leading * cell_width)
            self.assertIn('unresolved submission', visible_text(app))
            between = [node for node in nodes if float(first.get('y')) < float(node.get('y')) < float(second.get('y'))]
            self.assertFalse(between)
            self.assertIn('Jobs', visible_text(app))
            self.assertNotIn('mapping_long_', visible_text(app))
            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            nodes = list(ET.fromstring(app.export_screenshot()).iter('{http://www.w3.org/2000/svg}text'))
            third = next(node for node in nodes if ''.join(node.itertext()).startswith('sample_002'))
            explanation = next(node for node in nodes if 'progress\xa0unavailable:' in ''.join(node.itertext()))
            self.assertEqual(third.get('y'), explanation.get('y'))
            await pilot.resize_terminal(40, 24)
            await pilot.pause()
            nodes = list(ET.fromstring(app.export_screenshot()).iter('{http://www.w3.org/2000/svg}text'))
            first = next(node for node in nodes if ''.join(node.itertext()).startswith('sample_000'))
            detail = next(node for node in nodes if '1\xa0running' in ''.join(node.itertext()))
            self.assertGreater(float(detail.get('y')), float(first.get('y')))
            self.assertTrue(''.join(detail.itertext()).replace('\xa0', ' ').startswith('  '))
            await pilot.resize_terminal(80, 24)
            await pilot.press('slash', *'mapping_long_', 'enter')
            self.assertIn('Showing 100 of 100 selected Tasks', visible_text(app))
            await pilot.press('q')

    async def test_expanded_jobs_share_the_task_state_column(self):
        app = CapturedBrowser(details=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            rows = {}
            for node in ET.fromstring(app.export_screenshot()).iter('{http://www.w3.org/2000/svg}text'):
                rows.setdefault(node.get('y'), []).append(''.join(node.itertext()).replace('\xa0', ' '))
            rows = [''.join(parts) for parts in rows.values()]
            header = next(row for row in rows if 'Progress' in row)
            task = next(row for row in rows if 'sample_000' in row)
            job = next(row for row in rows if 'job_00' in row)
            self.assertEqual(header.index('State'), task.index('✓ reusable'))
            self.assertEqual(header.index('State'), job.index('✓ completed'))
            self.assertEqual(job.index('job_00'), task.index('mapping') + 2)
            await pilot.press('down', 'enter')
            self.assertIn('Observed state:', visible_text(app))
            self.assertIn('job_00', visible_text(app))
            await pilot.press('q')

    async def test_task_and_job_tables_align_columns_across_resize_and_drill_down(self):
        def snapshot():
            result = captured_snapshot()
            result['tasks'] = result['tasks'][:3]
            result['total'] = 3
            result['groups'] = result['groups'][:1]
            result['groups'][0]['members'] = [task['name'] for task in result['tasks']]
            result['notices'] = ''
            for task in result['tasks']:
                task['jobs'][1]['local'] = 'align_reference_reads'
                task['jobs'][1]['name'] = task['name'] + '__align_reference_reads'
            return result

        def rows(app):
            result = {}
            for node in ET.fromstring(app.export_screenshot()).iter('{http://www.w3.org/2000/svg}text'):
                text = ''.join(node.itertext()).replace('\xa0', ' ')
                if not text.strip() or node.get('textLength') is None:
                    continue
                cell_width = float(node.get('textLength')) / len(text)
                column = round(float(node.get('x')) / cell_width)
                # SVG omits blank spans between differently styled fields.
                row = result.get(node.get('y'), '')
                result[node.get('y')] = row.ljust(column) + text
            return list(result.values())

        app = CapturedBrowser(snapshot_factory=snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('enter')
            for size in ((80, 24), (120, 40)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                rendered = rows(app)
                header = next(row for row in rendered if 'Progress' in row)
                task = next(row for row in rendered if 'sample_000' in row)
                self.assertIn('10/10', task)
                self.assertIn('✓ reusable', task)
                self.assertEqual(header.index('Progress'), task.index('10/10'))
                self.assertEqual(header.index('State'), task.index('✓ reusable'))
            await pilot.press('down')
            selected = app.screen.query_one(OptionList).highlighted_option.id
            await pilot.resize_terminal(40, 12)
            await pilot.pause()
            self.assertIn('sample_001', visible_text(app))
            self.assertIn('10/10', visible_text(app))
            await pilot.resize_terminal(80, 24)
            await pilot.press('enter')
            for size in ((80, 24), (120, 40)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                rendered = rows(app)
                header = next(row for row in rendered if 'Name' in row and 'State' in row)
                for name in ('job_00', 'align_reference_reads'):
                    job = next(row for row in rendered if name in row)
                    self.assertIn('✓ completed', job)
                    self.assertEqual(header.index('State'), job.index('✓ completed'))
            await pilot.resize_terminal(40, 12)
            await pilot.pause()
            self.assertIn('completed', visible_text(app))
            await pilot.resize_terminal(80, 24)
            await pilot.press('enter')
            self.assertIn('Observed state:', visible_text(app))
            await pilot.press('escape', 'escape')
            self.assertEqual(app.screen.query_one(OptionList).highlighted_option.id, selected)
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')

    async def test_group_table_aligns_rows_and_preserves_selection_across_resize(self):
        def snapshot():
            result = captured_snapshot()
            result['notices'] = ''
            result['groups'][1]['label'] = 'compare_mutect2_truth'
            for task in result['tasks'][24:]:
                task['state'] = 'queued'
            return result

        app = CapturedBrowser(snapshot_factory=snapshot)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for size in ((80, 24), (120, 40)):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                svg = ET.fromstring(app.export_screenshot())
                rows = {}
                starts = {}
                for node in svg.iter('{http://www.w3.org/2000/svg}text'):
                    text = ''.join(node.itertext()).replace('\xa0', ' ')
                    rows.setdefault(node.get('y'), []).append(text)
                    for label in (' Groups ', 'Name ', 'mapping'):
                        if text.startswith(label):
                            cell_width = float(node.get('textLength')) / len(text)
                            starts[label] = float(node.get('x')) + (len(text) - len(text.lstrip())) * cell_width
                rows = [''.join(parts) for parts in rows.values()]
                first = next(row for row in rows if 'mapping' in row)
                second = next(row for row in rows if 'compare_mutect2_truth' in row)
                header = next(row for row in rows if 'Other states' in row)
                self.assertIn('14/24', first)
                self.assertIn('active 4', first)
                self.assertIn('failed 6', first)
                self.assertIn('0/76', second)
                self.assertIn('queued 76', second)
                self.assertEqual(first.index('['), second.index('['))
                self.assertEqual(header.index('Reusable'), first.index('14/24'))
                self.assertAlmostEqual(starts[' Groups '], starts['Name '])
                self.assertAlmostEqual(starts['Name '], starts['mapping'])

            await pilot.press('down')
            selected = app.screen.query_one(OptionList).highlighted_option.id
            await pilot.resize_terminal(40, 12)
            await pilot.pause()
            text = visible_text(app)
            self.assertIn('0/76', text)
            self.assertIn('queued', text)
            self.assertIn('q quit', text)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertEqual(app.screen.query_one(OptionList).highlighted_option.id, selected)
            await pilot.press('enter')
            self.assertIn('Groups > compare_mutect2_truth', visible_text(app))
            await pilot.press('escape')
            self.assertEqual(app.screen.query_one(OptionList).highlighted_option.id, selected)
            self.assertEqual(app.acquisitions, 1)
            await pilot.press('q')

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
            self.assertIn('Groups > mapping', text)
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
            self.assertIn('Groups > mapping', text)
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
