"""Request cancellation of selected current work without changing its lifecycle."""

from contextlib import nullcontext
from dataclasses import dataclass

import click
from gwf.backends import BackendStatus, create_backend
from gwf.core import pass_context

from . import admission
from .activity import activity
from .diagnostics import report_error
from ._frontend import _submission_guard, load_workflow
from .lifecycle import Store, ordered_targets
from .presentation import Report, output_options
from .selection import task_selection
from .workflow import lifecycle_jobs


@dataclass
class CancellationOutcome:
    local: str
    state: str
    observation: admission.JobObservation | None = None
    reason: str = ""

    @property
    def failed(self):
        return self.state in ("unresolved", "failed")

    @property
    def diagnostic(self):
        return (self.failed or self.state == "unconfirmed" or
                (self.observation is not None and self.state != "not_admitted" and
                 self.observation.backend_state == BackendStatus.UNKNOWN))


@output_options
@click.command()
@click.argument("selectors", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, help="Preview cancellation requests without sending them.")
@pass_context
def cancel(ctx, selectors, dry_run, details, plain):
    """Request cancellation for explicit Task names or quoted Task-name globs.

    Use '*' to select all declared and recorded Tasks. Only their current
    attempts are addressed; dependent-job handling belongs to the backend.
    Without --dry-run, requests are sent without a confirmation prompt.
    Requests return without waiting for jobs to stop. A later ordinary run
    can retry eligible work once existing inactivity checks permit it.

    Outcomes are grouped by Task. --details includes every per-job receipt
    and full available identities from this invocation. Failures and uncertain
    observations retain their diagnostic identities in the default report.
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
            outcomes_by_task: dict[str, list[CancellationOutcome]] = {}
            interrupted = False
            try:
                with activity("Waiting for scheduler..."), connection as backend:
                    for name in sorted(selected):
                        attempt = attempts.get(name)
                        outcomes = outcomes_by_task[name] = []
                        if attempt is not None:
                            outcomes.extend(cancel_jobs(store, attempt, backend, ctx.backend, dry_run))
                        errors += sum(item.failed for item in outcomes)
            except BaseException:
                interrupted = True
                raise
            finally:
                report = Report(store, plain=plain)
                for name, outcomes in outcomes_by_task.items():
                    attempt = attempts.get(name)
                    removed = "; no longer declared" if name not in workflow._task_declarations else ""
                    report.cancellations(name, attempt, outcomes, removed=removed,
                                         details=details or interrupted,
                                         interrupted=interrupted and attempt is not None)
    except OSError as error:
        raise click.ClickException(str(error)) from error
    if errors:
        raise click.ClickException(f"Cancellation incomplete: {errors} unresolved jobs or failed requests.")


def cancel_jobs(store, attempt, backend, backend_name, dry_run):
    """Yield each observed or requested outcome without changing Task records."""
    name = attempt["task"]
    for local in lifecycle_jobs(ordered_targets(attempt["structure"])):
        try:
            item = admission.observe_cancellation(store, attempt, local, backend, backend_name)
        except Exception as error:
            report_error(error, f"Observing {name}__{local}")
            yield CancellationOutcome(local, "unresolved",
                                      admission.cancellation_diagnostic_identity(store, attempt, local), str(error))
            continue
        if item.state in ("complete", "failed", "cancelled", "pending"):
            if item.backend_state in (BackendStatus.COMPLETED, BackendStatus.FAILED, BackendStatus.CANCELLED):
                state = "inactive"
            elif item.state == "complete":
                state = "recorded_success"
            elif item.state == "failed":
                state = "recorded_failure"
            else:
                state = "not_admitted"
            yield CancellationOutcome(local, state, item)
            continue
        try:
            admission.require_cancellation_identity(backend, backend_name, item)
        except Exception as error:
            report_error(error, f"Identifying {name}__{local}")
            yield CancellationOutcome(local, "unresolved", item, str(error))
            continue
        if dry_run:
            yield CancellationOutcome(local, "preview", item)
            continue
        outcome = CancellationOutcome(local, "unconfirmed", item)
        yield outcome
        try:
            admission.request_cancellation(backend, backend_name, item)
        except Exception as error:
            report_error(error, f"Canceling {name}__{local}")
            outcome.state, outcome.reason = "failed", str(error)
        else:
            outcome.state = "requested"
