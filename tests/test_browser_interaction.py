"""Status keyboard and layout behavior using Textual's public interaction API."""

import asyncio
import time
import unittest
import xml.etree.ElementTree as ET

from gwflow.browser import StatusBrowser
from rich.cells import cell_len
from support import LocalBackendTestCase
import test_graphs
import test_grouped_status


def visible_text(app):
    svg = ET.fromstring(app.export_screenshot())
    rows = {}
    for node in svg.iter('{http://www.w3.org/2000/svg}text'):
        text = ''.join(node.itertext()).replace('\xa0', ' ')
        if not text.strip() or node.get('textLength') is None:
            continue
        cell_width = float(node.get('textLength')) / cell_len(text)
        column = round(float(node.get('x')) / cell_width)
        row = rows.get(node.get('y'), '')
        # Styled spans are separate SVG nodes; preserve their visual spacing.
        rows[node.get('y')] = row + ' ' * max(0, column - cell_len(row)) + text
    return '\n'.join(rows.values())


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

    async def test_group_instances_use_declared_keys_and_keep_full_task_identity(self):
        test_grouped_status.GroupedStatusTests.configure_workflow(self)
        (self.work / 'second.py').write_text((self.work / 'library.py').read_text())
        (self.work / 'workflow.py').write_text(
            'from gwflow import Workflow\nfrom library import mapping\n'
            'from second import mapping as other\ngwf = Workflow()\n'
            "gwf.task(mapping(), alias='reads__map', key='A__rep1')\n"
            "gwf.task(mapping(), alias='reads__map', key='B')\n"
            "gwf.task(mapping(), alias='reads__map')\n"
            "gwf.task(other(), alias='reads__map', key='C')\n")
        app = self.browser()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.shown(app, pilot, 'reads__map@library.mapping')
            await pilot.press('enter')
            text = await self.shown(app, pilot, 'Instance')
            self.assertIn('A__rep1', text)
            self.assertIn('(no key)', text)
            self.assertNotIn('reads__map__A__rep1', text)
            self.assertEqual({task['name']: task['instance_key'] for task in app.snapshot['tasks']},
                             {'reads__map__A__rep1': 'A__rep1', 'reads__map__B': 'B',
                              'reads__map': None, 'reads__map__C': 'C'})
            await pilot.press('end', 'enter')
            await self.shown(app, pilot, 'Jobs completed:')
            self.assertEqual(app.screen.observation['name'], 'reads__map')
            await pilot.press('escape', 'slash', *'reads__map__A__rep1', 'enter')
            text = await self.shown(app, pilot, 'Showing 1 of 4 selected Tasks')
            self.assertIn('A__rep1', text)
            await pilot.press('enter')
            await self.shown(app, pilot, 'reads__map__A__rep1')
            self.assertEqual(app.screen.observation['name'], 'reads__map__A__rep1')
            await pilot.press('q')

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
                    self.assertIn('›', text)
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

    async def test_task_details_focus_on_jobs_without_extra_panels(self):
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
            self.assertFalse(app.screen.query('Button'))
            self.assertNotIn('Ordinary commands', visible_text(app))
            self.assertFalse(app.screen.query('Collapsible'))
            await pilot.press('enter')
            await self.shown(app, pilot, 'Observed state:')
            await pilot.press('q')
