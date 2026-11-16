## Agent skills

### Releases

For a concrete request such as “Make release vX.Y.Z” or “Make vX.Y.Z release”,
use [.agents/skills/gwflow-release/SKILL.md](.agents/skills/gwflow-release/SKILL.md).
It proposes notes for agreement, then delivers the approved release through
the protected PR, CI, Conda publication, and GitHub release process.

### Issue tracker

Before reading or publishing specs and tickets, read `docs/agents/issue-tracker.md`: use GitHub Issues in `MOMA-AUH/gwflow`.

### Fix issues

For “Fix open issues”, “Fix the next N issues”, a specified issue list, or
resuming that work, use [.agents/skills/fix-issues/SKILL.md](.agents/skills/fix-issues/SKILL.md).
It implements one issue at a time, creates a PR, repairs CI failures, and merges
after checks pass before continuing. Workflow questions and skill edits do not
start the queue.

### Triage labels

Before triaging or applying issue labels, read `docs/agents/triage-labels.md` for the five standard roles.

### Domain docs

Before exploring or specifying behavior, read `docs/agents/domain.md`: this project uses a single domain context.

### Validation evidence

Before planning validation or reviewing readiness, read the
[maintainer-led HPC testing context](docs/validation/README.md#maintainer-led-hpc-testing).

When adding or updating validation records:

- Use repository-relative paths, or `<REPO>/...` when preserving absolute-path comparisons. Explain placeholders in each standalone record. Normalize user-specific paths outside the checkout with documented placeholders too.
- Keep raw machine-specific logs under the ignored `build/validation/` directory. Preserve versions, commands, results, and relevant device/inode observations in committed evidence. Standard system paths such as `/usr/bin/sbatch` may remain.
- Before finishing, scan all changed records for checkout prefixes and user-specific home/storage paths, validate JSON syntax, and verify that normalization changed only paths and explanatory metadata.
