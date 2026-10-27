from __future__ import annotations

import datetime as dt
import base64
import json
import gzip
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

import scanner_service as scanner
import report_server
import four_h_segment_arc_scan as segment
import scan_four_h_segment_arc_market as market
from screen_aria_4h_pattern import Candle


def api_row(stamp):
    return [stamp, "10", "12", "9", "11", "3", stamp + scanner.BAR_MS - 1, "33"]


class FakeAPI:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, params))
        return [row for row in self.rows if params["startTime"] <= row[0] <= params["endTime"]]


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = scanner.Store(Path(self.temp.name) / "state.sqlite3")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_cache_only_fetches_missing_closed_bars(self):
        api = FakeAPI([api_row(i * scanner.BAR_MS) for i in range(4)])
        first = scanner.load_cached(self.store, api, "TUTUSDT", 0, 3 * scanner.BAR_MS)
        self.assertEqual(len(first), 3)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(scanner.load_cached(self.store, api, "TUTUSDT", 0, 3 * scanner.BAR_MS), first)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(len(scanner.load_cached(self.store, api, "TUTUSDT", 0, 4 * scanner.BAR_MS)), 4)
        self.assertEqual(api.calls[-1][1]["startTime"], 3 * scanner.BAR_MS)
        self.assertEqual(api.calls[-1][1]["endTime"], 4 * scanner.BAR_MS - 1)

    def test_gap_does_not_become_zero_match_success(self):
        api = FakeAPI([api_row(0), api_row(2 * scanner.BAR_MS)])
        with self.assertRaisesRegex(ValueError, "不完整"):
            scanner.load_cached(self.store, api, "TUTUSDT", 0, 3 * scanner.BAR_MS)
        api.rows.append(api_row(scanner.BAR_MS))
        self.assertEqual(len(scanner.load_cached(self.store, api, "TUTUSDT", 0, 3 * scanner.BAR_MS)), 3)
        self.assertEqual(api.calls[-1][1]["startTime"], scanner.BAR_MS)
        self.assertEqual(api.calls[-1][1]["endTime"], 2 * scanner.BAR_MS - 1)

    def test_invalid_price_cannot_enter_cache(self):
        raw = api_row(0)
        raw[2] = "NaN"
        with self.assertRaisesRegex(ValueError, "无效"):
            scanner.load_cached(self.store, FakeAPI([raw]), "TUTUSDT", 0, scanner.BAR_MS)
        self.assertEqual(self.store.read("TUTUSDT", 0, scanner.BAR_MS), [])

    def test_grouped_events_are_idempotent_and_keep_variants(self):
        row = {"symbol": "TUTUSDT", "blue_low_bj": "2026-07-08 16:00", "wash_start_bj": "2026-07-09 20:00", "wash_end_bj": "2026-07-11 12:00"}
        alternate = dict(row, yellow_start_bj="2026-07-06 20:00")
        self.store.observe("one", "TUTUSDT", [row, alternate])
        self.store.observe("two", "TUTUSDT", [alternate, row])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT kind FROM events").fetchone()[0], "baseline")
        self.store.observe("three", "TUTUSDT", [dict(row, wash_end_bj="2026-07-12 12:00")])
        self.store.observe("four", "TUTUSDT", [])
        self.store.observe("five", "TUTUSDT", [row])
        kinds = [r[0] for r in self.store.db.execute("SELECT kind FROM events ORDER BY id")]
        self.assertEqual(kinds, ["baseline", "updated", "not_in_current_window", "reappeared"])

    def test_beijing_backtest_and_january_rolling_window(self):
        start, end = scanner.date_window("2026-07-01", "2026-08-31")
        self.assertEqual((end - start) // scanner.BAR_MS, 372)
        self.assertEqual(dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).hour, 16)
        stamp = int(dt.datetime(2027, 1, 1, tzinfo=scanner.BJ).timestamp() * 1000)
        self.assertEqual(dt.datetime.fromtimestamp(scanner.rolling_start(stamp) / 1000, dt.timezone.utc),
                         dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc))

    def test_server_flag_cannot_allow_mac_network(self):
        with patch.object(scanner.sys, "platform", "darwin"), patch.dict(scanner.os.environ, {"MARKET_SCANNER_SERVER": "qqbot"}):
            with self.assertRaises(SystemExit):
                scanner.require_server()

    def test_resume_only_skips_completed_jobs(self):
        self.store.job("one", "TUTUSDT", "running")
        self.assertFalse(self.store.completed("one", "TUTUSDT"))
        self.store.job("one", "TUTUSDT", "failed", "数据缺失")
        self.assertFalse(self.store.completed("one", "TUTUSDT"))
        self.store.job("one", "TUTUSDT", "complete")
        self.assertTrue(self.store.completed("one", "TUTUSDT"))
        self.assertFalse(self.store.completed("two", "TUTUSDT"))

    def test_public_site_only_exposes_reports(self):
        root = Path(self.temp.name)
        run = root / "runs" / ("live-" + "a" * 20)
        run.mkdir(parents=True)
        (root / "latest").symlink_to(run)
        self.assertEqual(report_server.report_path(root, "/"), run / "index.html")
        self.assertEqual(report_server.report_path(root, "/latest/matches.csv"), run / "matches.csv")
        group = "a" * 24 + ".html"
        self.assertEqual(report_server.report_path(root, "/latest/groups/" + group), run / "groups" / group)
        for path in ("/scanner.sqlite3", "/config.json", "/latest/../scanner.sqlite3", "/latest/%2e%2e/config.json", "/latest/by_symbol/TUTUSDT.input.json", "/latest/charts/../summary.json"):
            self.assertIsNone(report_server.report_path(root, path), path)
        for path in ("/latest/groups/config.json", "/latest/groups/../summary.json", "/latest/groups/not-a-group.html"):
            self.assertIsNone(report_server.report_path(root, path), path)

    def test_latest_keeps_complete_scan_while_next_scan_runs(self):
        root = Path(self.temp.name)
        runs = root / "runs"
        previous, complete, running, backtest = [runs / (kind + "-" + key * 20) for kind, key in
                                               (("live", "a"), ("live", "b"), ("live", "c"), ("backtest", "d"))]
        for folder, status, end in ((previous, "complete", "2026-10-27T04:00:00+08:00"),
                                    (complete, "complete", "2026-10-27T08:00:00+08:00"),
                                    (running, "running", "2026-10-27T12:00:00+08:00"),
                                    (backtest, "complete", "2026-10-28T00:00:00+08:00")):
            folder.mkdir(parents=True)
            (folder / "index.html").write_text("<h1>report</h1>")
            scanner.atomic_json(folder / "summary.json", {"status": status, "end_bj_exclusive": end})
        (root / "latest").symlink_to(running)
        self.assertEqual(report_server.report_path(root, "/latest/index.html"), complete / "index.html")
        self.assertEqual(report_server.report_path(root, "/latest/matches.csv"), complete / "matches.csv")
        self.assertEqual(report_server.report_path(root, "/runs/" + running.name + "/index.html"), running / "index.html")
        scanner.atomic_json(running / "summary.json", {"status": "complete", "end_bj_exclusive": "2026-10-27T12:00:00+08:00"})
        self.assertEqual(report_server.report_path(root, "/latest/index.html"), running / "index.html")

    def test_latest_ignores_failed_or_damaged_reports(self):
        root = Path(self.temp.name)
        partial = root / "runs" / ("live-" + "a" * 20)
        broken = root / "runs" / ("live-" + "b" * 20)
        for folder in (partial, broken):
            folder.mkdir(parents=True)
            (folder / "index.html").write_text("<h1>report</h1>")
        scanner.atomic_json(partial / "summary.json", {"status": "partial"})
        (broken / "summary.json").write_text("{")
        (root / "latest").symlink_to(partial)
        self.assertEqual(report_server.report_path(root, "/latest/index.html"), partial / "index.html")

    def test_report_encryption_rejects_wrong_key_and_tampering(self):
        key = bytes(range(32))
        credential = {"key": base64.b64encode(key).decode(), "salt": base64.b64encode(b"sample-salt-12345").decode()}
        packet = json.loads(report_server.encrypted_packet(b"private chart payload", "image/svg+xml", credential))
        ciphertext = base64.b64decode(packet["ciphertext"])
        nonce = base64.b64decode(packet["nonce"])
        self.assertEqual(AESGCM(key).decrypt(nonce, ciphertext, None), b"private chart payload")
        self.assertNotIn(b"private chart payload", ciphertext)
        with self.assertRaises(InvalidTag):
            AESGCM(b"x" * 32).decrypt(nonce, ciphertext, None)
        with self.assertRaises(InvalidTag):
            AESGCM(key).decrypt(nonce, ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]), None)

    def test_compression_happens_before_encryption(self):
        key = b'z' * 32
        credential = {"key": base64.b64encode(key).decode(), "salt": base64.b64encode(b'sample-salt-12345').decode()}
        content = b'<tr><td>sample chart</td></tr>' * 2000
        packet = json.loads(report_server.encrypted_packet(content, 'text/html', credential, compress=True))
        decrypted = AESGCM(key).decrypt(base64.b64decode(packet['nonce']), base64.b64decode(packet['ciphertext']), None)
        self.assertEqual(gzip.decompress(decrypted), content)
        self.assertLess(len(decrypted), len(content) // 10)


class ReclaimTests(unittest.TestCase):
    def candle(self, index, close, amount, high=None):
        close = Decimal(str(close))
        return Candle(index * scanner.BAR_MS, close - Decimal('0.2'),
                      Decimal(str(high)) if high is not None else close + Decimal('0.5'),
                      close - Decimal('0.5'), close, Decimal('999999999'), Decimal(str(amount)))

    def test_wick_or_equal_close_is_not_yellow_breakout(self):
        self.assertFalse(segment.is_yellow_reclaim(self.candle(0, 9, 2000000, high=20), Decimal('10')))
        self.assertFalse(segment.is_yellow_reclaim(self.candle(0, 10, 2000000), Decimal('10')))

    def test_amount_is_quote_turnover_and_accepts_900k_boundary(self):
        self.assertFalse(segment.is_yellow_reclaim(self.candle(0, 11, '899999.99'), Decimal('10')))
        self.assertTrue(segment.is_yellow_reclaim(self.candle(0, 11, 900000), Decimal('10')))

    def test_arrow_selects_first_candle_meeting_both_conditions(self):
        candles = [self.candle(0, 9, 2000000, high=20), self.candle(1, 11, 899999), self.candle(2, 12, 900000)]
        self.assertEqual(segment.first_hold_reclaim_after_low(candles, 0, Decimal('10')), 2)

    def test_long_regions_and_short_wash_are_not_filtered_by_bar_counts(self):
        for blue_length, wash_length in ((30, 40), (2, 1)):
            closes = [8, 9, 10] + [8] * blue_length + [11] * wash_length + [12]
            reclaim_i = 3 + blue_length
            candles = [self.candle(i, close, 900000 if i == reclaim_i else 1000000 if i == len(closes) - 1 else 100)
                       for i, close in enumerate(closes)]
            args = scanner.core_defaults()
            args.max_market_yellow_bars = args.max_market_blue_bars = args.max_blue_below_close_count = 1
            args.min_market_wash_bars = 999
            args.max_market_wash_bars = 1
            found = segment.find_matches(candles, 'TESTUSDT', market.scan_args(args))
            kept = market.market_filter(found, args)
            self.assertTrue(any(m.reclaim.open_time == candles[reclaim_i].open_time and m.wash_end.open_time == candles[-2].open_time for m in kept))

    def test_chart_arrow_records_actual_price_and_turnover(self):
        import draw_four_h_segment_arc_matches_svg as chart
        import xml.etree.ElementTree as ET
        candles = [self.candle(i, close, 900000 if i == 5 else 1000000 if i == 7 else 100)
                   for i, close in enumerate([8, 9, 10, 8, 9, 11, 11, 12])]
        matches = segment.find_matches(candles, 'TESTUSDT', market.scan_args(scanner.core_defaults()))
        rendered = ET.fromstring(chart.render('TESTUSDT', candles, matches[0]))
        arrow = rendered.find('.//*[@id="reclaim-breakout"]')
        self.assertEqual(Decimal(arrow.attrib['data-yellow-highest-close']), Decimal('10'))
        self.assertEqual(Decimal(arrow.attrib['data-quote-volume']), Decimal('900000'))

    def test_dedupe_keeps_alternative_yellow_boundaries(self):
        from dataclasses import replace
        candles = [self.candle(i, close, 900000 if i == 5 else 1000000 if i == 7 else 100)
                   for i, close in enumerate([8, 9, 10, 8, 9, 11, 11, 12])]
        match = segment.find_matches(candles, 'TESTUSDT', market.scan_args(scanner.core_defaults()))[0]
        first = replace(match, yellow_start=candles[0])
        later = replace(match, yellow_start=candles[1])
        self.assertEqual(len(market.dedupe([first, later, first], 999)), 2)


if __name__ == "__main__":
    unittest.main()
