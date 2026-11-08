"""Real Apptainer descendants, logs, and native resource limits through the CLI."""

import os
import unittest

from process_probe import ProcessProbe
from support import LocalBackendTestCase
import test_graphs
import test_local_resources
import test_native_local


def with_container_image(source, declaration):
    assert source.count(declaration) == 1, "Container target declaration changed"
    return source.replace(declaration, declaration + f" image={os.environ['GWFLOW_TEST_SIF']!r},")


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
class ContainerSupervisionTests(ProcessProbe, LocalBackendTestCase):
    test_cancellation_stops_descendants_before_retry_and_cleanup = test_native_local.NativeTerminationTests.test_cancellation_stops_descendants_before_retry_and_cleanup
    test_worker_channel_loss_stops_descendants_but_stays_unresolved = test_native_local.NativeTerminationTests.test_worker_channel_loss_stops_descendants_but_stays_unresolved
    test_supervisor_loss_cannot_authorize_retry_or_cleanup = test_native_local.NativeTerminationTests.test_supervisor_loss_cannot_authorize_retry_or_cleanup

    def configure_workflow(self, **options):
        test_graphs.TaskGraphTests.configure_workflow(self, **options)
        workflow = self.work / "workflow.py"
        workflow.write_text(with_container_image(workflow.read_text(), "right = task.target('right',"))

    def start_probe(self, *, walltime=None):
        self.configure_workflow(right_command=self.probe(escape=True))
        if walltime is not None:
            workflow = self.work / "workflow.py"
            workflow.write_text(workflow.read_text().replace(
                "right = task.target('right',", f"right = task.target('right', walltime={walltime!r},"))
        self.cli("run")
        self.wait_for((self.work / "held").exists)

    def test_short_walltime_stops_descendants_before_retry(self):
        # Container startup belongs to the job budget; allow it to reach the gate.
        self.start_probe(walltime="00:00:10")
        self.settle()
        self.assert_probe_stopped()
        self.assertIn("Retry", self.cli("run", "--dry-run"))
        self.assertFalse((self.work / "results/sample/result.txt").exists())

    def test_live_streams_survive_cancellation_and_active_work_blocks_cleanup(self):
        self.configure_workflow(right_command=(
            "echo container-stdout; echo container-stderr >&2; " + self.probe(escape=True)))
        self.cli("run")
        self.wait_for((self.work / "held").exists)
        self.wait_for(lambda: "container-stdout" in self.cli("logs", "sample__right", "--no-pager"))
        self.assertIn("container-stderr", self.cli("logs", "sample__right", "--stderr", "--no-pager"))
        self.assertIn("active", self.cli("clean", "sample", "--delete"))
        self.assertTrue(self.running_probe_processes())
        self.assertFalse((self.work / "results/sample/result.txt").exists())
        self.cli("cancel", "sample")
        self.settle()
        self.assert_probe_stopped()
        for flags, marker in (((), "container-stdout"), (("--stderr",), "container-stderr")):
            self.assertIn(marker, self.cli("logs", "sample__right", *flags, "--no-pager"))


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
class ContainerResourceTests(LocalBackendTestCase):
    worker_memory = "2g"
    gated = test_local_resources.LocalResourceTests.gated
    queue_second = test_local_resources.LocalResourceTests.queue_second
    finish_second = test_local_resources.LocalResourceTests.finish_second
    test_cores_are_reserved_by_request_and_released_after_success = test_local_resources.LocalResourceTests.test_cores_are_reserved_by_request_and_released_after_success
    test_memory_reservation_blocks_even_when_a_core_is_available = test_local_resources.LocalResourceTests.test_memory_reservation_blocks_even_when_a_core_is_available

    def configure_workflow(self, **options):
        test_local_resources.LocalResourceTests.configure_workflow(self, **options)
        workflow = self.work / "workflow.py"
        workflow.write_text(with_container_image(workflow.read_text(), "target = task.target('compute',"))


@unittest.skipUnless(os.environ.get("GWFLOW_TEST_SIF"), "set GWFLOW_TEST_SIF for real Apptainer execution")
class ContainerWalltimeTests(LocalBackendTestCase):
    worker_debug = True
    assert_limits = test_local_resources.WalltimeTests.assert_limits
    test_unspecified_preparation_computation_completion_use_one_hour = test_local_resources.WalltimeTests.test_unspecified_preparation_computation_completion_use_one_hour

    def configure_workflow(self, **options):
        test_local_resources.WalltimeTests.configure_workflow(self, **options)
        workflow = self.work / "workflow.py"
        workflow.write_text(with_container_image(workflow.read_text(), "inputs=[], outputs=['out'],"))

    def test_long_lifecycle_limits_override_workflow_while_target_overrides_task(self):
        self.configure_workflow(workflow={"walltime": "00:00:20"}, task={"walltime": "00:00:15"},
                                target={"walltime": "00:00:12"}, preparation={"walltime": "02:00:00"},
                                completion={"walltime": "03:00:00"})
        self.assert_limits({"gwflow_prepare": 7200, "compute": 12, "gwflow_complete": 10800})
