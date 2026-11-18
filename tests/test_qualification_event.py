"""Release preflight through the public CLI and a real controlled Git repository."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).with_name("qualification_event.py").resolve()


class QualificationEventTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow-qualification-event-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git("init", "-b", "master")
        self.git("config", "user.name", "Release fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
        (self.root / "conda-recipe").mkdir()
        (self.root / "conda-recipe/meta.yaml").write_text('{% set version = "1.0.0" %}\n')
        self.git("add", ".")
        self.git("commit", "-m", "fixture")
        self.source = self.git("rev-parse", "HEAD").strip()
        self.git("remote", "add", "origin", str(self.root))
        self.git("tag", "v1.0.0")
        self.run = {"id": 42, "status": "completed", "conclusion": "success", "event": "push",
                    "head_branch": "v1.0.0", "head_sha": self.source,
                    "path": ".github/workflows/release-qualification.yml",
                    "repository": {"full_name": "MOMA-AUH/gwflow"},
                    "head_repository": {"full_name": "MOMA-AUH/gwflow"}}

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True, stderr=subprocess.DEVNULL)

    def command(self, mode, event, success=True, source=None, requested=None, tag="v1.0.0"):
        fixture = self.root / "event.json"
        fixture.write_text(json.dumps({"repository": {"full_name": "MOMA-AUH/gwflow"}, **event}))
        output = self.root / "outputs"
        output.unlink(missing_ok=True)
        result = subprocess.run([sys.executable, str(TOOL), mode, "--event", str(fixture),
                                 "--repo", "MOMA-AUH/gwflow", "--source", source or self.source,
                                 "--requested-source", requested or self.source, "--tag", tag,
                                 "--output", str(output)], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        if success:
            self.assertIn("source=" + self.source, output.read_text())
        else:
            self.assertFalse(output.exists())
        return result

    def test_tag_and_manual_exact_master_qualification(self):
        self.command("qualify", {"ref": "refs/tags/v1.0.0"})
        self.command("qualify", {"ref": "refs/heads/master", "inputs": {}})
        self.command("publish", {"workflow_run": self.run})

    def test_master_push_pr_wrong_versions_and_changed_source_fail(self):
        for event in ({"ref": "refs/heads/master"}, {"pull_request": {}},
                      {"ref": "refs/tags/v1.0.0", "deleted": True},
                      {"ref": "refs/heads/feature", "inputs": {}}):
            self.command("qualify", event, success=False)
        self.command("qualify", {"ref": "refs/tags/v1.0.1"}, tag="v1.0.1", success=False)
        self.command("qualify", {"ref": "refs/heads/master", "inputs": {}},
                     requested="b" * 40, success=False)

    def test_failed_cancelled_pr_and_manual_runs_never_publish(self):
        for changes in ({"conclusion": "failure"}, {"conclusion": "cancelled"},
                        {"status": "in_progress"}, {"event": "pull_request"},
                        {"event": "workflow_dispatch", "head_branch": "master"},
                        {"head_repository": {"full_name": "fork/gwflow"}},
                        {"path": ".github/workflows/ci.yml"}):
            with self.subTest(changes=changes):
                self.command("publish", {"workflow_run": {**self.run, **changes}}, success=False)

    def test_commit_outside_master_and_moved_tag_fail(self):
        self.git("checkout", "-b", "feature")
        (self.root / "feature").write_text("unmerged")
        self.git("add", "feature")
        self.git("commit", "-m", "unmerged")
        feature = self.git("rev-parse", "HEAD").strip()
        self.command("qualify", {"ref": "refs/heads/master", "inputs": {}}, source=feature,
                     requested=feature, success=False)
        self.git("tag", "-f", "v1.0.0", feature)
        self.command("publish", {"workflow_run": self.run}, success=False)
