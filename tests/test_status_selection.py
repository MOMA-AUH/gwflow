"""Strict Task selection and status views through the installed CLI."""

import json
import re

from support import LocalBackendTestCase
import test_grouped_status


class StatusSelectionTests(LocalBackendTestCase):
    configure_workflow = test_grouped_status.GroupedStatusTests.configure_workflow
    inject = test_grouped_status.GroupedStatusTests.inject
    snapshot = test_grouped_status.GroupedStatusTests.snapshot

    def names(self, output):
        return re.findall(r"(?m)^(\S+)[ \t]+\w+[ \t]+[?\d]+/\d+", output)

    def append_workflow(self, source):
        path = self.work / "workflow.py"
        path.write_text(path.read_text() + source)

    def test_exact_names_select_tasks_without_expanding_groups_or_jobs(self):
        for selector in ("mapping", "mapping__A", "remapping__B"):
            with self.subTest(selector=selector):
                output = self.cli("status", selector)
                self.assertEqual(self.names(output), [selector])
                self.assertRegex(output, rf"(?m)^{selector}\s+pending\s+0/3")
                self.assertNotIn("[preparation]", output)
        for selector in ("remapping", "mapping@library.mapping", "task:mapping",
                         "job:mapping__A__compute", "mapping__A__gwflow_prepare"):
            output = self.cli("status", selector, success=False)
            self.assertIn("Unmatched Task selectors", output)
            self.assertIn(repr(selector), output)

    def test_names_that_overlap_groups_or_public_jobs_still_select_only_tasks(self):
        self.append_workflow("gwf.task(mapping(), key='A__compute')\n"
                             "gwf.task(mapping(), alias='mapping__A__compute', key='Z')\n")
        for options in ((), ("--details",)):
            self.assertEqual(self.names(self.cli("status", "mapping__A__compute", *options)),
                             ["mapping__A__compute"])
        self.assertEqual(self.names(self.cli("status", "mapping__A__compute*")),
                         ["mapping__A__compute", "mapping__A__compute__Z"])
        self.cli("status", "job:mapping__A__compute", success=False)

    def test_qualified_group_labels_are_display_only(self):
        (self.work / "second.py").write_text((self.work / "library.py").read_text())
        self.append_workflow("from second import mapping as other\ngwf.task(other(), key='D')\n")
        overview = self.cli("status")
        self.assertIn("mapping@library.mapping", overview)
        self.assertIn("mapping@second.mapping", overview)
        self.assertEqual(self.names(self.cli("status", "mapping")), ["mapping"])
        self.assertEqual(self.names(self.cli("status", "mapping__D")), ["mapping__D"])
        for selector in ("mapping@library.mapping", "mapping@second.mapping", "mapping@*"):
            for options in ((), ("--status", "failed")):
                self.assertIn(repr(selector), self.cli("status", selector, *options, success=False))

    def test_globs_match_task_names_case_sensitively(self):
        for pattern in ("mapping@library.*", "Mapping*", "task:Mapping*", "job:Mapping*", "absent*"):
            self.assertIn("Unmatched Task selectors", self.cli("status", pattern, success=False))
        self.assertEqual(self.names(self.cli("status", "mapping*")), ["mapping__A", "mapping__C", "mapping"])
        self.assertEqual(self.names(self.cli("status", "mapping__[AC]")), ["mapping__A", "mapping__C"])
        self.assertEqual(self.names(self.cli("status", "?emapping__?")), ["remapping__B"])

    def test_all_unmatched_selectors_reject_before_state_filtering_and_changes(self):
        before = self.snapshot()
        unmatched = ("absent", "missing*", "task:mapping", "job:mapping__A__compute", "remapping")
        for options in ((), ("--status", "failed")):
            output = self.cli("status", "mapping", *unmatched, *options, success=False)
            self.assertIn("Unmatched Task selectors", output)
            for selector in unmatched:
                self.assertIn(repr(selector), output)
            self.assertNotIn("Tasks selected", output)
            self.assertEqual(self.snapshot(), before)
        output = self.cli("status", "mapping", "--status", "failed")
        self.assertIn("0 of 4 Tasks selected", output)
        self.assertIn("No Tasks selected", output)

    def test_overview_flat_rows_details_and_deduplicated_unions(self):
        cases = (
            ((), "overview", 4),
            (("--status", "pending"), "overview", 4),
            (("mapping",), "rows", 1),
            (("mapping", "remapping__B"), "rows", 2),
            (("mapping__A", "mapping__A", "mapping__[AC]"), "rows", 2),
            (("*",), "rows", 4),
            (("mapping*",), "rows", 3),
            (("--details",), "details", 4),
            (("mapping", "--details"), "details", 1),
            (("mapping*", "--details"), "details", 3),
        )
        for arguments, view, count in cases:
            with self.subTest(arguments=arguments):
                output = self.cli("status", *arguments)
                self.assertIn(f"{count} of 4 Tasks selected", output)
                self.assertEqual("Reusable" in output, view == "overview")
                self.assertEqual(len(self.names(output)), 0 if view == "overview" else count)
                self.assertEqual(output.count("[preparation]"), count if view == "details" else 0)

    def test_state_filters_keep_selected_task_progress_without_selecting_upstream(self):
        self.append_workflow("definition = mapping([a.outputs['result']])\n"
                             "definition.targets['compute'].group = 'report'\n"
                             "gwf.task(definition, alias='consumer')\n")
        for options in ((), ("--details",)):
            output = self.cli("status", "consumer", "--status", "pending", *options)
            self.assertEqual(self.names(output), ["consumer"])
            self.assertIn("1 of 5 Tasks selected", output)
            self.assertRegex(output, r"(?m)^consumer\s+pending\s+0/3")
            self.assertEqual(output.count("[preparation]"), 1 if options else 0)
        self.assertIn("No Tasks selected", self.cli("status", "consumer", "--status", "failed"))
        self.cli("status", "*", "--status", "active", success=False)
        for options in (("--instances",), ("--endpoints",), ("--group", "report"), ("--format", "tree")):
            self.assertIn("No such option", self.cli("status", *options, success=False))

    def test_observations_follow_selection_and_views_preserve_evidence(self):
        self.run_complete()
        environment = self.inject(running_prefix="mapping__A__compute", record_status=True)
        record = self.work / "backend-observations.jsonl"
        before = self.snapshot()
        all_names = {"mapping__A", "mapping__C", "mapping", "remapping__B"}
        for arguments, names in (((), all_names), (("mapping",), {"mapping"}),
                                 (("mapping__A",), {"mapping__A"}),
                                 (("mapping*", "--details"), all_names - {"remapping__B"}),
                                 (("*", "--status", "running"), all_names)):
            record.unlink(missing_ok=True)
            self.cli("-b", "recovery_fixture", "status", *arguments, env=environment)
            observed = [json.loads(line) for line in record.read_text().splitlines()]
            self.assertEqual({job.rsplit("__", 3)[0] for job in observed}, names)
            self.assertEqual(len(observed), len(names) * 3)
            self.assertEqual(self.snapshot(), before)
        record.unlink()
        self.cli("-b", "recovery_fixture", "status", "mapping", "absent*", env=environment, success=False)
        self.assertFalse(record.exists())
        self.assertEqual(self.snapshot(), before)

    def test_recorded_tasks_removed_from_declarations_are_not_selectable(self):
        self.run_complete()
        workflow = self.work / "workflow.py"
        workflow.write_text(workflow.read_text().replace("gwf.task(mapping())\n", ""))
        before = self.snapshot()
        self.assertIn("Unmatched Task selectors", self.cli("status", "mapping", success=False))
        self.assertEqual(self.names(self.cli("status", "mapping*")), ["mapping__A", "mapping__C"])
        self.assertEqual(self.snapshot(), before)
