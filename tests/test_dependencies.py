"""Whole-task ordering and workflow extension through the real local backend."""

from collections import Counter
import json
import os
import re
import shutil
import unittest

import test_reuse


class TaskDependencyCliTests(test_reuse.LocalBackendTestCase):
    def submissions(self, output, prefix="Would submit"):
        return set(re.findall(rf"{prefix} (\w+)", output))

    def task_block(self, output, name):
        return output.split(f"Task {name}\n", 1)[1].split("\nTask ", 1)[0].split(
            "\nOrdinary target ", 1,
        )[0]

    def configure_workflow(self, *, extend=False, gated=False, suffix="", report=False):
        shutil.copy(test_reuse.FIXTURES / "dependent_tasks.py", self.work)
        (self.work / "definition.json").write_text(json.dumps({
            "extend": extend, "gated": gated, "suffix": suffix, "report": report,
        }))
        (self.work / "workflow.py").write_text(
            "import json\n"
            "from pathlib import Path\n"
            "from gwflow import Workflow\n"
            "import dependent_tasks as tasks\n"
            "options = json.loads(Path('definition.json').read_text())\n"
            "gwf = Workflow()\n"
            "if options['report']:\n"
            "    gwf.task_from_template('report', tasks.report())\n"
            "samples = ['T2', 'N', 'T1'] if options['extend'] else ['N', 'T1']\n"
            "for sample in samples:\n"
            "    if sample != 'N':\n"
            "        gwf.task_from_template('Somatic_N_' + sample, tasks.somatic(sample))\n"
            "    gwf.task_from_template('Mapping_' + sample, tasks.mapping(\n"
            "        sample, gated=options['gated'],\n"
            "        command_suffix=options['suffix'] if sample == 'N' else ''))\n"
        )

    def clean_intermediates(self):
        paths = [*self.work.glob('*.tmp'), *self.work.glob('*.tail')]
        for path in paths:
            path.unlink()
        return paths

    def records(self):
        return {
            path.relative_to(self.work): path.read_bytes()
            for path in (self.work / '.gwf' / 'gwflow').rglob('*.json')
        }

    def test_concurrent_mapping_whole_task_ordering_and_extension(self):
        self.configure_workflow(gated=True)
        self.cli("run")
        # Both Mapping tasks must progress to early retained outputs before
        # either is released. The gates make serialization deadlock this test.
        self.wait_for(lambda: all(
            (self.work / f"Mapping_{sample}.started").exists()
            for sample in ("N", "T1")
        ))
        for sample in ("N", "T1"):
            self.assertTrue((self.work / f"Mapping_{sample}.txt").exists())
        self.assertFalse((self.work / "Somatic_N_T1.tmp").exists())
        self.assertNotIn("Submitted target", self.cli("run"))
        for sample in ("N", "T1"):
            (self.work / f"Mapping_{sample}.release").touch()
        self.finish()
        self.assertEqual((self.work / "Somatic_N_T1.txt").read_text(), "hello\nhello\n")
        events = (self.work / "trace.txt").read_text().splitlines()
        for sample in ("N", "T1"):
            self.assertLess(events.index(f"Mapping_{sample}:tail"), events.index("Somatic_N_T1:start"))

        deleted = self.clean_intermediates()
        before, records = self.trace(), self.records()
        self.configure_workflow(extend=True)
        # A reused producer's tail is absent now; new consumers must depend on
        # its completion record, not that deleted intermediate.
        output = self.run_complete()
        self.assertIn("Submitted target Mapping_T2__", output)
        self.assertIn("Submitted target Somatic_N_T2__", output)
        for name in ("Mapping_N", "Mapping_T1", "Somatic_N_T1"):
            self.assertNotIn(f"Submitted target {name}__", output)
        self.assertEqual(self.trace() - before, Counter({
            "Mapping_T2:prepare": 1, "Mapping_T2:retain": 1, "Mapping_T2:tail": 1,
            "Somatic_N_T2:start": 1, "Somatic_N_T2:finish": 1,
        }))
        for path in deleted:
            self.assertFalse(path.exists(), path)
        for path, data in records.items():
            self.assertEqual((self.work / path).read_bytes(), data)
        self.clean_intermediates()
        before = self.trace()
        self.assertNotIn("Submitted target", self.cli("run"))
        self.assertEqual(self.trace(), before)
        self.assertFalse(list(self.work.glob('*.tmp')))

    def test_upstream_work_invalidates_fresh_consumers_transitively(self):
        self.configure_workflow(report=True)
        self.configure(use_spec_hashes=True)
        self.run_complete()
        before = self.trace()
        # Change only a nonretained trailing command: all retained files and
        # downstream outputs remain fresh, and ordinary upstream work is partial.
        self.configure_workflow(report=True, suffix="changed")
        records = self.records()
        dry_run = self.cli("run", "--dry-run")
        for name in ("Mapping_N__tail", "Somatic_N_T1__start", "report__make"):
            self.assertIn(f"Would submit {name}", dry_run)
        self.assertEqual(self.records(), records)
        output = self.run_complete()
        self.assertNotIn("Submitted target Mapping_N__prepare", output)
        self.assertNotIn("Submitted target Mapping_N__retain", output)
        self.assertNotIn("Submitted target Mapping_T1__", output)
        self.assertEqual(self.trace() - before, Counter({
            "Mapping_N:tail": 1, "Somatic_N_T1:start": 1,
            "Somatic_N_T1:finish": 1, "report": 1,
        }))
        self.clean_intermediates()
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_explain_traces_independent_upstream_work_and_changed_attempts(self):
        self.configure_workflow(report=True)
        self.configure(use_spec_hashes=True)
        self.run_complete()
        before = self.trace()
        self.configure_workflow(report=True, suffix="changed")
        (self.work / "Mapping_T1.txt").unlink()

        explanation = self.cli("explain", "--details")
        somatic = self.task_block(explanation, "Somatic_N_T1")
        report = self.task_block(explanation, "report")
        self.assertIn("Mapping_N__tail", somatic)
        self.assertIn("Mapping_T1__retain", somatic)
        self.assertIn("changed Completion attempt", somatic)
        self.assertIn("planned upstream work", somatic)
        self.assertRegex(report, r"Mapping_N__tail[^\n]*-> Task Somatic_N_T1[^\n]*-> Task report")
        self.assertIn("additional causes", self.cli("explain"))
        expected = self.submissions(explanation)
        self.assertEqual(expected, self.submissions(self.cli("run", "--dry-run")))
        self.assertEqual(expected, self.submissions(self.run_complete(), "Submitted target"))
        self.assertEqual(self.trace()["Mapping_T1:prepare"], before["Mapping_T1:prepare"])

    def test_explain_traces_standalone_upstream_target(self):
        self.configure_workflow(report=True)
        with (self.work / "workflow.py").open("a") as stream:
            stream.write("gwf.target('seed', inputs=['extra.txt'], outputs=['input.txt']) << "
                         "'echo seed >> trace.txt; cp extra.txt input.txt'\n")
        (self.work / "input.txt").unlink()
        self.run_complete()
        stamp = (self.work / "extra.txt").stat().st_mtime_ns - 10_000_000_000
        os.utime(self.work / "input.txt", ns=(stamp, stamp))

        explanation = self.cli("explain", "--details")
        report = self.task_block(explanation, "report")
        self.assertIn("Ordinary target seed", explanation)
        self.assertRegex(report, r"Ordinary target seed[^\n]*-> Task Mapping_N[^\n]*"
                                 r"-> Task Somatic_N_T1[^\n]*-> Task report")
        self.assertEqual(self.submissions(explanation), self.submissions(self.cli("run", "--dry-run")))
        self.assertEqual(self.submissions(explanation), self.submissions(self.run_complete(), "Submitted target"))

    def test_explain_traces_standalone_target_chain(self):
        self.configure_workflow(report=True)
        with (self.work / "workflow.py").open("a") as stream:
            stream.write(
                "gwf.target('preseed', inputs=['extra.txt'], outputs=['prepared.txt']) << "
                "'cp extra.txt prepared.txt'\n"
                "gwf.target('seed', inputs=['prepared.txt'], outputs=['input.txt']) << "
                "'cp prepared.txt input.txt'\n"
            )
        (self.work / "input.txt").unlink()
        self.run_complete()
        (self.work / "extra.txt").write_text("changed\n")
        stamp = (self.work / "input.txt").stat().st_mtime_ns + 10_000_000_000
        os.utime(self.work / "extra.txt", ns=(stamp, stamp))

        explanation = self.cli("explain", "--details")
        report = self.task_block(explanation, "report")
        self.assertRegex(report, r"Ordinary target preseed[^\n]*-> Ordinary target seed[^\n]*"
                                 r"-> Task Mapping_N[^\n]*-> Task Somatic_N_T1[^\n]*-> Task report")
        self.assertEqual(self.submissions(explanation), self.submissions(self.cli("run", "--dry-run")))
        self.assertEqual(self.submissions(explanation), self.submissions(self.run_complete(), "Submitted target"))

    def test_explain_traces_task_through_standalone_target(self):
        with (self.work / "workflow.py").open("a") as stream:
            stream.write(
                "from gwflow import Task\n"
                "gwf.target('relay', inputs=['Mapping_N.txt'], outputs=['relay.txt']) << "
                "'cp Mapping_N.txt relay.txt'\n"
                "consumer = Task(inputs=['relay.txt'], outputs=['consumer.txt'])\n"
                "consumer.target('make', inputs=['relay.txt'], outputs=['consumer.txt']) "
                "<< 'cp relay.txt consumer.txt'\n"
                "gwf.task_from_template('consumer', consumer)\n"
            )
        self.run_complete()
        (self.work / "Mapping_N.txt").unlink()

        explanation = self.cli("explain", "--details")
        consumer = self.task_block(explanation, "consumer")
        self.assertRegex(consumer, r"Task Mapping_N target Mapping_N__retain[^\n]*"
                                   r"-> Ordinary target relay[^\n]*-> Task consumer")
        self.assertEqual(self.submissions(explanation), self.submissions(self.cli("run", "--dry-run")))
        self.assertEqual(self.submissions(explanation), self.submissions(self.run_complete(), "Submitted target"))

    def test_explain_traces_completion_only_repair(self):
        self.configure_workflow(report=True)
        self.run_complete()
        before = self.trace()
        records = list((self.work / ".gwf" / "gwflow" / "Mapping_N").glob("*.json"))
        next(path for path in records if path.name != "expected.json").write_text("invalid record")

        explanation = self.cli("explain", "--details")
        mapping = self.task_block(explanation, "Mapping_N")
        somatic = self.task_block(explanation, "Somatic_N_T1")
        self.assertIn("completion-only repair", mapping)
        self.assertNotIn("Mapping_N__tail (planned upstream work", somatic)
        self.assertIn("Mapping_N__gwflow_complete (changed Completion attempt)", somatic)
        self.assertEqual(self.submissions(explanation), self.submissions(self.cli("run", "--dry-run")))
        self.assertEqual(self.submissions(explanation), self.submissions(self.run_complete(), "Submitted target"))
        self.assertEqual(self.trace()["Mapping_N:tail"], before["Mapping_N:tail"])

    def test_missing_upstream_output_invalidates_fresh_consumers_without_hashes(self):
        self.configure_workflow(report=True)
        self.run_complete()
        before = self.trace()
        (self.work / "Mapping_N.txt").unlink()
        self.run_complete()
        self.assertEqual(self.trace() - before, Counter({
            "Mapping_N:retain": 1, "Mapping_N:tail": 1,
            "Somatic_N_T1:start": 1, "Somatic_N_T1:finish": 1, "report": 1,
        }))
        self.clean_intermediates()
        self.assertNotIn("Submitted target", self.cli("run"))

    def test_ordinary_upstream_work_expands_a_reusable_task(self):
        with (self.work / 'workflow.py').open('a') as stream:
            stream.write("gwf.target('seed', inputs=['extra.txt'], outputs=['input.txt']) << "
                         "'echo seed >> trace.txt; cp extra.txt input.txt'\n")
        (self.work / 'input.txt').unlink()
        self.run_complete()
        before = self.trace()
        # Rebuilding an ordinary target also prevents downstream omission,
        # even though existing task boundaries are currently fresh.
        # Make the seed's output strictly older than its input regardless of
        # how long the first local-worker run took.
        stamp = (self.work / 'extra.txt').stat().st_mtime_ns - 10_000_000_000
        os.utime(self.work / 'input.txt', ns=(stamp, stamp))
        self.run_complete()
        self.assertEqual(self.trace()['seed'], before['seed'] + 1)
        self.assertEqual(self.trace()['Somatic_N_T1:finish'], before['Somatic_N_T1:finish'] + 1)

    def test_complete_validation_rejects_dependency_on_reused_intermediate(self):
        self.run_complete()
        self.clean_intermediates()
        before, records = self.trace(), self.records()
        with (self.work / 'workflow.py').open('a') as stream:
            stream.write(
                "from gwflow import Task\n"
                "bad = Task(inputs=['Mapping_N.tmp'], outputs=['bad.txt'])\n"
                "bad.target('use', inputs=['Mapping_N.tmp'], outputs=['bad.txt']) << 'touch bad.txt'\n"
                "gwf.task_from_template('bad', bad)\n"
            )
        self.assertIn("not a retained output", self.cli("run", success=False))
        self.assertEqual(self.trace(), before)
        self.assertEqual(self.records(), records)

    def test_unused_boundary_input_cannot_expose_reused_intermediate(self):
        self.run_complete()
        self.clean_intermediates()
        records = self.records()
        with (self.work / "workflow.py").open("a") as stream:
            stream.write(
                "from gwflow import Task\n"
                "bad = Task(inputs=['Mapping_N.tmp'], outputs=['bad.txt'])\n"
                "bad.target('make', inputs=[], outputs=['bad.txt']) << 'touch bad.txt'\n"
                "gwf.task_from_template('bad', bad)\n"
            )
        self.assertIn("not a retained output", self.cli("run", success=False))
        self.assertEqual(self.records(), records)
        self.assertFalse((self.work / "bad.txt").exists())

    def test_file_cycle_through_reusable_tasks_is_rejected_before_omission(self):
        self.run_complete()
        self.clean_intermediates()
        records = self.records()
        with (self.work / "workflow.py").open("a") as stream:
            stream.write(
                "gwf.target('cycle', inputs=['Somatic_N_T1.txt'], outputs=['input.txt']) << "
                "'cp Somatic_N_T1.txt input.txt'\n"
            )
        self.assertIn("depends on itself", self.cli("run", success=False))
        self.assertEqual(self.records(), records)

    def test_whole_task_cycle_is_rejected_even_when_file_graph_is_acyclic(self):
        self.run_complete()
        self.clean_intermediates()
        records = self.records()
        # These boundary inputs are not used by the inner targets, so the
        # ordinary file DAG is acyclic. Whole-task ordering introduces a cycle.
        with (self.work / 'workflow.py').open('a') as stream:
            stream.write(
                "from gwflow import Task\n"
                "left = Task(inputs=['right.txt'], outputs=['left.txt'])\n"
                "left.target('make', inputs=[], outputs=['left.txt']) << 'touch left.txt'\n"
                "right = Task(inputs=['left.txt'], outputs=['right.txt'])\n"
                "right.target('make', inputs=[], outputs=['right.txt']) << 'touch right.txt'\n"
                "gwf.task_from_template('left', left)\n"
                "gwf.task_from_template('right', right)\n"
            )
        output = self.cli("run", success=False)
        self.assertIn("depends on itself", output)
        self.assertEqual(self.records(), records)
        self.assertFalse((self.work / 'left.txt').exists())


if __name__ == '__main__':
    unittest.main()
