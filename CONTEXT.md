# gwflow

gwflow extends gwf with tasks that can be reused after their internal intermediate files have been removed.

## Language

**Task**:
A higher-level unit containing multiple ordinary gwf targets, with explicitly declared external inputs and retained outputs.

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

**Completion**:
The task being considered finished under gwf's completion semantics, including their limitations. This does not imply stronger proof of successful execution or output correctness.

**Completion record**:
Bookkeeping that a task reached completion under gwf's semantics, retained so later reuse can ignore removed internal intermediates. It does not independently certify process success or output correctness.

**File freshness**:
The task-boundary file check using the same existence and modification-time rules as gwf targets, applied to external inputs and retained outputs. Freshness alone does not establish successful completion.
