#!/usr/bin/env python3
"""在 qqbot 从完整窗口自动搜索五例，人工节点只用于检验而不传给识别器。"""
from __future__ import annotations

import argparse
import csv
from decimal import Decimal
import hashlib
import json
import sys
from pathlib import Path
import time

import scanner_service as scanner
from screen_aria_4h_pattern import Candle
from platform_door_15m import PlatformSearch
from plot_platform_15m import render


def stamp(value):
    import datetime as dt
    return int(dt.datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=scanner.BJ).timestamp() * 1000)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, required=True)
    args = parser.parse_args()
    scanner.require_server()
    sys.path.insert(0, str(Path(scanner.__file__).resolve().parent))
    source = Path(__file__).parent
    known = list(csv.DictReader((args.base / 'fifteen_m_examples.csv').open()))
    known += list(csv.DictReader((args.base / 'pending_15m_examples.csv').open()))
    four_h = list(csv.DictReader((source / 'verified_examples.csv').open()))
    four_h += list(csv.DictReader((source / 'new_examples.csv').open()))
    four_h = {r['symbol']: r for r in four_h}
    rejected = list(csv.DictReader((args.base / 'rejected_15m_examples.csv').open()))
    checks = []
    for example in known:
        started = time.monotonic()
        symbol = example['symbol']
        path = args.base / 'cache' / (symbol + '.json')
        raw = json.loads(path.read_text())
        c = [Candle(int(r[0]), *(Decimal(x) for x in r[1:])) for r in raw['candles']]
        search = PlatformSearch(c)
        ref = four_h[symbol]
        after = stamp(ref.get('expected_reclaim_bj') or ref['expected_blue_end_bj']) + scanner.BAR_MS
        # 搜索器只收到行情与四小时阶段边界，不接收人工答案。
        found = search.find(after, after)
        lookup = {bar.open_time: i for i, bar in enumerate(c)}
        launch, first, second = [lookup[stamp(example[k])] for k in ('start_bj', 'first_retest_bj', 'second_retest_bj')]
        exact = [d for d in found if d.launch == launch and first in d.episodes[0]
                 and any(second in ep for ep in d.episodes[1:])]
        assert exact, symbol + ' 未自动复现用户节点'
        four_h_input = args.base.parent / 'reclaim-v14' / (symbol + '.input.json')
        assert four_h_input.is_file(), '缺少四小时复测行情缓存'
        four_h_output = args.base / 'joint-four-h' / symbol / 'result.json'
        four_h_output.parent.mkdir(parents=True, exist_ok=True)
        scanner.worker(four_h_input, four_h_output)
        four_h_rows = scanner.read_csv(four_h_output.with_suffix('.csv'))
        matching_four_h = [r for r in four_h_rows if
            r['yellow_start_bj'] == ref['expected_yellow_start_bj'] and
            r['yellow_end_bj'] == ref['expected_yellow_end_bj'] and
            (not ref.get('expected_wash_end_bj') or r['wash_end_bj'] == ref['expected_wash_end_bj'])]
        assert matching_four_h, symbol + ' 四小时人工边界未在本次识别中命中'
        joined = [(door, row) for door in exact for row in matching_four_h
            if c[door.platform].open_time >= stamp(row['reclaim_bj']) + scanner.BAR_MS
            and c[max(door.episodes[-1]) + 1].open_time + 900000 >= stamp(row['wash_end_bj']) + scanner.BAR_MS]
        assert joined, symbol + ' 四小时与十五分钟阶段不能正确关联'
        negative_checks = []
        for old in [r for r in rejected if r['symbol'] == symbol]:
            a, b, d = [lookup[stamp(old[k])] for k in ('start_bj', 'first_retest_bj', 'second_retest_bj')]
            bad = [door for door in found if door.launch == a and b in door.episodes[0]
                   and any(d in ep for ep in door.episodes[1:])]
            assert not bad, symbol + ' 仍把用户排除的三节点当成正确结构'
            negative_checks.append({'nodes': [old[k] for k in ('start_bj', 'first_retest_bj', 'second_retest_bj')], 'excluded': True})
        # 独立核对每个自动结果的整个支撑区间及价格、成交额关系。
        for door in found:
            platform = c[door.platform:door.launch]
            floor = min(v.low for v in platform)
            assert c[door.launch].close > max(v.close for v in platform)
            assert c[door.launch].quote_volume > max(v.quote_volume for v in platform)
            assert min(v.low for v in c[door.launch:max(door.retests) + 2]) >= floor
            assert len(door.episodes) >= 2
        best = exact[0]
        preview = args.base / 'platform-regression-charts'
        preview.mkdir(exist_ok=True)
        scanner.atomic_text(preview / (symbol + '.svg'), render(symbol, c, best))
        result = {'symbol': symbol, 'automatic_candidates': len(found), 'exact_user_node_variants': len(exact),
            'user_nodes': [example[k] for k in ('start_bj', 'first_retest_bj', 'second_retest_bj')],
            'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'closed_candles': len(c),
            'platform_start_bj': scanner.dt.datetime.fromtimestamp(c[best.platform].open_time / 1000, scanner.BJ).strftime('%Y-%m-%d %H:%M'),
            'support': str(c[best.support].low), 'excluded_old_nodes': negative_checks,
            'joint_four_h_and_15m': True, 'four_h_matching_boundaries': len(matching_four_h),
            'joint_matching_variants': len(joined),
            'seconds': round(time.monotonic() - started, 3)}
        checks.append(result)
        print(scanner.encoded(result), flush=True)
    scanner.atomic_json(args.base / 'platform-regression.json', {'status': 'complete', 'examples': checks,
        'engine_sha256': hashlib.sha256((source / 'platform_door_15m.py').read_bytes()).hexdigest(),
        'all_five_exact': True, 'all_four_rejected_excluded': True, 'joint_all_five': True})


if __name__ == '__main__':
    main()
