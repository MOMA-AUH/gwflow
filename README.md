# gwflow

gwflow adds managed Tasks to [gwf](https://gwf.app/): computation runs in disposable
work storage, while named retained files are copied into stable results storage.
Checked completion evidence lets a Task remain reusable after its work is removed.

The development branch is migrating to v0.3.0. The current managed lifecycle
supports input-free, single-target Tasks on one filesystem. External inputs,
Task graphs, retries, repair, force, and managed cleanup are being added in
[the implementation queue](https://github.com/MOMA-AUH/gwflow/issues/62).
Existing v0.2 factories and records are not converted or adopted. The older
examples will be migrated with the complete workflow demonstration.

Install with Python 3.12 and gwf 2.1.1:

```sh
python -m pip install .
```

Create `workflow.py`:

```python
from gwflow import Task, Workflow, shell

gwf = Workflow()
task = Task(inputs=[])
write = task.target("write", inputs=[], outputs=["message.txt", "scratch.txt"])
write << shell(
    "printf 'hello\\n' > {message}; printf temporary > scratch.txt",
    message=write.output("message.txt"),
)
task.retain("message", source=write.output("message.txt"), path="message.txt")
hello = gwf.task_from_template("hello", task)
# hello.outputs["message"] is the named retained-output reference.
```

Run and inspect through gwf:

```sh
gwf explain --details
gwf run --dry-run
gwf run
gwf status --details
gwf logs hello__write --no-pager
```

Use the normal gwf backend configuration and local workers or cluster backend.
Submission returns without waiting for computation. Run all frontend commands
for a workflow on one physical frontend; a guard serializes submission and
inspection through backend tracking persistence.

A target runs in private staging under `work/`, with a private writable `TMPDIR`
by default. `Workflow(managed_tmpdir=False)` preserves environment-selected
`TMPDIR`. Plain command strings are literal shell text; `shell()` uses named
file placeholders, quotes each substituted path as one shell argument, and uses
`{{`/`}}` for literal braces. Do not quote the placeholders yourself. Fixed-name
outputs can be declared without being bound into the command.

A zero command exit and every declared output being a regular file are required
before the output set commits. Finishing copies retained files into private
staging and installs the complete set in `results/hello/` before recording
Completion. Retained files are independent copies; only declared retained files
appear in results. Scratch, work outputs, and execution IDs remain in work or
bookkeeping. A Task may retain no files but still requires checked computation.

Results and bookkeeping must survive work removal. An unchanged completed Task
can be reused without its work. Incomplete attempts, damaged results, changed
tracked computation, and uncertain submissions currently fail explicitly when
they require a recovery operation that is not yet available. Generic `gwf clean`
and `gwf touch` are rejected for managed workflows. Ordinary `gwf.Workflow`
commands keep their usual behavior.

Structure is always tracked. Commands follow gwf's inherited `use_spec_hashes`
setting (default false); enabling it is recommended. File size and modification
time checks do not detect changes preserving both, and successful execution does
not certify output contents. Resources, packages, hidden parameters, and the
software environment have no independent invalidation component.

Work, results, and bookkeeping roots must be disjoint. Configured storage must
be visible to jobs and support coherent record visibility and atomic directory
rename on the relevant filesystem. Existing unowned destinations, changed
recorded locations, malformed records, and symlink traversal are rejected.
Records are atomically published with file and directory synchronization where
supported; these guarantees are not universal power-loss certification.

Run the installed-package tests with:

```sh
python -m unittest discover -s tests -v
python tests/release_smoke.py
```
