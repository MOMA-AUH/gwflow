# Compaction scale and shared-filesystem acceptance

This record covers issue #260 and the footprint and performance requirements in
specification #252. Record compaction preserves lifecycle evidence in one document.
The successful attempt also retains its coordination lock.

The [live scheduler record](slurm.json), [integrated journeys](integrated.json), and
[environment record](environment.json) preserve the measured results and versions.
The [scale record](scale.json) preserves independent file and directory counts,
planning operations, decoded bytes, memory, and descriptor observations.

The live Slurm journey passed on BeeGFS. An injected deletion error occurred after
publication of the authoritative document. The completion job failed, and Slurm
cancelled the dependent jobs without executing their commands. Ordinary run retried
completion in the same attempt. The retained output kept its inode and timestamp.
A delayed acknowledgement arrived after compaction and preserved existing records.
Two Repairs and repeated cleanup left each attempt with two files and one directory.

Frontend and compute-node observations reported the same directory inode with
different device numbers. This run used BeeGFS client 8.3.1, Slurm 25.11.6, Python
3.12.14, and gwf 2.1.1. The records preserve the exact observations and package versions.

## Scale results

Every successful snapshot retained two regular files and one directory per attempt.
That bound held after both Repairs and both cleanup invocations. The document grew
with history while the physical metadata count stayed fixed. In the final recount,
each single-Task workflow had four workflow bookkeeping files and four directories.
Task bookkeeping added one file and three directories, including its namespace root.
Two empty staging directories remained. These counts were independent of target
and retry counts.

| Targets per Task | Failed computation retries | Operator recoveries | Logical records after success | Document bytes |
| --- | --- | --- | --- | --- |
| 1 | 0 | 0 | 24 | 15,562 |
| 32 | 0 | 0 | 210 | 174,094 |
| 128 | 0 | 0 | 786 | 666,961 |
| 1 | 4 | 4 | 64 | 42,974 |
| 1 | 16 | 16 | 184 | 125,131 |
| 1 | 64 | 64 | 664 | 453,673 |

Each single-attempt status and dry-run probe decoded the authoritative document
once. Status used three JSON reads including fixed bookkeeping. The 128-target
status probe read 667,905 JSON input bytes, opened at most 28 tracked descriptors,
and reached 20,341,396 bytes of traced Python allocations. These values include
the probe overhead. The 16-Task case retained at most eight decoded documents and
59 tracked descriptors. Each document was decoded twice because later observations
revisited it after eviction. The measurements did not expose a production regression.

History checks compare complete admission and recovery records before success
with their values after success, both Repairs, and both cleanup invocations.
The largest history preserved all 451 captured records, including 64 recoveries.
The evidence retains the comparison counts and hashes. The tests also verify that
Repair preserves the Task attempt and computation identities without repeating commands.

## Reproduce

Install the candidate and example packages as described in the
[validation guide](../README.md). Choose unused directories for every run:

```sh
python tests/validate_compaction_scale.py --root build/validation/compaction-scale
python tests/validate_compaction_slurm.py --root build/validation/compaction-slurm --queue short
python tests/validate_automatic_compaction.py --root build/validation/compaction-integrated
python -m unittest discover -s tests -v
```

The scale runner varies targets per Task, then failed submissions and operator
recoveries within one attempt. Every recovery follows a rejected submission, so
there is no running work when the fixture asserts inactivity. Another case uses
16 Tasks to exceed the document cache capacity. Each case exercises two Repairs
and two cleanup invocations. The runner compares instrumented and ordinary command
output and requires the same behavior.

The Slurm runner uses the installed backend and actual scheduler commands.
Its wrapper injects the existing deletion fault into one real completion worker.
It also delays return of an accepted job ID until compaction finishes.
Scheduling, accounting, dependencies, and subsequent lifecycle commands remain real.
At sites that retain invalid dependencies as pending jobs, the runner cancels only
its isolated consumer before retry. This deployment cancelled those jobs automatically.
The runner requests one core, 1 GB, and five minutes per job. The default wait limit
is 600 seconds. A failed run retains its evidence. Inspect its accepted job IDs
before rerunning because timeout does not cancel outstanding jobs.

## Measurement scope

Counts include directory roots and classify regular files, directories, and symlinks
independently. Attempt metadata includes the attempt directory. Task and workflow
bookkeeping, logs and aliases, data storage, and validation artifacts have separate
categories. Work and results include their storage subdirectories.
`staging_data` separately counts `.gwf/gwflow/staging` and its contents.
Per-stage attempt counts come from the lifecycle observations. `final_footprint`
recounts the retained trees after the probes with all exclusions separated.
The original raw per-stage category totals included staging under workflow
bookkeeping, so those totals are excluded from the committed selection. Owned transient
conversion files have a separate category and must be absent after success.
An omitted count means zero. Intermediate failed states can exceed the final bound.

The existing planning probe counts attempted Python OS calls and stream reads.
`json_bytes` counts UTF-8 input consumed by JSON decoders, including fixed records.
It does not measure physical disk traffic or kernel cache misses.
`document_decodes` counts parsing of authoritative documents, including cache misses.
A revisit after eviction starts another observation. The cache retains at most eight
documents, and a new planning pass discards its contents. The focused storage test
also verifies eviction, pass disposal, and visibility of a changed record.

Memory measurements include interpreter imports and instrumentation. RSS is the
Linux process high-water mark in KiB. Traced memory reports current and peak Python
allocations in bytes. Descriptor counts cover handles opened through the probe.
Instrumented times include tracing overhead and are not latency benchmarks.

Raw workflows, logs, command transcripts, and complete path counters remain under
`build/validation/260/`. Committed records use `<REPO>` for the checkout and document
any other path placeholders. Exported measurements preserve numeric observations.
Path normalization is checked by reversing substitutions and comparing with the
selected raw record before adding explanatory metadata.

The infrastructure evidence applies to this BeeGFS and Slurm deployment. It does
not establish power-loss durability, cross-version interoperability, or universal
filesystem support. Process interruption and injected I/O errors cannot establish
those guarantees. The BeeGFS administration CLI was unavailable. The client version
came from `modinfo -F version beegfs`.

Apptainer is disabled locally for security. The frontend reported a user-namespace
limit of zero, and a compute-node probe also failed before container startup.
The local full-suite run encountered container failures for that reason.
Remaining local tests continued with the container fixtures unset. Local skips do
not establish container coverage. GitHub CI runs the real-container regressions.
The environment record preserves the failed infrastructure probes. No system
security settings were changed.
