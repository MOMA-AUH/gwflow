"""Published-release migration verifies copies before removing loose metadata."""

import json
import shutil
import subprocess
import sys
import unittest

import test_release_artifacts as fixtures


class ReleaseMigrationTests(unittest.TestCase):
    fixture = fixtures.ReleaseArtifactsTests.fixture
    command = fixtures.ReleaseArtifactsTests.command
    prepare = fixtures.ReleaseArtifactsTests.prepare
    publication = fixtures.ReleaseArtifactsTests.publication

    def setUp(self):
        fixtures.ReleaseArtifactsTests.setUp(self)
        self.version = '0.10.0'
        self.fixture()
        self.prepare()
        self.publication()
        self.remote.mkdir()
        shutil.copytree(self.output, self.remote / 'assets')
        self.state = {'tagName': 'v0.10.0', 'isDraft': False, 'isPrerelease': False, 'immutable': False,
                      'body': 'Original release notes'}
        self.save_state()
        self.notes = self.root / 'notes.md'
        self.notes.write_text('Release notes with the validation archive link.\n')
        self.backup = self.root / 'backup'
        self.package = self.record['artifact']['filename']
        self.original = {path.name: path.read_bytes() for path in (self.remote / 'assets').iterdir()}

    def save_state(self):
        (self.remote / 'state.json').write_text(json.dumps(self.state))

    def migrate(self, *arguments, success=True):
        result = subprocess.run([
            sys.executable, str(fixtures.ROOT / 'tests/migrate_release_assets.py'),
            '--repo', 'MOMA-AUH/gwflow', '--tag', 'v' + self.version, '--source', self.source,
            '--backup', str(self.backup), '--notes-file', str(self.notes), '--gh', str(self.github),
            *arguments], capture_output=True, text=True, env=self.environment, timeout=30)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def calls(self):
        return [json.loads(line) for line in (self.remote / 'calls.jsonl').read_text().splitlines()]

    def assert_no_mutations(self):
        self.assertFalse(any(call[1] in ('upload', 'delete-asset', 'edit', 'create') for call in self.calls()))

    def test_preview_then_apply_preserves_original_bytes_and_is_repeatable(self):
        self.migrate()
        self.assert_no_mutations()
        for name, data in self.original.items():
            self.assertEqual((self.backup / 'original-assets' / name).read_bytes(), data)
        self.migrate('--apply')
        self.assertEqual({p.name for p in (self.remote / 'assets').iterdir()},
                         {self.package, 'SHA256SUMS', 'gwflow-0.10.0-validation.tar.gz'})
        self.assertEqual((self.remote / 'assets' / self.package).read_bytes(), self.original[self.package])
        self.assertEqual(json.loads((self.remote / 'state.json').read_text())['body'], self.notes.read_text())
        calls = self.calls()
        deleted = next(i for i, call in enumerate(calls) if call[1] == 'delete-asset')
        self.assertTrue(any(call[1] == 'edit' for call in calls[:deleted]))
        self.assertTrue(any(call[1] == 'download' and 'gwflow-0.10.0-validation.tar.gz' in call
                            for call in calls[:deleted]))
        self.assertFalse(any(call[1] == 'upload' and any(arg.endswith(self.package) for arg in call) for call in calls))
        self.migrate('--apply')
        self.assertEqual((self.remote / 'assets' / self.package).read_bytes(), self.original[self.package])

    def test_upload_or_notes_failure_keeps_old_metadata_and_can_resume(self):
        for failure in ('release upload', 'release edit', 'release delete-asset'):
            with self.subTest(failure=failure):
                self.environment['GWFLOW_RELEASE_FAIL'] = failure
                self.migrate('--apply', success=False)
                for name in self.original:
                    self.assertTrue((self.remote / 'assets' / name).exists())
        del self.environment['GWFLOW_RELEASE_FAIL']
        # Simulate an interrupted deletion after one metadata asset was removed.
        (self.remote / 'assets/runtime.json').unlink()
        self.migrate('--apply')
        self.assertEqual(len(list((self.remote / 'assets').iterdir())), 3)

    def test_remote_changes_after_preview_are_not_overwritten(self):
        self.migrate()
        (self.remote / 'calls.jsonl').write_text('')
        (self.remote / 'assets/publication.json').write_text('unexpected replacement')
        result = self.migrate('--apply', success=False)
        self.assertIn('Published asset changed', result.stderr)
        self.assert_no_mutations()

    def test_immutable_draft_prerelease_and_stable_releases_are_refused(self):
        for change in ({'immutable': True}, {'isDraft': True}, {'isPrerelease': True}):
            with self.subTest(change=change):
                (self.remote / 'state.json').write_text(json.dumps({**self.state, **change}))
                self.migrate('--apply', success=False)
                self.assert_no_mutations()
        self.save_state()
        self.version = '1.0.0'
        self.migrate('--apply', success=False)
        self.assert_no_mutations()

    def test_notes_with_removed_links_are_refused_before_mutation(self):
        self.notes.write_text('https://github.com/MOMA-AUH/gwflow/releases/download/v0.10.0/publication.json')
        self.migrate('--apply', success=False)
        self.assert_no_mutations()
