"""Retain installed-worker acceptance evidence on a selected filesystem."""

import argparse
import importlib.metadata
import json
from pathlib import Path
import platform
import sys
import types
import unittest


TESTS = (
    'test_automatic_compaction.AutomaticCompactionTests.test_lost_completion_acknowledgement_reconciles_in_compacted_storage',
    'test_automatic_compaction.AutomaticCompactionTests.test_simultaneous_completions_and_producer_reads_keep_independent_records',
    'test_automatic_compaction.AutomaticCompactionTests.test_pinned_slurm_blocks_dependents_after_compaction_failure_and_retries',
    'test_record_compaction_integration.RecordCompactionIntegrationTests.test_real_completion_worker_compacts_before_late_frontend_acknowledgement',
    'test_record_compaction_integration.RecordCompactionIntegrationTests.test_ordinary_run_retries_only_compaction_after_publication_failure',
    'test_compacted_cleanup.CompactedProducerCleanupTests.test_historical_transfer_cleanup_gives_up_repair_and_allows_new_consumers',
    'test_compacted_repair.CompactedRepairTests.test_repeated_repair_restores_before_submission_and_recompacts_history',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='New directory on the filesystem under validation')
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    cases = []

    def make_workdir(case):
        work = root / case._testMethodName
        work.mkdir()
        return work

    for name in TESTS:
        case = next(iter(unittest.defaultTestLoader.loadTestsFromName(name)))
        case.make_workdir = types.MethodType(make_workdir, case)
        cases.append(case)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(cases))
    attempts = []
    for path in sorted(root.rglob('record-state.json')):
        state = json.loads(path.read_text())
        attempts.append({
            'path': str(path.parent), 'phase': state['phase'],
            'files': sorted(child.name for child in path.parent.iterdir()),
            'logical_records': len(state['records']) if state['records'] is not None else None,
            'lock_device': (path.parent / 'records.lock').stat().st_dev,
            'lock_inode': (path.parent / 'records.lock').stat().st_ino,
        })
    summary = {
        'command': [sys.executable, *sys.argv],
        'python': platform.python_version(),
        'packages': {name: importlib.metadata.version(name) for name in ('gwflow', 'gwf', 'rich', 'textual')},
        'tests': list(TESTS), 'tests_run': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors),
        'root_device': root.stat().st_dev, 'attempts': attempts,
        'scope': 'Real local workers on the selected filesystem; pinned gwf Slurm command fixture, not a live Slurm scheduler. Process interruption and I/O faults do not establish power-loss durability.',
    }
    (root / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
