from __future__ import annotations

import datetime as dt
import base64
import json
import gzip
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

import scanner_service as scanner
import report_server


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
        for path in ("/scanner.sqlite3", "/config.json", "/latest/../scanner.sqlite3", "/latest/%2e%2e/config.json", "/latest/by_symbol/TUTUSDT.input.json", "/latest/charts/../summary.json"):
            self.assertIsNone(report_server.report_path(root, path), path)

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


if __name__ == "__main__":
    unittest.main()
