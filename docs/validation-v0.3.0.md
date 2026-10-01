# v0.3.0 validation record

This record accounts for the acceptance matrix in
[the release specification, #62](https://github.com/MOMA-AUH/gwflow/issues/62)
and its [integration slice, #76](https://github.com/MOMA-AUH/gwflow/issues/76).
Validation uses Python 3.12 and pinned gwf 2.1.1, with gwflow source metadata
0.3.0 and independently installed summary/report factory packages 0.2.0.
It validates implementation readiness; no release publication or deployment is
part of this work.

## Repeatable checks

From an environment with those dependencies installed:

```sh
python -m pip install .
python -m pip install ./examples/packaged/packages/summary-task ./examples/packaged/packages/report-task
python -m unittest discover -s tests -v
python tests/release_smoke.py
python -m compileall -q src tests examples
```

Tests exercise the installed public API and CLI with real local workers and
temporary files, including paths containing spaces. Backend plugins control
scheduler observations, acknowledgements and tracking. The test-only process and
filesystem harnesses interrupt deterministic commit boundaries; there are no
production fault-injection switches. Assertions observe submissions, retained
data, reuse, exposed attempt selection and preserved/deleted work. Malformed
records are deliberate fault inputs, not a supported record-editing interface.

The release smoke runs the installed packaged A/B cleanup-and-extension scenario
also used by the suite. CI runs the suite against both the pip-installed package
and a built Conda artifact; the Conda job also runs this smoke. The tag-triggered
publication workflow installs the example packages for its existing smoke step.
Changing that workflow does not trigger publication.

## Parent acceptance matrix

Each row has focused coverage below. Names in backticks identify tests (or the
shared subject of several tests) in the linked file; the full suite integrates
these scenarios rather than relying on the packaged demonstration alone.

| Parent row | Focused evidence |
| --- | --- |
| Release demonstration | [test_example.py](../tests/test_example.py): `test_packaged_slurm_dependencies_wait_for_both_complete_producers` checks independent A/B admission, C's dependencies on both finishing jobs, and exact CSV rows; the cleanup test checks the entire human-facing file set. Real Slurm observations are recorded below. |
| Cleanup and extension | [test_example.py](../tests/test_example.py): `test_installed_producers_clean_then_new_consumer_computes_only_once` completes A/B, proves preview preserves bytes/times, deletes eligible work, adds C, checks A/B work remains absent and the next invocation submits nothing. |
| Defaults and placement | [test_storage.py](../tests/test_storage.py): separate work/results filesystems, required staging filesystem, root aliases, grouped results, and independent initial roots. [test_managed.py](../tests/test_managed.py) covers default/equivalent roots. |
| Authoring boundaries | [test_managed.py](../tests/test_managed.py), [test_graphs.py](../tests/test_graphs.py), [test_dependencies.py](../tests/test_dependencies.py): invalid output contracts, placeholders, bindings, target/Task cycles, unknown/cross-owner references, outputless and top-level targets fail before initialization. |
| Paths and quoting | [test_managed.py](../tests/test_managed.py): `test_template_quoting_literal_braces_and_multiple_nested_outputs`, invalid fields and independent factory snapshots. [test_storage.py](../tests/test_storage.py) uses punctuated roots; [test_example.py](../tests/test_example.py) runs fixed-filename tools in independent instances. |
| Inputs and scratch | [test_inputs.py](../tests/test_inputs.py): aliases remain absolute and read in place, including symlink parents. [test_managed.py](../tests/test_managed.py) checks private CWD/TMPDIR and opt-out; [test_graphs.py](../tests/test_graphs.py) checks fresh retry storage. |
| Target success | [test_managed.py](../tests/test_managed.py): failed/incomplete output sets, output directories/symlinks, successful nested multi-output sets and outputless retained sets. [test_graphs.py](../tests/test_graphs.py) checks dependent access to committed sets. |
| Target interruption | [test_graphs.py](../tests/test_graphs.py): interruption before output commit, after rename and before success publication; unchecked files never supply success. |
| Partial retry and generations | [test_graphs.py](../tests/test_graphs.py): retry preserves successful siblings, permits unrelated active siblings, invalidates dependent generations and protects queued/running dependents. |
| Preparation | [test_inputs.py](../tests/test_inputs.py): blocked preparation returns promptly, observes later inputs, retains its committed baseline and rejects changes before installation. [test_dependencies.py](../tests/test_dependencies.py) checks exact producer Completion at preparation, computation and finishing. |
| Reuse comparisons | [test_inputs.py](../tests/test_inputs.py): size-only/backward-time changes, equal-metadata alias retargeting and same-size/same-time content limits; [test_fresh.py](../tests/test_fresh.py) covers changed declared structure; [test_repair.py](../tests/test_repair.py) covers retained metadata changes. |
| Definition tracking | [test_fresh.py](../tests/test_fresh.py): reordering/resources reuse, retained mapping and enabled command changes refresh. [test_graphs.py](../tests/test_graphs.py) and [test_repair.py](../tests/test_repair.py) cover enabled/disabled command tracking for retry and repair. Generated execution paths do not prevent subsequent reuse. |
| Other changes | [test_fresh.py](../tests/test_fresh.py): `test_untracked_runtime_package_parameter_and_environment_need_force` keeps results after untracked runtime-code, parameter and environment changes, then force produces changed output. Resource-only reuse/retry is covered here and in [test_graphs.py](../tests/test_graphs.py). |
| Force propagation | [test_fresh.py](../tests/test_fresh.py): exact selected/all force, repeatable selectors, invalid options, unaffected B, and active/uncertain producer/consumer refusals. |
| Fresh initialization | [test_fresh.py](../tests/test_fresh.py): read-only previews, invalid selection/ownership, interruption before intent, selection, during removal and after ready, rejected admission and failed fresh computation without rollback. |
| Transfer | [test_transfer.py](../tests/test_transfer.py): partial copy, prepared manifest, and real distinct-results-filesystem recovery without repeated computation or partially readable results. |
| Installation recovery | [test_transfer.py](../tests/test_transfer.py): rename-before-Completion recovery, missing/incomplete operation association, missing ownership, empty retained sets and independent destination metadata. |
| Repair | [test_repair.py](../tests/test_repair.py): missing/edited results restore the entire set within the same attempt; timestamp preservation and actual destination observations; interruption through copying/removal/installation/Completion. |
| Repair and consumers | [test_repair.py](../tests/test_repair.py): same-metadata reuse, changed timestamp precision, active/uncertain consumer protection, existing consumer deferral and new-consumer dependencies. |
| Recompute fallback | [test_repair.py](../tests/test_repair.py): invalid/lost source evidence requires fresh computation; [test_cleanup.py](../tests/test_cleanup.py) checks result damage after cleanup; [test_managed.py](../tests/test_managed.py) checks reuse after work removal. |
| Producer identity | [test_fresh.py](../tests/test_fresh.py): `test_selected_force_refreshes_consumers_even_with_identical_metadata` and `test_producer_identity_invalidates_consumers_with_command_tracking` cover command tracking off and on. |
| Cleanup eligibility | [test_cleanup.py](../tests/test_cleanup.py): read-only preview, default protection of failed/older/damaged/active/uncertain work, owned transfer leftovers and exact Task selection. |
| Explicit cleanup and interruption | [test_explicit_cleanup.py](../tests/test_explicit_cleanup.py): failed/older UUID selections, lost progress/repair consequences, configured staging, all-selector validation, and interrupted deletion followed by fresh computation. |
| Cleanup versus consumers | [test_cleanup.py](../tests/test_cleanup.py): `test_active_consumer_does_not_block_completed_producer_work_cleanup`; [test_explicit_cleanup.py](../tests/test_explicit_cleanup.py) also verifies surviving logs, inputs, records and results. |
| Ownership and containment | [test_managed.py](../tests/test_managed.py), [test_storage.py](../tests/test_storage.py), [test_inputs.py](../tests/test_inputs.py): invalid/overlapping paths and managed aliases; runtime root, staging, result and workspace symlink substitutions are exercised through commit, transfer and cleanup. |
| Storage changes and lost records | [test_managed.py](../tests/test_managed.py): unowned results, changed roots, legacy/truncated records; [test_storage.py](../tests/test_storage.py) checks existing result placement; [test_transfer.py](../tests/test_transfer.py) and [test_repair.py](../tests/test_repair.py) corrupt selected evidence without adopting data. |
| Frontend races | [test_managed_recovery.py](../tests/test_managed_recovery.py): held tracking, interrupted inspectors and acknowledged submissions; [test_cleanup.py](../tests/test_cleanup.py): serialized submit/delete and eligibility recheck. |
| Admission uncertainty | [test_managed_recovery.py](../tests/test_managed_recovery.py): intent without acknowledgement, lost tracking, recovered IDs/execution evidence, proven rejection, unknown status and confirmed cancellation. [test_fresh.py](../tests/test_fresh.py), [test_repair.py](../tests/test_repair.py), [test_cleanup.py](../tests/test_cleanup.py) protect conflicting mutations. |
| Historical scheduler loss | [test_managed_recovery.py](../tests/test_managed_recovery.py): `test_pinned_slurm_dependency_contract_and_expired_history_reuse`; unresolved live admissions remain blocked separately. |
| Inspection | [test_inspection.py](../tests/test_inspection.py): actual execution agrees with explain/dry-run for all lifecycle actions, including deferred/blocked, cleaned work, result-removal reasons, filtered displays, logs and unchanged persistent state. |
| Unsafe generic commands | [test_managed.py](../tests/test_managed.py): `test_inspection_and_generic_mutation_commands_preserve_managed_evidence`; [test_plain.py](../tests/test_plain.py) preserves ordinary gwf behavior. |

## Pinned backend and infrastructure observations

On 2026-10-01, the available Slurm controller and shared BeeGFS workspace were
used for an isolated smoke with installed gwflow/factory distributions. Every
job requested one CPU, 256 MB memory and a two-minute walltime. The observed
partition was `short`. The initial A/B declaration used an unsupported
`partition` option, which gwf warned it ignored; accounting still reported
`short`. Subsequent declarations used gwf 2.1.1's supported `queue="short"`.

| Scenario | Scheduler evidence | Observed result |
| --- | --- | --- |
| A/B producers | Jobs 1228142–1228149, including preparation, two targets and finishing per Task: all `COMPLETED`, `ExitCode=0:0` | Both Tasks reusable; clean-work preview selected both; `--delete` removed owned work and preserved results. |
| Add C after cleanup | Jobs 1228153–1228156: all `COMPLETED`, `ExitCode=0:0` | Only C submitted; exact report rows `apples,17,2,15` and `pears,8,1,7`; A/B work remained absent. |
| Unchanged invocation | CLI after C's checked Completion | A/B/C all reported reuse; zero submitted jobs. |
| Nonzero command after writing output | Preparation 1228161 completed; computation 1228162 `FAILED`, `ExitCode=1:0`; dependent finisher 1228163 `CANCELLED` | Authored command exited 7; the pinned wrapper propagated failure as nonzero (normalized to 1), no retained file or Completion was published, and status planned a retry. No cancellation was issued by the validation operator. |

The automated Slurm contract tests invoke the pinned backend and execute its
scripts through local workers. They verify `afterok` edges, producer finishing
dependencies, nonzero wrapper failure and acknowledgement survival after frontend
termination before gwf tracking is flushed. Recovery submits only remaining work;
force refuses while the acknowledged computation is still active. Lost scheduler
history does not invalidate checked completed attempts. Those deterministic crash
windows were exercised with the backend fixture, not by disrupting live Slurm
admissions.

Real separate-filesystem tests use XFS temporary work/bookkeeping and tmpfs
`/dev/shm` results or work. They cover cross-device copying, explicit staging on
the results filesystem, interruption and recovery, repair, cleanup, and rejection
of wrong-device staging before initialization. Timestamp precision and unsupported
preservation are additionally covered with controlled filesystem fault responses.
These tests explicitly skip if a writable separate filesystem is unavailable.

## Limits

Local workers, real Slurm, and distinct-filesystem storage were available for
this validation. There was no power-loss, hardware-loss, multi-frontend, or
universal cluster/filesystem certification. Deterministic process interruption
and atomic namespace tests do not simulate storage-controller durability.
Operation assumes shared coherent storage and same-filesystem atomic directory
rename at each commit, with file/directory synchronization where supported.

Content changes preserving size and modification time can go undetected. Package,
parameter, tool and environment changes have no independent invalidation key.
Command hashes remain opt-in (recommended); explicit force is required when no
tracked effect changes. Checked Completion establishes execution and complete
retained-set evidence, not scientific output correctness. Uncertain live
admission and insufficient ownership continue to block conflicting changes.
