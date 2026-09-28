# Issue tracker

Specs and tickets live in GitHub Issues in `MOMA-AUH/gwflow`. Use the `gh` CLI with that repository.

- Read: `gh issue view <number> --comments` and inspect labels.
- List: `gh issue list` with the appropriate state and label filters.
- Publish: `gh issue create --title <title> --body-file <file>`; supply the required triage label.
- Update: `gh issue edit`; use `--body-file` for multiline replacement bodies.
- Comment: `gh issue comment --body-file <file>`.
- Close: `gh issue close` with the applicable explanation.

“Publish to the issue tracker” means create a GitHub issue. “Fetch the ticket” means read the issue and its comments.

PRs as a request surface: no.

Use small, focused PRs for repository changes. Ticket generation and implementation remain separate stages invoked by the user.
