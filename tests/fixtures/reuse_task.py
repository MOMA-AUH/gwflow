"""A configurable ordinary task library for CLI reuse acceptance cases."""

from gwflow import Task


def text_task(*, boundary_extra=False, inner_extra=False, retain_middle=False,
              extra_output=False, side=False, retain_side=False, side_gate=False, reverse=False,
              version="1.0", cores=1, command_suffix="", prepare_name="prepare"):
    external = ["input.txt"] + (["extra.txt"] if boundary_extra else [])
    retained = ["result.txt"] + (["middle.txt"] if retain_middle else [])
    if retain_side:
        retained.append("side.txt")
    prepared = ["middle.txt"] + (["extra-output.txt"] if extra_output else [])
    consumed = ["input.txt"] + (["extra.txt"] if inner_extra else [])
    commands = [
        (prepare_name, consumed, prepared,
         "echo prepare log; echo prepare >> trace.txt; cat input.txt > middle.txt"
         + ("; touch extra-output.txt" if extra_output else "")
         + f" # {command_suffix}"),
        ("finish", ["middle.txt"], ["result.txt"],
         "echo finish >> trace.txt; tr '[:lower:]' '[:upper:]' < middle.txt > result.txt"),
    ]
    if side or side_gate:
        gate = "touch side_started; while [ ! -f release ]; do sleep 0.05; done; " if side_gate else ""
        commands.append(("side", [], ["side.txt"], gate + "echo side >> trace.txt; touch side.txt"))
    if reverse:
        external.reverse()
        retained.reverse()
        commands.reverse()
        for _, inputs, outputs, _ in commands:
            inputs.reverse()
            outputs.reverse()
    task = Task(inputs=external, outputs=retained)
    globals()["__version__"] = version
    for name, inputs, outputs, command in commands:
        task.target(name, inputs=inputs, outputs=outputs, cores=cores) << command
    return task
