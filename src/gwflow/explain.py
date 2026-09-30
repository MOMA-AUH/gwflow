"""Human-readable ordinary whole-workflow planning through gwf's CLI."""

import click

from gwf import Workflow as GwfWorkflow
from gwf.backends import BackendStatus
from gwf.core import pass_context
from gwf.exceptions import WorkflowError

from ._state import state_name
from ._frontend import _submission_guard
from .planning import plan_workflow
from .workflow import Workflow, _bookkeeping_name


def _forced_treatment(state):
    return ("forced resubmission" if state in (
        BackendStatus.COMPLETED, BackendStatus.SUBMITTED, BackendStatus.RUNNING
    ) else "forced submission")


def _target_details(plan, names, reused, completion_name, *, force=False):
    lines = []
    for name in names:
        state = plan.observed[name]
        if reused:
            treatment = "omitted by Reuse"
        elif force and name in plan.submissions:
            treatment = _forced_treatment(state)
        elif name in plan.submissions:
            treatment = "retry" if state in (BackendStatus.FAILED, BackendStatus.CANCELLED) else "submit"
        elif state in (BackendStatus.SUBMITTED, BackendStatus.RUNNING):
            treatment = "active work left alone"
        else:
            treatment = "up to date; no submission"
        role = "bookkeeping Completion job" if name == completion_name else "target"
        lines.append(f"    {role} {name}: Current: backend {state_name(state)}; Planned: {treatment}.")
    return lines


def _overview(plan, *, details=False, force=False, task_name=None):
    lines = ["Forced whole-workflow plan" if force else "Ordinary whole-workflow plan"]
    if task_name is not None:
        lines[0] += f" (displaying Task {task_name})"
    if not details:
        lines.append("Summary reasons; additional causes may apply. Use --details for evidence, "
                     "upstream chains, and target treatment.")
    owned = set()
    for name, (_, inner, _) in plan.tasks.items():
        if task_name is not None and name != task_name:
            continue
        completion_name = _bookkeeping_name(name)
        names = {target.name for target in inner} | {completion_name}
        owned.update(names)
        current = ("Reuse bypassed by force" if force else
                   "reusable" if name in plan.reused else "not reusable")
        states = sorted({
            state_name(plan.observed[target]) for target in names
            if plan.observed[target] not in (BackendStatus.COMPLETED, BackendStatus.UNKNOWN)
        })
        if states:
            current += "; " + ", ".join(states)
        pending = sorted(names & plan.submissions)
        if not pending:
            action = "reuse; no work" if name in plan.reused else "no new submissions"
        elif pending == [completion_name]:
            action = "completion-only repair"
        else:
            action = "submit target work"
        lines.extend([
            f"Task {name}",
            f"  Current: {current}. Reason: {plan.reasons[name]}.",
            f"  Planned: {action}.",
        ])
        lines.extend(f"    Would submit {target}" for target in pending)
        lines.extend(f"  Recovery: {note}." for note in plan.recovery[name])
        if details:
            lines.append("  Direct evidence:")
            lines.extend(f"    {reason}." for reason in plan.evidence[name])
            if plan.upstream_chains[name]:
                lines.append("  Upstream cause chains:")
                lines.extend(f"    {chain}." for chain in plan.upstream_chains[name])
            lines.extend(_target_details(
                plan, [*(target.name for target in inner), completion_name],
                name in plan.reused, completion_name, force=force,
            ))
    for target in plan.targets if task_name is None else ():
        if target.name in owned:
            continue
        state = plan.observed[target.name]
        will_submit = target.name in plan.submissions
        action = (_forced_treatment(state) if force and will_submit else
                  "submit" if will_submit else "no new submissions")
        lines.extend([
            f"Ordinary target {target.name}",
            f"  Current: backend {state_name(state)}.",
            f"  Planned: {action}. "
            f"Reason: {plan.target_reasons[target.name]}.",
        ])
        if will_submit:
            lines.append(f"    Would submit {target.name}")
    if task_name is None and not plan.tasks and not plan.targets:
        lines.append("Empty workflow; no work.")
    return "\n".join(lines)


@click.command()
@click.option("--details", is_flag=True, help="Show every Task target and direct Reuse evidence.")
@click.option("--force", is_flag=True, help="Preview a forced whole-workflow run.")
@click.argument("task_name", required=False)
@pass_context
def explain(ctx, details, force, task_name):
    """Explain a whole-workflow run without submitting jobs."""
    workflow = GwfWorkflow.from_context(ctx)
    if not isinstance(workflow, Workflow):
        raise WorkflowError("gwf explain requires a gwflow.Workflow; plain gwf.Workflow is unsupported")
    with _submission_guard(
        ctx.working_dir,
        waiting_message="Waiting for another invocation to finish submission bookkeeping...",
    ):
        workflow._validate_task_boundaries()
        plan = plan_workflow(workflow, list(dict.values(workflow.targets)), ctx,
                             force=force, details=details)
        if task_name is not None and task_name not in plan.tasks:
            raise WorkflowError(f"Unknown Task name {task_name!r}")
        output = _overview(plan, details=details, force=force, task_name=task_name)
    click.echo(output)
