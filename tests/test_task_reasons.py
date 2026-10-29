"""Task reasons through captured workflow inspection and keyboard browsing."""

import os
import subprocess
import sys
import unittest

from textual.widgets import Collapsible, OptionList, Static

from support import FIXTURES, LocalBackendTestCase
import test_browser_interaction
import test_graphs


class TaskReasonTests(LocalBackendTestCase, unittest.IsolatedAsyncioTestCase):
    browser = test_browser_interaction.BrowserInteractionTests.browser
    shown = test_browser_interaction.BrowserInteractionTests.shown

    def configure_workflow(self):
        test_graphs.TaskGraphTests.configure_workflow(self)
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace(
            'empty_task(inputs=[])', "empty_task(inputs=['input.txt', 'extra.txt'])"))

    async def test_changed_inputs_have_concise_reason_and_keyboard_details(self):
        self.run_complete()
        for name in ('input.txt', 'extra.txt'):
            path = self.work / name
            path.write_text('changed input content\n')
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            f"\nwith open({str(self.work / 'reason-imports')!r}, 'a') as stream: stream.write('import\\n')\n")
        result = self.work / 'results/sample/result.txt'
        retained = result.read_bytes()
        app = self.browser(selectors=('sample',))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('enter')
            text = await self.shown(app, pilot, 'Input metadata changed (2 files).')
            self.assertIn('Fresh attempt required;', text)
            self.assertIn('previous results must be removed', text)
            self.assertIn('before submission.', ' '.join(text.replace('│', '').split()))
            self.assertIn('Show details · 2 changed files', text)
            self.assertIn('gwflow_prepare', text)
            self.assertNotIn('mtime_ns', text)
            self.assertNotIn(str(self.work), text)
            inspected = app.snapshot['finished']
            await pilot.press('shift+tab', 'enter')
            await self.shown(app, pilot, 'Hide details · 2 changed files')
            await self.shown(app, pilot, 'Changed external inputs')
            await pilot.press('enter')
            text = await self.shown(app, pilot, 'Show details · 2 changed files')
            self.assertNotIn('Changed external inputs', text)
            self.assertEqual(app.snapshot['finished'], inspected)
            self.assertEqual((self.work / 'reason-imports').read_text(), 'import\n')
            self.assertEqual(result.read_bytes(), retained)
            await pilot.press('q')

    async def test_file_evidence_preserves_metadata_and_full_paths(self):
        for name in ('input.txt', 'extra.txt'):
            os.utime(self.work / name, ns=(1792746577000000000, 1792746577000000000))
        self.run_complete()
        for name in ('input.txt', 'extra.txt'):
            path = self.work / name
            path.write_text('changed input content\n')
            os.utime(path, ns=(1792749675000000000, 1792749675000000000))
        static = self.cli('run', '--dry-run', '--details')
        app = self.browser(selectors=('sample',))
        async with app.run_test(size=(100, 35)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('enter', 'shift+tab', 'enter')
            text = await self.shown(app, pilot, 'Changed external inputs')
            self.assertIn('Folder: .', text)
            self.assertIn('Size (bytes): 6 → 22', text)
            self.assertIn('mtime_ns: 1792746577000000000 → 1792749675000000000', text)
            await pilot.press('tab', 'end')
            text += await self.shown(app, pilot, 'Before submission')
            self.assertIn('extra.txt', text)
            self.assertIn('input.txt', text)
            self.assertIn('results/sample', text)
            self.assertNotIn(str(self.work), text)
            await pilot.press('tab', 'enter')
            await self.shown(app, pilot, 'Hide full paths')
            full = app.screen.query_one('#reason-full-paths', Static).content.plain
            for name in ('input.txt', 'extra.txt', 'results/sample'):
                self.assertIn(str(self.work / name), full)
                self.assertIn(str(self.work / name), static)
            self.assertIn(app.snapshot['tasks'][0]['reason'], full)
            self.assertIn('1792746577000000000 -> 1792749675000000000', static)
            await pilot.press('tab')
            self.assertIsInstance(app.screen.focused, OptionList)
            await pilot.press('enter')
            await self.shown(app, pilot, 'Observed state:')
            await pilot.press('q')

    async def test_other_long_reasons_keep_required_preparation_visible(self):
        self.run_complete()
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace('printf left', 'printf changed'))
        app = self.browser(selectors=('sample',))
        async with app.run_test(size=(80, 24)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('enter')
            text = await self.shown(app, pilot, 'Show details')
            prose = ' '.join(text.replace('│', '').split())
            self.assertIn('Reason: Changed commands', prose)
            self.assertIn('Fresh attempt required; previous results must be removed before submission.', prose)
            self.assertNotIn('changed files', text)
            await pilot.press('shift+tab', 'enter')
            text = await self.shown(app, pilot, 'Before submission')
            self.assertIn('results/sample', text)
            self.assertNotIn(str(self.work), text)
            await pilot.press('q')

    async def test_initialization_recovery_keeps_existing_attempt_wording(self):
        self.run_complete()
        result = subprocess.run(
            [sys.executable, str(FIXTURES / 'frontend_fault.py'), str(self.work),
             'before_removal', 'sample', 'run', '--force', 'sample'],
            cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 92, result.stdout + result.stderr)
        app = self.browser(selectors=('sample',))
        async with app.run_test(size=(100, 35)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('enter')
            text = await self.shown(app, pilot, 'Resume selected initialization')
            self.assertNotIn('Fresh attempt required', text)
            self.assertIn('Previous results must be removed before submission.', text)
            await pilot.press('q')

    async def test_input_changes_keep_retained_diagnostics_in_first_expansion(self):
        self.run_complete()
        self.cli('clean', '--delete', '--attempt', self.task_attempts()['sample'])
        (self.work / 'input.txt').write_text('changed input content\n')
        (self.work / 'results/sample/result.txt').unlink()
        app = self.browser(selectors=('sample',))
        async with app.run_test(size=(100, 35)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('enter', 'shift+tab', 'enter', 'tab', 'end')
            text = await self.shown(app, pilot, 'Before submission')
            self.assertIn('missing', text.lower())
            self.assertIn('result.txt', text)
            self.assertTrue(app.screen.query_one('#reason-paths', Collapsible).collapsed)
            await pilot.press('q')

    async def test_changed_resolved_paths_remain_literal_and_complete(self):
        path = self.work / 'input.txt'
        previous = self.work / 'old [reads]; sample.txt'
        current = self.work / 'new [reads]; sample.txt'
        path.rename(previous)
        current.write_bytes(previous.read_bytes())
        info = previous.stat()
        os.utime(current, ns=(info.st_atime_ns, info.st_mtime_ns))
        path.symlink_to(previous)
        self.run_complete()
        path.unlink()
        path.symlink_to(current)
        app = self.browser(selectors=('sample',))
        async with app.run_test(size=(100, 35)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('enter', 'shift+tab', 'enter')
            text = await self.shown(app, pilot, 'Old resolved path:')
            self.assertIn('Old resolved path: old [reads]; sample.txt', text)
            self.assertIn('New resolved path: new [reads]; sample.txt', text)
            await pilot.press('tab', 'end', 'tab', 'enter')
            await self.shown(app, pilot, 'Hide full paths')
            full = app.screen.query_one('#reason-full-paths', Static).content.plain
            self.assertIn('Input: ' + str(path), full)
            self.assertIn('Old resolved path: ' + str(previous), full)
            self.assertIn('New resolved path: ' + str(current), full)
            await pilot.press('q')

    async def test_narrow_details_keep_jobs_reachable_and_refresh_captured_evidence(self):
        self.run_complete()
        (self.work / 'input.txt').write_text('changed\n')
        app = self.browser(selectors=('sample',))
        async with app.run_test(size=(40, 12)) as pilot:
            await self.shown(app, pilot, 'sample')
            await pilot.press('enter', 'shift+tab')
            await self.shown(app, pilot, 'Show details · 1 changed file')
            await pilot.press('enter', 'tab', 'end', 'tab', 'enter')
            await self.shown(app, pilot, 'Hide full paths')
            self.assertLessEqual(app.screen.query_one('.reason-evidence').size.height, 5)
            (self.work / 'input.txt').write_text('changed once more\n')
            await pilot.press('r')
            for _ in range(200):
                await pilot.pause(0.05)
                fields = app.screen.query('#reason-full-paths')
                focused = app.screen.focused
                if (fields and 'size: 6 -> 18' in fields.first(Static).content.plain
                        and focused is not None and focused.parent.id == 'reason-paths'):
                    break
            else:
                self.fail('Refreshed input evidence and restored keyboard focus did not arrive')
            self.assertFalse(app.screen.query_one('#reason-details', Collapsible).collapsed)
            self.assertFalse(app.screen.query_one('#reason-paths', Collapsible).collapsed)
            self.assertIn('size: 6 -> 18', app.screen.query_one('#reason-full-paths', Static).content.plain)
            await pilot.press('tab')
            self.assertIsInstance(app.screen.focused, OptionList)
            await self.shown(app, pilot, 'gwflow_prepare')
            await pilot.press('enter')
            await self.shown(app, pilot, 'Observed state:')
            await pilot.press('q')
