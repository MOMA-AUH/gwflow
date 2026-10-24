# Compaction scale and shared-filesystem acceptance

This record covers issue #260 and the footprint and performance requirements in
specification #252. Record compaction preserves lifecycle evidence in one document.
The successful attempt also retains its coordination lock.

The live Slurm journey passed on BeeGFS. An injected deletion error occurred after
publication of the authoritative document. The completion job failed, and Slurm
cancelled the dependent jobs without executing their commands. Ordinary run retried
completion in the same attempt. The retained output kept its inode and timestamp.
A delayed acknowledgement arrived after compaction and preserved existing records.
Two Repairs and repeated cleanup left each attempt with two files and one directory.

Frontend and compute-node observations reported the same directory inode with
different device numbers. This run used BeeGFS client 8.3.1, Slurm 25.11.6, Python
3.12.14, and gwf 2.1.1. The records preserve the exact observations and package versions.

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
categories. Work and results include their storage subdirectories. Owned transient
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
