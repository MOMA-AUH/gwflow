## Agent skills

### Issue tracker

Before reading or publishing specs and tickets, read `docs/agents/issue-tracker.md`: use GitHub Issues in `MOMA-AUH/gwflow`.

### Triage labels

Before triaging or applying issue labels, read `docs/agents/triage-labels.md` for the five standard roles.

### Domain docs

Before exploring or specifying behavior, read `docs/agents/domain.md`: this project uses a single domain context.

### Validation evidence

When adding or updating validation records:

- Use repository-relative paths, or `<REPO>/...` when preserving absolute-path comparisons. Explain placeholders in each standalone record. Normalize user-specific paths outside the checkout with documented placeholders too.
- Keep raw machine-specific logs under the ignored `build/validation/` directory. Preserve versions, commands, results, and relevant device/inode observations in committed evidence. Standard system paths such as `/usr/bin/sbatch` may remain.
- Before finishing, scan all changed records for checkout prefixes and user-specific home/storage paths, validate JSON syntax, and verify that normalization changed only paths and explanatory metadata.
