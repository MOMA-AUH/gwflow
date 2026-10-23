"""Lossless conversion through the internal attempt-record interface."""

import json
from pathlib import Path
import tempfile
import unittest

from gwflow._records import AttemptRecords


class RecordCompactionTests(unittest.TestCase):
    def setUp(self):
        self.new_records()

    def new_records(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.task_directory = Path(temporary.name) / 'task'
        self.attempt = dict(owner='c' * 32, task='task', attempt='a' * 32, operation='b' * 32)
        self.records = AttemptRecords(self.task_directory, self.attempt)
        self.records.initialize()
        self.root = self.task_directory / 'attempts' / self.attempt['attempt']
        self.collection = {
            'inputs.json': self.record('inputs', inputs={'source': 'accepted'}),
            'operations/' + 'd' * 32 + '/completion.json': self.record('completion', operation='d' * 32, outputs={}),
            'admissions/' + 'e' * 32 + '/outcome.json': self.record('job-outcome', state='failed'),
            'recoveries/' + 'e' * 32 + '/recovery.json': self.record('operator-recovery', assertion='inactive'),
        }
        for name, value in self.collection.items():
            self.records.publish(name, value)
        self.outcome_name = 'admissions/' + 'f' * 32 + '/outcome.json'
        self.outcome = self.record('job-outcome', state='complete')

    def record(self, kind, **fields):
        return dict(kind='gwflow.' + kind, schema=1,
                    **{key: self.attempt[key] for key in ('owner', 'task', 'attempt')}, **fields)

    def compact(self):
        self.records.compact(self.outcome_name, self.outcome, validate=lambda: None)

    def assert_compacted(self):
        state = json.loads((self.root / 'record-state.json').read_text())
        self.assertEqual(state['phase'], 'compacted')
        self.assertEqual(state['records'], {**self.collection, self.outcome_name: self.outcome})
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['record-state.json', 'records.lock'])

    def test_preserves_all_history_and_publishes_success_with_bounded_footprint(self):
        inode = (self.root / 'records.lock').stat().st_ino
        self.compact()
        self.assert_compacted()
        self.assertEqual((self.root / 'records.lock').stat().st_ino, inode)
        self.compact()
        self.assert_compacted()

    def test_publication_recovery_preserves_authority_and_refuses_unsafe_temporaries(self):
        from gwf.exceptions import WorkflowError
        import os
        self.compact()
        before = (self.root / 'record-state.json').read_bytes()
        temporary = self.root / ('.pending-' + '1' * 32)
        temporary.write_text('incomplete publication')
        self.records.recover_publications()
        self.assertFalse(temporary.exists())
        self.assertEqual((self.root / 'record-state.json').read_bytes(), before)
        self.assert_compacted()
        external = self.task_directory / 'unowned'
        external.write_text('preserve')
        temporary.symlink_to(external)
        with self.assertRaisesRegex(WorkflowError, 'Unsafe attempt publication temporary'):
            self.records.recover_publications()
        self.assertEqual(external.read_text(), 'preserve')
        temporary.unlink()
        os.link(external, temporary)
        with self.assertRaisesRegex(WorkflowError, 'Unsafe attempt publication temporary'):
            self.records.recover_publications()
        self.assertTrue(temporary.exists())
        self.assertEqual((self.root / 'record-state.json').read_bytes(), before)

    def test_uncertain_authority_publication_keeps_sources_until_recovery_syncs(self):
        from unittest.mock import patch
        import os
        import stat
        from gwf.exceptions import WorkflowError
        original_replace, original_sync = os.replace, os.fsync
        switched = False

        def replace(*args, **kwargs):
            nonlocal switched
            result = original_replace(*args, **kwargs)
            switched = True
            return result

        def sync(fd):
            if switched and stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('replacement synchronization unavailable')
            return original_sync(fd)

        with patch('os.replace', side_effect=replace), patch('os.fsync', side_effect=sync):
            with self.assertRaisesRegex(WorkflowError, 'synchronization'):
                self.compact()
        state = json.loads((self.root / 'record-state.json').read_text())
        self.assertEqual(state['phase'], 'consolidating')
        self.assertEqual(state['records'][self.outcome_name]['state'], 'compacting')
        for name in self.collection:
            self.assertTrue((self.root / name).is_file())
        with patch('os.fsync', side_effect=OSError('still unavailable')):
            with self.assertRaisesRegex(WorkflowError, 'replacement synchronization'):
                self.compact()
        for name in self.collection:
            self.assertTrue((self.root / name).is_file())
        self.compact()
        self.assert_compacted()

    def test_refuses_unexpected_files_and_unsafe_substitutions_without_deletion(self):
        import os
        from gwf.exceptions import WorkflowError
        outside = self.task_directory.parent / 'outside'
        outside.write_text('preserve me')
        path = self.root / 'unexpected'
        substitutions = ('ordinary', 'symlink', 'fifo', 'hardlink', 'directory')
        for substitution in substitutions:
            with self.subTest(substitution=substitution):
                if substitution == 'ordinary':
                    path.write_text('{}')
                elif substitution == 'symlink':
                    path.symlink_to(outside)
                elif substitution == 'fifo':
                    os.mkfifo(path)
                elif substitution == 'hardlink':
                    os.link(outside, path)
                else:
                    path.mkdir()
                try:
                    with self.assertRaises(WorkflowError):
                        self.compact()
                    self.assertEqual(json.loads((self.root / 'record-state.json').read_text())['phase'], 'individual')
                    for name in self.collection:
                        self.assertTrue((self.root / name).is_file())
                    self.assertEqual(outside.read_text(), 'preserve me')
                finally:
                    path.rmdir() if substitution == 'directory' else path.unlink()

    def test_refuses_changed_source_after_authority_switch_and_preserves_embedded_evidence(self):
        import os
        from unittest.mock import patch
        from gwf.exceptions import WorkflowError
        original_unlink = os.unlink

        def fail(path, **kwargs):
            if not str(path).startswith('.pending-'):
                raise OSError('redundant-file deletion refused')
            return original_unlink(path, **kwargs)

        with patch('os.unlink', side_effect=fail):
            with self.assertRaisesRegex(WorkflowError, 'redundant-file deletion refused'):
                self.compact()
        source = self.root / 'inputs.json'
        saved = source.read_bytes()
        source.write_text(json.dumps(self.record('inputs', inputs={'source': 'changed'})))
        with self.assertRaisesRegex(WorkflowError, 'ownership'):
            self.compact()
        self.assertEqual(self.records.read('inputs.json'), self.collection['inputs.json'])
        source.write_bytes(saved)
        self.compact()
        self.assert_compacted()

    def test_abrupt_exit_and_io_errors_at_every_publication_deletion_and_sync_boundary(self):
        import errno
        import os
        from unittest.mock import patch
        from gwf.exceptions import WorkflowError

        operations = ('replace', 'unlink', 'rmdir', 'fsync')
        originals = {name: getattr(os, name) for name in operations}
        trace = []

        def traced(name):
            def call(*args, **kwargs):
                trace.append(name)
                return originals[name](*args, **kwargs)
            return call

        from contextlib import ExitStack
        with ExitStack() as patches:
            for name in operations:
                patches.enter_context(patch('os.' + name, side_effect=traced(name)))
            self.compact()
        self.assertGreater(len(trace), 20)
        # Each boundary is exercised with actual filesystem calls both before
        # and after abrupt death, and with an I/O error before the operation.
        for boundary, operation in enumerate(trace):
            for fault in ('exit-before', 'exit-after', 'io-error'):
                with self.subTest(boundary=boundary, operation=operation, fault=fault):
                    self.new_records()
                    calls = 0

                    def intercepted(name):
                        def call(*args, **kwargs):
                            nonlocal calls
                            selected = calls == boundary
                            calls += 1
                            if selected and fault == 'exit-before':
                                os._exit(71)
                            if selected and fault == 'io-error':
                                raise OSError(errno.EIO, 'injected ' + name + ' failure')
                            try:
                                result = originals[name](*args, **kwargs)
                            finally:
                                if selected and fault == 'exit-after':
                                    os._exit(71)
                            return result
                        return call

                    if fault.startswith('exit'):
                        pid = os.fork()
                        if pid == 0:
                            try:
                                with ExitStack() as patches:
                                    for name in operations:
                                        patches.enter_context(patch('os.' + name, side_effect=intercepted(name)))
                                    self.compact()
                            except BaseException:
                                os._exit(72)
                            os._exit(0)
                        _, status = os.waitpid(pid, 0)
                        self.assertEqual(os.waitstatus_to_exitcode(status), 71)
                    else:
                        with ExitStack() as patches:
                            for name in operations:
                                patches.enter_context(patch('os.' + name, side_effect=intercepted(name)))
                            with self.assertRaisesRegex(WorkflowError, 'injected'):
                                self.compact()
                    for name, value in self.collection.items():
                        self.assertEqual(self.records.read(name), value)
                    self.compact()
                    self.assert_compacted()

    def test_late_writer_and_reader_wait_for_conversion_and_preserve_all_evidence(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from unittest.mock import patch
        import os
        entered, release, reading, writing = (Event() for _ in range(4))
        original_replace = os.replace
        held = False

        def replace(*args, **kwargs):
            nonlocal held
            result = original_replace(*args, **kwargs)
            if not held:
                held = True
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError('conversion gate timed out')
            return result

        late_name = 'admissions/' + 'f' * 32 + '/ack.json'
        late_value = self.record('submission-ack', job_id=17)

        def read():
            reading.set()
            return AttemptRecords(self.task_directory, self.attempt).read('inputs.json')

        def write():
            writing.set()
            AttemptRecords(self.task_directory, self.attempt).publish(late_name, late_value)

        with ThreadPoolExecutor(max_workers=3) as workers, patch('os.replace', side_effect=replace):
            converter = workers.submit(self.compact)
            try:
                self.assertTrue(entered.wait(timeout=5))
                reader, writer = workers.submit(read), workers.submit(write)
                self.assertTrue(reading.wait(timeout=5))
                self.assertTrue(writing.wait(timeout=5))
                self.assertFalse(reader.done())
                self.assertFalse(writer.done())
            finally:
                release.set()
            converter.result(timeout=5)
            self.assertEqual(reader.result(timeout=5), self.collection['inputs.json'])
            writer.result(timeout=5)
        self.collection[late_name] = late_value
        self.assert_compacted()

    def test_owned_publication_leftovers_are_removed_including_interrupted_exclusive_link(self):
        import os
        (self.root / ('.pending-' + '1' * 32)).write_bytes(b'{partial publication')
        source = self.root / 'inputs.json'
        os.link(source, self.root / ('.pending-' + '2' * 32))
        self.compact()
        self.assert_compacted()

    def test_filesystem_full_before_authority_switch_preserves_all_originals(self):
        import errno
        from unittest.mock import patch
        from gwf.exceptions import WorkflowError

        def full(record, stream, **kwargs):
            stream.write('{partial publication')
            raise OSError(errno.ENOSPC, 'No space left on device')

        with patch('json.dump', side_effect=full):
            with self.assertRaisesRegex(WorkflowError, 'filesystem full.*before replacement'):
                self.compact()
        self.assertEqual(json.loads((self.root / 'record-state.json').read_text())['phase'], 'individual')
        for name, value in self.collection.items():
            self.assertEqual(self.records.read(name), value)
        self.compact()
        self.assert_compacted()
