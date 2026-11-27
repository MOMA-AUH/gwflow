"""Exercise the release version check used before preparation and qualification."""

from pathlib import Path
import subprocess
import sys
import unittest

from check_release_version import check_release_version


class CheckReleaseVersionTests(unittest.TestCase):
    def test_accepts_one_increment_and_resets_lower_components(self):
        for latest, requested in (
            ("v1.2.3", "v1.2.4"), ("v1.2.3", "v1.3.0"), ("v1.2.3", "v2.0.0"),
            ("v0.12.9", "v0.12.10"), ("v0.12.9", "v1.0.0"),
        ):
            with self.subTest(latest=latest, requested=requested):
                check_release_version(latest, requested)

    def test_rejects_skips_downgrades_and_malformed_tags(self):
        for requested in (
            "v1.3.1", "v1.4.0", "v1.2.2", "v.1.2.2", "v1.2.3", "v1.2.5",
            "v2.1.0", "v2.0.1", "v3.0.0", "v01.3.0", "v1.03.0", "v1.2.04",
            "v1.2.4.0", "v1.2.4x", "1.3.0",
        ):
            with self.subTest(requested=requested), self.assertRaises(ValueError):
                check_release_version("v1.2.3", requested)

    def test_prereleases_must_target_an_allowed_version(self):
        for requested in ("v1.2.4.dev1", "v1.3.0rc1", "v2.0.0b2", "v2.0.0a1.dev2"):
            with self.subTest(requested=requested):
                check_release_version("v1.2.3", requested)
        with self.assertRaises(ValueError):
            check_release_version("v1.2.3", "v1.3.1rc1")

    def test_requires_a_canonical_final_baseline(self):
        for latest in ("", "v1.02.3", "v1.2.3rc1"):
            with self.subTest(latest=latest), self.assertRaises(ValueError):
                check_release_version(latest, "v1.2.4")

    def test_command_reports_the_allowed_versions_on_rejection(self):
        script = Path(__file__).with_name("check_release_version.py")
        result = subprocess.run([sys.executable, script, "v1.2.3", "v1.3.1"],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        for version in ("v1.2.3", "v1.2.4", "v1.3.0", "v2.0.0"):
            self.assertIn(version, result.stderr)
