#!/usr/bin/env python3
"""准备全市场候选的十五分钟独立缓存，不与四小时缓存混用。"""
from __future__ import annotations

import csv
import argparse
import datetime as dt
from decimal import Decimal
import fcntl
import json
from pathlib import Path
import time

import scanner_service as scanner

INTERVAL = 900000


def stamp(value):
    return int(dt.datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=scanner.BJ).timestamp() * 1000)


def gaps(times, first, end):
    start = None
    for value in range(first, end, INTERVAL):
        if value not in times and start is None:
            start = value
        elif value in times and start is not None:
            yield start, value
            start = None
    if start is not None:
        yield start, end


def prepare(source_scope=None):
    scanner.require_server()
    root = Path('/home/ubuntu/market-pattern-scanner/state')
    lock = (root / 'fifteen_m_fetch.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    manifest = root / 'fifteen_m_market_manifest.json'
    if manifest.is_file() and (source_scope is None or json.loads(manifest.read_text())['source_scope'] == source_scope):
        data = json.loads(manifest.read_text())
    else:
        completed = []
        for path in (root / 'runs').glob('live-*/summary.json'):
            item = json.loads(path.read_text())
            if item.get('status') == 'complete' and (path.parent / 'by_symbol').is_dir() and (source_scope is None or path.parent.name == source_scope):
                completed.append((item['end_bj_exclusive'], item.get('updated_at_bj', ''), path.parent, item))
        _, _, source, meta = max(completed)
        with (source / 'matches.csv').open() as handle:
            matches = list(csv.DictReader(handle))
        begin = {}
        for row in matches:
            start = stamp(row['reclaim_bj']) + scanner.BAR_MS
            begin[row['symbol']] = min(start, begin.get(row['symbol'], start))
        infos = json.loads((source / 'symbols.json').read_text())
        data = {'source_scope': source.name, 'source_summary': meta, 'total_symbols': len(infos),
            'candidate_symbols': len(begin), 'end_ms_exclusive': int(dt.datetime.fromisoformat(meta['end_bj_exclusive']).timestamp() * 1000),
            'interval': '15m', 'source': 'Binance USD-M /fapi/v1/klines',
            'symbols': [{**info, 'fetch_start_ms': begin.get(info['symbol'])} for info in infos]}
        scanner.atomic_json(manifest, data)
    path = root / 'fifteen_m.sqlite3'
    assert path.name != 'scanner.sqlite3'
    store = scanner.Store(path)
    version = store.db.execute('PRAGMA user_version').fetchone()[0]
    assert version in (0, 15)
    store.db.execute('PRAGMA user_version=15')
    api = scanner.PublicAPI(20, 3)
    progress = {'status': 'running', 'source_scope': data['source_scope'], 'total_symbols': data['total_symbols'],
        'candidate_symbols': data['candidate_symbols'], 'completed_symbols': 0, 'skipped_no_4h': 0, 'failed_symbols': [], 'candles': 0}
    started = time.monotonic()
    status_path = root / 'fifteen_m_fetch_status.json'
    for info in data['symbols']:
        symbol = info['symbol']
        if info['fetch_start_ms'] is None:
            progress['skipped_no_4h'] += 1
            progress['completed_symbols'] += 1
            continue
        first = max(info['fetch_start_ms'], (info['onboard'] // INTERVAL) * INTERVAL)
        end = data['end_ms_exclusive']
        progress['current_symbol'] = symbol
        try:
            existing = store.read(symbol, first, end)
            for begin, stop in gaps({r[0] for r in existing}, first, end):
                cursor = begin
                while cursor < stop:
                    raw = api.get('/fapi/v1/klines', {'symbol': symbol, 'interval': '15m',
                        'startTime': cursor, 'endTime': stop - 1, 'limit': 1000})
                    if not isinstance(raw, list) or not raw:
                        raise ValueError('十五分钟行情缺失或格式异常')
                    values = []
                    for row in raw:
                        ts = int(row[0])
                        assert cursor <= ts < stop and ts % INTERVAL == 0
                        numbers = [Decimal(str(row[i])) for i in (1, 2, 3, 4, 5, 7)]
                        assert all(n.is_finite() for n in numbers)
                        o, h, low, close, volume, quote = numbers
                        assert 0 < low <= min(o, close) <= max(o, close) <= h and min(volume, quote) >= 0
                        values.append([ts, *map(str, numbers)])
                    assert all(a[0] < b[0] for a, b in zip(values, values[1:]))
                    store.put(symbol, values)
                    cursor = values[-1][0] + INTERVAL
                    progress.update(requests=api.requests, updated_at_bj=scanner.now_text(), elapsed_seconds=round(time.monotonic() - started, 2))
                    scanner.atomic_json(status_path, progress)
                    # 为原有四小时行情任务保留接口请求余量。
                    time.sleep(.3)
            rows = store.read(symbol, first, end)
            assert [r[0] for r in rows] == list(range(first, end, INTERVAL)), '十五分钟时间序列不完整'
            progress['candles'] += len(rows)
            progress['completed_symbols'] += 1
            print(scanner.encoded({'symbol': symbol, 'candles': len(rows), 'progress': progress['completed_symbols']}), flush=True)
        except Exception as exc:
            progress['failed_symbols'].append({'symbol': symbol, 'error': str(exc)[:500]})
            print(scanner.encoded({'symbol': symbol, 'error': str(exc)[:500]}), flush=True)
        scanner.atomic_json(status_path, progress)
    progress.update(status='complete' if not progress['failed_symbols'] else 'partial', requests=api.requests,
        elapsed_seconds=round(time.monotonic() - started, 2), updated_at_bj=scanner.now_text())
    scanner.atomic_json(status_path, progress)
    print(scanner.encoded(progress), flush=True)
    store.db.close()
    if progress['failed_symbols']:
        raise RuntimeError('十五分钟缓存有失败交易对')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-scope')
    args = parser.parse_args()
    prepare(args.source_scope)


if __name__ == '__main__':
    main()
