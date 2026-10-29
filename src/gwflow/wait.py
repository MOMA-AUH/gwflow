"""Wait for existing attempts without advancing their work."""

import math
import time

import click

from ._inspection import InspectionProcess


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
    click.echo('Waiting for existing Task attempts.', err=True)
    inspection = None
    previous = {}
    awaited = None
    scheduler_errors = []
    previous_output = None
    try:
        while True:
            pause(0)
            inspection = InspectionProcess((options, selectors), module='gwflow.wait_snapshot', command='wait-snapshot', request=awaited)
            while not inspection.completed():
                pause(0.025)
            pause(0)
            snapshot, failure, output = inspection.outcome()
            returncode = inspection.process.returncode
            inspection.close()
            inspection = None
            captured = output + (snapshot['inspection_diagnostics'] if snapshot is not None else '')
            if captured and captured != previous_output:
                click.echo(captured, err=True, nl=False)
            previous_output = captured
            pause(0)
            if failure:
                click.echo(failure, err=True)
                click.echo('Waiting failed. Submitted jobs may still be queued or running.', err=True)
                raise click.exceptions.Exit(returncode if returncode == 2 else 1)
            errors = snapshot['scheduler_errors']
            if errors != scheduler_errors:
                if errors:
                    click.echo('Scheduler unavailable; current job states are unavailable: ' + '; '.join(errors), err=True)
                else:
                    click.echo('Scheduler observations recovered.', err=True)
                scheduler_errors = errors
            if awaited is None:
                options = snapshot['inspection_options'] + verbosity
                awaited = {task['name']: task['attempt'] for task in snapshot['tasks']}
                selectors = tuple(awaited)
            for task in snapshot['tasks']:
                message = f"{task['name']}: {task['state']}; {task['reason']}"
                if previous.get(task['name']) != message:
                    click.echo(message, err=True)
                    previous[task['name']] = message
            pause(0)
            if not errors and all(task['complete'] for task in snapshot['tasks']):
                click.echo('All awaited Task attempts are complete.', err=True)
                return
            if any(not (task['complete'] or task['pending']) for task in snapshot['tasks']):
                click.echo('Waiting cannot complete; another command is required. Submitted jobs may still be queued or running.', err=True)
                raise click.exceptions.Exit(1)
            pause(interval)
    except (KeyboardInterrupt, _WaitTimeout) as error:
        if inspection is not None:
            inspection.stop()
            output, diagnostics = inspection.output()
            if output or diagnostics:
                click.echo(output + diagnostics, err=True, nl=False)
        message = 'Waiting interrupted.' if isinstance(error, KeyboardInterrupt) else f'Waiting timed out after {timeout:g} seconds.'
        click.echo(message + ' Submitted jobs may still be queued or running.', err=True)
        raise click.exceptions.Exit(130 if isinstance(error, KeyboardInterrupt) else 124)
    finally:
        if inspection is not None:
            inspection.close()
