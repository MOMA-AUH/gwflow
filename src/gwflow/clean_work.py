"""Preview or delete owned work under the shared frontend guard."""

import click
from gwf.backends import create_backend
from gwf.core import pass_context
from gwf.exceptions import WorkflowError

from . import cleanup
from .activity import activity
from ._frontend import _submission_guard, load_workflow
from .lifecycle import Store, declarations
from .presentation import Report
from .selection import task_selection


@click.command("clean")
@click.argument("selectors", nargs=-1)
@click.option("--delete", is_flag=True, help="Delete eligible owned work without an interactive prompt.")
@click.option("--attempt", "attempt_ids", multiple=True, help="Select exact recorded inactive attempts, giving up retry progress and repair sources.")
@click.option("--plain", is_flag=True, help="Use undecorated output.")
@pass_context
def clean_work(ctx, selectors, delete, attempt_ids, plain):
    """Preview work for Task names or quoted globs; omit selectors for all Tasks.

    Reports group attempts by Task and keep complete identities and paths.
    --delete reports actual removals, kept work, and any incomplete removal.
    Retained results and logs are preserved.
    """
    if selectors and attempt_ids:
        raise WorkflowError("Cannot combine Task selectors and --attempt")
    workflow = load_workflow(ctx)
    with _submission_guard(ctx.working_dir):
        store = Store.for_workflow(workflow)
        for name, declaration in workflow._task_declarations.items():
            declarations(declaration, store, workflow)
            store.current(name, workflow._result_dirs[name])
        attempts = store.recorded_attempts()
        known = set(workflow._task_declarations) | {attempt["task"] for attempt in attempts}
        selected_names = task_selection(known, selectors) if not attempt_ids else set()
        unknown = set(attempt_ids) - {attempt["attempt"] for attempt in attempts}
        if unknown:
            raise WorkflowError("Unknown attempts for --attempt: " + ", ".join(sorted(unknown)))
        selected = [attempt for attempt in attempts if attempt["attempt"] in attempt_ids] if attempt_ids else [
            attempt for attempt in attempts if attempt["task"] in selected_names]
        observations = [cleanup.CleanupObservation(attempt, "unassessed", "inspection did not complete",
                                                   explicit=bool(attempt_ids)) for attempt in selected]
        interrupted = False
        try:
            with activity("Waiting for scheduler..."), create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
                for index, attempt in enumerate(selected):
                    observations[index] = cleanup.observe(store, attempt, backend, ctx.backend, explicit=bool(attempt_ids))
            if delete and any(item.action == "blocked" for item in observations):
                raise WorkflowError("Cleanup refused for ownership or activity")
            if delete:
                with activity("Rechecking scheduler before deletion..."), create_backend(ctx.backend, working_dir=ctx.working_dir, config=ctx.config) as backend:
                    for item in observations:
                        if item.action == "eligible":
                            cleanup.remove(store, item, backend, ctx.backend)
            if delete and any(item.action == "blocked" for item in observations):
                raise WorkflowError("Cleanup refused after rechecking ownership or activity")
        except BaseException:
            interrupted = True
            raise
        finally:
            if delete and interrupted:
                for item in observations:
                    if item.action == "eligible":
                        item.action, item.reason = "not_processed", "eligible work was not processed"
            Report(store, plain=plain).cleanup(observations, selected_names, delete=delete, interrupted=interrupted)
