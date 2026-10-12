"""Run gwf's local workers with their required log directory prepared."""

from pathlib import Path

import click
from gwf.core import pass_context
from gwf.plugins.workers import workers as gwf_workers


@pass_context
def _workers(ctx, **options):
    # Local workers write logs beneath their own working directory, including
    # when submitted jobs belong to a workflow in a nested directory.
    Path(ctx.logs_dir).mkdir(parents=True, exist_ok=True)
    return click.get_current_context().invoke(gwf_workers, **options)


workers = click.Command("workers", callback=_workers, params=list(gwf_workers.params),
                        help=gwf_workers.help)
