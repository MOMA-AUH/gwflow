"""Shared progress styling at browser, static-report, and submission boundaries."""

from collections import Counter
from contextlib import contextmanager, redirect_stdout
from io import StringIO
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rich.text import Text

from gwflow.browser import GroupPrompt, ProgressColumns, TaskPrompt
from gwflow.presentation import Report


class TerminalBuffer(StringIO):
    def isatty(self):
        return True


class ProgressPresentationTests(unittest.TestCase):
    def render(self, source, *, width=100, no_color=False, plain=False, completed=2):
        stream = TerminalBuffer()
        with redirect_stdout(stream), patch.dict(os.environ, {"TERM": "xterm-256color",
                                                              "COLORTERM": "truecolor",
                                                              "NO_COLOR": "1" if no_color else ""}):
            report = Report(None, plain=plain)
            report.console.width = width
            task = dict(state="failed", completed=completed, total=4, detail="")
            row = SimpleNamespace(**task, task=SimpleNamespace(name="sample"))
            if source == "browser-task":
                report.console.print(TaskPrompt(task, ProgressColumns(8, 3, state_width=8), "sample"))
            elif source == "browser-group":
                report.console.print(GroupPrompt("sample", Counter(reusable=2, failed=2), 4, None,
                                                 ProgressColumns(8, 3)))
            elif source == "static-task":
                report.status([row], 1)
            elif source == "static-group":
                rows = [SimpleNamespace(state=state, task=SimpleNamespace(name=str(index)))
                        for index, state in enumerate(("reusable", "reusable", "failed", "failed"))]
                group = SimpleNamespace(label="sample", members=[row.task.name for row in rows])
                report.overview([group], rows, 4)
            else:
                @contextmanager
                def live(**options):
                    report.console.print(options["get_renderable"]())
                    yield

                outcomes = [SimpleNamespace(confirmed=index < 2) for index in range(4)]
                with patch("gwflow.presentation.live_report", live):
                    with report.submission_progress(outcomes):
                        pass
        return report.console, stream.getvalue()

    def test_fill_is_standard_green_without_dimming_in_every_view(self):
        for source in ("browser-task", "browser-group", "static-task", "static-group", "submission"):
            for width in (40, 100):
                with self.subTest(source=source, width=width):
                    console, output = self.render(source, width=width)
                    text = Text.from_ansi(output)
                    self.assertIn("2/4", text.plain)
                    self.assertIn("❯", text.plain)
                    self.assertIn("›", text.plain)
                    self.assertIn("❯›", text.plain)
                    for index, char in enumerate(text.plain):
                        if char in "❯›[]":
                            style = text.get_style_at_offset(console, index)
                            self.assertFalse(style.dim)
                            self.assertFalse(style.bold)
                            if char == "❯":
                                self.assertEqual(style.color.number, 2, "Fill must use standard green")

    def test_unknown_progress_stays_yellow_and_distinct_from_zero(self):
        for source in ("browser-task", "static-task"):
            with self.subTest(source=source):
                console, output = self.render(source, completed=None)
                text = Text.from_ansi(output)
                self.assertIn("?/4", text.plain)
                self.assertIn("[////", text.plain)
                self.assertNotIn("❯", text.plain)
                style = text.get_style_at_offset(console, text.plain.index("[/") + 1)
                self.assertEqual(style.color.number, 3)
                self.assertFalse(style.dim)
                _, zero = self.render(source, completed=0)
                self.assertIn("[›", Text.from_ansi(zero).plain)

    def test_monochrome_and_plain_reports_preserve_counts(self):
        for source in ("static-task", "static-group", "submission"):
            with self.subTest(source=source):
                _, output = self.render(source, no_color=True)
                self.assertNotIn("\x1b[", output)
                self.assertIn("❯", output)
                self.assertIn("›", output)
                self.assertIn("2/4", output)
        for source in ("static-task", "static-group"):
            with self.subTest(source=source, plain=True):
                _, output = self.render(source, plain=True)
                self.assertNotRegex(output, "[❯›\\x1b]")
                self.assertIn("2/4", output)
