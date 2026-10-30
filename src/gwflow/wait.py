"""Wait for existing attempts without advancing their work."""

import math
import time

import click

from ._inspection import InspectionProcess
from .wait_report import WaitReport


class _WaitTimeout(Exception):
    """The command's overall waiting limit expired."""


@click.command(name='wait')
@click.argument('selectors', nargs=-1)
@click.option('--interval', type=click.FloatRange(min=0, min_open=True), default=60, show_default=True,
              help='Seconds between inspections.')
@click.option('--timeout', type=click.FloatRange(min=0, min_open=True), metavar='SECONDS',
              help='Limit the whole command, including loading, lock waits, and inspections. No limit by default.')
@click.pass_context
def wait(ctx, selectors, interval, timeout):
    """Observe existing Tasks selected by exact names or quoted patterns.

    Include required upstream Tasks. Submit, retry, and cancel no work.
    Inspect immediately, then pause between checks. Messages use stderr;
    stdout stays empty. Exit 0 on completion, 1 on failure or required action,
    2 on invalid usage, 124 on timeout, or 130 on Ctrl+C. A timeout does not
    establish Task failure. Jobs can continue after waiting stops.
    """
    if not math.isfinite(interval):
        raise click.BadParameter('must be a finite number greater than zero', param_hint='--interval')
    if timeout is not None and not math.isfinite(timeout):
        raise click.BadParameter('must be a finite number greater than zero', param_hint='--timeout')
    root = ctx.find_root()
    deadline = root.meta['wait_started'] + timeout if timeout is not None else None

    def pause(seconds):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _WaitTimeout
            seconds = min(seconds, remaining)
        time.sleep(seconds)
        if deadline is not None and time.monotonic() >= deadline:
            raise _WaitTimeout

    options = ['--file', root.params['file']]
    if root.params['backend'] is not None:
        options.extend(('--backend', root.params['backend']))
    verbosity = []
    if root.get_parameter_source('verbose') == click.core.ParameterSource.COMMANDLINE:
        verbosity = ['--verbose', root.params['verbose']]
    options.extend(verbosity)
    report = WaitReport()
    report.message('Waiting for existing Task attempts.')
    inspection = None
    awaited = None
    scheduler_errors = []
    previous_output = None
    with report.live():
        try:
            while True:
                pause(0)
                inspection = InspectionProcess((options, selectors), module='gwflow.wait_snapshot', command='wait-snapshot', request=awaited)
                while not inspection.completed():
                    phase = inspection.result.with_suffix('.phase')
                    if phase.exists() and not report.tasks:
                        report.phase = phase.read_text()
                    pause(0.025)
                pause(0)
                snapshot, failure, output = inspection.outcome()
                returncode = inspection.process.returncode
                inspection.close()
                inspection = None
                captured = output + (snapshot['inspection_diagnostics'] if snapshot is not None else '')
                if captured and captured != previous_output:
                    report.message(captured, nl=False)
                previous_output = captured
                pause(0)
                if failure:
                    report.message(failure)
                    report.message('Waiting failed. Submitted jobs may still be queued or running.')
                    raise click.exceptions.Exit(returncode if returncode == 2 else 1)
                errors = snapshot['scheduler_errors']
                if errors != scheduler_errors:
                    if errors:
                        report.message('Scheduler unavailable; current job states are unavailable: ' + '; '.join(errors))
                    else:
                        report.message('Scheduler observations recovered.')
                    scheduler_errors = errors
                if awaited is None:
                    options = snapshot['inspection_options'] + verbosity
                    awaited = {task['name']: task['attempt'] for task in snapshot['tasks']}
                    selectors = tuple(awaited)
                report.update(snapshot['tasks'])
                pause(0)
                if not errors and all(task['complete'] for task in snapshot['tasks']):
                    report.phase = 'All awaited Task attempts are complete.'
                    if not report.decorated:
                        report.message(report.phase)
                    return
                if any(not (task['complete'] or task['pending']) for task in snapshot['tasks']):
                    report.message('Waiting cannot complete; another command is required. Submitted jobs may still be queued or running.')
                    raise click.exceptions.Exit(1)
                pause(interval)
        except (KeyboardInterrupt, _WaitTimeout) as error:
            if inspection is not None:
                inspection.stop()
                phase = inspection.result.with_suffix('.phase')
                if phase.exists():
                    report.message(phase.read_text())
                output, diagnostics = inspection.output()
                if output or diagnostics:
                    report.message(output + diagnostics, nl=False)
            message = 'Waiting interrupted.' if isinstance(error, KeyboardInterrupt) else f'Waiting timed out after {timeout:g} seconds.'
            report.message(message + ' Submitted jobs may still be queued or running.')
            raise click.exceptions.Exit(130 if isinstance(error, KeyboardInterrupt) else 124)
        finally:
            if inspection is not None:
                inspection.close()
