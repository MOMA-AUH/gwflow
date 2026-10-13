# Give managed workflows a dedicated command line

Starting with v0.6.0, managed workflows use the `gwflow` executable, whose workflow operations require a `gwflow.Workflow`; ordinary `gwf.Workflow` workflows remain under `gwf`. gwflow no longer registers command overrides with gwf, while continuing to use its scheduling and backend services. This gives managed Tasks a command interface suited to their lifecycle without exposing unsupported ordinary-target operations or maintaining ordinary-workflow behavior in the managed CLI.

Ordinary `gwf` workflow commands reject a managed `gwflow.Workflow` with a clear message directing the user to `gwflow`, before scheduling or modifying workflow state. Removing command overrides alone would expose managed Tasks' placeholder targets to ordinary execution, so the executable boundary must be enforced explicitly.

Workflow commands report a clear error when no workflow file is found, with guidance on selecting a file. Startup does not offer interactive workflow creation: the inherited initializer creates an ordinary `gwf.Workflow`, which is unsuitable for the managed CLI. Help and version information remain available without a workflow file.

Workflow discovery retains the `workflow.py:gwf` default and `--file PATH[:OBJECT]` override, with `gwf` as the default object name when omitted. Relative file paths are searched from the current directory through its parents. The Python object name is independent of the executable name, preserving a single familiar discovery convention.

The v0.6.0 command set was `run`, `status`, `logs`, `clean`, `config`, and `workers`. Managed work cleanup was renamed from `clean-work` to `clean`, retaining its managed cleanup semantics and protection of retained results. The inherited `info`, `touch`, and `cancel` commands were omitted; cancellation remained a backend operation for that release, pending a separate managed cancellation design.

Starting with v0.7.0, the managed `cancel` command follows the contract recorded in
[ADR 0009](0009-cancel-admitted-work-without-pausing-tasks.md).

The remaining backend and configuration infrastructure stays unchanged: backend selection and discovery, settings, verbosity, and color controls continue to serve managed workflows. Logs retain their individual job-name selection and existing options, as do configuration and local workers. The `.gwfconf.json` and `.gwf/` locations, configured work/results directories, and image-cache location are preserved; changing the executable does not require changing storage conventions. `gwflow --version` reports the gwflow release.

For v0.7.0, the Task-level troubleshooting extension presents failed computation and lifecycle jobs, their Task attempts and logs, and the planner's Next action with its reason. It brings together observed evidence and existing recovery guidance; automatic interpretation of arbitrary tool logs to diagnose causes or suggest fixes is deferred.

Task-level log access extends the existing command: `gwflow logs TASK` shows a job/log index for the Task's current attempt, with failures first. The index identifies the attempt and each job's state and available log streams, provides commands to read individual logs, and points to `run TASK --dry-run --details` for recovery guidance. Individual-job selection continues to display log contents. This keeps parallel jobs' output distinguishable while making their logs discoverable by Task name.

The default log index shows current job submissions within the current Task attempt. `--history` also exposes earlier retry submissions and previous Task attempts, explicitly marked as historical so resolved failures do not appear to be current problems. Task attempt identity alone does not distinguish retries, since retries can preserve that identity.

Task log indexes accept multiple exact Task names and quoted glob patterns, with results grouped by Task. Reading individual-job log contents continues to require one exact job name, keeping selection of multiple Tasks an index operation.

The `-e`/`--stderr` option selects stderr only when reading an individual job's log contents. Task indexes always show availability of both stdout and stderr. Using `--stderr` in Task-index mode is a usage error with guidance to select an individual job.

Task log indexes remain available when the scheduler cannot supply current job states: recorded jobs and available logs are still listed, with unavailable live state marked explicitly. Reading recorded logs does not require image acquisition or execution planning. Recovery explanations remain in the separate run preview, and unavailable job state never implies inactivity.

With no selectors, `gwflow logs` shows all Tasks' current log indexes, consistent with the read-only overview provided by bare `status`. Earlier submissions and attempts appear in Task indexes only with `--history`; an exact historical submission name still selects its log contents directly.

Log indexes select Task names from both current declarations and recorded attempts, including Tasks removed from the workflow. Removed Tasks are marked as "no longer declared"; declaration removal does not hide their recorded work or logs. The same selection set applies to bare `logs`, while `--history` continues to control inclusion of earlier submissions and attempts.

Log Task selectors follow the shared rule that every supplied selector must match. Declared Tasks without recorded attempts match successfully and report no recorded work. Indexes identify missing log streams, while requesting a missing individual stream remains an error. `--history` applies only to Task indexes and is rejected with an individual-job content request. For a Task that is no longer declared, recovery guidance explains that its declaration must be restored before using the run preview.

For v0.7.0, Task names must not collide with public computation or lifecycle job names. Workflow construction rejects these collisions regardless of declaration order and identifies both conflicting declarations, deliberately replacing the earlier allowance for Task/job name overlap while retaining the existing naming syntax. Log resolution also checks retained public job and historical submission names for conflicts with Task names and reports ambiguity as an error. This keeps current and historical selection unambiguous without name-precedence rules or a separate `--job` option.

