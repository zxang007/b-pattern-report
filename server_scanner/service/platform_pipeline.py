#!/usr/bin/env python3
"""四小时扫描成功后更新十五分钟缓存与联合结果，不发送 QQ 消息。"""
from pathlib import Path
import json
import subprocess
import sys

import scanner_service as scanner
from prepare_15m_market import prepare


def main():
    scanner.require_server()
    root = Path('/home/ubuntu/market-pattern-scanner/state')
    proof = json.loads((root / 'platform-validated-engine.json').read_text())
    assert proof.get('joint_all_five'), '需要完成五例两周期联合回归'
    sources = []
    for path in (root / 'runs').glob('live-*/summary.json'):
        summary = json.loads(path.read_text())
        if summary.get('status') == 'complete' and (path.parent / 'by_symbol').is_dir():
            sources.append((summary['end_bj_exclusive'], summary.get('updated_at_bj', ''), path.parent.name))
    source = max(sources)[2]
    prepare(source)
    subprocess.run([sys.executable, str(Path(__file__).with_name('run_platform_market.py')), '--mode', 'live',
        '--regression', str(root / 'platform-validated-engine.json')], check=True)


if __name__ == '__main__':
    main()
