"""Release selection through the CLI with a controlled read-only GitHub API."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).with_name("select_ci_release.py").resolve()


class CiReleaseSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="gwflow-ci-release-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = "a" * 40
        self.repository = "MOMA-AUH/gwflow"
        self.run = {"id": 100, "status": "completed", "conclusion": "success", "event": "workflow_dispatch",
                    "head_branch": "master", "head_sha": self.source, "path": ".github/workflows/release-qualification.yml",
                    "repository": {"full_name": self.repository}, "head_repository": {"full_name": self.repository}}
        self.artifact = {"id": 200, "name": "gwflow-conda-release", "expired": False, "size_in_bytes": 500}
        self.fixture = self.root / "api.json"
        executable = self.root / "gh"
        executable.write_text(f"#!{sys.executable}\n" + """import json, os, sys
from pathlib import Path
data = json.loads(Path(os.environ['CI_API_FIXTURE']).read_text())
endpoint = sys.argv[2]
assert sys.argv[1] == 'api' and endpoint.startswith('repos/MOMA-AUH/gwflow/')
if '/compare/' in endpoint:
    assert endpoint.endswith('a' * 40 + '...master')
    print(json.dumps(data['comparison']))
elif '/workflows/release-qualification.yml/runs?' in endpoint:
    assert 'branch=' not in endpoint and 'event=' not in endpoint
    assert 'head_sha=' + 'a' * 40 in endpoint and 'status=success' in endpoint
    print(json.dumps({'workflow_runs': data['runs']}))
elif endpoint.endswith('/runs/100'):
    print(json.dumps(data['runs'][0]))
else:
    assert '/runs/100/artifacts?' in endpoint
    print(json.dumps({'artifacts': data['artifacts']}))
""")
        executable.chmod(0o755)
        self.environment = {**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                            "CI_API_FIXTURE": str(self.fixture)}

    def command(self, runs, artifacts, success, extra=(), comparison=None):
        self.fixture.write_text(json.dumps({"runs": runs, "artifacts": artifacts, "comparison": comparison or {"status": "ahead", "base_commit": {"sha": self.source}, "merge_base_commit": {"sha": self.source}}}))
        output = self.root / "outputs"
        output.unlink(missing_ok=True)
        result = subprocess.run([sys.executable, str(TOOL), "--repo", self.repository,
                                 "--source", self.source, "--output", str(output), *extra],
                                env=self.environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        if success:
            self.assertEqual(output.read_text(), "run_id=100\nartifact_id=200\n")
        else:
            self.assertFalse(output.exists())

    def test_exact_dispatch_success_selects_immutable_artifact_id(self):
        older = {**self.run, "id": 99}
        newer_pr = {**self.run, "id": 101, "event": "pull_request"}
        self.command([older, newer_pr, self.run], [self.artifact], True)

    def test_successful_v_prefixed_feature_dispatch_cannot_qualify_unmerged_source(self):
        run = {**self.run, "head_branch": "v-feature"}
        for comparison in ({"status": "diverged", "base_commit": {"sha": self.source},
                            "merge_base_commit": {"sha": "b" * 40}},
                           {"status": "ahead", "base_commit": {"sha": "b" * 40},
                            "merge_base_commit": {"sha": self.source}}):
            self.command([run], [self.artifact], False, comparison=comparison)

    def test_requested_run_is_pinned_and_response_identity_checked(self):
        self.command([self.run], [self.artifact], True, extra=("--run-id", "100"))
        self.command([{**self.run, "id": 99}], [self.artifact], False, extra=("--run-id", "100"))

    def test_exact_release_tag_push_can_be_selected(self):
        self.command([{**self.run, "event": "push", "head_branch": "v1.0.0"}], [self.artifact], True)

    def test_master_push_and_cancelled_runs_are_rejected(self):
        for changes in ({"event": "push"}, {"conclusion": "cancelled"}):
            self.command([{**self.run, **changes}], [self.artifact], False)

    def test_other_commits_prs_forks_failures_and_workflows_are_rejected(self):
        mutations = {"head_sha": "b" * 40, "head_branch": "feature", "event": "pull_request",
                     "status": "in_progress", "conclusion": "failure", "path": ".github/workflows/other.yml",
                     "repository": {"full_name": "other/gwflow"}, "head_repository": {"full_name": "other/gwflow"}}
        for field, value in mutations.items():
            with self.subTest(field=field):
                self.command([{**self.run, field: value}], [self.artifact], False)

    def test_missing_expired_empty_or_ambiguous_bundles_are_rejected(self):
        for artifacts in ([], [{**self.artifact, "expired": True}], [{**self.artifact, "size_in_bytes": 0}],
                          [{**self.artifact, "name": "gwflow-conda-candidate"}], [self.artifact, copy.deepcopy(self.artifact)]):
            with self.subTest(artifacts=artifacts):
                self.command([self.run], artifacts, False)

    def test_missing_successful_run_is_rejected(self):
        self.command([], [self.artifact], False)
