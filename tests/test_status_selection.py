"""Visible-name resolution and progressive views through the installed CLI."""

import re

from support import LocalBackendTestCase
import test_grouped_status


class StatusSelectionTests(LocalBackendTestCase):
    configure_workflow = test_grouped_status.GroupedStatusTests.configure_workflow
    inject = test_grouped_status.GroupedStatusTests.inject
    snapshot = test_grouped_status.GroupedStatusTests.snapshot

    def names(self, output):
        return re.findall(r"(?m)^Task (\S+)\s+", output)

    def append_workflow(self, source):
        path = self.work / "workflow.py"
        path.write_text(path.read_text() + source)

    def test_exact_groups_and_namespaces_do_not_expand_other_aliases(self):
        for selector in ("mapping", "mapping@library.mapping"):
            output = self.cli("status", selector)
            self.assertEqual(self.names(output), ["mapping__A", "mapping__C", "mapping"])
            self.assertNotIn("[preparation]", output)
        for selector, expected in (("remapping", "remapping__B"),
                                   ("task:mapping", "mapping"),
                                   ("mapping__A", "mapping__A"),
                                   ("job:mapping__A__compute", "mapping__A"),
                                   ("mapping__A__gwflow_prepare", "mapping__A")):
            with self.subTest(selector=selector):
                output = self.cli("status", selector)
                self.assertEqual(self.names(output), [expected])
                self.assertRegex(output, rf"Task {expected}\s+pending\s+0/3")
                self.assertEqual("[preparation]" in output, selector != "remapping")

    def test_exact_precedence_and_namespace_overrides_with_real_overlaps(self):
        self.append_workflow("gwf.task(mapping(), key='A__compute')\n")
        # A Task can have the same name as another Task's public job.
        self.assertEqual(self.names(self.cli("status", "mapping__A__compute")),
                         ["mapping__A__compute"])
        self.assertEqual(self.names(self.cli("status", "job:mapping__A__compute")), ["mapping__A"])
        self.append_workflow("gwf.task(mapping(), alias='mapping__A__compute', key='Z')\n")
        for flags in ((), ("--details",), ("--instances",)):
            self.assertEqual(self.names(self.cli("status", "mapping__A__compute", *flags)),
                             ["mapping__A__compute__Z"])
        self.assertEqual(self.names(self.cli("status", "task:mapping__A__compute")),
                         ["mapping__A__compute"])
        output = self.cli("status", "mapping__A__compute*", "--instances")
        self.assertEqual(self.names(output), ["mapping__A", "mapping__A__compute", "mapping__A__compute__Z"])
        self.assertEqual(self.names(self.cli("status", "task:mapping__A__compute*", "--instances")),
                         ["mapping__A__compute", "mapping__A__compute__Z"])

    def test_ambiguity_is_resolved_before_filters_without_task_or_job_fallback(self):
        (self.work / "second.py").write_text((self.work / "library.py").read_text())
        self.append_workflow("from second import mapping as other\n"
                             "gwf.task(other(), key='D')\n")
        for options in ((), ("--status", "failed"), ("--endpoints",),
                        ("--group", "absent*"), ("--instances",), ("--details",)):
            output = self.cli("status", "mapping", *options, success=False)
            self.assertIn("Ambiguous", output)
            self.assertIn("mapping@library.mapping", output)
            self.assertIn("mapping@second.mapping", output)
        self.assertEqual(self.names(self.cli("status", "task:mapping")), ["mapping"])
        self.assertEqual(self.names(self.cli("status", "mapping@second.mapping")), ["mapping__D"])
        self.append_workflow("gwf.task(mapping(), alias='mapping__A__compute', key='E')\n"
                             "gwf.task(other(), alias='mapping__A__compute', key='F')\n")
        self.assertIn("Ambiguous", self.cli("status", "mapping__A__compute", success=False))
        self.assertEqual(self.names(self.cli("status", "job:mapping__A__compute")), ["mapping__A"])

    def test_globs_only_match_visible_names_case_sensitively(self):
        for pattern in ("mapping@library.*", "Mapping*", "task:Mapping*", "job:Mapping*", "absent*"):
            output = self.cli("status", pattern)
            self.assertIn("0 of 4 Tasks selected", output)
            self.assertIn("No Tasks selected", output)
        self.assertEqual(self.names(self.cli("status", "task:mapping*", "--instances")),
                         ["mapping__A", "mapping__C", "mapping"])
        self.assertEqual(self.names(self.cli("status", "job:mapping__?__compute", "--instances")),
                         ["mapping__A", "mapping__C"])
        (self.work / "second.py").write_text((self.work / "library.py").read_text())
        self.append_workflow("from second import mapping as other\ngwf.task(other(), key='D')\n")
        output = self.cli("status", "mapping@library.*")
        self.assertIn("3 of 5 Tasks selected", output)
        self.assertRegex(output, r"(?m)^mapping@library.mapping\s+0/3")
        self.assertNotIn("mapping@second.mapping", output)

    def test_unknown_exact_selectors_reject_the_whole_invocation(self):
        for selector in ("absent", "task:absent", "job:absent", "task:", "job:",
                         "group:mapping", "mapping@old.mapping"):
            for flags in ((), ("--status", "failed")):
                with self.subTest(selector=selector, flags=flags):
                    output = self.cli("status", "mapping", selector, *flags, success=False)
                    self.assertIn(selector, output)
                    self.assertIn("Unknown", output)
                    self.assertNotIn("Tasks selected", output)
        output = self.cli("status", "mapping", "--status", "failed")
        self.assertIn("0 of 4 Tasks selected", output)
        self.assertIn("No Tasks selected", output)

    def test_complete_view_precedence_and_deduplicated_unions(self):
        cases = (
            ((), "overview", 4),
            (("--status", "pending"), "overview", 4),
            (("mapping",), "instances", 3),
            (("mapping", "remapping__B"), "instances", 4),
            (("mapping", "job:remapping__B__compute"), "instances", 4),
            (("mapping__A", "job:mapping__A__compute", "task:mapping__A"), "details", 1),
            (("task:mapping__A",), "details", 1),
            (("mapping__A", "absent*"), "overview", 1),
            (("mapping", "remapping*"), "overview", 4),
            (("task:mapping__[A]",), "overview", 1),
            (("job:mapping__A__*",), "overview", 1),
            (("mapping__A", "--instances"), "instances", 1),
            (("mapping*", "--instances"), "instances", 3),
            (("mapping", "--details"), "details", 3),
            (("mapping*", "--instances", "--details"), "details", 3),
        )
        for args, view, count in cases:
            with self.subTest(args=args):
                output = self.cli("status", *args)
                self.assertIn(f"{count} of 4 Tasks selected", output)
                self.assertEqual("Reusable" in output, view == "overview")
                self.assertEqual(len(self.names(output)), 0 if view == "overview" else count)
                self.assertEqual(output.count("[preparation]"), count if view == "details" else 0)

    def test_filters_intersect_resolved_membership_and_keep_whole_owner_progress(self):
        self.append_workflow("definition = mapping([a.outputs['result']])\n"
                             "definition.targets['compute'].group = 'report'\n"
                             "gwf.task(definition, alias='consumer')\n")
        output = self.cli("status", "mapping", "--endpoints")
        self.assertEqual(self.names(output), ["mapping__C", "mapping"])
        output = self.cli("status", "mapping*", "--endpoints")
        self.assertRegex(output, r"(?m)^mapping\s+0/2\s+2 pending$")
        output = self.cli("status", "*", "--endpoints", "--group", "report", "--status", "pending")
        self.assertIn("1 of 5 Tasks selected", output)
        self.assertRegex(output, r"(?m)^consumer\s+0/1\s+1 pending$")
        output = self.cli("status", "job:consumer__compute", "--group", "report", "--status", "pending")
        self.assertRegex(output, r"Task consumer\s+pending\s+0/3")
        self.assertEqual(output.count("[preparation]"), 1)
        self.assertIn("No Tasks selected", self.cli("status", "mapping", "--group", "report"))
        self.cli("status", "*", "--status", "active", success=False)

    def test_selection_and_views_do_not_add_observations_or_change_evidence(self):
        self.run_complete()
        environment = self.inject(running_prefix="mapping__A__compute", record_status=True)
        record = self.work / "backend-observations.jsonl"
        before = self.snapshot()
        baseline = None
        for args in ((), ("mapping",), ("task:mapping__A",), ("job:mapping__A__compute",),
                     ("mapping*", "--instances"), ("mapping", "--details"),
                     ("*", "--status", "running"), ("absent*",)):
            record.unlink(missing_ok=True)
            self.cli("-b", "recovery_fixture", "status", *args, env=environment)
            observed = record.read_text().splitlines()
            if baseline is None:
                baseline = observed
            self.assertEqual(observed, baseline)
            self.assertEqual(self.snapshot(), before)
