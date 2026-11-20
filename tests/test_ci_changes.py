"""Select lightweight CI only for a complete, known prose-only PR diff."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).with_name("ci_changes.py")


class CiChangesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow-ci-changes-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.test")
        self.write("README.md", "# Fixture\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *arguments):
        return subprocess.check_output(["git", *arguments], cwd=self.root, text=True, stderr=subprocess.PIPE)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def command(self, *arguments, success=True):
        result = subprocess.run([sys.executable, str(TOOL), *arguments], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def plan(self):
        result = self.command("classify", "--source", str(self.root), "--base", self.base, "--head", "HEAD")
        return json.loads(result.stdout)

    def test_known_prose_changes_use_lightweight_checks(self):
        self.write("README.md", "# Changed\n")
        self.write("docs/operating.md", "# Operations\n")
        self.commit()
        plan = self.plan()
        self.assertEqual(plan["mode"], "docs")
        self.assertEqual(plan["paths"], ["README.md", "docs/operating.md"])

    def test_known_application_changes_omit_only_release_simulations(self):
        self.write("src/gwflow/presentation.py", "# Application presentation\n")
        self.write("tests/test_presentation.py", "# Application regression\n")
        self.commit()
        plan = self.plan()
        self.assertEqual(plan["mode"], "full")
        self.assertEqual(plan["release_simulations"], "skip")
        output = self.root / "outputs"
        self.command("classify", "--source", str(self.root), "--base", self.base, "--head", "HEAD",
                     "--github-output", str(output))
        self.assertEqual(output.read_text(), "mode=full\nrelease_simulations=skip\n")

    def test_release_notes_use_lightweight_checks_through_their_lifecycle(self):
        name = "docs/releases/v0.11.3.md"
        self.write(name, "# Release notes\n")
        self.commit()
        self.assertEqual(self.plan()["mode"], "docs")
        self.assertEqual(self.plan()["release_simulations"], "skip")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write(name, "# Verified release notes\n")
        self.commit()
        self.assertEqual(self.plan()["mode"], "docs")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("mv", name, "docs/releases/v0.11.4.md")
        self.commit()
        self.assertEqual(self.plan()["mode"], "docs")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("rm", "docs/releases/v0.11.4.md")
        self.commit()
        self.assertEqual(self.plan()["mode"], "docs")

    def test_release_notes_with_application_changes_omit_only_release_simulations(self):
        self.write("docs/releases/v0.11.3.md", "# Release notes\n")
        self.write("src/gwflow/presentation.py", "# Presentation\n")
        self.commit()
        self.assertEqual(self.plan()["mode"], "full")
        self.assertEqual(self.plan()["release_simulations"], "skip")

    def test_release_note_renames_check_both_paths(self):
        self.write("docs/releases/v0.11.3.md", "# Release notes\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("mv", "docs/releases/v0.11.3.md", "docs/unknown.md")
        self.commit()
        self.assertEqual(self.plan()["mode"], "full")
        self.assertEqual(self.plan()["release_simulations"], "run")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("mv", "docs/unknown.md", "docs/releases/v0.11.3.md")
        self.commit()
        self.assertEqual(self.plan()["mode"], "full")
        self.assertEqual(self.plan()["release_simulations"], "run")

    def test_release_notes_with_sensitive_changes_run_full_validation(self):
        self.write("docs/releases/v0.11.3.md", "# Release notes\n")
        self.write("src/gwflow/lifecycle.py", "# Lifecycle\n")
        self.commit()
        self.assertEqual(self.plan()["mode"], "full")
        self.assertEqual(self.plan()["release_simulations"], "run")

    def test_executable_and_symlink_release_notes_run_full_validation(self):
        path = self.write("docs/releases/v0.11.3.md", "# Release notes\n")
        path.chmod(0o755)
        self.commit()
        self.assertEqual(self.plan()["mode"], "full")
        self.assertEqual(self.plan()["release_simulations"], "run")
        path.unlink()
        path.symlink_to("../../README.md")
        self.commit()
        self.assertEqual(self.plan()["mode"], "full")
        self.assertEqual(self.plan()["release_simulations"], "run")

    def test_release_simulation_plan_is_conservative_and_release_events_cannot_skip(self):
        for event, selection, expected in (("pull_request", "skip", False), ("pull_request", "run", True),
                                           ("workflow_dispatch", "skip", True), ("push", "skip", True),
                                           ("unknown", "skip", True)):
            result = self.command("simulation-plan", "--event", event, "--selection", selection)
            self.assertEqual(json.loads(result.stdout)["run"], expected)
        for selection in ("", "false", "unknown"):
            self.command("simulation-plan", "--event", "pull_request", "--selection", selection, success=False)

    def test_release_simulation_gate_rejects_failures_cancellation_or_incomplete_selection(self):
        for selected in ("true", "false"):
            outcome = "success" if selected == "true" else "skipped"
            needs = {"selection": {"result": "success"},
                     **{name: {"result": outcome} for name in ("capture", "controlled", "bundle")}}
            self.command("simulation-gate", "--run", selected, "--needs", json.dumps(needs))
            for name in needs:
                for result in ("failure", "cancelled", "skipped", "success"):
                    if result == needs[name]["result"]:
                        continue
                    changed = {key: dict(value) for key, value in needs.items()}
                    changed[name]["result"] = result
                    self.command("simulation-gate", "--run", selected, "--needs", json.dumps(changed), success=False)
        self.command("simulation-gate", "--run", "", "--needs", "{}", success=False)
        self.command("simulation-gate", "--run", "true", "--needs", "{}", success=False)

    def test_required_gate_rejects_failed_cancelled_or_unexpectedly_skipped_jobs(self):
        for mode, selected, unselected in (("docs", "documentation", "qualification"),
                                            ("full", "qualification", "documentation")):
            needs = {"classification": {"result": "success"}, selected: {"result": "success"}, unselected: {"result": "skipped"}}
            self.command("gate", "--mode", mode, "--needs", json.dumps(needs))
            for job in needs:
                for outcome in ("failure", "cancelled", "skipped", "success"):
                    if outcome == needs[job]["result"]:
                        continue
                    with self.subTest(mode=mode, job=job, outcome=outcome):
                        changed = {name: dict(value) for name, value in needs.items()}
                        changed[job]["result"] = outcome
                        self.command("gate", "--mode", mode, "--needs", json.dumps(changed), success=False)
            self.command("gate", "--mode", "", "--needs", json.dumps(needs), success=False)
            self.command("gate", "--mode", mode, "--needs", "{}", success=False)

    def test_unknown_sensitive_and_mixed_changes_always_run_full_validation(self):
        for name in ("src/gwflow/cli.py", "tests/test_readme.py", "pyproject.toml", "environment.yml",
                     "conda-recipe/meta.yaml", ".github/workflows/ci.yml", ".github/actions/example/action.yml",
                     "tests/fixtures/container.def", "docs/validation/compatibility/baseline.json",
                     "docs/new.md", "docs/releasing.md", "docs/adr/0024-example.md", "AGENTS.md",
                     "docs/operating.md\nextra", "docs/é.md", "tests/conda_runtime.py",
                     "tests/compare_runtime.py", "tests/qualification_event.py", "src/gwflow/lifecycle.py",
                     "tests/test_new_release_feature.py", "docs/releases/helper.py", "docs/releases/notes.json",
                     "docs/releases/nested/notes.md", "docs/releases-extra/notes.md",
                     "docs/releases/notes.md\nextra"):
            with self.subTest(path=name):
                self.git("reset", "--hard", self.base)
                self.write("README.md", "# Updated\n")
                self.write(name, "changed\n")
                self.commit()
                plan = self.plan()
                self.assertEqual(plan["mode"], "full")
                self.assertEqual(plan["release_simulations"], "run")

    def test_additions_deletions_and_both_rename_paths_are_classified(self):
        self.write("docs/operating.md", "# Operations\n")
        self.commit()
        self.assertEqual(self.plan()["mode"], "docs")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("mv", "docs/operating.md", "docs/reference.md")
        self.commit()
        self.assertEqual(self.plan()["mode"], "docs")
        self.git("rm", "README.md")
        self.commit()
        self.assertEqual(self.plan()["mode"], "docs")
        self.git("mv", "docs/reference.md", "implementation.py")
        self.commit()
        self.assertEqual(self.plan()["mode"], "full")
        self.assertEqual(self.plan()["release_simulations"], "run")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("mv", "implementation.py", "README.md")
        self.commit()
        self.assertEqual(self.plan()["mode"], "full")

    def test_complete_pr_history_uses_merge_base_not_last_commit(self):
        self.write("src/module.py", "changed\n")
        self.commit()
        self.write("README.md", "# Changed\n")
        self.commit()
        head = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-qb", "base-updated", self.base)
        self.write("base-only.py", "base change\n")
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "--detach", head)
        plan = self.plan()
        self.assertEqual(plan["mode"], "full")
        self.assertEqual(plan["paths"], ["README.md", "src/module.py"])
        self.assertEqual(plan["release_simulations"], "run")

    def test_empty_invalid_and_file_type_changes_never_select_lightweight(self):
        self.assertEqual(self.plan()["mode"], "full")
        self.assertEqual(self.plan()["release_simulations"], "run")
        self.command("classify", "--source", str(self.root), "--base", "missing-ref", "--head", "HEAD", success=False)
        path = self.root / "README.md"
        path.chmod(0o755)
        self.commit()
        self.assertEqual(self.plan()["mode"], "full")
        self.assertEqual(self.plan()["release_simulations"], "run")
        self.git("reset", "--hard", self.base)
        path.unlink()
        path.symlink_to("untrusted.md")
        self.commit()
        self.assertEqual(self.plan()["mode"], "full")
        self.assertEqual(self.plan()["release_simulations"], "run")
