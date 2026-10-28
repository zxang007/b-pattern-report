#!/usr/bin/env python3
"""四小时候选与十五分钟平台结构联合筛选，完整保留命中及回踩证据。"""
from __future__ import annotations

import argparse
import csv
from decimal import Decimal
import fcntl
import hashlib
import html
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request
import ssl

import scanner_service as scanner
from screen_aria_4h_pattern import Candle
from platform_door_15m import PlatformSearch
from plot_platform_15m import bj, render
from prepare_15m_market import stamp, INTERVAL
from joint_report_view import regroup_report
from retest_validity import retest_validity


RULES = [
    '先通过现有4小时黄蓝白结构；红箭头已收盘，收盘严格高于整个黄区最高收盘，成交额至少90万 USDT。',
    '15分钟平台由价格往返与支撑低点界定；放量阳线的收盘和成交额均超过该平台此前的最高收盘与最大单根成交额。15分钟不套用90万门槛。',
    '第一次回踩影线回到平台价格范围，且收盘低于启动根收盘；整段影线不破起涨前平台支撑。',
    '两次主要回踩之间有收盘重新离开平台上沿；后续回踩回到第一次下杀影线触及的价格带。允许后一次略低于前一次，但不能破平台。',
    '每次回踩同时保留影线与收盘低点；中途回踩和后续回踩继续记录，不限制三节点，不用固定根数、形态时长或拟合百分比筛选。',
    '15分钟最终确认根的收盘价必须严格高于对应整个蓝区的最高收盘价；相等不通过，蓝区不含红箭头收复根，影线最高价不作此比较。',
    '15分钟的最终确认不能早于对应4小时白区末根收盘；只使用本轮截止时间之前已经收盘的行情。',
    '当前候选另行检查末次主要回踩的影线最低价：之后任何15分钟收盘严格跌破即排除，后来收回也不恢复，相等保留。五例仅验证历史形态复现，不要求通过当前候选的失效筛选。',
]

STYLE = '<style>body{max-width:1200px;margin:24px auto;padding:0 18px;background:#111827;color:#e5e7eb;font:16px/1.8 system-ui}a{color:#93c5fd}img{width:100%;height:auto}table{border-collapse:collapse;width:100%}th,td{padding:8px;border-bottom:1px solid #374151;text-align:left;overflow-wrap:anywhere}section{padding:18px 0;border-top:1px solid #475569}button{padding:7px 12px;background:#93c5fd;border:0;border-radius:6px}summary{cursor:pointer}small{color:#a8b5c8}</style>'


def page(title, content):
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(title) + '</title>' + STYLE + content + '</html>'


def write_csv(path, rows):
    fields = list(rows[0]) if rows else ['symbol', 'group_id', 'launch_bj', 'confirmed_bj']
    pending = path.with_name(path.name + '.pending')
    # 大型结果逐行写出，避免在内存中同时保留两份完整 CSV 字符串。
    with pending.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    pending.replace(path)


def read_groups(source):
    grouped = {}
    blue_store=scanner.Store(source.parent.parent/'scanner.sqlite3')
    blue_cache={}
    with (source / 'matches.csv').open() as handle:
        for row in csv.DictReader(handle):
            key = scanner.group_id(row)
            reclaim = stamp(row['reclaim_bj']) + scanner.BAR_MS
            available = stamp(row['wash_end_bj']) + scanner.BAR_MS
            if key not in grouped:
                grouped[key] = {'group_id': key, 'symbol': row['symbol'], 'reclaim_ms': reclaim,
                    'available_ms': available, 'four_h_variants': 0}
            item = grouped[key]
            begin,end=stamp(row['blue_start_bj']),stamp(row['reclaim_bj'])
            cache_key=(row['symbol'],begin,end)
            if cache_key not in blue_cache:
                raw=blue_store.read(row['symbol'],begin,end)
                assert [v[0] for v in raw]==list(range(begin,end,scanner.BAR_MS)), '蓝区行情不完整'
                blue_cache[cache_key]=max(Decimal(v[4]) for v in raw)
            pair=(available,blue_cache[cache_key])
            variants=item.setdefault('blue_variants',{})
            variants[pair]=variants.get(pair,0)+1
            item['four_h_variants'] += 1
            item['reclaim_ms'] = min(item['reclaim_ms'], reclaim)
            item['available_ms'] = min(item['available_ms'], available)
    blue_store.db.close()
    symbols = {}
    for item in grouped.values():
        symbols.setdefault(item['symbol'], []).append(item)
    return symbols


def eligible_blue_variants(group,confirm_close,confirmed_ms):
    return [(available,blue,count) for (available,blue),count in group['blue_variants'].items()
            if available<=confirmed_ms and confirm_close>blue]