The `run`, `status`, and `clean` commands accept multiple positional exact Task names and quoted glob patterns, resolved only against Task names. Factory-group and public-job names do not expand to their member or owning Tasks, and the `task:` and `job:` namespaces are removed. Automatic grouping remains available in the status overview, while a shared Task-selection language keeps the command interface consistent.

When no Task selectors are supplied to `run`, `status`, or `clean`, all Tasks available to that command are Selected Tasks. An unqualified `gwflow run` therefore retains whole-workflow planning and execution with normal Reuse, and `gwflow run --force` starts fresh attempts for every Task.

Every supplied Task selector must match at least one Task in `run`, `status`, and `clean`. If any selector is unmatched, the entire invocation fails before job submission, Task-state changes, or cleanup deletion and reports all unmatched selectors, even when other selectors match. A partially matched request must not appear to have addressed all intended Tasks.

The `--force` option starts fresh attempts only for Selected Tasks. Required upstream Tasks added to the Execution scope undergo normal validity checks and may be reused; they are not forced merely because a selected consumer is forced. For `A → B → C`, `gwflow run C --force` therefore forces C alone when A and B remain reusable.

The CLI omits `--force-task`, `--no-deps`, and `--group`. Selection and `--force` are the only controls for choosing execution scope and requesting fresh computation; forcing one producer and then continuing the broader workflow takes separate invocations. This deliberately favors a lean command interface over the convenience of separate execution and force selections in one invocation.

The separate `explain` command is removed. `run --dry-run` is the sole execution preview, using the same Task selectors and force semantics as `run`, with `--details` available for deeper diagnostics. Keeping preview within the run command avoids overlapping command interfaces.

Status displays Selected Tasks, checking upstream dependencies as needed and reporting upstream blockers that explain a selected Task's condition. The run preview displays the full Execution scope, including required upstream Tasks. For `A → B → C`, `gwflow status C` therefore focuses on C, while `gwflow run C --dry-run` shows A, B, and C.

Status shows a grouped overview when no Task selectors or `--details` are supplied. Explicit Task selectors, whether exact names or globs, show flat Task rows; `--details` expands Selected Tasks with their computation targets and lifecycle jobs. The `--instances` option is removed, so `status '*'` provides all Task rows and selecting a subset no longer produces a grouped overview.

Status retains `--status` filtering by primary Task state and omits `--endpoints`. The ordinary-workflow-only `--format` option also disappears from the managed-only CLI; the view rules above determine presentation. Task selectors are resolved before state filtering, so an unmatched selector fails while a valid selection emptied by the state filter succeeds with no matching Tasks.

Run and status wrap long terminal text rather than truncating it, and `--no-truncate` is removed. Names and text within the chosen view remain complete; `--details` controls diagnostic depth. Redirected output remains complete as before.

Run and status retain `--plain` for explicitly requesting undecorated terminal output. Global `--no-color` disables color while preserving the formatted layout, and redirected reports become plain automatically. These controls serve distinct presentation preferences without changing the default view.

Cleanup uses positional Task selectors in place of `--task` and applies only to matched Tasks, without adding upstream dependencies. `clean` previews by default; `--delete` removes work eligible under the managed cleanup rules, preserving retained results.

Cleanup matches Task names from both current declarations and recorded attempts, so removing a Task declaration does not hide its recorded work. A declared Task without recorded attempts matches successfully and reports no recorded work; a selector matching neither declarations nor records is unmatched. Run and status continue to select from the currently declared Tasks.

Cleanup previews list every recorded attempt for the matched Tasks, including older or failed attempts that ordinary cleanup keeps. Each entry exposes its attempt UUID, paths, and eligibility or reason for keeping it, so users can discover attempts before choosing explicit cleanup. Selection determines what is inspected; the existing managed cleanup rules determine what can be deleted.

Cleanup retains repeatable `--attempt UUID` selection for deliberately removing inactive failed, superseded, or repairable attempts. This mode cannot be combined with Task selectors, still requires `--delete` to perform deletion, and never bypasses ownership or inactivity checks. Explicit attempt selection preserves a supported way to reclaim work that ordinary cleanup keeps.

The v0.6.0 release also rewrites the README as a short, precise getting-started guide for the average user. It should explain gwflow's purpose briefly, give prerequisites and installation instructions, present one minimal runnable workflow with the backend or worker setup needed to execute it, and show everyday use of the lean CLI: previewing, running all or selected Tasks, checking status and logs, forcing a selected Task, and previewing or performing work cleanup. Explain Reuse and protection of retained results in plain language alongside those examples. Current usage examples must use `gwflow` and the agreed command interface; `--help` supplies the complete option reference.

Detailed authoring, container, lifecycle, recovery, and storage guidance belongs in linked documentation or examples rather than the README. Release history, implementation detail, and validation evidence stay outside the getting-started path, with useful existing material preserved in the appropriate reference or historical documentation. Current reference examples must also reflect the new CLI; historical validation evidence remains a record of the version it tested. This keeps installation and ordinary use easy to find without discarding the depth needed for advanced use and troubleshooting.
