"""Human-readable ordinary whole-workflow planning through gwf's CLI."""

import click

from gwf import Workflow as GwfWorkflow
from gwf.backends import BackendStatus
from gwf.core import pass_context
from gwf.exceptions import WorkflowError

from .reuse import _submission_guard, plan_workflow
from .workflow import Workflow, _bookkeeping_name


def _target_details(plan, names, reused, completion_name):
    lines = []
    for name in names:
        state = plan.observed[name]
        if reused:
            treatment = "omitted by Reuse"
        elif name in plan.submissions:
            treatment = "retry" if state in (BackendStatus.FAILED, BackendStatus.CANCELLED) else "submit"
        elif state in (BackendStatus.SUBMITTED, BackendStatus.RUNNING):
            treatment = "active work left alone"
        else:
            treatment = "up to date; no submission"
        role = "bookkeeping Completion job" if name == completion_name else "target"
        lines.append(f"    {role} {name}: Current: backend {state.name.lower()}; Planned: {treatment}.")
    return lines


def _overview(plan, *, details=False):
    lines = ["Ordinary whole-workflow plan"]
    if not details:
        lines.append("Summary reasons; additional causes may apply. Use --details for evidence, "
                     "upstream chains, and target treatment.")
    owned = set()
    for name, (_, inner, _) in plan.tasks.items():
        completion_name = _bookkeeping_name(name)
        names = {target.name for target in inner} | {completion_name}
        owned.update(names)
        current = "reusable" if name in plan.reused else "not reusable"
        states = sorted({
            plan.observed[target].name.lower() for target in names
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
                name in plan.reused, completion_name,
            ))
    for target in plan.targets:
        if target.name in owned:
            continue
        state = plan.observed[target.name]
        will_submit = target.name in plan.submissions
        lines.extend([
            f"Ordinary target {target.name}",
            f"  Current: backend {state.name.lower()}.",
            f"  Planned: {'submit' if will_submit else 'no new submissions'}. "
            f"Reason: {plan.target_reasons[target.name]}.",
        ])
        if will_submit:
            lines.append(f"    Would submit {target.name}")
    if not plan.tasks and not plan.targets:
        lines.append("Empty workflow; no work.")
    return "\n".join(lines)


@click.command()
@click.option("--details", is_flag=True, help="Show every Task target and direct Reuse evidence.")
@pass_context
def explain(ctx, details):
    """Explain an ordinary whole-workflow run without submitting jobs."""
    workflow = GwfWorkflow.from_context(ctx)
    if not isinstance(workflow, Workflow):
        raise WorkflowError("gwf explain requires a gwflow.Workflow; plain gwf.Workflow is unsupported")
    with _submission_guard(ctx.working_dir):
        workflow._validate_task_boundaries()
        plan = plan_workflow(workflow, list(dict.values(workflow.targets)), ctx, details=details)
        output = _overview(plan, details=details)
    click.echo(output)
