"""Wait for existing attempts without advancing their work."""

import math
import time

import click
from gwf.core import pass_context

from ._inspection import InspectionProcess


@click.command(name='wait')
@click.argument('selectors', nargs=-1)
@click.option('--interval', type=click.FloatRange(min=0, min_open=True), default=60, show_default=True,
              help='Seconds between inspections.')
@pass_context
def wait(ctx, selectors, interval):
    """Observe existing Tasks selected by exact names or quoted patterns.

    Include required upstream Tasks. Submit, retry, and cancel no work.
    Inspect immediately, then pause between checks. Messages use stderr;
    stdout stays empty. Exit 0 on completion, 1 on failure or required action,
    2 on invalid usage, or 130 on Ctrl+C. Jobs can continue after waiting stops.
    """
    if not math.isfinite(interval):
        raise click.BadParameter('must be a finite number greater than zero', param_hint='--interval')
    options = ['--file', f'{ctx.workflow_file}:{ctx.workflow_obj}', '--backend', ctx.backend]
    root = click.get_current_context().find_root()
    if root.get_parameter_source('verbose') == click.core.ParameterSource.COMMANDLINE:
        options.extend(('--verbose', root.params['verbose']))
    click.echo('Waiting for existing Task attempts.', err=True)
    inspection = None
    previous = {}
    awaited = None
    scheduler_errors = []
    previous_output = None
    try:
        while True:
            inspection = InspectionProcess((options, selectors), module='gwflow.wait_snapshot', command='wait-snapshot', request=awaited)
            while not inspection.completed():
                time.sleep(0.025)
            snapshot, failure, output = inspection.outcome()
            returncode = inspection.process.returncode
            inspection.close()
            inspection = None
            captured = output + (snapshot['inspection_diagnostics'] if snapshot is not None else '')
            if captured and captured != previous_output:
                click.echo(captured, err=True, nl=False)
            previous_output = captured
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
                awaited = {task['name']: task['attempt'] for task in snapshot['tasks']}
                selectors = tuple(awaited)
            for task in snapshot['tasks']:
                message = f"{task['name']}: {task['state']}; {task['reason']}"
                if previous.get(task['name']) != message:
                    click.echo(message, err=True)
                    previous[task['name']] = message
            if not errors and all(task['complete'] for task in snapshot['tasks']):
                click.echo('All awaited Task attempts are complete.', err=True)
                return
            if any(not (task['complete'] or task['pending']) for task in snapshot['tasks']):
                click.echo('Waiting cannot complete; another command is required. Submitted jobs may still be queued or running.', err=True)
                raise click.exceptions.Exit(1)
            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo('Waiting interrupted. Submitted jobs may still be queued or running.', err=True)
        raise click.exceptions.Exit(130)
    finally:
        if inspection is not None:
            inspection.close()
