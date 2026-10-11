import base64
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ops'))
from ec2_probe import extract_report


class ConsoleReportTests(unittest.TestCase):
    def test_timestamp_insertions_and_partial_console(self):
        expected = {'public_gate_pass': True, 'rest': [{'status': 200}]}
        payload = base64.b64encode(json.dumps(expected).encode()).decode()
        self.assertIsNone(extract_report('MINI_PROBE_JSON=' + payload[:19]))
        captured = ('boot\nMINI_PROBE_JSON=' + payload[:19]
                    + '[2026-10-11T05:37:57.747461]' + payload[19:]
                    + '\r\nlogin:')
        self.assertEqual(extract_report(captured), expected)
        self.assertIsNone(extract_report('MINI_PROBE_JSON=e30='))
        self.assertEqual(extract_report('MINI_PROBE_JSON=bad\n' + captured), expected)


if __name__ == '__main__':
    unittest.main()
