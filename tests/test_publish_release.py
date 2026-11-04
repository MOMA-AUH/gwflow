"""Publication retries through the workflow's maintainer command and controlled services."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest

import test_release_artifacts as artifacts

ROOT = Path(__file__).resolve().parents[1]


class PublicationTests(artifacts.ReleaseArtifactFixture):
    def setUp(self):
        super().setUp()
        self.prepare()
        self.publication()
        self.channel = self.root / "channel"
        self.channel.mkdir()
        self.environment.update(GWFLOW_CHANNEL_FIXTURE_ROOT=str(self.channel), GITHUB_RUN_ID="101",
                                GITHUB_REPOSITORY="MOMA-AUH/gwflow", ANACONDA_API_TOKEN="fixture",
                                NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
        self.environment["PATH"] = str(Path(sys.executable).parent) + ":" + self.environment["PATH"]
        self.conda = self.root / "conda"
        shutil.copyfile(ROOT / "tests/fixtures/publication_conda.py", self.conda)
        self.conda.chmod(0o755)
        self.lookup_status = 200
        self.download_status = 200
        self.metadata_changes: dict = {}
        self.missing_lookups = 0
        case = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                package = case.channel / "package.conda"
                lookup = self.path.startswith("/dist/")
                status = case.lookup_status if lookup else case.download_status
                if lookup and case.missing_lookups:
                    case.missing_lookups -= 1
                    status = 404
                if status != 200:
                    self.send_error(status)
                    return
                if not package.exists():
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                if lookup:
                    self.wfile.write(json.dumps({"basename": "noarch/" + case.record["artifact"]["filename"],
                                                "version": case.version, "attrs": {"subdir": "noarch", "build": "py_0"},
                                                "sha256": case.record["artifact"]["sha256"], "labels": ["main"],
                                                **case.metadata_changes}).encode())
                else:
                    self.wfile.write(package.read_bytes())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def publish(self, success=True, extra=()):
        result = subprocess.run([sys.executable, str(ROOT / "tests/publish_release.py"),
                                 "--bundle", str(self.output), "--tag", "v1.0.0", "--source", self.source,
                                 "--repo", "MOMA-AUH/gwflow", "--source-ci-run", "42", "--output", str(self.root / "publication"),
                                 "--conda", str(self.conda), "--gh", str(self.github),
                                 "--api-url", f"http://127.0.0.1:{self.server.server_port}", "--retry-delay", "0", *extra],
                                env=self.environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def test_missing_actions_identity_fails_before_publication_mutations(self):
        self.environment.pop("GITHUB_REPOSITORY", None)
        self.environment.pop("GITHUB_RUN_ID", None)
        self.publish(success=False)
        self.assertEqual(self.channel_calls(), [])
        self.assertFalse((self.channel / "package.conda").exists())
        self.assertFalse((self.remote / "calls.jsonl").exists())

    def test_local_retry_without_actions_variables_records_its_own_context(self):
        self.environment.pop("GITHUB_REPOSITORY", None)
        self.environment.pop("GITHUB_RUN_ID", None)
        self.environment["GWFLOW_RELEASE_INTERRUPT_AFTER"] = "gwflow-1.0.0-validation.tar.gz"
        self.publish(success=False, extra=("--local",))
        original = (self.remote / "assets/gwflow-1.0.0-validation.tar.gz").read_bytes()
        del self.environment["GWFLOW_RELEASE_INTERRUPT_AFTER"]
        self.publish(extra=("--local",))
        record = json.loads((self.root / "publication/publication-attempt.json").read_text())
        self.assertEqual(record["verification_context"], "local")
        self.assertIsNone(record["workflow_run"])
        self.assertEqual(record["source_ci_run"], "https://github.com/MOMA-AUH/gwflow/actions/runs/42")
        self.assertEqual(record["source_commit"], self.source)
        self.assertEqual(sum("anaconda" in call for call in self.channel_calls()), 1)
        self.assertEqual((self.remote / "assets/gwflow-1.0.0-validation.tar.gz").read_bytes(), original)
        self.assertEqual(len(list((self.remote / "assets").iterdir())), 3)

    def test_invalid_provenance_fails_before_channel_or_github_operations(self):
        cases = (({}, ("--local", "--source-ci-run", "")),
                 ({}, ("--local", "--source-ci-run", "../42")),
                 ({}, ("--local", "--source-ci-run", "0")),
                 ({}, ("--local", "--repo", "invalid")),
                 ({}, ("--local", "--source", "short")),
                 ({"GITHUB_REPOSITORY": "another/repository"}, ()),
                 ({"GITHUB_RUN_ID": ""}, ()),
                 ({"GITHUB_RUN_ID": "not-a-run"}, ()))
        for environment, extra in cases:
            with self.subTest(environment=environment, extra=extra):
                self.environment.update(GITHUB_REPOSITORY="MOMA-AUH/gwflow", GITHUB_RUN_ID="101")
                self.environment.update(environment)
                self.publish(success=False, extra=extra)
                self.assertEqual(self.channel_calls(), [])
                self.assertFalse((self.channel / "package.conda").exists())
                self.assertFalse((self.remote / "calls.jsonl").exists())

    def test_local_provenance_checks_installed_artifact_before_attaching_assets(self):
        self.environment.pop("GITHUB_REPOSITORY", None)
        self.environment.pop("GITHUB_RUN_ID", None)
        self.environment["GWFLOW_PUBLICATION_FAIL"] = "installed-checksum"
        result = self.publish(success=False, extra=("--local",))
        self.assertIn("does not match the tested artifact filename and SHA256", result.stderr)
        calls = [json.loads(line) for line in (self.remote / "calls.jsonl").read_text().splitlines()]
        self.assertTrue(all(call[0] == "api" for call in calls))
        self.assertFalse((self.root / "publication").exists())

    def test_unmatched_source_ci_fails_before_upload(self):
        self.environment.pop("GITHUB_REPOSITORY", None)
        self.environment.pop("GITHUB_RUN_ID", None)
        for change in ({"head_sha": "b" * 40}, {"conclusion": "failure"}, {"event": "pull_request"},
                       {"head_branch": "feature"}, {"path": ".github/workflows/release.yml"},
                       {"repository": {"full_name": "another/repository"}},
                       {"head_repository": {"full_name": "another/repository"}}, {"id": 99}):
            with self.subTest(change=change):
                self.environment["GWFLOW_SOURCE_CI_CHANGES"] = json.dumps(change)
                self.publish(success=False, extra=("--local",))
                self.assertEqual(self.channel_calls(), [])
                self.assertFalse((self.channel / "package.conda").exists())
                self.assertFalse((self.remote / "state.json").exists())

    def test_retry_after_upload_runs_checks_and_finishes_without_replacing_package(self):
        self.environment["GWFLOW_PUBLICATION_FAIL"] = "smoke"
        failed = self.publish(success=False)
        self.assertIn("injected smoke failure", failed.stderr)
        original = (self.channel / "package.conda").read_bytes()
        del self.environment["GWFLOW_PUBLICATION_FAIL"]
        self.environment["GITHUB_RUN_ID"] = "102"
        self.publish()
        self.assertEqual((self.channel / "package.conda").read_bytes(), original)
        calls = [json.loads(line) for line in (self.channel / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(sum("anaconda" in call for call in calls), 1)
        self.assertEqual(sum("tests/release_smoke.py" in call for call in calls), 2)
        self.assertEqual(sum("tests/release_provenance.py" in call for call in calls), 1)
        self.assertEqual(len(list((self.remote / "assets").iterdir())), 3)

    def test_retry_keeps_prior_archive_and_records_fresh_provenance(self):
        archive = "gwflow-1.0.0-validation.tar.gz"
        self.environment["GWFLOW_RELEASE_INTERRUPT_AFTER"] = archive
        self.publish(success=False)
        original = (self.remote / "assets" / archive).read_bytes()
        del self.environment["GWFLOW_RELEASE_INTERRUPT_AFTER"]
        self.environment["GITHUB_RUN_ID"] = "102"
        self.publish()
        self.assertEqual((self.remote / "assets" / archive).read_bytes(), original)
        current = json.loads((self.root / "publication/publication-attempt.json").read_text())
        self.assertEqual(current["workflow_run"], "https://github.com/MOMA-AUH/gwflow/actions/runs/102")
        self.command("unpack", "--bundle", str(self.root / "publication/assets"),
                     "--tag", "v1.0.0", "--source", self.source, "--output", str(self.root / "restored"))
        previous = json.loads((self.root / "restored/publication.json").read_text())
        self.assertEqual(previous["workflow_run"], "https://github.com/MOMA-AUH/gwflow/actions/runs/101")
        self.assertEqual((self.root / "restored/runtime.explicit.txt").read_bytes(),
                         (self.capture / "runtime.explicit.txt").read_bytes())

    def channel_package(self, data=b"exact tested and qualified gwflow bytes"):
        (self.channel / "package.conda").write_bytes(data)

    def channel_calls(self):
        path = self.channel / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_fresh_publication_uploads_installs_checks_and_attaches(self):
        self.publish()
        calls = self.channel_calls()
        self.assertEqual(sum("anaconda" in call for call in calls), 1)
        installs = [call for call in calls if call[0] == "create"]
        self.assertEqual(len(installs), 1)
        self.assertIn("gwflow=1.0.0=py_0", installs[0])
        self.assertEqual(sum("tests/release_smoke.py" in call for call in calls), 1)
        self.assertEqual(sum("tests/release_provenance.py" in call for call in calls), 1)
        self.assertEqual(len(list((self.remote / "assets").iterdir())), 3)
        record = json.loads((self.root / "publication/publication-attempt.json").read_text())
        self.assertEqual(record["verification_context"], "github-actions")
        self.assertEqual(record["workflow_run"], "https://github.com/MOMA-AUH/gwflow/actions/runs/101")
        self.assertEqual(record["source_ci_run"], "https://github.com/MOMA-AUH/gwflow/actions/runs/42")

    def test_conflicting_downloaded_bytes_are_rejected_despite_matching_metadata(self):
        self.channel_package(b"conflicting remote package")
        result = self.publish(success=False)
        self.assertIn("bytes differ; refusing replacement", result.stderr)
        self.assertEqual(self.channel_calls(), [])
        self.assertEqual((self.channel / "package.conda").read_bytes(), b"conflicting remote package")

    def test_conflicting_identity_is_rejected_before_installation(self):
        self.channel_package()
        for change in ({"version": "0.9.0"}, {"basename": "noarch/other.conda"},
                       {"sha256": "0" * 64}, {"labels": ["testing"]}):
            with self.subTest(change=change):
                self.metadata_changes = change
                self.assertIn("identity differs", self.publish(success=False).stderr)
        self.assertEqual(self.channel_calls(), [])

    def test_lookup_and_download_errors_never_authorize_an_upload(self):
        self.channel_package()
        for lookup, download in ((403, 200), (503, 200), (200, 403), (200, 404), (200, 503)):
            with self.subTest(lookup=lookup, download=download):
                self.lookup_status, self.download_status = lookup, download
                self.publish(success=False)
                self.assertEqual(self.channel_calls(), [])

    def test_upload_race_requires_fresh_downloaded_byte_verification(self):
        self.channel_package()
        self.missing_lookups = 1
        self.publish()
        self.assertEqual(sum("anaconda" in call for call in self.channel_calls()), 1)
        self.assertEqual((self.channel / "package.conda").read_bytes(), b"exact tested and qualified gwflow bytes")

    def test_conflicting_upload_race_is_not_suppressed(self):
        self.channel_package(b"conflict")
        self.missing_lookups = 1
        self.assertIn("bytes differ", self.publish(success=False).stderr)
        self.assertFalse(any(call[0] == "create" for call in self.channel_calls()))
        self.assertEqual((self.channel / "package.conda").read_bytes(), b"conflict")

    def test_failed_installation_or_provenance_can_be_retried(self):
        for failure in ("install", "provenance"):
            with self.subTest(failure=failure):
                self.environment["GWFLOW_PUBLICATION_FAIL"] = failure
                self.publish(success=False)
                self.assertFalse((self.remote / "state.json").exists())
        del self.environment["GWFLOW_PUBLICATION_FAIL"]
        self.publish()
        self.assertEqual(sum("anaconda" in call for call in self.channel_calls()), 1)

    def test_each_partial_attachment_boundary_resumes_without_replacement(self):
        self.environment["GWFLOW_RELEASE_REVERSE_BATCH"] = "1"
        for name in ("gwflow-1.0.0-validation.tar.gz", "SHA256SUMS", self.record["artifact"]["filename"]):
            with self.subTest(name=name):
                self.environment["GWFLOW_RELEASE_INTERRUPT_AFTER"] = name
                self.publish(success=False)
                before = {path.name: path.read_bytes() for path in (self.remote / "assets").iterdir()}
                self.assertIn("gwflow-1.0.0-validation.tar.gz", before)
                del self.environment["GWFLOW_RELEASE_INTERRUPT_AFTER"]
                self.environment["GITHUB_RUN_ID"] = str(int(self.environment["GITHUB_RUN_ID"]) + 1)
                self.publish()
                for filename, data in before.items():
                    self.assertEqual((self.remote / "assets" / filename).read_bytes(), data)
                shutil.rmtree(self.root / "publication")
                shutil.rmtree(self.remote)

    def test_conflicting_draft_asset_is_not_replaced(self):
        self.environment["GWFLOW_RELEASE_INTERRUPT_AFTER"] = "SHA256SUMS"
        self.publish(success=False)
        del self.environment["GWFLOW_RELEASE_INTERRUPT_AFTER"]
        sums = self.remote / "assets/SHA256SUMS"
        sums.write_text("conflicting checksums")
        self.assertIn("refusing replacement", self.publish(success=False).stderr)
        self.assertEqual(sums.read_text(), "conflicting checksums")
        self.assertFalse((self.remote / "assets" / self.record["artifact"]["filename"]).exists())

    def test_published_release_and_github_errors_are_not_mutated(self):
        self.publish()
        shutil.rmtree(self.root / "publication")
        state = self.remote / "state.json"
        record = json.loads(state.read_text())
        record["isDraft"] = False
        state.write_text(json.dumps(record))
        calls = self.remote / "calls.jsonl"
        calls.write_text("")
        self.assertIn("immutable", self.publish(success=False).stderr)
        for failure in ("release view", "release download"):
            record["isDraft"] = True
            state.write_text(json.dumps(record))
            self.environment["GWFLOW_RELEASE_FAIL"] = failure
            self.publish(success=False)
        self.assertNotIn('"upload"', calls.read_text())
        self.assertNotIn('"create"', calls.read_text())

    def test_archive_from_different_ci_evidence_is_rejected_even_when_self_consistent(self):
        self.environment["GWFLOW_RELEASE_INTERRUPT_AFTER"] = "gwflow-1.0.0-validation.tar.gz"
        self.publish(success=False)
        del self.environment["GWFLOW_RELEASE_INTERRUPT_AFTER"]
        # A different qualified capture of the same package/source is not the selected bundle.
        self.evidence["additional_evidence"] = "different qualification"
        self.qualification.write_text(json.dumps(self.evidence))
        self.output = self.root / "different-ci-bundle"
        self.prepare()
        self.assertIn("differs from selected CI bundle", self.publish(success=False).stderr)
        self.assertEqual(len(list((self.remote / "assets").iterdir())), 1)

    def test_matching_legacy_partial_assets_complete_and_conflicting_sums_fail(self):
        self.publish()
        shutil.rmtree(self.root / "publication")
        archive = self.remote / "assets/gwflow-1.0.0-validation.tar.gz"
        original = archive.read_bytes()
        archive.unlink()
        # An older upload order could leave only sums and/or the package.
        self.publish()
        self.assertEqual(archive.read_bytes(), original)
        shutil.rmtree(self.root / "publication")
        archive.unlink()
        self.environment["GITHUB_RUN_ID"] = "103"
        self.assertIn("refusing replacement", self.publish(success=False).stderr)
        self.assertFalse(archive.exists())

    def test_retry_reruns_smoke_even_with_complete_matching_draft(self):
        self.publish()
        before = {path.name: path.read_bytes() for path in (self.remote / "assets").iterdir()}
        shutil.rmtree(self.root / "publication")
        self.environment["GWFLOW_PUBLICATION_FAIL"] = "smoke"
        self.publish(success=False)
        self.assertEqual({path.name: path.read_bytes() for path in (self.remote / "assets").iterdir()}, before)
        del self.environment["GWFLOW_PUBLICATION_FAIL"]
        self.environment["GITHUB_RUN_ID"] = "104"
        self.publish()
        self.assertEqual(sum("tests/release_smoke.py" in call for call in self.channel_calls()), 3)

    def test_bundle_integrity_failure_prevents_all_remote_operations(self):
        (self.output / "runtime.explicit.txt").write_text("changed")
        self.publish(success=False)
        self.assertEqual(self.channel_calls(), [])
        self.assertFalse((self.remote / "calls.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
