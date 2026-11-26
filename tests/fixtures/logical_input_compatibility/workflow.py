"""The selected representation changes without editing the workflow between installations."""

from pathlib import Path

from gwflow import Task, Workflow, shell, task_template


@task_template
def sample():
    task = Task(inputs=["read_text.py"])
    compressed = Path(__file__).with_name("select-gzip").exists()
    text = task.logical_input("text", identity="greeting-v1",
                              files={"source": "input.txt.gz" if compressed else "input.txt"},
                              metadata={"encoding": "gzip" if compressed else "plain"})
    decode = task.target("decode", inputs=["read_text.py", text], outputs=["decoded"])
    decode << shell("python {reader} {descriptor} > decoded", reader="read_text.py", descriptor=text)
    checked = decode.output("decoded")
    join = task.target("join", inputs=["read_text.py", text, checked], outputs=["joined"])
    join << shell("python {reader} {descriptor} {checked} > joined",
                  reader="read_text.py", descriptor=text, checked=checked)
    task.retain("text", source=join.output("joined"), path="text.txt")
    return task


gwf = Workflow()
gwf.task(sample())
