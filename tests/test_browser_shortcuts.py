"""Readable footer pairs and context-specific actions across terminal sizes."""

import unittest

from rich.console import Console
from textual.widgets import Static

from fixtures.browser_workload import CapturedBrowser
from gwflow.browser import ErrorView, Shortcuts


def render(shortcuts, width):
    console = Console(width=width, height=25, color_system=None)
    with console.capture() as captured:
        console.print(shortcuts, end='')
    return captured.get().removesuffix('\n')


class ShortcutPresentationTests(unittest.TestCase):
    def test_pairs_fit_without_wrapping_or_losing_help_and_exit(self):
        shortcuts = Shortcuts(('Enter', 'Open'), ('↑↓', 'Select'), ('Tab', 'Switch panel'),
                              ('u', 'Reload history'), back='Back')
        pairs = {'Enter Open', '↑↓ Select', 'Tab Switch panel', 'u Reload history', 'Esc Back', '? Help', 'q Quit'}
        for width in (40, 60, 80, 120):
            with self.subTest(width=width):
                text = render(shortcuts, width)
                self.assertNotIn('\n', text)
                self.assertLessEqual(len(text), width)
                self.assertTrue(set(text.split(' │ ')) <= pairs)
                self.assertTrue(text.endswith('Esc Back │ ? Help │ q Quit'))
                self.assertTrue(text.startswith('Enter Open'))
        self.assertIn('u Reload history', render(shortcuts, 120))

    def test_keys_have_one_accent_color_and_labels_remain_plain(self):
        console = Console(width=80)
        segments = list(console.render(Shortcuts(('Enter', 'Open'))))
        keys = [segment for segment in segments if segment.text in ('Enter', '?', 'q')]
        self.assertEqual(len(keys), 3)
        self.assertTrue(all(segment.style.bold and segment.style.color.name == 'cyan' for segment in keys))
        self.assertTrue(all(segment.style.dim for segment in segments if segment.text == ' │ '))
        self.assertTrue(all(not segment.style or not segment.style.bold
                            for segment in segments if segment.text in (' Open', ' Help', ' Quit')))


class ShortcutViewTests(unittest.IsolatedAsyncioTestCase):
    async def check_footer(self, app, pilot, *, expected=(), absent=(), exit_pair='q Quit'):
        for size in ((200, 40), (80, 24), (40, 12)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            footer = app.screen.query_one('.shortcuts', Static)
            text = render(footer.content, footer.size.width)
            self.assertEqual(footer.region.height, 1)
            self.assertNotIn('\n', text)
            self.assertIn(exit_pair, text)
            for item in absent:
                self.assertNotIn(item, text)
            if size[0] == 200:
                for item in expected:
                    self.assertIn(item, text)

    async def test_navigation_and_menus_only_advertise_relevant_actions(self):
        app = CapturedBrowser()
        async with app.run_test(size=(200, 40)) as pilot:
            await pilot.pause()
            await self.check_footer(app, pilot, expected=('Enter Open', '/ Search'), absent=('Esc Back', 'l Logs'))
            await pilot.press('enter', 'enter')
            await self.check_footer(app, pilot, expected=('Tab Switch panel', 'u Reload history'))
            await pilot.press('enter')
            await self.check_footer(app, pilot, expected=('↑↓/jk Select stream',), absent=('l Logs', 'u Reload history'))
            await pilot.press('enter')
            await self.check_footer(app, pilot, expected=('F Live/manual', 'w Wrap', '←→ Scroll sideways'),
                                    absent=('Enter Open', 'u Reload history'))
            await pilot.press('escape')
            await pilot.press('question_mark')
            await self.check_footer(app, pilot, expected=('↑↓/jk Scroll', 'Esc Close'), absent=('? Help',))
            await pilot.press('escape', 'slash')
            await self.check_footer(app, pilot, expected=('Enter Apply', 'Esc Cancel'),
                                    absent=('q Quit', '? Help'), exit_pair='Esc Cancel')
            await pilot.press('escape', 'f')
            await self.check_footer(app, pilot, expected=('Space Toggle', 'c Clear'), exit_pair='Esc Cancel')
            await pilot.press('escape', 'q')

    async def test_initial_error_does_not_advertise_unavailable_help_or_back(self):
        app = CapturedBrowser()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.snapshot = None
            await app.switch_screen(ErrorView('Inspection failed', ''))
            await self.check_footer(app, pilot, expected=('r Retry', 'Tab Controls'), absent=('? Help', 'Esc Back'))
            await pilot.press('q')
