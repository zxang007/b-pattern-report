"""仅在历史核对中比较人工节点至样本窗口高点的价格，不向生产传入答案。"""
import argparse
import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import scanner_service as scanner
from platform_door_15m import PlatformSearch
from plot_platform_15m import bj
from prepare_15m_market import stamp, INTERVAL
from retest_validity import retest_validity
from screen_aria_4h_pattern import Candle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    scanner.require_server()
    known = list(csv.DictReader((args.base / 'fifteen_m_examples.csv').open()))
    known += list(csv.DictReader((args.base / 'pending_15m_examples.csv').open()))
    checks = []
    for case in known:
        source = args.base / 'cache' / (case['symbol'] + '.json')
        raw = json.loads(source.read_text())
        c = [Candle(int(r[0]), *(Decimal(x) for x in r[1:])) for r in raw['candles']]
        lookup = {bar.open_time: i for i, bar in enumerate(c)}
        launch, first, last = [lookup[stamp(case[k])] for k in
            ('start_bj', 'first_retest_bj', 'second_retest_bj')]
        search = PlatformSearch(c)
        exact = [door for door in search.describe_all(launch)
            if first in door.episodes[0] and any(last in ep for ep in door.episodes[1:])]
        assert exact, case['symbol'] + ' 未复现历史形态'
        status = retest_validity(search, SimpleNamespace(episodes=((first,), (last,))))
        # 样本窗口高点只用于这份事后核对，线上失效条件不读取它。
        peak = search.high.at(last + 1, len(c))
        before = min((bar.close for bar in c[last + 1:peak]), default=None)
        checks.append({'symbol': case['symbol'], 'last_retest_bj': bj(c[last].open_time),
            'window_end_bj': bj(c[-1].open_time + INTERVAL), 'highest_bj': bj(c[peak].open_time),
            'highest_price': str(c[peak].high), 'close_before_highest': str(before) if before is not None else '',
            'held_before_highest': before is None or before >= c[last].low,
            'peak_bar_low': str(c[peak].low), 'automatic_shape_reproduced': True,
            'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), **status})
    result = {'purpose': 'historical_diagnostic_only', 'examples': checks,
        'shape_passed': len(checks), 'held_before_highest': sum(row['held_before_highest'] for row in checks),
        'validity_sha256': hashlib.sha256(Path(__file__).with_name('retest_validity.py').read_bytes()).hexdigest()}
    scanner.atomic_json(args.output, result)
    print(scanner.encoded(result), flush=True)


if __name__ == '__main__':
    main()
