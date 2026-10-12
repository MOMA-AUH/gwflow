"""Read one public-job or historical-submission log."""

import click
from gwf.core import pass_context
from gwf.plugins.logs import logs as gwf_logs

from ._frontend import load_workflow


@click.command()
@click.argument("target")
@click.option("-e", "--stderr", is_flag=True)
@click.option("--no-pager", is_flag=True)
@pass_context
def logs(ctx, target, stderr, no_pager):
    """Display an individual job's log using its exact name."""
    load_workflow(ctx)
    try:
        click.get_current_context().invoke(gwf_logs, target=target, stderr=stderr, no_pager=no_pager)
    except OSError as error:
        raise click.ClickException(str(error)) from error
