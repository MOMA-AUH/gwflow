"""Small-file Mapping/Somatic tasks with observable work and explicit gates."""

from gwflow import Task


def mapping(sample, *, gated=False, command_suffix=""):
    name = f"Mapping_{sample}"
    task = Task(inputs=["input.txt"], outputs=[f"{name}.txt"])
    task.target("prepare", inputs=["input.txt"], outputs=[f"{name}.tmp"]) << (
        f"echo {name}:prepare >> trace.txt; cp input.txt {name}.tmp"
    )
    task.target("retain", inputs=[f"{name}.tmp"], outputs=[f"{name}.txt"]) << (
        f"echo {name}:retain >> trace.txt; cp {name}.tmp {name}.txt"
    )
    gate = (
        f"touch {name}.started; "
        f"while [ ! -f {name}.release ]; do sleep 0.05; done; "
        if gated else ""
    )
    task.target("tail", inputs=[f"{name}.txt"], outputs=[f"{name}.tail"]) << (
        gate + f"echo {name}:tail >> trace.txt; touch {name}.tail # {command_suffix}"
    )
    return task


def somatic(sample):
    name = f"Somatic_N_{sample}"
    inputs = ["Mapping_N.txt", f"Mapping_{sample}.txt"]
    task = Task(inputs=inputs, outputs=[f"{name}.txt"])
    # This root does not consume retained files itself. Whole-task ordering
    # still requires it to wait for both Mapping tasks' trailing work.
    task.target("start", inputs=[], outputs=[f"{name}.tmp"]) << (
        "grep -qx Mapping_N:tail trace.txt && "
        f"grep -qx Mapping_{sample}:tail trace.txt && "
        f"echo {name}:start >> trace.txt && touch {name}.tmp"
    )
    task.target("finish", inputs=[*inputs, f"{name}.tmp"], outputs=[f"{name}.txt"]) << (
        f"echo {name}:finish >> trace.txt; cat {' '.join(inputs)} > {name}.txt"
    )
    return task


def report():
    task = Task(inputs=["Somatic_N_T1.txt"], outputs=["report.txt"])
    task.target("make", inputs=["Somatic_N_T1.txt"], outputs=["report.txt"]) << (
        "echo report >> trace.txt; cp Somatic_N_T1.txt report.txt"
    )
    return task
