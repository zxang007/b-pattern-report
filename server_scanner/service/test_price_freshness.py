"""验证四小时形态截止与十五分钟失效检查使用独立边界。"""
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
import tempfile
import json
import unittest

import report_server
from platform_door_15m import PlatformSearch
from prepare_15m_market import INTERVAL, price_cutoff
from retest_validity import retest_validity


class PriceFreshnessTests(unittest.TestCase):
    def test_price_check_advances_beyond_four_hour_source_without_open_bar(self):
        source = 16 * INTERVAL
        now = 29 * INTERVAL + INTERVAL - 1
        self.assertEqual(price_cutoff(source, now), 29 * INTERVAL)
        self.assertEqual(source, 16 * INTERVAL)

    def test_boundary_includes_just_closed_candle_and_rejects_clock_error(self):
        self.assertEqual(price_cutoff(16 * INTERVAL, 20 * INTERVAL), 20 * INTERVAL)
        with self.assertRaises(ValueError):
            price_cutoff(16 * INTERVAL, 15 * INTERVAL)

    def test_new_close_invalidates_frozen_signal_after_original_cutoff(self):
        def bar(i, low, close):
            return SimpleNamespace(open_time=i * INTERVAL, open=Decimal('12'), high=Decimal('14'),
                low=Decimal(str(low)), close=Decimal(str(close)), quote_volume=Decimal('1'))
        original = [bar(0, 8, 12), bar(1, 9, 12), bar(2, 10, 12), bar(3, 11, 12)]
        door = SimpleNamespace(episodes=((1,), (2,)))
        old = retest_validity(PlatformSearch(original), door)
        latest = retest_validity(PlatformSearch(original + [bar(4, 9, '9.5'), bar(5, 11, 12)]), door)
        self.assertTrue(old['price_valid'])
        self.assertFalse(latest['price_valid'])
        self.assertEqual(latest['last_retest_low'], old['last_retest_low'])
        self.assertEqual(latest['last_retest_break_bj'], '1970-01-01 09:00')

    def test_late_export_of_old_prices_cannot_replace_fresher_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number, price, updated in [(1, '23:15', '23:59'), (2, '23:30', '23:35')]:
                run = root / 'runs' / ('live-' + str(number) * 20)
                run.mkdir(parents=True)
                (run / 'index.html').write_text('<h1>报告</h1>')
                (run / 'summary.json').write_text(json.dumps({'status': 'complete', 'stage': '4h_and_15m',
                    'validation_ok': True, 'end_bj_exclusive': '2026-10-28T20:00:00+08:00',
                    'price_checked_through_bj': '2026-10-28T' + price + ':00+08:00',
                    'updated_at_bj': '2026-10-28T' + updated + ':00+08:00'}))
            self.assertEqual(report_server.latest_report(root).name, 'live-' + '2' * 20)


if __name__ == '__main__':
    unittest.main()
