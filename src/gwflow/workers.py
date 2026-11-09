"""Run managed local workers using gwf's settings and wire protocol."""

import asyncio

import click
from gwf.core import pass_context
from gwf.plugins.workers import workers as gwf_workers

from ._local_workers import serve


@pass_context
def _workers(ctx, max_cores, max_memory):
    asyncio.run(serve(ctx.working_dir, max_cores, max_memory))


workers = click.Command("workers", callback=_workers, params=list(gwf_workers.params),
                        help=gwf_workers.help)
