"""Static status and discovery through the installed public CLI."""

import re
import shlex

from support import LocalBackendTestCase
import test_grouped_status


class StaticStatusTests(LocalBackendTestCase):
    configure_workflow = test_grouped_status.GroupedStatusTests.configure_workflow
    inject = test_grouped_status.GroupedStatusTests.inject
    settle = test_grouped_status.GroupedStatusTests.settle

    def test_explicit_static_modes_preserve_entry_views_and_plain_wins(self):
        for view in ((), ("*",), ("--details",)):
            with self.subTest(view=view):
                expected = self.cli("status", *view)
                self.assertEqual(self.cli("status", *view, "--static"), expected)
                terminal = self.terminal_cli("status", *view, "--static", "--plain").stdout
                self.assertNotRegex(terminal, r"\x1b\[|[━─]")
                self.assertEqual("Reusable" in terminal, not view)
                self.assertEqual(terminal.count("[preparation]"), 4 if "--details" in view else 0)
                self.assertIn("0/3", terminal)

    def test_bars_preserve_counts_unknown_reasons_and_terminal_fallbacks(self):
        for width in (24, 40, 80, 120):
            for view in ((), ("*",), ("--details",)):
                with self.subTest(width=width, view=view):
                    output = self.terminal_cli("status", *view, "--static", width=width).stdout
                    self.assertRegex(output, r"0/3\s+\[─+\]")
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace(
            "a = gwf.task(mapping(), key='A')",
            "definition = mapping()\ndefinition.targets['compute'].image = 'missing.sif'\n"
            "a = gwf.task(definition, key='A')"))
        output = self.terminal_cli("status", "mapping__A", "--static", width=40).stdout
        output = re.sub(r"\x1b\[[0-9;]*m", "", output)
        self.assertRegex(output, r"\?/3\s+\[/+\]")
        self.assertIn("image unavailable", output)
        self.assertIn("progress not assessed", " ".join(output.split()))
        for options, environment in ((("--plain",), {}), ((), {"TERM": "dumb"}),
                                     ((), {"PYTHONIOENCODING": "ascii"})):
            output = self.terminal_cli("status", "mapping__A", "--static", *options, env=environment).stdout
            self.assertNotRegex(output, r"\x1b\[|[━─]|\[/+\]")
            self.assertIn("?/3", output)
            self.assertIn("image unavailable", output)

    def test_filtered_groups_keep_selected_membership_and_separate_match_counts(self):
        (self.work / "workflow.py").write_text(
            "from gwflow import Workflow\nfrom library import mapping\ngwf = Workflow()\n"
            "for index in range(24):\n"
            "    definition = mapping()\n"
            "    if index >= 18: definition.targets['compute'].spec = 'exit 8'\n"
            "    gwf.task(definition, key=str(index))\n")
        self.cli("run")
        self.settle()
        environment = self.inject(job_states={
            **{f"mapping__{index}__compute": "RUNNING" for index in range(14, 18)},
            **{f"mapping__{index}__compute": "FAILED" for index in range(18, 24)},
        })
        for terminal in (False, True):
            result = (self.terminal_cli if terminal else self.cli_result)(
                "-b", "recovery_fixture", "status", "--static", "--status", "failed", env=environment)
            output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
            self.assertIn("14/24", output)
            self.assertIn("4 active", output)
            self.assertIn("6 failed", output)
            self.assertIn("6 Tasks match the filter", output)
            self.assertIn("Showing 6 of 24 selected Tasks", output)
        output = self.terminal_cli("-b", "recovery_fixture", "status", "*", "--static", env=environment).stdout
        output = re.sub(r"\x1b\[[0-9;]*m", "", output)
        self.assertRegex(output, r"reusable\s+3/3 \[━+\]")
        self.assertRegex(output, r"running\s+2/3 \[━+─+\]")
        self.assertRegex(output, r"failed\s+1/3 \[━+─+\]")

    def test_group_discovery_offers_only_exact_patterns_and_keeps_context(self):
        overview = self.cli("status", "--static")
        self.assertIn("Groups summarize Tasks; selectors match Task names", overview)
        self.assertIn("gwflow status '*' --static", overview)
        output = self.cli("--file", "workflow.py:gwf", "--backend", "local",
                          "status", "remapping", success=False)
        self.assertIn("display label", output)
        self.assertIn("remapping__B", output)
        command = "gwflow --file workflow.py:gwf --backend local status 'remapping__*' --static"
        self.assertIn(command, output)
        self.assertIn("remapping__B", self.cli(*shlex.split(command)[1:]))
        # A real Task wins over a matching display label.
        self.assertNotIn("mapping__A", self.cli("status", "mapping"))
        # A broad prefix would accidentally select a different implementation.
        (self.work / "second.py").write_text((self.work / "library.py").read_text())
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text() +
                            "from second import mapping as other\n"
                            "gwf.task(other(), alias='remapping', key='X')\n")
        output = self.cli("status", "remapping@library.mapping", "absent*", success=False)
        self.assertIn("remapping__B", output)
        self.assertIn("'absent*'", output)
        self.assertNotIn("'remapping__*'", output)
        self.assertIn("gwflow status '*' --static", output)

    def test_group_discovery_bounds_examples(self):
        (self.work / "workflow.py").write_text(
            "from gwflow import Workflow\nfrom library import mapping\ngwf = Workflow()\n"
            "for index in range(10): gwf.task(mapping(), key=str(index))\n")
        output = self.cli("status", "mapping", success=False)
        self.assertIn("mapping__0", output)
        self.assertIn("mapping__4", output)
        self.assertNotIn("mapping__5", output)
        self.assertIn("5 more", output)
