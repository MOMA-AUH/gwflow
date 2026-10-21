"""Inspection counts reflect finished Tasks without claiming scheduler progress."""

from pathlib import Path
import unittest

from gwflow.activity import activity, observe_activity
from support import FIXTURES, LocalBackendTestCase
import test_grouped_status
import test_interactive_status


class ActivityEventsTests(unittest.TestCase):
    def test_nested_waits_restore_counts_and_failure_does_not_finish_them(self):
        events = []
        with observe_activity(lambda state: events.append(dict(state) if state else None)):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                with activity('Inspecting Tasks...', total=3) as update:
                    update(1)
                    with activity('Waiting for image acquisition: image'):
                        pass
                    raise RuntimeError('interrupted')
        self.assertEqual([(event['phase'], event['completed'], event['total']) if event else None
                          for event in events], [
                              ('Inspecting Tasks...', 0, 3),
                              ('Inspecting Tasks...', 1, 3),
                              ('Waiting for image acquisition: image', 0, None),
                              ('Inspecting Tasks...', 1, 3), None])


class InspectionProgressTests(LocalBackendTestCase):
    configure_workflow = test_grouped_status.GroupedStatusTests.configure_workflow
    terminal = test_interactive_status.InteractiveStatusTests.terminal

    def setUp(self):
        super().setUp()
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            f'\nimport sys\nsys.path.insert(0, {str(FIXTURES)!r})\n'
                            f'from inspection_progress import install\ninstall({str(self.work)!r})\n')
        (self.work / 'block-first').touch()

    def hold(self):
        for phase in ('scheduler', 'task'):
            (self.work / ('hold-' + phase)).touch()
            (self.work / (phase + '-held')).unlink(missing_ok=True)
            (self.work / (phase + '-release')).unlink(missing_ok=True)

    def release(self):
        for phase in ('scheduler', 'task'):
            (self.work / (phase + '-release')).touch()

    def inspect(self, session, checkpoint=0):
        text_start = len(session.text)
        self.wait_for((self.work / 'scheduler-held').exists)
        session.wait_for('Waiting for scheduler', after=checkpoint)
        self.assertNotIn('1/3', session.text[text_start:])
        checkpoint = len(session.output)
        text_start = len(session.text)
        (self.work / 'scheduler-release').touch()
        self.wait_for((self.work / 'task-held').exists)
        session.wait_for('1/3', after=checkpoint)
        self.assertIn('Inspecting Tasks', session.text[text_start:])
        self.assertNotIn('3/3', session.text[text_start:])
        (self.work / 'task-release').touch()

    def test_static_progress_counts_blocked_tasks_and_selected_scope(self):
        for width, no_color in ((80, ''), (40, '1')):
            with self.subTest(width=width, no_color=no_color):
                self.hold()
                session = self.terminal('status', 'mapping*', '--static', size=(width, 24),
                                        env={'NO_COLOR': no_color})
                try:
                    self.inspect(session)
                    self.assertEqual(session.finish(), 0, session.text)
                    self.assertIn('3/3', session.text)
                    self.assertIn('Checking Task dependencies', session.text)
                    self.assertIn('blocked', session.text)
                    self.assertNotIn('4/4', session.text)
                    if no_color:
                        self.assertNotRegex(session.output.decode(), r'\x1b\[[0-9;]*m')
                finally:
                    self.release()

    def test_browser_shows_progress_on_initial_load_and_refresh(self):
        self.hold()
        session = self.terminal('status', 'mapping*')
        try:
            self.inspect(session)
            session.wait_for('3 of 3 selected Tasks')
            self.hold()
            checkpoint = session.send('r')
            self.inspect(session, checkpoint)
            session.wait_for('Refreshing', after=checkpoint)
            session.send('q')
            self.assertEqual(session.finish(), 0, session.text)
        finally:
            self.release()

    def test_plain_output_reports_phases_without_per_task_updates(self):
        result = self.cli_result('status', 'mapping*')
        self.assertEqual(result.stderr.count('Inspecting Tasks...'), 1)
        self.assertIn('Waiting for scheduler...', result.stderr)
        self.assertIn('Checking Task dependencies...', result.stderr)
        self.assertNotRegex(result.stderr, r'\d+/\d+|\x1b\[')
        self.assertNotIn('Inspecting Tasks', result.stdout)

    def test_quit_during_counted_inspection_stops_the_owned_process(self):
        (self.work / 'hold-task').touch()
        session = self.terminal('status', 'mapping*')
        try:
            self.wait_for((self.work / 'task-held').exists)
            session.wait_for('1/3')
            pid = (self.work / 'task-held').read_text()
            session.send('q')
            self.assertEqual(session.finish(), 1, session.text)
            self.assertFalse(Path('/proc', pid).exists())
        finally:
            self.release()
