"""Vim-style j/k navigation across the interactive status views."""

import unittest

from textual.widgets import DataTable, OptionList, SelectionList

from fixtures.browser_workload import CapturedBrowser
from gwflow.browser import LogField
from test_browser_interaction import visible_text


class VimKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_jk_move_group_and_task_rows_and_open_the_highlighted_row(self):
        app = CapturedBrowser()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            groups = app.screen.query_one(OptionList)
            self.assertEqual(groups.highlighted, 0)
            await pilot.press('j')
            self.assertEqual(groups.highlighted, 1)
            await pilot.press('j')
            self.assertEqual(groups.highlighted, 0)
            await pilot.press('k')
            self.assertEqual(groups.highlighted, 1)
            await pilot.press('k')
            self.assertEqual(groups.highlighted, 0)
            await pilot.press('enter')
            await pilot.pause()
            tasks = app.screen.query_one(OptionList)
            self.assertEqual(tasks.highlighted, 0)
            await pilot.press('j', 'j')
            self.assertEqual(tasks.highlighted, 2)
            await pilot.press('enter')
            await pilot.pause()
            self.assertEqual(app.screen.observation['name'], app.snapshot['tasks'][2]['name'])
            await pilot.press('q')

    async def test_jk_move_job_rows_in_task_details(self):
        app = CapturedBrowser(selectors=('*',))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('enter')
            await pilot.pause()
            jobs = app.screen.query_one('#task-jobs', OptionList)
            self.assertIs(app.screen.focused, jobs)
            self.assertEqual(jobs.highlighted, 0)
            await pilot.press('j')
            self.assertEqual(jobs.highlighted, 1)
            await pilot.press('enter')
            await pilot.pause()
            self.assertIn('Observed state:', visible_text(app))
            self.assertIn('job_01', visible_text(app))
            await pilot.press('q')

    async def test_jk_enter_text_in_name_search(self):
        app = CapturedBrowser(selectors=('*',))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('slash')
            await pilot.press('j', 'k')
            await pilot.press('enter')
            await pilot.pause()
            self.assertEqual(app.name_query, 'jk')
            self.assertIn('Showing 0 of 100', visible_text(app))
            await pilot.press('q')

    async def test_jk_move_state_filter_choices(self):
        app = CapturedBrowser(selectors=('*',))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('f')
            await pilot.pause()
            choices = app.screen.query_one(SelectionList)
            self.assertIs(app.screen.focused, choices)
            self.assertEqual(choices.highlighted, 0)
            await pilot.press('j')
            self.assertEqual(choices.highlighted, 1)
            await pilot.press('k')
            self.assertEqual(choices.highlighted, 0)
            await pilot.press('escape')
            await pilot.pause()
            self.assertEqual(app.statuses, ())
            await pilot.press('q')

    async def test_jk_select_log_streams_in_job_details(self):
        app = CapturedBrowser(selectors=('*',))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('enter', 'enter')
            await pilot.pause()
            streams = list(app.screen.query(LogField))
            self.assertEqual([field.stream for field in streams], ['stdout', 'stderr'])
            self.assertIs(app.screen.focused, streams[0])
            await pilot.press('j')
            self.assertIs(app.screen.focused, streams[1])
            await pilot.press('k')
            self.assertIs(app.screen.focused, streams[0])
            await pilot.press('q')

    async def test_jk_move_the_history_table_cursor(self):
        app = CapturedBrowser(selectors=('*',))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press('enter')
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            table.clear()
            for index in range(3):
                table.add_row(f'job_{index}', key=f'job_{index}')
            table.focus()
            await pilot.pause()
            self.assertEqual(table.cursor_row, 0)
            await pilot.press('j')
            self.assertEqual(table.cursor_row, 1)
            await pilot.press('j', 'j')
            self.assertEqual(table.cursor_row, 2)
            await pilot.press('k')
            self.assertEqual(table.cursor_row, 1)
            await pilot.press('q')