def evidence(symbol, search, door, group):
    c = search.c
    episodes = [[{'bj': bj(c[i].open_time), 'low': str(c[i].low), 'close': str(c[i].close)} for i in episode]
                for episode in door.episodes]
    final = max(door.episodes[-1])
    available = c[final + 1].open_time + INTERVAL
    support, ceiling = c[door.support].low, search.close.value(door.platform, door.launch)
    return {'symbol': symbol, 'group_id': group['group_id'], 'four_h_available_bj': bj(group['available_ms']),
        'confirm_close': str(c[final+1].close), 'blue_max_close': str(group['blue_max_close']),
        'platform_start_bj': bj(c[door.platform].open_time), 'platform_support': str(support),
        'platform_highest_close': str(ceiling), 'launch_bj': bj(c[door.launch].open_time),
        'launch_close': str(c[door.launch].close), 'launch_quote_usdt': str(c[door.launch].quote_volume),
        'platform_max_quote_usdt': str(search.amount.value(door.platform, door.launch)),
        'first_retest_bj': bj(c[min(door.episodes[0], key=lambda i: c[i].low)].open_time),
        'latest_retest_bj': bj(c[min(door.episodes[-1], key=lambda i: c[i].low)].open_time),
        'confirmed_bj': bj(available), 'retest_episodes': json.dumps(episodes, ensure_ascii=False, separators=(',', ':')),
        'retest_episodes_count': len(episodes),
        'later_support_break_bj': bj(c[door.invalidated_at].open_time) if door.invalidated_at < len(c) else '',
        'four_h_variants': group['four_h_variants']}


