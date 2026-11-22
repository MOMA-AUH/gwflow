"""Committed synthetic inputs and the public multi-step bioinformatics example."""

import csv
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest

from support import LocalBackendTestCase


EXAMPLE = Path(__file__).resolve().parents[1] / "example"


class SyntheticExampleDataTests(unittest.TestCase):
    def test_committed_inputs_regenerate_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            subprocess.run([sys.executable, str(EXAMPLE / "generate_data.py"),
                            "--output-dir", str(output)], check=True)
            expected = {path.name: path.read_bytes() for path in (EXAMPLE / "data").iterdir()}
            self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, expected)

    def test_materializer_rejects_a_mate_from_the_wrong_sample(self):
        data = EXAMPLE / "data"
        identity = json.loads((data / "reads.json").read_text())["sample_a"]["sha256"]
        with tempfile.TemporaryDirectory() as directory:
            descriptor = Path(directory) / "reads.json"
            descriptor.write_text(json.dumps({
                "identity": identity, "metadata": {"format": "fastq"},
                "files": {"R1": str(data / "sample_a_R1.fastq"), "R2": str(data / "sample_b_R2.fastq")},
            }))
            result = subprocess.run([sys.executable, str(EXAMPLE / "materialize_reads.py"), str(descriptor)],
                                    cwd=directory, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("R2 differs from its recorded FASTQ checksum", result.stderr)


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_BIOINFORMATICS") == "1",
                     "set GWFLOW_TEST_BIOINFORMATICS=1 for real Bioconda container execution")
