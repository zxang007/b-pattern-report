#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal

import scanner_service as scanner


def timestamp(value):
    import datetime as dt
    return int(dt.datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=scanner.BJ).timestamp() * 1000)


def evidence(row, candles):
    start, end = timestamp(row['yellow_start_bj']), timestamp(row['yellow_end_bj'])
    yellow = [c for c in candles if start <= int(c[0]) <= end]
    arrow = next(c for c in candles if int(c[0]) == timestamp(row['reclaim_bj']))
    highest = max(Decimal(c[4]) for c in yellow)
    close, amount = Decimal(arrow[4]), Decimal(arrow[6])
    return {'arrow_bj': row['reclaim_bj'], 'yellow_highest_close': str(highest),
            'arrow_close': str(close), 'quote_volume': str(amount),
            'valid_price': close > highest, 'valid_amount': amount >= Decimal('900000')}


def nearest(rows, case):
    fields = [('yellow_start_bj', 'expected_yellow_start_bj'), ('yellow_end_bj', 'expected_yellow_end_bj'),
              ('blue_start_bj', 'expected_blue_start_bj'),
              ('wash_start_bj', 'expected_wash_start_bj'), ('wash_end_bj', 'expected_wash_end_bj')]
    # 旧样本把红箭头记作蓝区结束；新标记单独区分蓝区末根与红箭头。
    explicit_arrow = bool(case.get('expected_reclaim_bj'))
    fields.append(('reclaim_bj', 'expected_reclaim_bj' if explicit_arrow else 'expected_blue_end_bj'))
    def gaps(row):
        values = [abs(timestamp(row[field]) - timestamp(case[target])) / 3600000
                  for field, target in fields if case.get(target)]
        if explicit_arrow and case.get('expected_blue_end_bj'):
            blue_end = timestamp(row['reclaim_bj']) - scanner.BAR_MS
            values.append(abs(blue_end - timestamp(case['expected_blue_end_bj'])) / 3600000)
        return values
    if not rows or not case.get('expected_yellow_start_bj'):
        return None
    row = min(rows, key=lambda row: (max(gaps(row)), sum(gaps(row))))
    return {'row': row, 'max_gap_hours': max(gaps(row)),
            'within_saved_tolerance': max(gaps(row)) <= float(case.get('max_segment_gap_hours') or 0)}


def main():
    parser = argparse.ArgumentParser(description='只在 qqbot 对保存的人工样本执行新旧规则对照，不在识别器中导入样本答案。')
    parser.add_argument('--examples', type=Path, required=True)
    parser.add_argument('--new-examples', type=Path, default=Path(__file__).with_name('new_examples.csv'))
    parser.add_argument('--old-release', type=Path, required=True)
    parser.add_argument('--new-release', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-state', type=Path, required=True)
    args = parser.parse_args()
    scanner.require_server()
    args.output.mkdir(parents=True, exist_ok=True)
    cases = list(csv.DictReader(args.examples.open()))
    if args.new_examples.is_file():
        cases.extend(csv.DictReader(args.new_examples.open()))
    cache = scanner.Store(args.output / 'cache.sqlite3')
    source = sqlite3.connect(f'file:{args.source_state / "scanner.sqlite3"}?mode=ro', uri=True)
    for case in cases:
        cache.put(case['symbol'], [json.loads(r[0]) for r in source.execute(
            'SELECT payload FROM candles WHERE symbol=? ORDER BY ts', (case['symbol'],))])
    source.close()
    api = scanner.PublicAPI(20, 3)
    results = []
    for case in cases:
        symbol = case['symbol']
        begin, end = scanner.date_window(case['scan_start_date'], case['scan_end_date'])
        candles = scanner.load_cached(cache, api, symbol, begin, end)
        source_path = args.output / (symbol + '.input.json')
        scanner.atomic_json(source_path, {'symbol': symbol, 'candles': candles})
        result = {'symbol': symbol, 'case': case, 'versions': {}}
        for label, release in [('before', args.old_release), ('after', args.new_release)]:
            output = args.output / label / symbol
            output.mkdir(parents=True, exist_ok=True)
            meta = output / 'result.json'
            started = time.monotonic()
            process = subprocess.run([sys.executable, str(release / 'scanner_service.py'), 'worker',
                                      '--input', str(source_path), '--output', str(meta)],
                                     capture_output=True, text=True, timeout=300)
            if process.returncode:
                raise RuntimeError(symbol + ' ' + label + ': ' + process.stderr[-2000:])
            rows = scanner.read_csv(meta.with_suffix('.csv'))
            audit = [evidence(row, candles) for row in rows]
            version = {'count': len(rows), 'seconds': round(time.monotonic() - started, 3),
                       'invalid_price': sum(not r['valid_price'] for r in audit),
                       'invalid_amount': sum(not r['valid_amount'] for r in audit),
                       'nearest': nearest(rows, case)}
            scanner.atomic_json(output / 'arrow_audit.json', audit)
            result['versions'][label] = version
            print(scanner.encoded({'symbol': symbol, 'version': label, **version}), flush=True)
            if label == 'after' and (version['invalid_price'] or version['invalid_amount']):
                raise RuntimeError('新版本仍存在不满足红箭头条件的候选: ' + symbol)
        results.append(result)
        scanner.atomic_json(args.output / 'results.json', results)
    scanner.atomic_json(args.output / 'complete.json', {'status': 'complete', 'cases': len(cases), 'results': results})
    cache.db.close()


if __name__ == '__main__':
    main()
