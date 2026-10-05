# gwflow

gwflow extends gwf with tasks that can be reused after their internal intermediate files have been removed.

## Language

**Task**:
A higher-level unit containing multiple ordinary gwf targets, with a defined boundary of external inputs and retained outputs.

**Task name**:
An author-supplied name identifying one task instance, unique within its workflow and stable across runs. Names may be generated in Python and do not depend on declaration order.

**Task implementation**:
A reusable definition of the work used to construct task instances.

**Task version**:
A release identifier for a task implementation, independent of the main pipeline's release. It is distinct from the name of an individual task instance.

**Pipeline version**:
A release of the orchestration that selects task implementations and connects their instances.

**Local target name**:
A name identifying one target within its owning task. Different task instances may use the same local target names.

**Computation target**:
An author-declared gwf target within a Task that performs part of its computation.

**Lifecycle job**:
A job introduced by gwflow to prepare a Task for computation or bring it to Completion. Preparation and completion jobs are distinct from the Task's computation targets.

**Task progress**:
The count of completed computation targets and lifecycle jobs, relative to their total, after excluding steps known to require repetition; a required fresh attempt starts at zero, while retries and Repair retain valid completed steps. It measures completed steps rather than time, effort, or eligibility for Reuse; the count is unknown when completion cannot be established.

**Task state**:
The primary condition of a Task, summarizing its lifecycle, blockers, and job observations. Blockage, failure, and cancellation take precedence in that order over ongoing work.

**Next action**:
The decision about how a Task will be handled when the workflow is run, together with its reason. It describes what is planned rather than the Task's current state.

**External input**:
A file supplied from outside a task, declared as data by its author or implied by a target's image selection. This includes another task's retained output.

**Input baseline**:
The external-input state accepted at task preparation, against which later input observations are compared.

**Image dependency**:
An external input selected as a target's container image, required for meaningful workflow inspection as well as container execution, even when its owning task can otherwise be reused.

**Image reference**:
A declaration identifying an image dependency by a local SIF pathname or an OCI/Docker registry source.

**Image cache**:
A collection of acquired image dependencies shared across one user's workflows.

**Retained output**:
A file declared by the task author as an output to keep after successful task completion. Files passed from one task to another must be retained outputs of their producing task.

**Internal intermediate**:
A file used within a task that need not be retained after successful completion and is not available as a dependency of another task. Its absence alone must not invalidate an otherwise reusable task.

**Reuse**:
Accepting a completed task without rerunning its targets while its external inputs and retained outputs remain valid. Completion carries the same guarantees and limitations as gwf target completion.

**Repair**:
Restoring missing or changed retained outputs from a Task's valid existing work without rerunning its computation.

**Deferral**:
Postponing further handling of a Task until upstream retained outputs have been recovered and its external-input state can be reassessed. Proceeding requires a later workflow invocation.

**Completion**:
The task being considered finished under gwf's completion semantics, including their limitations. This does not imply stronger proof of successful execution or output correctness.

**Completion record**:
Bookkeeping that a task reached completion under gwf's semantics, retained so later reuse can ignore removed internal intermediates. It does not independently certify process success or output correctness.

**File freshness**:
The existence-and-modification-time relationship between a target's inputs and outputs under gwf's rules. Freshness alone does not establish successful execution or a managed task's eligibility for Reuse.
