from pathlib import Path

from gwflow import Task, Workflow, shell, task_template


@task_template
def document():
    representation = Path(__file__).with_name("representation.txt").read_text().strip()
    if representation == "files":
        files = {"heading": "parts/heading/text.txt", "body": "parts/body/text.txt"}
    elif representation == "zip":
        files = {"archive": "document.zip"}
    else:
        raise ValueError("Set representation.txt to 'files' or 'zip'")

    task = Task(inputs=["materialize.py"])
    source = task.logical_input("source", identity={"document": "meeting-notes", "revision": 1},
                                files=files, metadata={"format": representation})
    materialize = task.target("materialize", image="python.sif", inputs=[source, "materialize.py"],
                              outputs=["heading.txt", "body.txt"], stage_as={"inputs/source.json": source})
    materialize << shell("python {script} {descriptor}", script="materialize.py", descriptor=source)

    heading, body = materialize.output("heading.txt"), materialize.output("body.txt")
    join = task.target("join", image="python.sif", inputs=[heading, body], outputs=["report.txt"])
    join << shell("cat {heading} {body} > report.txt", heading=heading, body=body)
    task.retain("report", source=join.output("report.txt"), path="report.txt")
    return task


gwf = Workflow()
gwf.task(document())
