"""Exercise installed composition, reuse, troubleshooting, and cancellation."""

import sys
import unittest

from test_example import PackagedExampleTests
from test_troubleshooting import TroubleshootingJourneyTests


if __name__ == "__main__":
    suite = unittest.TestSuite([
        PackagedExampleTests("test_installed_producers_clean_then_new_consumer_computes_only_once"),
        TroubleshootingJourneyTests("test_logs_retry_history_cancel_and_ordinary_recovery"),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