class BioinformaticsExampleTests(LocalBackendTestCase):
    def configure_workflow(self):
        for name in ("workflow.py", "task_library.py", "materialize_reads.py", "multiqc_config.yaml"):
            shutil.copy(EXAMPLE / name, self.work)
        shutil.copytree(EXAMPLE / "data", self.work / "data")
        self.expected = json.loads((self.work / "data/expected.json").read_text())["samples"]

    def setUp(self):
        super().setUp()
        # Real registry acquisition can take longer than an ordinary CLI check.
        # Use the public CLI with live local workers, as in the walkthrough.
        result = subprocess.run([self.executable, "run", "--dry-run"], cwd=self.work,
                                capture_output=True, text=True, timeout=900)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def wait_for_completion(self, *selectors):
        result = subprocess.run(
            [self.executable, "wait", *selectors, "--interval", "0.2", "--timeout", "180"],
            cwd=self.work, capture_output=True, text=True, timeout=200,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def samtools(self, *arguments):
        from gwflow.images import binding

        reference = runpy.run_path(str(EXAMPLE / "task_library.py"))["SAMTOOLS"]
        image = binding(reference, self.work)
        result = subprocess.run(["apptainer", "exec", "--cleanenv", image, "samtools", *map(str, arguments)],
                                cwd=self.work, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def assert_alignments(self):
        truth = {}
        with (self.work / "data/truth.tsv").open() as stream:
            for row in csv.DictReader(stream, delimiter="\t"):
                truth[row["read_pair"]] = row
        for sample, expected in self.expected.items():
            result = self.work / "results" / sample
            bam = result / "alignment.bam"
            self.samtools("quickcheck", bam)
            # Reading through the index verifies the retained BAM/index pair.
            self.assertEqual(int(self.samtools("view", "-c", bam, "synthetic_reference")),
                             expected["mapped_primary_reads"])
            records = [line.split("\t") for line in self.samtools("view", "-F", "2304", bam).splitlines()]
            self.assertEqual(len(records), expected["primary_reads"])
            self.assertEqual(sum(not int(row[1]) & 4 for row in records), expected["mapped_primary_reads"])
            self.assertEqual(sum(bool(int(row[1]) & 4) for row in records), expected["unmapped_primary_reads"])
            self.assertEqual(sum(bool(int(row[1]) & 2) for row in records), expected["properly_paired_reads"])
            for row in records:
                pair = truth[row[0]]
                flag = int(row[1])
                self.assertEqual(row[2], pair["reference"])
                if not flag & 4:
                    position = (int(pair["fragment_start"]) if flag & 64
                                else int(pair["fragment_end"]) - 149)
                    self.assertEqual(int(row[3]), position)
                    self.assertEqual(row[5], "150M")
            flagstat = (result / "flagstat.txt").read_text()
            self.assertIn(f'{expected["primary_reads"]} + 0 in total', flagstat)
            self.assertIn(f'{expected["mapped_primary_reads"]} + 0 mapped (', flagstat)

    def assert_report(self):
        report = self.work / "results/report"
        self.assertIn("MultiQC", (report / "report.html").read_text())
        with (report / "summary.tsv").open() as stream:
            rows = {row["Sample"]: row for row in csv.DictReader(stream, delimiter="\t")}
        self.assertEqual(set(rows), set(self.expected))
        for sample, expected in self.expected.items():
            self.assertEqual(float(rows[sample]["samtools_flagstat-mapped_passed"]),
                             expected["mapped_primary_reads"])
            self.assertEqual(float(rows[sample]["samtools_flagstat-mapped_passed_pct"]),
                             expected["mapped_percent"])

    def test_cleaned_multi_step_tasks_feed_report_without_recomputation(self):
        first = self.cli("run", "align__*", "--details")
        self.wait_for_completion("align__*")
        for sample in self.expected:
            for target in ("materialize", "align", "sort", "index", "statistics"):
                self.assertIn(f"align__{sample}__{target}: confirmed submitted", first)
            self.assertTrue(list((self.work / "work" / f"align__{sample}").rglob("alignment.sam")))
        self.assertNotIn("report__report: confirmed submitted", first)
        self.assert_alignments()

        retained = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in (self.work / "results").rglob("*") if path.is_file()}
        self.assertIn("eligible", self.cli("clean", "align__*"))
        self.cli("clean", "align__*", "--delete")
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in retained}, retained)
        for sample in self.expected:
            self.assertFalse(list((self.work / "work" / f"align__{sample}").iterdir()))

        preview = self.cli("run", "report", "--dry-run")
        for sample in self.expected:
            self.assertRegex(preview, rf"Task align__{sample}\s+Reuse\s+")
        self.assertRegex(preview, r"Task report\s+Run\s+")
        second = self.cli("run", "report", "--details")
        self.wait_for_completion("report")
        self.assertIn("report__report: confirmed submitted", second)
        self.assertNotRegex(second, r"align__\w+: confirmed submitted")
        self.assert_report()
        self.assertNoSubmissions(self.cli("run"))
        self.cli("clean", "--delete")
        self.assertNoSubmissions(self.cli("run"))

    def test_full_workflow_reuses_archived_reads_and_can_recompute_from_spring(self):
        self.cli("run")
        self.wait_for_completion()
        self.assert_alignments()
        self.assert_report()
        attempts = self.task_attempts()
        sample = "align__sample_a"
        self.cli("clean", sample, "--delete")

        data = self.work / "data"
        mates = ["sample_a_R1.fastq", "sample_a_R2.fastq"]
        subprocess.run(["spring", "-c", "-t", "1", "-i", *mates, "-o", "sample_a.spring"],
                       cwd=data, check=True, capture_output=True)
        restored = ["restored_R1.fastq", "restored_R2.fastq"]
        subprocess.run(["spring", "-d", "-t", "1", "-i", "sample_a.spring", "-o", *restored],
                       cwd=data, check=True, capture_output=True)
        for original, decoded in zip(mates, restored):
            self.assertEqual((data / decoded).read_bytes(), (data / original).read_bytes())
        for path in mates + restored:
            (data / path).unlink()
        manifest = data / "reads.json"
        inputs = json.loads(manifest.read_text())
        inputs["sample_a"]["format"] = "spring"
        manifest.write_text(json.dumps(inputs))

        preview = self.cli("run", "--dry-run")
        for name in (sample, "align__sample_b", "report"):
            self.assertRegex(preview, rf"Task {name}\s+Reuse\s+")
        self.assertNoSubmissions(self.cli("run"))
        self.assertEqual(self.task_attempts(), attempts)

        self.cli("run", sample, "--force")
        self.wait_for_completion(sample)
        self.assertNotEqual(self.task_attempts()[sample], attempts[sample])
        self.assertIn("Verified both FASTQ mates from spring",
                      self.cli("logs", f"{sample}__materialize", "--no-pager"))
        self.assert_alignments()
