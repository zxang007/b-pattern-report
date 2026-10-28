"""验证收盘跌破、影线下探、收回、相等和末次回踩的边界。"""
from decimal import Decimal
from types import SimpleNamespace
import unittest

from platform_door_15m import PlatformSearch
from retest_validity import retest_validity


class RetestValidityTests(unittest.TestCase):
    def check_lows(self, lows, episode=(2,), closes=None):
        candles = [SimpleNamespace(open_time=i * 900000, open=Decimal('12'),
            high=Decimal('14'), low=Decimal(str(low)), close=Decimal(str(closes[i])) if closes is not None else Decimal('12'),
            quote_volume=Decimal('1')) for i, low in enumerate(lows)]
        search = PlatformSearch(candles)
        door = SimpleNamespace(episodes=((1,), episode))
        return retest_validity(search, door)

    def test_wick_break_is_retained_if_closes_hold(self):
        result = self.check_lows([8, 9, 10, '9.999999999999999999', 11])
        self.assertTrue(result['price_valid'])
        self.assertEqual(result['last_retest_break_bj'], '')

    def test_equal_low_is_valid(self):
        self.assertTrue(self.check_lows([8, 9, 10, 10, 11], closes=[12, 12, 12, 10, 12])['price_valid'])

    def test_uses_final_episode_wick_not_first_retest_or_close(self):
        result = self.check_lows([8, 9, 11, 10, '10.5'], (2, 3))
        self.assertEqual(result['last_retest_low'], '10')
        self.assertTrue(result['price_valid'])

    def test_checks_full_following_interval_including_confirmation_bar(self):
        result = self.check_lows([8, 9, 10, '9.5', 11, '9.4'], closes=[12, 12, 12, '9.9', 12, 12])
        self.assertFalse(result['price_valid'])
        self.assertEqual(result['last_retest_break_bj'], '1970-01-01 08:45')

    def test_has_no_fixed_age_or_bar_window(self):
        self.assertTrue(self.check_lows([8, 9, 10] + [11] * 2000)['price_valid'])
        self.assertFalse(self.check_lows([8, 9, 10] + [11] * 2000 + [9], closes=[12] * 2003 + [9])['price_valid'])


if __name__ == '__main__':
    unittest.main()
