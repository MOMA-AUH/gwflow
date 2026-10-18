"""Record the automated follow workload's real read counts and resource checks."""

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import subprocess
import unittest

from test_status_follow import StatusFollowReadBoundsTests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    case = StatusFollowReadBoundsTests(
        'test_follow_reads_and_resources_remain_bounded_under_file_changes_and_view_cycles')
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([case]))
    if not result.wasSuccessful():
        raise SystemExit(1)
    root = Path(__file__).resolve().parents[1]
    sources = ('src/gwflow/browser.py', 'src/gwflow/browser_evidence.py', 'src/gwflow/_browser_read.py',
               'tests/fixtures/browser_workload.py', 'tests/fixtures/log_read_probe.c',
               'tests/test_status_logs.py', 'tests/test_status_follow.py', 'tests/benchmark_status_follow.py')
    cpu = next(line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
               if line.startswith('model name'))
    record = dict(
        recorded_at=datetime.now(timezone.utc).isoformat(),
        scope='Issue #232 live log following; release preparation and publication remain separate.',
        result='passed: bounded follow reads and repeated follow/pause/close resource checks',
        command='<PYTHON> tests/benchmark_status_follow.py --output build/validation/status-follow/measurements.json',
        placeholders={'<PYTHON>': 'Python executable of the installed candidate environment'},
        commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
        source={path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in sources},
        versions=dict(python=platform.python_version(),
                      **{name: version(name) for name in ('gwflow', 'gwf', 'textual', 'rich')}),
        machine=dict(system=platform.system(), release=platform.release(), machine=platform.machine(), cpu=cpu,
                     logical_cpus=os.cpu_count(), available_cpus=len(os.sched_getaffinity(0)),
                     load_average_end=os.getloadavg()),
        workload={'selected_tasks': 1, 'selected_jobs': 1, 'initial_utf8_payload_bytes': 400000,
                  'replacement_count': 5, 'replacement_utf8_payload_bytes': 160000,
                  'rapid_appends': 30, 'bytes_per_rapid_append': 8192,
                  'split_utf8_sequence_bytes': 4, 'follow_pause_close_cycles': 12},
        asserted_limits={'read_bytes_per_request': 131088, 'rendered_log_text_bytes': 200704,
                         'extra_async_tasks_after_cycles': 2, 'extra_descriptors_after_cycles': 1,
                         'remaining_reader_processes': 0, 'remaining_closed_views': 0},
        measurements=case.measurements,
        limits=['Public Textual Pilot interactions with captured observations and controlled temporary files.',
                'Test-only libc instrumentation counts returned bytes from actual selected-log reads.',
                'Elapsed time includes file creation, keyboard delivery, rendering, and one-second follow checks.',
                'Compiler setup is excluded from the measured child-workload elapsed time.',
                'Child maximum RSS includes the controlled workload and Python/UI runtime; it is not a text-size bound.',
                'Shared-host measurement; no tight wall-clock gate and no production-cluster performance claim.'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + '\n')


if __name__ == '__main__':
    main()
