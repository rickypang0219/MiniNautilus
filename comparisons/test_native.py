"""Native adapter regressions; run with the pinned comparison environment."""
import unittest

from adapters import backtrader
from cases import case, submit


class BacktraderNotificationTests(unittest.TestCase):
    def test_new_order_notification_does_not_publish_future_execution(self):
        workload = case([100, 101, 102], {
            0: [submit(1, 'Sell', 1, 102)],
            1: [submit(2, 'Sell', 1, 102)],
        })
        result = backtrader(workload)
        self.assertEqual([(f['step'], f['id']) for f in result['fills']],
                         [(2, 1), (2, 2)])

    def test_partial_notifications_emit_each_execution_once(self):
        workload = case([101, 100, 100, 100], {
            0: [submit(1, 'Buy', 5, 100)],
        }, [100, 1, 2, 2])
        result = backtrader(workload, volume_filler=True)
        self.assertEqual([(f['step'], f['qty']) for f in result['fills']],
                         [(1, 1), (2, 2), (3, 2)])


if __name__ == '__main__':
    unittest.main()
