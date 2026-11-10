"""Keep the README's default installation on the published Conda package."""

from pathlib import Path
import re
import shlex
import unittest


class ReadmeInstallationTests(unittest.TestCase):
    def test_recommended_install_uses_moma_auh_conda_package(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        install = readme.split("## Install\n", 1)[1].split("\n## ", 1)[0]
        blocks = re.findall(r"```(?:sh|bash)\n(.*?)```", install, re.DOTALL)
        self.assertTrue(blocks, "Document the recommended installation command")
        commands = [shlex.split(line) for line in blocks[0].replace("\\\n", " ").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        first = commands[0]
        self.assertEqual(first[:2], ["conda", "create"],
                         "The first install command must create a Conda package environment")
        self.assertIn("--override-channels", first)
        channels = [first[index + 1] for index, token in enumerate(first) if token in ("-c", "--channel")]
        self.assertEqual(channels, ["MOMA-AUH", "gwforg", "conda-forge"])
        self.assertIn("gwflow", first, "Install the released gwflow package")
        self.assertIn(["conda", "activate", "gwflow"], commands)
        self.assertNotIn("pip install", install, "Keep source installation in the developer guide")
        self.assertNotIn("conda env create", install, "Do not replace the published package with a checkout environment")


if __name__ == "__main__":
    unittest.main()
