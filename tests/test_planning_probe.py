"""Check measurement units against a known UTF-8 JSON document."""

import json
import io
from pathlib import Path
import tempfile
import unittest

from fixtures.planning_probe import Probe


class PlanningProbeTests(unittest.TestCase):
    def test_counts_decodes_and_actual_utf8_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'record-state.json'
            payload = '{"value": "æ"}\n'
            path.write_text(payload, encoding='utf-8')
            probe = Probe()
            with probe:
                for _ in range(2):
                    with path.open(encoding='utf-8') as stream:
                        self.assertEqual(json.load(stream), {'value': 'æ'})
            report = probe.report()
            self.assertEqual(report['counts']['startup']['json_decodes'], 2)
            self.assertEqual(report['counts']['startup']['json_bytes'], 32)
            self.assertEqual(report['document_decodes'][str(path)], 2)

    def test_in_memory_json_stream_does_not_require_a_descriptor(self):
        probe = Probe()
        with probe:
            self.assertEqual(json.load(io.StringIO('{"ok": true}')), {'ok': True})
        self.assertEqual(probe.report()['counts']['startup']['json_decodes'], 1)
        self.assertEqual(probe.report()['document_decodes'], {})
