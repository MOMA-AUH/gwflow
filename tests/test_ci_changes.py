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
                     "docs/operating.md\nextra", "docs/é.md"):
            with self.subTest(path=name):
                self.git("reset", "--hard", self.base)
                self.write("README.md", "# Updated\n")
                self.write(name, "changed\n")
                self.commit()
                self.assertEqual(self.plan()["mode"], "full")

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

    def test_empty_invalid_and_file_type_changes_never_select_lightweight(self):
        self.assertEqual(self.plan()["mode"], "full")
        self.command("classify", "--source", str(self.root), "--base", "missing-ref", "--head", "HEAD", success=False)
        path = self.root / "README.md"
        path.chmod(0o755)
        self.commit()
        self.assertEqual(self.plan()["mode"], "full")
        self.git("reset", "--hard", self.base)
        path.unlink()
        path.symlink_to("untrusted.md")
        self.commit()
        self.assertEqual(self.plan()["mode"], "full")