def validate_assets(run, origin):
    prefix = '/runs/' + run.name + '/'
    checked = 0
    for target in [run / 'index.html', run / 'matches.csv', run / 'excluded.csv', run / 'events.json',
                   *sorted((run / 'charts').glob('*.svg')), *sorted((run / 'groups').glob('*.html'))]:
        relative = target.relative_to(run).as_posix()
        if target.suffix == '.svg':
            assert re.fullmatch(r'charts/[0-9a-f]{32}\.svg', relative)
        if target.parent.name == 'groups':
            assert re.fullmatch(r'groups/[0-9a-f]{24}\.html', relative)
        request = urllib.request.Request(origin + prefix + relative + '?encrypted=1', headers={'Host': '129.226.223.91'})
        with urllib.request.urlopen(request, context=ssl._create_unverified_context(), timeout=20) as response:
            packet = json.load(response)
        assert packet.get('ciphertext') and packet.get('version') == 1, relative
        checked += 1
    return checked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wait-for-cache', action='store_true')
    parser.add_argument('--validation-origin', default='https://127.0.0.1:80', choices=['https://127.0.0.1:80', 'http://127.0.0.1:18767'])
    parser.add_argument('--mode', choices=['live', 'backtest'], default='backtest')
    parser.add_argument('--regression', type=Path, default=Path('/home/ubuntu/market-pattern-scanner/validation/fifteen-m-v1/platform-regression.json'))
    args = parser.parse_args()
    scanner.require_server()
    root = Path('/home/ubuntu/market-pattern-scanner/state')
    lock = (root / 'platform_market.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    module = Path(__file__).parent
    engine_sha = hashlib.sha256((module / 'platform_door_15m.py').read_bytes()).hexdigest()
    regression = json.loads(args.regression.read_text())
    assert regression['all_five_exact'] and regression['all_four_rejected_excluded']
    assert regression['engine_sha256'] == engine_sha, '识别器变更后必须重新完成五例回归'
    manifest = json.loads((root / 'fifteen_m_market_manifest.json').read_text())
    scope = args.mode + '-' + scanner.digest({'source': manifest['source_scope'], 'engine': engine_sha,
        'runner': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'validity': hashlib.sha256((module / 'retest_validity.py').read_bytes()).hexdigest(),
        'plot': hashlib.sha256((module / 'plot_platform_15m.py').read_bytes()).hexdigest()})[:20]
    run = root / 'runs' / scope
    (run / 'charts').mkdir(parents=True, exist_ok=True)
    (run / 'groups').mkdir(exist_ok=True)
    summary = {'scope': scope, 'status': 'preparing', 'stage': '4h_and_15m', 'source_scope': manifest['source_scope'],
        'start_bj': manifest['source_summary']['start_bj'], 'end_bj_exclusive': manifest['source_summary']['end_bj_exclusive'],
        'total_symbols': manifest['total_symbols'], 'completed_symbols': 0, 'failed_symbols': 0,
        'four_h_candidate_symbols': manifest['candidate_symbols'], 'matched_symbols': 0, 'matches': 0, 'charts': 0,
        'validation_ok': False, 'closed_candles_only': True, 'engine_sha256': engine_sha,
        'candidate_filter': 'last_retest_close_never_broken' if args.mode == 'live' else 'historical_unfiltered',
        'price_checked_through_bj': manifest['source_summary']['end_bj_exclusive'],
        'regression_purpose': 'historical_shape_only'}
    scanner.atomic_json(run / 'summary.json', summary)
    scanner.atomic_json(root / 'platform_status.json', summary)
    while True:
        status = json.loads((root / 'fifteen_m_fetch_status.json').read_text())
        assert status['source_scope'] == manifest['source_scope']
        if status['status'] == 'complete':
            break
        if status['status'] == 'partial' or not args.wait_for_cache:
            raise RuntimeError('十五分钟缓存尚未完整，不得标为全量成功')
        time.sleep(10)
    started = time.monotonic()
    source = root / 'runs' / manifest['source_scope']
    groups = read_groups(source)
    store = scanner.Store(root / 'fifteen_m.sqlite3')
    assert store.db.execute('PRAGMA user_version').fetchone()[0] == 15
    row_index, cards, failures = {}, [], []
    historical_groups = 0
    summary['status'] = 'running'
    for info in manifest['symbols']:
        symbol = info['symbol']
        symbol_groups = groups.get(symbol, [])
        if not symbol_groups:
            summary['completed_symbols'] += 1
            continue
        try:
            first = max(info['fetch_start_ms'], (info['onboard'] // INTERVAL) * INTERVAL)
            raw = store.read(symbol, first, manifest['end_ms_exclusive'])
            assert [r[0] for r in raw] == list(range(first, manifest['end_ms_exclusive'], INTERVAL))
            candles = [Candle(int(r[0]), *(Decimal(x) for x in r[1:])) for r in raw]
            search = PlatformSearch(candles)
            doors = search.find(first, first)
            validity = {}
            for door in doors:
                if door.episodes[-1] not in validity:
                    validity[door.episodes[-1]] = retest_validity(search, door)
            for group in symbol_groups:
                variants = []
                for door in doors:
                    if candles[door.platform].open_time < group['reclaim_ms']:
                        continue
                    final = max(door.episodes[-1])
                    if candles[final + 1].open_time + INTERVAL < group['available_ms']:
                        continue
                    confirm_close=candles[final+1].close
                    valid_blue=eligible_blue_variants(group,confirm_close,candles[final+1].open_time+INTERVAL)
                    if not valid_blue: continue
                    selected_blue=min(valid_blue,key=lambda v:(v[1],v[0]))
                    eligible_group={**group,'available_ms':selected_blue[0],'blue_max_close':selected_blue[1],
                        'four_h_variants':sum(v[2] for v in valid_blue)}
                    item = evidence(symbol, search, door, eligible_group)
                    item.update(validity[door.episodes[-1]])
                    variants.append((item, door))
                if not variants:
                    continue
                historical_groups += 1
                for value, door in variants:
                    # 排除记录也保留全部四小时关联，便于核对新筛选造成的变化。
                    key = (symbol, door.platform, door.launch, door.episodes)
                    if key not in row_index:
                        row_index[key] = {**value, 'associated_group_ids': []}
                    row_index[key]['associated_group_ids'].append(group['group_id'])
                if args.mode == 'live':
                    variants = [(value, door) for value, door in variants if value['price_valid']]
                if not variants:
                    continue
                variants.sort(key=lambda pair: (pair[0]['confirmed_bj'], Decimal(pair[0]['launch_quote_usdt'])), reverse=True)
                item, selected = variants[0]
                chart = scanner.digest({'symbol': symbol, 'platform': selected.platform, 'launch': selected.launch, 'episodes': selected.episodes})[:32] + '.svg'
                scanner.atomic_text(run / 'charts' / chart, render(symbol, candles, selected))
                gid = group['group_id']
                source_url = f'/runs/{source.name}/groups/{gid}.html'
                image_url = f'/runs/{scope}/charts/{chart}'
                table = ''.join('<tr>' + ''.join(f'<td>{html.escape(str(value[key]))}</td>' for key in
                    ['launch_bj', 'first_retest_bj', 'latest_retest_bj', 'confirmed_bj', 'platform_support', 'launch_quote_usdt', 'last_retest_low', 'post_retest_min_close', 'last_retest_break_bj', 'confirm_close', 'blue_max_close']) + '</tr>' for value, _ in variants)
                detail = f'<h1>{symbol} · 4小时＋15分钟联合命中</h1><button type="button" data-copy-name="{symbol}">复制 {symbol}</button><p><a href="/runs/{scope}/index.html">返回全部结果</a> · <a href="{source_url}">对应4小时图及全部边界</a></p><p>本组保留 {len(variants)} 个平台与放量段组合。预览取最近形成的一组，完整节点与成交额见 CSV。当前名单要求最后回踩低点此后未被收盘跌破，排除原因另存 CSV；五例用于历史形态回归。</p><a href="{image_url}"><img src="{image_url}" alt="{symbol} 15分钟平台及主要回踩"></a><h2>全部识别记录</h2><table><tr><th>放量启动</th><th>首次回踩</th><th>末次回踩</th><th>可见确认</th><th>平台支撑</th><th>启动成交额USDT</th><th>末次回踩最低价</th><th>此后最低收盘</th><th>首次跌破末次回踩</th><th>15分钟确认收盘</th><th>蓝区最高收盘</th></tr>{table}</table>'
                scanner.atomic_text(run / 'groups' / (gid + '.html'), page(symbol + ' 联合结构', detail))
                cards.append({'symbol': symbol, 'group_id': gid, 'confirmed_bj': item['confirmed_bj'], 'variants': len(variants),
                    'launch_bj': item['launch_bj'], 'latest_retest_bj': item['latest_retest_bj'],
                    'last_retest_low': item['last_retest_low'], 'post_retest_low': item['post_retest_low']})
            summary['completed_symbols'] += 1
        except Exception as exc:
            failures.append({'symbol': symbol, 'error': str(exc)[:700]})
            summary['failed_symbols'] = len(failures)
        active_count = sum(row['price_valid'] or args.mode != 'live' for row in row_index.values())
        summary.update(matches=active_count, historical_matches=len(row_index), excluded_matches=len(row_index)-active_count,
            charts=len(cards), current_symbol=symbol,
            matched_symbols=len({r['symbol'] for r in cards}), updated_at_bj=scanner.now_text())
        scanner.atomic_json(run / 'summary.json', summary)
        scanner.atomic_json(root / 'platform_status.json', summary)
        print(scanner.encoded({'symbol': symbol, 'completed': summary['completed_symbols'], 'matches': len(row_index), 'groups': len(cards), 'failed': len(failures)}), flush=True)
    store.db.close()
    historical_rows = list(row_index.values())
    rows = [row for row in historical_rows if row['price_valid'] or args.mode != 'live']
    excluded = [row for row in historical_rows if not row['price_valid'] and args.mode == 'live']
    summary.update(historical_matched_symbols=len({row['symbol'] for row in historical_rows}),
        historical_structure_groups=historical_groups)
    for row in historical_rows:
        row['associated_group_ids'] = json.dumps(sorted(set(row['associated_group_ids'])), separators=(',', ':'))
    rows.sort(key=lambda row: (row['confirmed_bj'], row['launch_bj'], row['symbol']), reverse=True)
    cards.sort(key=lambda row: (row['confirmed_bj'], row['launch_bj'], row['symbol']), reverse=True)
    write_csv(run / 'matches.csv', rows)
    excluded.sort(key=lambda row: (row['confirmed_bj'], row['launch_bj'], row['symbol']), reverse=True)
    write_csv(run / 'excluded.csv', excluded)
    scanner.atomic_json(run / 'failed_symbols.json', failures)
    scanner.atomic_json(run / 'events.json', {'rules': RULES, 'regression': regression, 'source': manifest,
        'groups': cards, 'retention': '同一15分钟组合共用一条CSV记录，associated_group_ids保存全部4小时关联，不丢弃关联；同一4小时结构聚合显示，预览采用最近形成的15分钟组合。'})
    listing = ''.join(f'<tr><td><button type="button" data-copy-name="{r["symbol"]}">{r["symbol"]}</button></td><td>{r["confirmed_bj"]}</td><td>{r["latest_retest_bj"]}</td><td>{r["variants"]}</td><td><a href="/runs/{scope}/groups/{r["group_id"]}.html">查看15分钟图与4小时依据</a></td></tr>' for r in cards)
    regression_rows = []
    for example in regression['examples']:
        sample = args.regression.parent / 'platform-regression-charts' / (example['symbol'] + '.svg')
        if not sample.is_file():
            # 定时流程复用已验证的样本图，不重新请求历史接口。
            sample = root / 'platform-regression-charts' / (example['symbol'] + '.svg')
        assert sample.is_file(), '缺少五例自动复测图'
        name = hashlib.sha256(sample.read_bytes()).hexdigest()[:32] + '.svg'
        shutil.copyfile(sample, run / 'charts' / name)
        image = f'/runs/{scope}/charts/{name}'
        regression_rows.append('<tr><td>' + example['symbol'] + '</td><td>' + ' → '.join(example['user_nodes'])
            + f'</td><td>自动命中，时间误差0<br><a href="{image}"><img src="{image}" alt="{example["symbol"]} 自动复测输出"></a></td></tr>')
    regression_table = ''.join(regression_rows)
    peak_proof = json.loads((root / 'retest-peak-validation.json').read_text())
    assert peak_proof['validity_sha256'] == hashlib.sha256((module / 'retest_validity.py').read_bytes()).hexdigest()
    peak_rows = ''.join('<tr>' + ''.join('<td>' + html.escape(str(example[key])) + '</td>' for key in
        ['symbol', 'last_retest_bj', 'last_retest_low', 'highest_bj', 'highest_price', 'close_before_highest', 'last_retest_break_bj'])
        + '<td>' + ('未跌破' if example['held_before_highest'] else '曾跌破，严格规则排除') + '</td></tr>' for example in peak_proof['examples'])
    peak_table = '<details><summary>五例末次回踩至窗口高点核对：' + str(peak_proof['held_before_highest']) + '/5 未跌破</summary><p>各例使用已保存样本窗口，最高点仅用于事后核对。按收盘价检查，单纯影线下探不排除。ARIA 的影线下探不影响其收盘守住回踩低点。最高点所在K线内的先后顺序不作推断。</p><table><tr><th>币名</th><th>末次回踩</th><th>影线低点</th><th>窗口高点时间</th><th>最高价</th><th>高点前最低收盘</th><th>首次跌破时间</th><th>结论</th></tr>' + peak_rows + '</table></details>'

    rules_html = '<ol>' + ''.join('<li>' + html.escape(rule) + '</li>' for rule in RULES) + '</ol>'
    body = f'<h1>4小时＋15分钟联合筛选</h1><p>本轮覆盖 {summary["total_symbols"]} 个币安 USDT 永续交易对，4小时候选 {manifest["candidate_symbols"]} 个币；两级同时命中 <strong>{summary["matched_symbols"]}</strong> 个币、{len(cards)} 组4小时结构，完整保留 {len(rows)} 条15分钟组合。</p><p>窗口：{summary["start_bj"]} 至 {summary["end_bj_exclusive"]}（不含截止时刻后的K线）。按最近可见确认时间由新到旧排列；同一4小时结构的多次15分钟回踩聚合展示。</p><p><a href="matches.csv">当前候选CSV</a> · <a href="excluded.csv">跌破排除CSV</a> · <a href="events.json">规则、来源与五例回归证据</a> · <a href="/runs/{source.name}/index.html">全部4小时候选</a></p><details><summary>本轮统一规则</summary>{rules_html}</details><details><summary>历史形态复测：五例5/5通过、旧错例4/4排除（不要求五例通过当前失效筛选）</summary><table>{regression_table}</table></details><table><tr><th>币名（点按复制USDT）</th><th>最近可见确认</th><th>末次主要回踩</th><th>保留组合</th><th>图形</th></tr>{listing}</table>'
    body = body.replace('<table><tr><th>币名（点按复制USDT）</th>', peak_table + '<table><tr><th>币名（点按复制USDT）</th>', 1)
    scanner.atomic_text(run / 'index.html', page('全市场联合筛选', body))
    scanner.atomic_json(run / 'summary.json', summary)
    summary.update(regroup_report(run))
    if failures:
        summary.update(status='partial', elapsed_seconds=round(time.monotonic() - started, 2))
        scanner.atomic_json(run / 'summary.json', summary)
        raise RuntimeError('部分交易对失败，未发送完成通知')
    assert summary['completed_symbols'] == summary['total_symbols']
    checked = validate_assets(run, args.validation_origin)
    summary.update(status='complete', validation_ok=True, encrypted_assets_verified=checked,
        structure_groups=len(cards), charts=len(list((run / 'charts').glob('*.svg'))),
        elapsed_seconds=round(time.monotonic() - started, 2), updated_at_bj=scanner.now_text())
    scanner.atomic_json(run / 'summary.json', summary)
    scanner.atomic_json(root / 'platform_status.json', summary)
    print(scanner.encoded({'summary': summary, 'url': f'https://129.226.223.91:80/runs/{scope}/index.html'}), flush=True)
    scanner.atomic_json(root / 'platform_last_result.json', {'scope': scope, 'summary': summary,
        'url': f'https://129.226.223.91:80/runs/{scope}/index.html'})


if __name__ == '__main__':
    main()
