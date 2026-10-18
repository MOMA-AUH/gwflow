"""Request cancellation of selected current work without changing its lifecycle."""

from contextlib import nullcontext

import click
from gwf.backends import BackendStatus, create_backend
from gwf.core import pass_context

from . import admission
from .activity import activity
from .diagnostics import report_error
from ._frontend import _submission_guard, load_workflow
from .lifecycle import Store, ordered_targets
from .selection import task_selection
from .workflow import lifecycle_jobs


@click.command()
@click.argument("selectors", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, help="Preview cancellation requests without sending them.")
@pass_context
def cancel(ctx, selectors, dry_run):
    """Request cancellation for explicit Task names or quoted Task-name globs.

    Use '*' to select all declared and recorded Tasks. Only their current
    attempts are addressed; dependent-job handling belongs to the backend.
    Without --dry-run, requests are sent without a confirmation prompt.
    Requests return without waiting for jobs to stop. A later ordinary run
    can retry eligible work once existing inactivity checks permit it.
    """
    workflow = load_workflow(ctx)
    errors = 0
    try:
        with _submission_guard(ctx.working_dir):
            store = Store.for_workflow(workflow)
            attempts = {attempt["task"]: attempt for attempt in store.recorded_tasks()}
            selected = task_selection(set(workflow._task_declarations) | set(attempts), selectors)
            connection = (create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config)
                          if selected & attempts.keys() else nullcontext())
            with activity("Waiting for scheduler..."), connection as backend:
                for name in sorted(selected):
                    removed = "; no longer declared" if name not in workflow._task_declarations else ""
                    attempt = attempts.get(name)
                    if attempt is None:
                        click.echo(f"Task {name}: no admitted work to cancel{removed}")
                        continue
                    click.echo(f"Task {name}: current attempt {attempt['attempt']}{removed}")
                    has_requestable_jobs = False
                    task_errors = False
                    for local in lifecycle_jobs(ordered_targets(attempt["structure"])):
                        try:
                            item = admission.observe_cancellation(store, attempt, local, backend, ctx.backend)
                        except Exception as error:
                            report_error(error, f"Observing {name}__{local}")
                            click.echo(f"  {name}__{local}: Unresolved job identity: {error}; no request")
                            errors += 1
                            task_errors = True
                            continue
                        label = f"{name}__{local}: submission {item.submission or 'not submitted'}"
                        if item.state in ("complete", "failed", "cancelled", "pending"):
                            if item.backend_state in (BackendStatus.COMPLETED, BackendStatus.FAILED, BackendStatus.CANCELLED):
                                reason = f"backend confirms inactivity ({item.backend_state.name.lower()})"
                            elif item.state == "complete":
                                reason = "completed work preserved; live state unavailable"
                            elif item.state == "failed":
                                reason = "recorded failure; live state unavailable"
                            else:
                                reason = "not admitted"
                            click.echo(f"  {label}; {reason}; no request")
                        else:
                            try:
                                admission.require_cancellation_identity(backend, ctx.backend, item)
                            except Exception as error:
                                report_error(error, f"Identifying {name}__{local}")
                                click.echo(f"  {label}; Unresolved job identity: {error}; no request")
                                errors += 1
                                task_errors = True
                                continue
                            has_requestable_jobs = True
                            if dry_run:
                                click.echo(f"  {label}; Would request cancellation (backend job {item.job_id})")
                                continue
                            try:
                                admission.request_cancellation(backend, ctx.backend, item)
                            except Exception as error:
                                report_error(error, f"Canceling {name}__{local}")
                                click.echo(f"  {label}; Cancellation request failed: {error}")
                                errors += 1
                            else:
                                click.echo(f"  {label}; Cancellation requested; job may still be stopping (backend job {item.job_id})")
                    if not has_requestable_jobs:
                        click.echo("  No cancellation requests sent" if task_errors else "  No admitted work to cancel")
    except OSError as error:
        raise click.ClickException(str(error)) from error
    if errors:
        raise click.ClickException(f"Cancellation incomplete: {errors} unresolved jobs or failed requests.")
