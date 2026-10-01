"""Exercise installed factory composition, work cleanup, extension, and reuse."""

import sys
import unittest

from test_example import PackagedExampleTests


if __name__ == "__main__":
    suite = unittest.TestSuite([
        PackagedExampleTests("test_installed_producers_clean_then_new_consumer_computes_only_once")
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
