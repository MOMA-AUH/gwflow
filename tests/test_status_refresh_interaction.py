"""Refresh context and captured filters through Textual's public interaction API."""

import unittest

from support import LocalBackendTestCase
import test_browser_interaction
import test_grouped_status


class StatusRefreshInteractionTests(LocalBackendTestCase, unittest.IsolatedAsyncioTestCase):
    configure_workflow = test_grouped_status.GroupedStatusTests.configure_workflow
    browser = test_browser_interaction.BrowserInteractionTests.browser
    shown = test_browser_interaction.BrowserInteractionTests.shown

    async def test_refresh_preserves_row_identity_and_scroll_when_earlier_rows_are_inserted(self):
        prefix = 'mapping_' + 'long_reference_' * 10
        workflow = self.work / 'workflow.py'
        declaration = ('from gwflow import Workflow\nfrom library import mapping\ngwf = Workflow()\n' +
                       ''.join(f"gwf.task(mapping(), alias={prefix!r}, key='sample_{index:03d}_end')\n"
                               for index in range(30)))
        workflow.write_text(declaration)
        app = self.browser(selectors=('*',))
        async with app.run_test(size=(40, 12)) as pilot:
            await self.shown(app, pilot, 'sample_000_end')
            await pilot.press('end')
            await self.shown(app, pilot, 'sample_029_end')
            workflow.write_text(declaration.replace('gwf = Workflow()\n',
                                f"gwf = Workflow()\ngwf.task(mapping(), alias={prefix!r}, key='inserted')\n"))
            await pilot.press('r')
            text = await self.shown(app, pilot, 'Showing 31/31 selected')
            self.assertIn('sample_029_end', text)
            await pilot.press('enter')
            text = await self.shown(app, pilot, 'Jobs completed:')
            self.assertIn('sample_029_end', text)
            await pilot.press('escape')
            await self.shown(app, pilot, 'sample_029_end')
            await pilot.press('q')

    async def test_literal_case_insensitive_search_edits_shortcut_letters_without_inspection(self):
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() + "gwf.task(mapping(), key='query_r')\n")
        imports = self.work / 'imports'
        library = self.work / 'library.py'
        library.write_text(library.read_text() +
                           f"\nwith open({str(imports)!r}, 'a') as stream: stream.write('import\\n')\n")
        app = self.browser(selectors=('mapping*',), statuses=('pending',))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.shown(app, pilot, 'Showing 4 of 4 selected Tasks')
            await pilot.press('slash')
            await self.shown(app, pilot, 'Search Task names', timeout=3)
            await pilot.press(*'QuErY_R', 'enter')
            text = await self.shown(app, pilot, 'Showing 1 of 4 selected Tasks')
            self.assertIn('mapping__query_r', text)
            self.assertNotIn('mapping__A', text)
            await pilot.press('slash', 'ctrl+shift+a', 'backspace', *'other', 'escape')
            await self.shown(app, pilot, 'Showing 1 of 4 selected Tasks')
            await pilot.press('slash', 'ctrl+shift+a', 'backspace', *'mapping*', 'enter')
            await self.shown(app, pilot, 'Showing 0 of 4 selected Tasks')
            await pilot.press('slash', 'ctrl+shift+a', 'backspace', 'enter')
            text = await self.shown(app, pilot, 'Showing 4 of 4 selected Tasks')
            self.assertNotIn('remapping__B', text)
            self.assertEqual(imports.read_text(), 'import\n')
            await pilot.press('q')

    async def test_state_filter_starts_from_cli_and_can_be_cleared_without_expanding_scope(self):
        self.cli('run', 'mapping__A')
        self.finish()
        imports = self.work / 'state-imports'
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            f"\nwith open({str(imports)!r}, 'a') as stream: stream.write('import\\n')\n")
        app = self.browser(selectors=('mapping*',), statuses=('pending',))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.shown(app, pilot, 'Showing 2 of 3 selected Tasks')
            await pilot.press('f')
            await self.shown(app, pilot, 'Filter Task states', timeout=3)
            # The initially selected pending checkbox is the first row.
            await pilot.press('space', 'enter')
            text = await self.shown(app, pilot, 'Showing 3 of 3 selected Tasks')
            self.assertIn('State: all', text)
            self.assertNotIn('remapping__B', text)
            await pilot.press('f', 'space', 'escape')
            await self.shown(app, pilot, 'Showing 3 of 3 selected Tasks')
            await pilot.press('f', 'space', 'enter')
            await self.shown(app, pilot, 'Showing 2 of 3 selected Tasks')
            self.assertEqual(imports.read_text(), 'import\n')
            await pilot.press('q')
