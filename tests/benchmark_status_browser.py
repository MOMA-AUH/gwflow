"""Measure captured browser interactions without imposing CI timing thresholds."""

import argparse
import asyncio
import gc
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import time
import weakref

from fixtures.browser_workload import CapturedBrowser


async def measure(size, cycles):
    samples = {}
    app = CapturedBrowser(selectors=('*',), details=True)
    start = time.perf_counter()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        initial = time.perf_counter() - start
        baseline_tasks = len(asyncio.all_tasks())
        baseline_fds = len(list(Path('/proc/self/fd').iterdir()))
        peak_tasks, peak_fds = baseline_tasks, baseline_fds
        old_snapshots = []
        for _ in range(cycles):
            for name, keys in (
                ('last_row', ('end',)), ('first_row', ('home',)),
                ('open_task_job_and_return', ('enter', 'enter', 'escape', 'escape')),
                ('search', ('slash', *'sample_023', 'enter')),
                ('clear_search', ('slash', 'ctrl+shift+a', 'backspace', 'enter')),
                ('filter_pending', ('f', 'c', 'home', 'space', 'enter')),
                ('clear_state_filter', ('f', 'c', 'enter')), ('replace_snapshot', ('r',)),
            ):
                if name == 'replace_snapshot':
                    old_snapshots.append(weakref.ref(app.snapshot))
                start = time.perf_counter()
                await pilot.press(*keys)
                await pilot.wait_for_scheduled_animations()
                samples.setdefault(name, []).append(time.perf_counter() - start)
            gc.collect()
            peak_tasks = max(peak_tasks, len(asyncio.all_tasks()))
            peak_fds = max(peak_fds, len(list(Path('/proc/self/fd').iterdir())))
        remaining = sum(reference() is not None for reference in old_snapshots)
        await pilot.press('q')
    return dict(size=list(size), initial_seconds=initial, cycles=cycles,
                actions={name: dict(median_seconds=statistics.median(values),
                                    maximum_seconds=max(values), samples_seconds=values)
                         for name, values in samples.items()},
                resources=dict(baseline_async_tasks=baseline_tasks, maximum_async_tasks=peak_tasks,
                               baseline_file_descriptors=baseline_fds, maximum_file_descriptors=peak_fds,
                               old_snapshots_remaining=remaining))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cycles', type=int, default=12)
    args = parser.parse_args()
    if args.cycles < 1:
        parser.error('--cycles must be positive')
    root = Path(__file__).resolve().parents[1]
    cpu = next(line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
               if line.startswith('model name'))
    record = dict(command='<PYTHON> tests/benchmark_status_browser.py --cycles ' + str(args.cycles) +
                          ' --output build/validation/status-browser/measurements.json',
                  placeholders={'<PYTHON>': 'Python executable of the installed candidate environment'},
                  source={path: hashlib.sha256((root / path).read_bytes()).hexdigest()
                          for path in ('src/gwflow/browser.py', 'tests/fixtures/browser_workload.py',
                                       'tests/benchmark_status_browser.py')},
                  commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
                  machine=dict(system=platform.system(), release=platform.release(), machine=platform.machine(),
                               cpu=cpu, logical_cpus=os.cpu_count(), available_cpus=len(os.sched_getaffinity(0)),
                               load_average_start=os.getloadavg()),
                  versions=dict(python=platform.python_version(),
                                **{name: version(name) for name in ('gwflow', 'gwf', 'textual', 'rich')}),
                  workload=dict(tasks=100, jobs=1000, group_sizes=[24, 76], expanded=True),
                  limits=['Headless Textual public Pilot interactions, including key delivery and animation settling.',
                          'Captured data excludes workflow imports, scheduler queries, and terminal emulator latency.',
                          'Shared host measurements; no wall-clock pass/fail threshold.'],
                  results=[await measure(size, args.cycles) for size in ((40, 12), (80, 24), (120, 40))],
                  maximum_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + '\n')


if __name__ == '__main__':
    asyncio.run(main())
