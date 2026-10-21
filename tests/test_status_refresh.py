"""Manual status refresh through the installed command and a real keyboard."""

from support import FIXTURES, LocalBackendTestCase
from terminal_session import TerminalSession
import os
import json
from pathlib import Path
import sys
import test_grouped_status
import test_interactive_status
import test_graphs


class StatusRefreshTests(LocalBackendTestCase):
    configure_workflow = test_grouped_status.GroupedStatusTests.configure_workflow
    terminal = test_interactive_status.InteractiveStatusTests.terminal
    slurm_environment = test_graphs.TaskGraphTests.slurm_environment

    def test_refresh_installs_blocked_snapshot_and_notices_even_when_filter_hides_task(self):
        session = self.terminal('status', 'mapping__A', '--status', 'reusable')
        session.wait_for('Showing 0 of 1 selected Tasks')
        library = self.work / 'library.py'
        library.write_text(library.read_text().replace('    target <<',
                            "    target.image = 'missing-image.sif'\n    target <<"))
        checkpoint = session.send('r')
        session.wait_for('Workflow blocked', after=checkpoint)
        session.wait_for('Showing 0 of 1 selected Tasks', after=checkpoint)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)

    def test_repeated_real_refreshes_release_children_and_file_handles(self):
        workflow = self.work / 'workflow.py'
        declaration = workflow.read_text() + (
            "\nimport os\nwith open('inspection-pids', 'a') as stream: stream.write(str(os.getpid()) + '\\n')\n")
        workflow.write_text(declaration)
        session = self.terminal('status', 'mapping*')
        session.wait_for('Showing 3 of 3 selected Tasks')
        pid = session.process.pid
        children = Path(f'/proc/{pid}/task/{pid}/children')
        self.wait_for(lambda: not children.read_text().strip())
        baseline = len(list(Path(f'/proc/{pid}/fd').iterdir()))
        for index in range(8):
            workflow.write_text(declaration + f"gwf.task(mapping(), key='cycle_{index:02}')\n")
            checkpoint = session.send('r')
            session.wait_for(f'mapping__cycle_{index:02}', after=checkpoint)
            self.wait_for(lambda: not children.read_text().strip())
            self.assertLessEqual(len(list(Path(f'/proc/{pid}/fd').iterdir())), baseline + 1)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)
        pids = (self.work / 'inspection-pids').read_text().splitlines()
        self.assertEqual(len(pids), 9)
        for pid in pids:
            self.assertFalse(Path(f'/proc/{pid}').exists(), pid)

    def test_interface_failure_during_refresh_stops_owned_work_and_preserves_traceback(self):
        workflow = self.work / 'workflow.py'
        declaration = workflow.read_text()
        session = TerminalSession([sys.executable, str(FIXTURES / 'browser_fault.py'), 'status', 'mapping*'],
                                  cwd=self.work)
        self.addCleanup(session.close)
        session.wait_for('Showing 3 of 3 selected Tasks')
        workflow.write_text(declaration + "\nimport os, time\nfrom pathlib import Path\n"
                            "Path('fault-refresh-pid').write_text(str(os.getpid()))\n"
                            "while True: time.sleep(0.025)\n")
        session.send('r')
        self.wait_for((self.work / 'fault-refresh-pid').exists)
        pid = int((self.work / 'fault-refresh-pid').read_text())
        session.send('x')
        self.assertEqual(session.finish(), 1, session.text)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        after_restore = session.output.decode().rsplit('\x1b[?1049l', 1)[-1]
        self.assertIn("KeyError: 'original interface failure'", after_restore)
        self.assertIn('RuntimeError: injected interface failure', after_restore)
        workflow.write_text(declaration)
        self.cli('status', '--static')

    def test_simulated_slurm_refresh_updates_state_queries_once_and_recovers_failure(self):
        environment = {**self.slurm_environment(), 'GWFLOW_TEST_RECORD_SLURM_QUERIES': '1'}
        library = self.work / 'library.py'
        library.write_text(library.read_text().replace('printf result > out.txt',
                           f'touch {self.work}/started; while test ! -e {self.work}/release; do sleep 0.05; done; '
                           'printf result > out.txt').replace(str(self.work), '"' + str(self.work) + '"'))
        self.cli('-b', 'slurm', 'run', 'mapping__A', env=environment)
        self.wait_for((self.work / 'started').exists)
        session = self.terminal('-b', 'slurm', 'status', 'mapping__A', env=environment)
        session.wait_for('running')
        queries = self.work / 'slurm-queries.jsonl'
        before = queries.read_text().splitlines()
        session.send('/')
        session.wait_for('Search Task names')
        session.send('mapping\r')
        session.wait_for('Name: mapping')
        session.send('f')
        session.wait_for('Filter Task states')
        checkpoint = session.send('\x1b')
        session.wait_for('Name: mapping', after=checkpoint)
        self.assertEqual(queries.read_text().splitlines(), before)
        (self.work / 'release').touch()
        self.finish()
        checkpoint = session.send('r')
        session.wait_for('reusable', after=checkpoint)
        added = [json.loads(line)['command'] for line in queries.read_text().splitlines()[len(before):]]
        self.assertEqual(added.count('squeue'), 1)
        self.assertEqual(added.count('sacct'), 1)
        marker = self.work / 'slurm-query-error'
        marker.touch()
        checkpoint = session.send('r')
        session.wait_for('Refresh failed:', after=checkpoint)
        marker.unlink()
        checkpoint = session.send('r')
        session.wait_for('Last inspection finished', after=checkpoint)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)
        after_restore = session.output.decode().rsplit('\x1b[?1049l', 1)[-1]
        self.assertNotIn('Controlled scheduler query failure', after_restore)

    def test_refresh_reimports_definitions_and_reapplies_original_glob(self):
        self.cli('run', 'mapping__A')
        self.finish()
        library = self.work / 'library.py'
        library.write_text(library.read_text() +
                           "\nwith open('imports', 'a') as stream: stream.write('import\\n')\n")
        session = self.terminal('status', 'mapping*')
        session.wait_for('Showing 3 of 3 selected Tasks')
        session.wait_for('3/3')
        stamp = library.stat()
        library.write_text(library.read_text().replace('printf result > out.txt', 'printf edited > out.txt'))
        os.utime(library, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() + "gwf.task(mapping(), key='D')\n")
        checkpoint = session.send('r')
        session.wait_for('Showing 4 of 4 selected Tasks', after=checkpoint, timeout=5)
        session.wait_for('mapping__D', after=checkpoint)
        session.wait_for('fresh attempt required', after=checkpoint)
        self.assertEqual((self.work / 'imports').read_text(), 'import\nimport\n')
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)

    def test_busy_refresh_keeps_navigation_accepts_only_one_request_and_quit_uses_last_result(self):
        workflow = self.work / 'workflow.py'
        declaration = workflow.read_text()
        workflow.write_text(declaration + "\nwith open('busy-imports', 'a') as stream: stream.write('import\\n')\n")
        session = self.terminal('status', 'mapping*')
        session.wait_for('Showing 3 of 3 selected Tasks')
        workflow.write_text(workflow.read_text() +
                            "gwf.task(mapping(), key='D')\n"
                            "import os, time\nfrom pathlib import Path\nPath('refresh-pid').write_text(str(os.getpid()))\n"
                            "while not Path('refresh-release').exists(): time.sleep(0.025)\n")
        checkpoint = session.send('rrrr')
        self.wait_for((self.work / 'refresh-pid').exists)
        session.wait_for('Refreshing...', after=checkpoint)
        session.send('\r')
        session.wait_for('Jobs completed: 0/3')
        session.send('\r')
        session.wait_for('Public job:     mapping__A__gwflow_prepare')
        session.send('\x1b\x1b')
        (self.work / 'refresh-release').touch()
        session.wait_for('Showing 4 of 4 selected Tasks', after=checkpoint)
        session.send('?')
        session.wait_for('Keyboard reference')
        self.assertEqual((self.work / 'busy-imports').read_text(), 'import\nimport\n')
        (self.work / 'refresh-release').unlink()
        (self.work / 'refresh-pid').unlink()
        session.send('r')
        self.wait_for((self.work / 'refresh-pid').exists)
        pid = int((self.work / 'refresh-pid').read_text())
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertIn(b'\x1b[?1049l', session.output)

    def test_failed_refresh_keeps_complete_error_and_quitting_retry_retains_failed_result(self):
        workflow = self.work / 'workflow.py'
        declaration = workflow.read_text()
        session = self.terminal('status', 'mapping*')
        session.wait_for('Showing 3 of 3 selected Tasks')
        workflow.write_text("try:\n    raise KeyError('refresh cause')\n"
                            "except KeyError as error:\n    raise RuntimeError('refresh failure') from error\n")
        checkpoint = session.send('r')
        session.wait_for('Refresh failed:', after=checkpoint)
        checkpoint = session.send('\t')
        session.wait_for('Press Enter for error details', after=checkpoint)
        session.send('\r')
        session.wait_for('Error details')
        session.send('\r')
        session.wait_for("KeyError: 'refresh cause'")
        checkpoint = session.send('\x1b')
        session.wait_for('Showing 3 of 3 selected Tasks', after=checkpoint)
        workflow.write_text("import os, time\nfrom pathlib import Path\n"
                            "Path('retry-pid').write_text(str(os.getpid()))\n"
                            "while True: time.sleep(0.025)\n" + declaration)
        session.send('r')
        self.wait_for((self.work / 'retry-pid').exists)
        pid = int((self.work / 'retry-pid').read_text())
        session.send('q')
        self.assertEqual(session.finish(), 1, session.text)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        after_restore = session.output.decode().rsplit('\x1b[?1049l', 1)[-1]
        self.assertIn("KeyError: 'refresh cause'", after_restore)
        self.assertIn('RuntimeError: refresh failure', after_restore)

    def test_unmatched_glob_refresh_keeps_prior_snapshot_beside_valid_selector_and_exits_failed(self):
        session = self.terminal('status', 'mapping__A', 'remapping*')
        session.wait_for('Showing 2 of 2 selected Tasks')
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text().replace("gwf.task(mapping(), alias='remapping', key='B')\n", ''))
        checkpoint = session.send('r')
        session.wait_for('Refresh failed:', after=checkpoint, timeout=5)
        session.wait_for('previous snapshot', after=checkpoint)
        checkpoint = session.send('\x1b[F\r')
        session.wait_for('Tasks > remapping__B', after=checkpoint)
        session.send('q')
        self.assertEqual(session.finish(), 1, session.text)
        self.assertIn("Unmatched Task selectors: 'remapping*'", session.text)

    def test_initial_failure_can_retry_with_visible_loading_and_clear_failed_exit(self):
        workflow = self.work / 'workflow.py'
        declaration = workflow.read_text()
        workflow.write_text('if True\n    pass\n')
        session = self.terminal('status', 'mapping*')
        session.wait_for('No snapshot available')
        session.wait_for('workflow.py:1')
        workflow.write_text(
            "import time\nfrom pathlib import Path\nPath('retry-started').touch()\n"
            "while not Path('retry-release').exists(): time.sleep(0.025)\n" + declaration)
        checkpoint = session.send('r')
        try:
            self.wait_for((self.work / 'retry-started').exists)
            session.wait_for('Inspecting workflow', after=checkpoint, timeout=5)
        finally:
            (self.work / 'retry-release').touch()
        session.wait_for('Showing 3 of 3 selected Tasks', after=checkpoint)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)
        self.assertNotIn('could not load', session.output.decode().rsplit('\x1b[?1049l', 1)[-1])

    def test_refresh_preserves_current_job_and_updates_its_task_summary(self):
        self.cli('run', 'mapping__A')
        self.finish()
        session = self.terminal('status', 'mapping*')
        session.wait_for('3/3')
        session.send('\r')
        session.wait_for('Jobs completed: 3/3')
        session.send('\x1b[B\r')
        session.wait_for('Public job:     mapping__A__compute')
        library = self.work / 'library.py'
        library.write_text(library.read_text().replace('printf result > out.txt', 'printf changed_result > out.txt'))
        checkpoint = session.send('r')
        session.wait_for('Requires repetition', after=checkpoint, timeout=5)
        session.wait_for('Public job:     mapping__A__compute', after=checkpoint)
        checkpoint = session.send('\x1b')
        session.wait_for('Jobs completed: 0/3', after=checkpoint)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)

    def test_refresh_retains_group_identity_when_display_labels_become_qualified(self):
        session = self.terminal('status')
        session.wait_for('Showing 4 of 4 selected Tasks')
        session.send('\r')
        session.wait_for('Instance')
        session.send('\r')
        session.wait_for('Jobs completed: 0/3')
        session.send('\r')
        session.wait_for('Public job:     mapping__A__gwflow_prepare')
        (self.work / 'second.py').write_text((self.work / 'library.py').read_text())
        workflow = self.work / 'workflow.py'
        workflow.write_text(workflow.read_text() +
                            "from second import mapping as other\ngwf.task(other(), key='D')\n")
        checkpoint = session.send('r')
        session.wait_for('Groups > mapping@library.mapping > mapping__A', after=checkpoint, timeout=5)
        checkpoint = session.send('\x1b')
        session.wait_for('Jobs completed: 0/3', after=checkpoint)
        checkpoint = session.send('\x1b')
        session.wait_for('Instance', after=checkpoint)
        session.wait_for('mapping@library.mapping', after=checkpoint)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)

    def test_disappearing_job_returns_to_its_surviving_task_with_an_explanation(self):
        session = self.terminal('status', 'mapping__A')
        session.wait_for('Showing 1 of 1 selected Tasks')
        session.send('\r')
        session.wait_for('Jobs completed: 0/3')
        session.send('\x1b[B\r')
        session.wait_for('Public job:     mapping__A__compute')
        library = self.work / 'library.py'
        library.write_text(library.read_text().replace("target('compute'", "target('replacement'"))
        checkpoint = session.send('r')
        session.wait_for('no longer available', after=checkpoint, timeout=5)
        session.wait_for('Jobs completed: 0/3', after=checkpoint)
        session.wait_for('replacement', after=checkpoint)
        session.send('q')
        self.assertEqual(session.finish(), 0, session.text)
