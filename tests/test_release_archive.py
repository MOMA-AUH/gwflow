"""Release archives preserve records and reject unsafe or mismatched input."""

import gzip
import hashlib
import io
import json
import tarfile
import unittest

import test_release_artifacts as fixtures
from release_artifacts import MAX_METADATA_BYTES


class ReleaseArchiveTests(unittest.TestCase):
    setUp = fixtures.ReleaseArtifactsTests.setUp
    fixture = fixtures.ReleaseArtifactsTests.fixture
    command = fixtures.ReleaseArtifactsTests.command
    prepare = fixtures.ReleaseArtifactsTests.prepare
    publication = fixtures.ReleaseArtifactsTests.publication
    packed = fixtures.ReleaseArtifactsTests.packed

    def compact(self):
        self.prepare()
        self.publication()
        return self.packed()

    def unpack(self, assets, *, success=True):
        return self.command('unpack', '--bundle', str(assets), '--output', str(self.root / 'unpacked'),
                            '--tag', 'v1.0.0', '--source', self.source, success=success)

    def replace_archive(self, assets, entries):
        archive = assets / 'gwflow-1.0.0-validation.tar.gz'
        with tarfile.open(archive, 'w:gz') as output:
            for entry, data in entries:
                output.addfile(entry, io.BytesIO(data) if data is not None else None)
        (assets / 'SHA256SUMS').write_text(''.join(
            f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n'
            for path in sorted(assets.iterdir()) if path.name != 'SHA256SUMS'))

    def entries(self, assets):
        with tarfile.open(assets / 'gwflow-1.0.0-validation.tar.gz', 'r:gz') as archive:
            return [(entry, archive.extractfile(entry).read()) for entry in archive]

    def test_round_trip_preserves_every_original_byte_and_is_deterministic(self):
        assets = self.compact()
        self.unpack(assets)
        for path in self.output.iterdir():
            self.assertEqual((self.root / 'unpacked' / path.name).read_bytes(), path.read_bytes())
        original = {path.name: path.read_bytes() for path in assets.iterdir()}
        self.packed()
        self.assertEqual({path.name: path.read_bytes() for path in assets.iterdir()}, original)
        self.assertEqual(len(original), 3)

    def test_package_and_archive_corruption_fail_before_extraction(self):
        assets = self.compact()
        for filename in (self.record['artifact']['filename'], 'gwflow-1.0.0-validation.tar.gz'):
            with self.subTest(filename=filename):
                path = assets / filename
                original = path.read_bytes()
                path.write_bytes(b'corrupt')
                self.unpack(assets, success=False)
                self.assertFalse((self.root / 'unpacked').exists())
                path.write_bytes(original)

    def test_unsafe_duplicate_missing_and_oversized_members_are_rejected(self):
        assets = self.compact()
        entries = self.entries(assets)
        attacks = []
        for name, kind in (('../outside', tarfile.REGTYPE), ('/tmp/outside', tarfile.REGTYPE),
                           ('runtime.json', tarfile.SYMTYPE), ('runtime.json', tarfile.LNKTYPE),
                           ('runtime.json', tarfile.DIRTYPE)):
            member = tarfile.TarInfo(name)
            member.type, member.linkname = kind, '../outside'
            attacks.append([(member, None)] + entries[1:])
        member = tarfile.TarInfo('runtime.json')
        member.size = MAX_METADATA_BYTES + 1
        # A sparse header with a declared oversize is sufficient: reject before reading.
        for index, attack in enumerate(attacks + [entries + [entries[0]], entries[:-1]]):
            with self.subTest(attack=index):
                self.replace_archive(assets, attack)
                self.unpack(assets, success=False)
                self.assertFalse((self.root / 'unpacked').exists())
                self.assertFalse((self.root / 'outside').exists())
        archive = assets / 'gwflow-1.0.0-validation.tar.gz'
        archive.write_bytes(gzip.compress(member.tobuf() + b'\0' * 1024))
        (assets / 'SHA256SUMS').write_text(''.join(
            f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n'
            for path in sorted(assets.iterdir()) if path.name != 'SHA256SUMS'))
        self.unpack(assets, success=False)
        self.assertFalse((self.root / 'unpacked').exists())

    def test_outer_hashes_do_not_replace_internal_identity_checks(self):
        assets = self.compact()
        entries = self.entries(assets)
        for filename in ('runtime.explicit.txt', 'publication.json'):
            with self.subTest(filename=filename):
                changed = []
                for member, data in entries:
                    member = tarfile.TarInfo(member.name)
                    if member.name == filename:
                        data = (b'changed manifest' if filename.endswith('.txt') else
                                json.dumps({'release': 'v1.0.0', 'source_commit': self.source,
                                            'artifact': {'filename': 'wrong', 'sha256': '0' * 64}}).encode())
                    member.size = len(data)
                    changed.append((member, data))
                self.replace_archive(assets, changed)
                self.unpack(assets, success=False)
                self.assertFalse((self.root / 'unpacked').exists())

    def test_mismatched_tag_source_and_duplicate_checksums_are_rejected(self):
        assets = self.compact()
        for tag, source in (('v1.1.0', self.source), ('v1.0.0', 'b' * 40)):
            self.command('unpack', '--bundle', str(assets), '--output', str(self.root / 'unpacked'),
                         '--tag', tag, '--source', source, success=False)
        sums = assets / 'SHA256SUMS'
        sums.write_text(sums.read_text() * 2)
        self.unpack(assets, success=False)
        self.assertFalse((self.root / 'unpacked').exists())
