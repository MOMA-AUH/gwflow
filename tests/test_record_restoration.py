"""Recoverable restoration through the attempt-record interface."""

import json
import unittest

import test_record_compaction


class RecordRestorationTests(unittest.TestCase):
    setUp = test_record_compaction.RecordCompactionTests.setUp
    new_records = test_record_compaction.RecordCompactionTests.new_records
    record = test_record_compaction.RecordCompactionTests.record
    compact = test_record_compaction.RecordCompactionTests.compact
    assert_compacted = test_record_compaction.RecordCompactionTests.assert_compacted

    def test_restores_losslessly_and_recompacts_with_the_same_lock(self):
        self.compact()
        lock = (self.root / 'records.lock').stat().st_ino
        self.records.restore()
        self.assertEqual(json.loads((self.root / 'record-state.json').read_text())['phase'], 'individual')
        for name, record in {**self.collection, self.outcome_name: self.outcome}.items():
            self.assertEqual(self.records.read(name), record)
            self.assertEqual(json.loads((self.root / name).read_text()), record)
        self.records.restore()
        self.compact()
        self.assert_compacted()
        self.assertEqual((self.root / 'records.lock').stat().st_ino, lock)

    def test_interruptions_and_io_errors_at_every_reconstruction_boundary(self):
        from contextlib import ExitStack
        import errno
        import os
        from unittest.mock import patch
        from gwf.exceptions import WorkflowError

        operations = ('mkdir', 'link', 'rename', 'replace', 'unlink', 'rmdir', 'fsync')
        originals = {name: getattr(os, name) for name in operations}
        trace = []

        def traced(name):
            def call(*args, **kwargs):
                trace.append(name)
                return originals[name](*args, **kwargs)
            return call

        self.compact()
        with ExitStack() as patches:
            for name in operations:
                patches.enter_context(patch('os.' + name, side_effect=traced(name)))
            self.records.restore()
        self.assertGreater(len(trace), 20)
        for boundary, operation in enumerate(trace):
            for fault in ('exit-before', 'exit-after', 'io-error'):
                with self.subTest(boundary=boundary, operation=operation, fault=fault):
                    self.new_records()
                    self.compact()
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
                                return originals[name](*args, **kwargs)
                            finally:
                                if selected and fault == 'exit-after':
                                    os._exit(71)
                        return call

                    if fault.startswith('exit'):
                        pid = os.fork()
                        if pid == 0:
                            try:
                                with ExitStack() as patches:
                                    for name in operations:
                                        patches.enter_context(patch('os.' + name, side_effect=intercepted(name)))
                                    self.records.restore()
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
                                self.records.restore()
                    for name, value in self.collection.items():
                        self.assertEqual(self.records.read(name), value)
                    self.records.restore()
                    self.compact()
                    self.assert_compacted()

    def test_unowned_destinations_and_replaced_reconstruction_are_not_adopted(self):
        import os
        from unittest.mock import patch
        from gwf.exceptions import WorkflowError

        self.compact()
        (self.root / 'inputs.json').write_text(json.dumps(self.collection['inputs.json']))
        with self.assertRaisesRegex(WorkflowError, 'Unowned restoration metadata'):
            self.records.restore()
        self.assertEqual(self.records.read('inputs.json'), self.collection['inputs.json'])
        (self.root / 'inputs.json').unlink()
        original = os.rename

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('interrupted reconstruction installation')

        with patch('os.rename', side_effect=interrupted):
            with self.assertRaisesRegex(WorkflowError, 'interrupted reconstruction'):
                self.records.restore()
        installed = next(path for path in self.root.rglob('*.json')
                         if path.name != 'record-state.json'
                         and not any(part.startswith('.restoring-') for part in path.parts))
        data = installed.read_bytes()
        replacement = self.task_directory / 'replacement'
        replacement.write_bytes(data)
        replacement.replace(installed)
        with self.assertRaisesRegex(WorkflowError, 'Unowned restoration metadata'):
            self.records.restore()
        self.assertEqual(installed.read_bytes(), data)
        for name, value in self.collection.items():
            self.assertEqual(self.records.read(name), value)

    def test_restoration_retry_preserves_late_writes_to_embedded_authority(self):
        import os
        from unittest.mock import patch
        from gwf.exceptions import WorkflowError

        self.compact()
        original = os.rename

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('interrupted install')

        with patch('os.rename', side_effect=interrupted):
            with self.assertRaisesRegex(WorkflowError, 'interrupted install'):
                self.records.restore()
        late = 'admissions/' + 'f' * 32 + '/ack.json'
        value = self.record('submission-ack', job_id=17)
        self.records.publish(late, value)
        self.collection[late] = value
        self.records.restore()
        self.assertEqual(self.records.read(late), value)
        self.compact()
        self.assert_compacted()

    def test_restoration_can_resume_a_partly_deleted_consolidation(self):
        import os
        from unittest.mock import patch
        from gwf.exceptions import WorkflowError

        original = os.unlink

        def interrupted(name, **kwargs):
            original(name, **kwargs)
            if name == 'inputs.json':
                raise OSError('interrupted compaction deletion')

        with patch('os.unlink', side_effect=interrupted):
            with self.assertRaisesRegex(WorkflowError, 'interrupted compaction deletion'):
                self.compact()
        self.records.restore()
        for name, value in self.collection.items():
            self.assertEqual(self.records.read(name), value)
        self.compact()
        self.assert_compacted()

    def test_readers_and_late_writers_wait_for_the_authority_switch(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from unittest.mock import patch
        import os
        from gwflow._records import AttemptRecords

        self.compact()
        entered, release, reading, writing = (Event() for _ in range(4))
        original = os.rename
        held = False

        def gate(*args, **kwargs):
            nonlocal held
            result = original(*args, **kwargs)
            if not held:
                held = True
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError('restoration gate timed out')
            return result

        late = 'admissions/' + 'f' * 32 + '/ack.json'
        value = self.record('submission-ack', job_id=17)

        def read():
            reading.set()
            return AttemptRecords(self.task_directory, self.attempt).read('inputs.json')

        def write():
            writing.set()
            AttemptRecords(self.task_directory, self.attempt).publish(late, value)

        with ThreadPoolExecutor(max_workers=3) as workers, patch('os.rename', side_effect=gate):
            converter = workers.submit(self.records.restore)
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
        self.collection[late] = value
        self.compact()
        self.assert_compacted()
