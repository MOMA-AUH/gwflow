"""Status keyboard and layout behavior using Textual's public interaction API."""

import asyncio
import shlex
import time
import unittest
import xml.etree.ElementTree as ET

from gwflow.browser import StatusBrowser
from support import LocalBackendTestCase
import test_graphs
import test_grouped_status


def visible_text(app):
    svg = ET.fromstring(app.export_screenshot())
    return '\n'.join(''.join(node.itertext()).replace('\xa0', ' ')
                     for node in svg.iter('{http://www.w3.org/2000/svg}text'))


class BrowserInteractionTests(LocalBackendTestCase, unittest.IsolatedAsyncioTestCase):
    configure_workflow = test_graphs.TaskGraphTests.configure_workflow

    def browser(self, *, selectors=(), statuses=(), details=False):
        app = StatusBrowser((['--file', str(self.work / 'workflow.py') + ':gwf', '--backend', 'local'], selectors),
                            selectors=selectors, statuses=statuses, details=details)
        self.addCleanup(app.close_inspection)
        return app

    async def shown(self, app, pilot, expected, *, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await pilot.pause()
            text = visible_text(app)
            if expected in text:
                return text
            await asyncio.sleep(0.05)
        self.fail(f'{expected!r} not visible:\n{text}')

    async def test_normal_compact_and_large_views_survive_resize_without_reinspection(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            f"\nwith open({str(self.work / 'layout-imports')!r}, 'a') as stream: stream.write('import\\n')\n")
        for index, size in enumerate(((40, 12), (80, 24), (120, 40)), 1):
            with self.subTest(size=size):
                app = self.browser()
                async with app.run_test(size=size) as pilot:
                    await self.shown(app, pilot, 'sample')
                    await pilot.press('enter')
                    text = await self.shown(app, pilot, '0/5')
                    self.assertIn('pending', text)
                    self.assertIn('─', text)
                    self.assertIn('q quit', text)
                    await pilot.resize_terminal(30, 8)
                    await self.shown(app, pilot, 'Terminal too small', timeout=3)
                    await pilot.resize_terminal(*size)
                    await self.shown(app, pilot, '0/5')
                    self.assertEqual((self.work / 'layout-imports').read_text(), 'import\n' * index)
                    await pilot.press('q')

    async def test_long_rows_keep_state_progress_and_keyboard_position_visible(self):
        test_grouped_status.GroupedStatusTests.configure_workflow(self)
        prefix = 'long_task_' + 'reference_' * 19
        (self.work / 'workflow.py').write_text(
            'from gwflow import Workflow\nfrom library import mapping\ngwf = Workflow()\n' +
            ''.join(f"gwf.task(mapping(), alias={prefix!r}, key='sample_{index:03d}_end')\n"
                    for index in range(30)))
        app = self.browser(selectors=('*',))
        async with app.run_test(size=(40, 12)) as pilot:
            text = await self.shown(app, pilot, 'sample_000_end')
            self.assertIn('pending', text)
            self.assertIn('0/3', text)
            self.assertIn('…', text)
            await pilot.press('end')
            await self.shown(app, pilot, 'sample_029_end')
            await pilot.press('enter')
            await self.shown(app, pilot, 'Jobs completed:')
            await pilot.press('escape', 'enter')
            text = await self.shown(app, pilot, 'Jobs completed:')
            self.assertIn('sample_029_end', text)
            await pilot.press('escape', 'home')
            await self.shown(app, pilot, 'sample_000_end')
            await pilot.press('pagedown')
            self.assertNotIn('sample_000_end', visible_text(app))
            await pilot.press('pageup')
            await self.shown(app, pilot, 'sample_000_end')
            await pilot.press('q')

    async def test_tab_controls_expose_diagnostics_and_copy_complete_commands(self):
        name = 'sample_' + 'long_name_' * 18 + 'final'
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace("alias='sample'", f'alias={name!r}') + '\ncustom = gwf\n')
        custom = self.work / 'custom workflow.py'
        workflow.rename(custom)
        app = StatusBrowser((['--file', str(custom) + ':custom', '--backend', 'local'], (name,)), selectors=(name,))
        self.addCleanup(app.close_inspection)
        async with app.run_test(size=(40, 12)) as pilot:
            await self.shown(app, pilot, '0/5')
            await pilot.press('enter')
            await self.shown(app, pilot, 'Jobs completed:')
            await pilot.press('tab', 'tab', 'enter')
            command = shlex.split(app.clipboard)
            self.assertEqual(command[-3:], ['run', name, '--dry-run'])
            self.assertEqual(command[command.index('--file') + 1], str(custom) + ':custom')
            self.assertEqual(command[command.index('--backend') + 1], 'local')
            await pilot.press('shift+tab', 'enter')
            await self.shown(app, pilot, 'Attempt:')
            await pilot.press('q')
