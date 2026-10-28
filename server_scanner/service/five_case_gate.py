"""Run the five saved historical examples before a changed scanner can scan markets."""
import csv
import datetime as dt
import fcntl
import hashlib
from functools import lru_cache
import json
from pathlib import Path
import subprocess
import sys
from decimal import Decimal

BJ = dt.timezone(dt.timedelta(hours=8))
SYMBOLS = {'ARIAUSDT', 'MERLUSDT', 'ZECUSDT', 'VVVUSDT', 'TUTUSDT'}

@lru_cache(maxsize=10000)
def stamp(value):
    return int(dt.datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=BJ).timestamp() * 1000)


def ensure_five_cases(release: Path, state: Path):
    files = sorted([*release.glob('*.py'), *release.glob('*.js'), *release.glob('*.csv'), *(release / 'regression_inputs').glob('*.json')])
    digest = hashlib.sha256()
    for file in files:
        digest.update(str(file.relative_to(release)).encode())
        digest.update(file.read_bytes())
    signature = digest.hexdigest()
    state.mkdir(parents=True, exist_ok=True)
    with (state / 'five-case-gate.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        proof_path = state / 'five-case-validated.json'
        if proof_path.exists():
            proof = json.loads(proof_path.read_text())
            if proof.get('signature') == signature and proof.get('passed') is True and set(proof.get('cases', {})) == SYMBOLS:
                return proof
        cases = list(csv.DictReader((release / 'verified_examples.csv').open())) + list(csv.DictReader((release / 'new_examples.csv').open()))
        if {case['symbol'] for case in cases} != SYMBOLS or len(cases) != 5:
            raise RuntimeError('Five-case gate requires the five saved examples')
        output = state / 'five-case-validation' / signature[:16]
        output.mkdir(parents=True, exist_ok=True)
        proof = {'signature': signature, 'passed': False, 'cases': {}, 'output': str(output)}
        for case in cases:
            symbol = case['symbol']
            source = release / 'regression_inputs' / (symbol + '.json')
            data = json.loads(source.read_text())
            assert data['symbol'] == symbol
            meta = output / (symbol + '.json')
            process = subprocess.run([sys.executable, str(release / 'scanner_service.py'), 'worker', '--input', str(source), '--output', str(meta)], capture_output=True, text=True, timeout=300)
            if process.returncode:
                raise RuntimeError('Five-case worker failed: ' + symbol)
            rows = list(csv.DictReader(meta.with_suffix('.csv').open()))
            candles = data['candles']
            for row in rows:
                yellow = [Decimal(c[4]) for c in candles if stamp(row['yellow_start_bj']) <= int(c[0]) <= stamp(row['yellow_end_bj'])]
                blue = [Decimal(c[4]) for c in candles if stamp(row['blue_start_bj']) <= int(c[0]) < stamp(row['reclaim_bj'])]
                threshold = Decimal(row['yellow_highest_close'])
                arrow = next(c for c in candles if int(c[0]) == stamp(row['reclaim_bj']))
                assert yellow and max(yellow) == threshold
                assert blue and max(blue) < threshold
                assert Decimal(arrow[4]) > threshold and Decimal(arrow[6]) >= Decimal('900000')
            from regression_reclaims import nearest
            match = nearest(rows, case)
            if not match or not match['within_saved_tolerance']:
                raise RuntimeError('Five-case boundary regression failed: ' + symbol)
            proof['cases'][symbol] = {'passed': True, 'matches': len(rows), 'max_gap_hours': match['max_gap_hours'], 'matched': match['row']}
            # Render the matched sample for review using the same production chart code.
            import scanner_service as scanner
            import scan_four_h_segment_arc_market as market
            import four_h_segment_arc_scan as segment
            import screen_aria_4h_pattern as live
            import draw_four_h_segment_arc_matches_svg as chart
            values = [live.Candle(int(c[0]), *(Decimal(v) for v in c[1:])) for c in candles]
            candidates = market.dedupe(market.market_filter(segment.find_matches(values, symbol, market.scan_args(scanner.core_defaults())), scanner.core_defaults()))
            expected = match['row']
            chosen = next(m for m in candidates if m.yellow_start.open_time == stamp(expected['yellow_start_bj']) and m.yellow_end.open_time == stamp(expected['yellow_end_bj']) and m.reclaim.open_time == stamp(expected['reclaim_bj']) and m.wash_end.open_time == stamp(expected['wash_end_bj']))
            (output / (symbol + '.svg')).write_text(chart.render(symbol, values, chosen))
            (output / 'progress.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2))
            print('FIVE_CASE_PASS ' + symbol, flush=True)
        proof['passed'] = True
        proof['completed_at_bj'] = dt.datetime.now(BJ).isoformat()
        temporary = proof_path.with_suffix('.pending')
        temporary.write_text(json.dumps(proof, ensure_ascii=False, indent=2))
        temporary.replace(proof_path)
        return proof
