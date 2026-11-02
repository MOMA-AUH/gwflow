"""Run managed local workers using gwf's settings and wire protocol."""

import asyncio
from pathlib import Path

import click
from gwf.core import pass_context
from gwf.plugins.workers import workers as gwf_workers

from ._local_workers import serve


@pass_context
def _workers(ctx, host, port, num_workers):
    # Local workers write logs beneath their own working directory, including
    # when submitted jobs belong to a workflow in a nested directory.
    Path(ctx.logs_dir).mkdir(parents=True, exist_ok=True)
    asyncio.run(serve(ctx.working_dir, num_workers, host, port))


workers = click.Command("workers", callback=_workers, params=list(gwf_workers.params),
                        help=gwf_workers.help)
