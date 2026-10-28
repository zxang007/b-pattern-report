import csv
import datetime as dt
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from decimal import Decimal

ROOT = Path('/home/ubuntu/market-pattern-scanner')
RELEASE = Path(__file__).parent
OUT = ROOT / 'validation' / 'blue-close-v25'
OUT.mkdir(parents=True, exist_ok=True)
BJ = dt.timezone(dt.timedelta(hours=8))

def timestamp(value):
    return int(dt.datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=BJ).timestamp() * 1000)

source = sqlite3.connect(f'file:{ROOT / "state/scanner.sqlite3"}?mode=ro', uri=True)
results = []
for symbol in ['BTWUSDT', 'AINUSDT', 'ARIAUSDT', 'MERLUSDT', 'ZECUSDT', 'VVVUSDT', 'TUTUSDT']:
    candles = [json.loads(row[0]) for row in source.execute('select payload from candles where symbol=? order by ts', (symbol,))]
    if not candles:
        raise RuntimeError('Missing cached candles: ' + symbol)
    input_path = OUT / (symbol + '.input.json')
    output_path = OUT / (symbol + '.json')
    input_path.write_text(json.dumps({'symbol': symbol, 'candles': candles}))
    subprocess.run([sys.executable, str(RELEASE / 'scanner_service.py'), 'worker', '--input', str(input_path), '--output', str(output_path)], check=True, capture_output=True, timeout=300)
    rows = list(csv.DictReader(output_path.with_suffix('.csv').open()))
    for row in rows:
        blue = [c for c in candles if timestamp(row['blue_start_bj']) <= int(c[0]) < timestamp(row['reclaim_bj'])]
        assert blue and all(Decimal(c[4]) < Decimal(row['yellow_highest_close']) for c in blue), (symbol, row)
    result = {'symbol': symbol, 'candles': len(candles), 'matches': len(rows), 'invalid_blue_closes': 0}
    results.append(result)
    print(json.dumps(result), flush=True)
    (OUT / 'results.json').write_text(json.dumps(results, indent=2))
source.close()
