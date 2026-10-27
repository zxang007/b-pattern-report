#!/usr/bin/env python3
"""按实际十五分钟信号合并展示，完整保留各四小时候选及其识别记录。"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
import re
import shutil

import scanner_service as scanner


def signal_groups(cards):
    groups = {}
    for card in cards:
        key = tuple(card[name] for name in ['symbol', 'launch_bj', 'confirmed_bj', 'latest_retest_bj'])
        if key not in groups:
            groups[key] = {'signal_id': scanner.digest({'display_signal': key})[:24],
                **{name: card[name] for name in ['symbol', 'launch_bj', 'confirmed_bj', 'latest_retest_bj']}, 'sources': []}
        if card['group_id'] not in {c['group_id'] for c in groups[key]['sources']}:
            groups[key]['sources'].append(dict(card))
    return sorted(groups.values(), key=lambda item: (item['confirmed_bj'], item['launch_bj'], item['symbol']), reverse=True)


def page(title, style, content):
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(title) + '</title>' + style + content + '</html>'


def regroup_report(run):
    events = json.loads((run / 'events.json').read_text())
    summary = json.loads((run / 'summary.json').read_text())
    signals = signal_groups(events['groups'])
    source_scope = events['source']['source_scope']
    original = (run / 'index.html').read_text()
    style = ''.join(re.findall(r'<style>.*?</style>', original, re.S))
    raw_ids = {item['group_id'] for item in events['groups']}
    for signal in signals:
        assert signal['signal_id'] not in raw_ids
        images = []
        for child in signal['sources']:
            detail = (run / 'groups' / (child['group_id'] + '.html')).read_text()
            urls = re.findall(r'<img[^>]+src="([^"]+)"', detail)
            for url in urls:
                assert re.fullmatch(r'/runs/(?:live|backtest)-[0-9a-f]{20}/charts/[0-9a-f]{32}\.svg', url)
                if url not in images:
                    images.append(url)
        assert images
        first = images[0]
        symbol = html.escape(signal['symbol'])
        content = f'<h1>{symbol} · 同一15分钟信号</h1><button type="button" data-copy-name="{symbol}">复制 {symbol}</button><p><a href="/runs/{run.name}/index.html">返回全部信号</a></p><p>放量启动：{signal["launch_bj"]}；主要回踩：{signal["latest_retest_bj"]}；15分钟确认：{signal["confirmed_bj"]}。</p><p>此信号关联 {len(signal["sources"])} 组4小时依据，首页合并为一行。全部依据及各自组合保留在下方。</p><a href="{first}"><img src="{first}" alt="{symbol} 十五分钟信号"></a>'
        if len(images) > 1:
            content += '<details><summary>其他平台边界图</summary>' + ''.join(f'<a href="{url}"><img src="{url}" alt="{symbol} 平台边界备选"></a>' for url in images[1:]) + '</details>'
        content += '<h2>关联的4小时依据</h2><p>各依据可能共享15分钟组合；组合数不是独立命中次数。</p><table><tr><th>依据</th><th>该依据下保留的组合</th><th>4小时图</th><th>完整记录</th></tr>'
        for index, child in enumerate(signal['sources'], 1):
            gid = child['group_id']
            content += f'<tr><td>{index}</td><td>{child["variants"]}</td><td><a href="/runs/{source_scope}/groups/{gid}.html">黄蓝白结构及边界</a></td><td><a href="/runs/{run.name}/groups/{gid}.html">该依据的全部识别记录</a></td></tr>'
        content += '</table>'
        scanner.atomic_text(run / 'groups' / (signal['signal_id'] + '.html'), page(signal['symbol'] + ' 信号与依据', style, content))
    markers = ['<table data-signal-list="1">', '<table><tr><th>币名（点按复制USDT）</th>']
    positions = [original.index(marker) for marker in markers if marker in original]
    assert positions, '未找到报告主列表，未修改原首页'
    prefix = original[:min(positions)]
    intro = f'<h1>4小时＋15分钟联合筛选</h1><p>本轮覆盖 {summary["total_symbols"]} 个币安 USDT 永续交易对，4小时候选 {summary["four_h_candidate_symbols"]} 个币；两级同时命中 <strong>{summary["matched_symbols"]}</strong> 个币、<strong>{len(signals)}</strong> 个15分钟信号，关联 {len(events["groups"])} 组4小时依据，完整保留 {summary["matches"]} 条15分钟组合。</p>'
    prefix, replaced = re.subn(r'<h1>4小时＋15分钟联合筛选</h1><p>本轮覆盖.*?</p>', lambda _: intro, prefix, count=1, flags=re.S)
    assert replaced == 1
    prefix = prefix.replace('按最近可见确认时间由新到旧排列；同一4小时结构的多次15分钟回踩聚合展示。',
        '按15分钟确认时间由新到旧排列；相同放量启动和回踩确认只显示一行，全部4小时依据列入明细。')
    table = '<table data-signal-list="1"><tr><th>币名（点按复制USDT）</th><th>放量启动</th><th>15分钟确认</th><th>末次主要回踩</th><th>4小时依据</th><th>图形</th></tr>'
    for item in signals:
        symbol = html.escape(item['symbol'])
        table += f'<tr><td><button type="button" data-copy-name="{symbol}">{symbol}</button></td><td>{item["launch_bj"]}</td><td>{item["confirmed_bj"]}</td><td>{item["latest_retest_bj"]}</td><td>{len(item["sources"])} 组</td><td><a href="/runs/{run.name}/groups/{item["signal_id"]}.html">查看图形与依据</a></td></tr>'
    scanner.atomic_text(run / 'index.html', prefix + table + '</table></html>')
    events['signals'] = signals
    events['display_grouping'] = 'exact_15m_signal'
    scanner.atomic_json(run / 'events.json', events)
    summary.update(display_signals=len(signals), display_grouping='exact_15m_signal')
    scanner.atomic_json(run / 'summary.json', summary)
    root = run.parent.parent
    for name in ['platform_status.json', 'platform_last_result.json']:
        target = root / name
        if target.is_file():
            value = json.loads(target.read_text())
            if value.get('scope') == run.name:
                if 'summary' in value:
                    value['summary'] = summary
                else:
                    value.update(display_signals=len(signals), display_grouping='exact_15m_signal')
                scanner.atomic_json(target, value)
    assert {c['group_id'] for signal in signals for c in signal['sources']} == raw_ids
    return {'display_signals': len(signals), 'display_grouping': 'exact_15m_signal'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--symbols', default='', help='仅显示指定币名的诊断统计，不改变合并范围。')
    args = parser.parse_args()
    scanner.require_server()
    run = args.run.resolve()
    assert re.fullmatch(r'(?:live|backtest)-[0-9a-f]{20}', run.name)
    events = json.loads((run / 'events.json').read_text())
    signals = signal_groups(events['groups'])
    print(scanner.encoded({'before': len(events['groups']), 'after': len(signals),
        'examples': {symbol: {'before': sum(c['symbol'] == symbol for c in events['groups']),
            'after': sum(c['symbol'] == symbol for c in signals)} for symbol in args.symbols.split(',') if symbol}}), flush=True)
    if args.apply:
        backup = run.parent.parent.parent / 'validation' / 'signal-view-backups' / run.name
        backup.mkdir(parents=True, exist_ok=True)
        for name in ['index.html', 'events.json', 'summary.json']:
            if not (backup / name).exists():
                shutil.copyfile(run / name, backup / name)
        print(scanner.encoded(regroup_report(run)), flush=True)


if __name__ == '__main__':
    main()
