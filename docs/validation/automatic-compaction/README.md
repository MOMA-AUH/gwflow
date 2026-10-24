# Automatic completion compaction

The [acceptance record](summary.json) covers issue #259 on the checkout's shared
filesystem. Seven installed-command journeys passed with real local worker
processes. All 16 retained attempt directories were compacted and contained only
`record-state.json` and `records.lock`. The record preserves package versions,
commands, logical-record counts, and observed lock device and inode values.
`<REPO>` means the checkout root.

The journeys cover late and reconciled acknowledgements, simultaneous completions,
producer reads, cleanup followed by a new consumer, repeated Repair, and ordinary
retries after interrupted compaction. They also exercise gwf 2.1.1's Slurm
`afterok` dependency contract through controlled scheduler commands and real
local workers. This run did not submit jobs to a live Slurm scheduler.

Run from the repository with an installed candidate and Python 3.12:

```sh
python tests/validate_automatic_compaction.py --root build/validation/automatic-compaction
```

Use a new root directory on the filesystem under validation. The runner preserves
workflows, records, and a raw `summary.json` there. Raw logs for this run remain
under the ignored `build/validation/259/` directory. Normalize paths before
committing a new record. Process interruptions and injected I/O errors do not
establish power-loss durability or certify other filesystems.
