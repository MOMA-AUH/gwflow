# gwflow

gwflow extends gwf with tasks that can be reused after their internal intermediate files have been removed.

## Language

**Task**:
A higher-level unit containing multiple ordinary gwf targets, with a defined boundary of external inputs and retained outputs.

**Task attempt**:
A distinct instance of a Task's lifecycle, encompassing preparation, computation, and Completion. Retries and Repair can preserve the same attempt identity.

**Execution scope**:
The set of whole Tasks considered for execution by a run, including selected Tasks and their required upstream dependencies.

**Task selector**:
A Task name or Task-name pattern used to choose whole Tasks for a command.

**Selected Tasks**:
The Tasks directly chosen for a command, before any required upstream dependencies are included.

**Task name**:
A stable name identifying one Task instance, unique within its workflow, distinct from Public job names and Submission names, and independent of declaration order. It uses the Task alias when supplied, or otherwise the Task implementation's name, with an Instance key appended when supplied.

**Instance key**:
An optional author-supplied value distinguishing Task instances under the same name prefix, stable across runs and independent of declaration order.

**Task implementation**:
A reusable definition of the work used to construct Task instances. Distinct implementations remain distinct even when they share a short name.

**Task alias**:
An author-supplied name for a particular use of a Task implementation, used as its Task-name prefix and status-group label. Different aliases do not create different Task implementations.

**Task version**:
A release identifier for a task implementation, independent of the main pipeline's release. It is distinct from the name of an individual task instance.

**Pipeline version**:
A release of the orchestration that selects task implementations and connects their instances.

**Local target name**:
A name identifying one target within its owning task. Different task instances may use the same local target names.

**Public job name**:
A workflow-wide name identifying one computation target or lifecycle job, independent of the particular Task attempt or retry.

**Submission name**:
A name identifying one specific submission of a computation target or lifecycle job, including when its logs are accessed historically.

**Computation target**:
An author-declared gwf target within a Task that performs part of its computation.

**Lifecycle job**:
A job introduced by gwflow to prepare a Task for computation or bring it to Completion. Preparation and completion jobs are distinct from the Task's computation targets.

**Cancellation**:
A request to stop a Task's admitted computation and lifecycle jobs, preserving valid completed work and retained outputs without imposing a persistent pause. The request alone does not establish job inactivity.

**Task progress**:
The count of completed computation targets and lifecycle jobs, relative to their total, after excluding steps known to require repetition; a required fresh attempt starts at zero, while retries and Repair retain valid completed steps. It measures completed steps rather than time, effort, or eligibility for Reuse; the count is unknown when completion cannot be established.

**Task state**:
The primary condition of a Task, summarizing its lifecycle, blockers, and job observations. Blockage, failure, and cancellation take precedence in that order over ongoing work.

**Next action**:
The decision about how a Task will be handled within an Execution scope, together with its reason. It describes what is planned rather than the Task's current state.

**External input**:
A file supplied from outside a task, declared as data by its author or implied by a target's image selection. This includes another task's retained output.

**Input baseline**:
The external-input state accepted at task preparation, against which later input observations are compared.

**Image dependency**:
An external input selected as a target's container image, required for meaningful inspection of its owning Task as well as container execution, even when that Task can otherwise be reused.

**Image reference**:
A declaration identifying an image dependency by a local SIF pathname or an OCI/Docker registry source.

**Image cache**:
A collection of acquired image dependencies shared across one user's workflows.

**Retained output**:
A file declared by the task author as an output to keep after successful task completion. Files passed from one task to another must be retained outputs of their producing task.

**Internal intermediate**:
A file used within a task that need not be retained after successful completion and is not available as a dependency of another task. Its absence alone must not invalidate an otherwise reusable task.

**Reuse**:
Accepting a completed Task without repeating computation while its tracked definition, inputs, producer attempts, and retained outputs remain valid. Reuse survives removal of internal intermediates.

**Repair**:
Restoring missing or changed retained outputs from a Task's valid existing work without rerunning its computation.

**Deferral**:
Postponing further handling of a Task until upstream retained outputs have been recovered and its external-input state can be reassessed. Proceeding requires a later workflow invocation.

**Completion**:
The checked finished state of a Task attempt, established by successful computation and installation of its complete retained-output set for the accepted inputs. It does not certify scientific output correctness.

**Completion record**:
Durable evidence of a Task attempt's checked Completion, retained so later Reuse can ignore removed internal intermediates. It does not certify scientific output correctness.

**File freshness**:
The existence-and-modification-time relationship between a target's inputs and outputs under gwf's rules. Freshness alone does not establish successful execution or a managed task's eligibility for Reuse.
