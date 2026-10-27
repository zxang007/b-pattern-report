#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import fcntl
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal


BAR_MS = 4 * 60 * 60 * 1000
BJ = dt.timezone(dt.timedelta(hours=8))
CORE_FILES = (
    "four_h_segment_arc_scan.py", "scan_four_h_segment_arc_market.py",
    "market_archive_pattern_scan.py", "screen_aria_4h_pattern.py",
)
DRAW_FILES = ("draw_four_h_segment_arc_matches_svg.py", "draw_market_current_logic_from_csv.py")


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def now_text() -> str:
    return dt.datetime.now(BJ).isoformat(timespec="seconds")


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    pending.write_text(content, encoding="utf-8")
    pending.replace(path)


def atomic_json(path: Path, value) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def require_server() -> None:
    if sys.platform != "linux" or not (
        os.environ.get("SSH_CONNECTION") or os.environ.get("MARKET_SCANNER_SERVER") == "qqbot"
    ):
        raise SystemExit("行情请求只允许在 qqbot Linux 服务器执行；本机只能离线检查。")


def rolling_start(cutoff_ms: int) -> int:
    # 沿用 GitHub 日报的日期范围：北京时间所在月份向前两个月的十五日，按 UTC 取日界。
    day = dt.datetime.fromtimestamp(cutoff_ms / 1000, BJ).date()
    index = day.year * 12 + day.month - 1 - 2
    date = dt.datetime(index // 12, index % 12 + 1, 15, tzinfo=dt.timezone.utc)
    return int(date.timestamp() * 1000)


def date_window(start: str, end: str) -> tuple[int, int]:
    first = dt.datetime.combine(dt.date.fromisoformat(start), dt.time.min, BJ)
    last = dt.datetime.combine(dt.date.fromisoformat(end) + dt.timedelta(days=1), dt.time.min, BJ)
    if last <= first:
        raise ValueError("结束日期不得早于开始日期。")
    return int(first.timestamp() * 1000), int(last.timestamp() * 1000)


class PublicAPI:
    def __init__(self, timeout: float = 20, retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        self.requests = 0

    def get(self, path: str, params: dict | None = None):
        require_server()
        url = "https://fapi.binance.com" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": "market-pattern-scanner/2"})
        for attempt in range(self.retries):
            self.requests += 1
            try:
                # 使用系统证书校验，失败时不降级为不安全连接。
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    value = json.load(response)
                time.sleep(0.2)
                return value
            except urllib.error.HTTPError as exc:
                if exc.code == 418 or exc.code not in (408, 429, 500, 502, 503, 504):
                    raise RuntimeError(f"行情接口 HTTP {exc.code}: {path}") from None
                wait = max(float(exc.headers.get("Retry-After", "0")), 2 ** (attempt + 1))
            except (urllib.error.URLError, TimeoutError, OSError):
                wait = 2 ** (attempt + 1)
            if attempt + 1 == self.retries:
                raise RuntimeError(f"行情接口重试失败: {path}")
            time.sleep(min(wait, 120))


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL, ts INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(symbol, ts)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                scope TEXT NOT NULL, symbol TEXT NOT NULL, status TEXT NOT NULL,
                updated TEXT NOT NULL, error TEXT, seconds REAL,
                PRIMARY KEY(scope, symbol)
            );
            CREATE TABLE IF NOT EXISTS structures (
                group_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, signature TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, active INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                group_id TEXT NOT NULL, kind TEXT NOT NULL, seen_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observed_symbols (symbol TEXT PRIMARY KEY);
        """)

    def read(self, symbol: str, start: int, end: int) -> list[list]:
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM candles WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts",
            (symbol, start, end),
        )]

    def put(self, symbol: str, rows: list[list]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO candles VALUES(?,?,?)", [
                (symbol, int(row[0]), encoded(row)) for row in rows
            ])

    def job(self, scope: str, symbol: str, status: str, error="", seconds=0) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?)",
                            (scope, symbol, status, now_text(), error, seconds))

    def completed(self, scope: str, symbol: str) -> bool:
        row = self.db.execute("SELECT status FROM jobs WHERE scope=? AND symbol=?", (scope, symbol)).fetchone()
        return row is not None and row[0] == "complete"

    def observe(self, scope: str, symbol: str, rows: list[dict]) -> None:
        groups: dict[str, list[dict]] = {}
        for row in rows:
            groups.setdefault(group_id(row), []).append(row)
        stamp = now_text()
        baseline = self.db.execute("SELECT 1 FROM observed_symbols WHERE symbol=?", (symbol,)).fetchone() is None
        previous = {row["group_id"]: row for row in self.db.execute(
            "SELECT * FROM structures WHERE symbol=? AND active=1", (symbol,)
        )}
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO observed_symbols VALUES(?)", (symbol,))
            for key, variants in groups.items():
                signature = digest(sorted(encoded(row) for row in variants))
                old = self.db.execute("SELECT * FROM structures WHERE group_id=?", (key,)).fetchone()
                kind = ("baseline" if baseline else "new") if old is None else "reappeared" if not old["active"] else "updated"
                if old is None or old["signature"] != signature or not old["active"]:
                    self.db.execute("INSERT INTO events(scope,group_id,kind,seen_at,payload) VALUES(?,?,?,?,?)",
                                    (scope, key, kind, stamp, encoded(variants)))
                self.db.execute("INSERT OR REPLACE INTO structures VALUES(?,?,?,?,?,1)",
                                (key, symbol, signature, old["first_seen"] if old else stamp, stamp))
            for key in previous.keys() - groups.keys():
                self.db.execute("UPDATE structures SET active=0,last_seen=? WHERE group_id=?", (stamp, key))
                self.db.execute("INSERT INTO events(scope,group_id,kind,seen_at,payload) VALUES(?,?,?,?,?)",
                                (scope, key, "not_in_current_window", stamp, "[]"))


def group_id(row: dict) -> str:
    return digest([row["symbol"], row["blue_low_bj"], row["wash_start_bj"]])[:24]


def missing_ranges(times: set[int], start: int, end: int) -> list[tuple[int, int]]:
    ranges = []
    first = None
    for stamp in range(start, end, BAR_MS):
        if stamp not in times and first is None:
            first = stamp
        elif stamp in times and first is not None:
            ranges.append((first, stamp))
            first = None
    if first is not None:
        ranges.append((first, end))
    return ranges


def load_cached(store: Store, api, symbol: str, start: int, end: int, onboard: int = 0) -> list[list]:
    # 上市当根可能不是整点开盘，取其所在四小时区间；真实数据仍逐根检查。
    first = max(start, (onboard // BAR_MS) * BAR_MS)
    existing = store.read(symbol, first, end)
    for begin, stop in missing_ranges({int(row[0]) for row in existing}, first, end):
        cursor = begin
        while cursor < stop:
            response = api.get("/fapi/v1/klines", {
                "symbol": symbol, "interval": "4h", "startTime": cursor,
                "endTime": stop - 1, "limit": 499,
            })
            if not isinstance(response, list):
                raise ValueError("K 线接口返回格式错误。")
            rows = []
            for raw in response:
                if len(raw) < 8:
                    raise ValueError("K 线字段缺失。")
                stamp = int(raw[0])
                if not cursor <= stamp < stop or stamp % BAR_MS:
                    raise ValueError("K 线时间边界异常。")
                values = [str(raw[i]) for i in (1, 2, 3, 4, 5, 7)]
                numbers = list(map(Decimal, values))
                if not all(n.is_finite() for n in numbers):
                    raise ValueError("K 线包含无效数值。")
                o, h, low, c, volume, quote = numbers
                if min(o, h, low, c) <= 0 or low > min(o, c) or h < max(o, c) or min(volume, quote) < 0:
                    raise ValueError("K 线价格或成交量异常。")
                rows.append([stamp, *values])
            if not rows:
                break
            if any(int(a[0]) >= int(b[0]) for a, b in zip(rows, rows[1:])):
                raise ValueError("K 线接口返回重复或倒序数据。")
            store.put(symbol, rows)
            cursor = int(rows[-1][0]) + BAR_MS
    result = store.read(symbol, first, end)
    expected = list(range(first, end, BAR_MS))
    actual = [int(row[0]) for row in result]
    if not result or actual != expected:
        raise ValueError(f"历史 K 线不完整: expected={len(expected)} actual={len(actual)}")
    return result


def core_defaults():
    import scan_four_h_segment_arc_market as market
    previous = sys.argv
    try:
        sys.argv = ["scan_four_h_segment_arc_market.py"]
        return market.parse_args()
    finally:
        sys.argv = previous


def worker(source: Path, output: Path) -> None:
    import four_h_segment_arc_scan as segment
    import scan_four_h_segment_arc_market as market
    import screen_aria_4h_pattern as live
    data = json.loads(source.read_text())
    args = core_defaults()
    candles = [live.Candle(int(row[0]), *(Decimal(v) for v in row[1:])) for row in data["candles"]]
    raw = segment.find_matches(candles, data["symbol"], market.scan_args(args))
    selected = market.dedupe(market.market_filter(raw, args), args.merge_cluster_gap_bars)
    selected.sort(key=lambda match: (market.market_rank_key(match), -match.wash_end.open_time))
    # 不截断每币数量，也不更改识别、过滤、排序和市场去重规则。
    market.write_rows(str(output.with_suffix(".csv")), selected)
    atomic_json(output, {"raw_candidates": len(raw), "matches": len(selected)})


def list_symbols(api, requested: str | None = None) -> list[dict]:
    data = api.get("/fapi/v1/exchangeInfo")
    selected = {s.strip().upper() for s in requested.split(",") if s.strip()} if requested else None
    infos = []
    for item in data.get("symbols", []):
        symbol = item.get("symbol", "")
        if selected is not None and symbol not in selected:
            continue
        if item.get("quoteAsset") != "USDT" or item.get("contractType") != "PERPETUAL":
            continue
        if selected is None and (item.get("status") != "TRADING" or symbol.startswith("1000")):
            continue
        if not re.fullmatch(r"[^/\\.\s]+USDT", symbol):
            raise ValueError("交易对名称包含异常字符。")
        infos.append({"symbol": symbol, "onboard": int(item.get("onboardDate", 0))})
    if selected is not None and selected != {item["symbol"] for item in infos}:
        raise ValueError("指定的 USDT 永续交易对不存在。")
    if not infos:
        raise ValueError("交易对列表为空，不得当作扫描成功。")
    return sorted(infos, key=lambda item: item["symbol"])


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def export_report(store: Store, root: Path, run: Path, summary: dict, infos: list[dict], scope: str, live_run: bool) -> None:
    rows = []
    fields = None
    for info in infos:
        path = run / "by_symbol" / (info["symbol"] + ".csv")
        if store.completed(scope, info["symbol"]) and path.exists():
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                fields = reader.fieldnames
                rows.extend(reader)
    rows.sort(key=lambda row: (row["wash_end_bj"], row["symbol"]), reverse=True)
    fields = fields or ["symbol", "blue_low_bj", "wash_start_bj", "wash_end_bj"]
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(run / "matches.csv", text.getvalue())
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(group_id(row), []).append(row)
    cards = []
    chart_count = 0
    if rows:
        import draw_four_h_segment_arc_matches_svg as chart
        import draw_market_current_logic_from_csv as report
        import screen_aria_4h_pattern as live
    charts = run / "charts"
    charts.mkdir(exist_ok=True)
    for key, variants in groups.items():
        row = variants[0]
        contents = []
        first_chart = None
        for variant in variants:
            filename = digest(variant)[:32] + ".svg"
            destination = charts / filename
            if not destination.exists():
                first_ms = max(report.parse_bj_ms(variant["yellow_start_bj"]) - 8 * BAR_MS,
                               int(dt.datetime.fromisoformat(summary["start_bj"]).timestamp() * 1000))
                last_ms = min(report.parse_bj_ms(variant["wash_end_bj"]) + 13 * BAR_MS,
                              int(dt.datetime.fromisoformat(summary["end_bj_exclusive"]).timestamp() * 1000))
                raw_candles = store.read(variant["symbol"], first_ms, last_ms)
                candles = [live.Candle(int(c[0]), *(Decimal(v) for v in c[1:])) for c in raw_candles]
                match = report.row_to_match(variant, {c.open_time: c for c in candles})
                atomic_text(destination, chart.render(variant["symbol"], candles, match))
            chart_count += 1
            first_chart = first_chart or filename
            cells = "".join(f"<td>{html.escape(variant.get(col, ''))}</td>" for col in
                            ("yellow_start_bj", "yellow_end_bj", "yellow_hold_close", "wash_end_bj"))
            contents.append(f'<tr>{cells}<td><a href="charts/{filename}">打开图形</a></td></tr>')
        cards.append(f"<details><summary>{html.escape(row['symbol'])} · 下杀 {row['blue_low_bj']} · 洗盘 {row['wash_start_bj']} · {len(variants)} 条候选</summary>"
                     f'<a href="charts/{first_chart}"><img loading="lazy" src="charts/{first_chart}" alt="{html.escape(row["symbol"])} 黄蓝白结构图"></a>'
                     f"<div class='table'><table><tr><th>拉升开始</th><th>拉升结束</th><th>hold</th><th>洗盘结束</th><th>全部候选图</th></tr>{''.join(contents)}</table></div></details>")
    summary.update({"matches": len(rows), "charts": chart_count, "structure_groups": len(groups), "updated_at_bj": now_text()})
    atomic_json(run / "summary.json", summary)
    events = [dict(row) for row in store.db.execute("SELECT * FROM events WHERE scope=? ORDER BY id", (scope,))]
    atomic_json(run / "events.json", events)
    failed = [dict(row) for row in store.db.execute("SELECT symbol,error FROM jobs WHERE scope=? AND status!='complete'", (scope,))]
    atomic_json(run / "failed_symbols.json", failed)
    page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>四小时形态扫描</title><style>body{{max-width:1200px;margin:32px auto;padding:0 20px;background:#111827;color:#e5e7eb;font:16px/1.7 system-ui}}a{{color:#93c5fd}}details{{margin:12px 0;padding:12px;border:1px solid #374151}}summary{{cursor:pointer}}table{{border-collapse:collapse;white-space:nowrap}}td,th{{padding:8px 14px;border-bottom:1px solid #374151}}.table{{overflow:auto}}p{{color:#cbd5e1}}img{{display:block;width:100%;height:auto;margin:16px 0}}</style>
<h1>四小时形态扫描</h1><p>更新时间：{summary['updated_at_bj']} · 状态：{html.escape(summary['status'])}<br>
范围（北京时间）：{summary['start_bj']} 至 {summary['end_bj_exclusive']}（不含终点）<br>
已完成 {summary['completed_symbols']}/{summary['total_symbols']} 个交易对，{len(rows)} 条候选，{len(groups)} 组结构。<br>
只使用已收盘 K 线。保留全部候选；同一下杀低点和洗盘起点归组展示。未出现在当前窗口不等于交易信号失效。</p>
<p><a href="matches.csv">完整候选 CSV</a> · <a href="summary.json">运行状态</a> · <a href="events.json">结构变化记录</a> · <a href="failed_symbols.json">失败列表</a></p>
{''.join(cards) or '<p>当前没有已完成扫描的命中候选；请同时检查完成数量与失败列表。</p>'}</html>"""
    atomic_text(run / "index.html", page)
    if live_run:
        link = root / "latest.pending"
        link.unlink(missing_ok=True)
        link.symlink_to(run.relative_to(root), target_is_directory=True)
        link.replace(root / "latest")
        atomic_json(root / "status.json", summary)
        atomic_text(root / "index.html", '<meta http-equiv="refresh" content="0;url=latest/index.html">')


def run_scan(options) -> int:
    require_server()
    config = json.loads(Path(options.config).read_text())
    root = Path(config["state_dir"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / "scan.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("已有扫描进程，跳过重复启动。", flush=True)
        return 0
    api = PublicAPI(config.get("timeout_seconds", 20), config.get("http_retries", 3))
    current = int(api.get("/fapi/v1/time")["serverTime"])
    closed_end = ((current - config.get("close_delay_seconds", 90) * 1000) // BAR_MS) * BAR_MS
    live_run = options.start_date is None
    start, end = (rolling_start(closed_end), closed_end) if live_run else date_window(options.start_date, options.end_date)
    if end > closed_end:
        raise ValueError("请求范围包含未收盘 K 线，请缩短结束日期。")
    infos = list_symbols(api, options.symbols)
    infos = [info for info in infos if info["onboard"] < end]
    if not infos:
        raise ValueError("指定窗口内没有已上市且可扫描的交易对。")
    sources = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest() for name in CORE_FILES + DRAW_FILES}
    signature = digest({"start": start, "end": end, "sources": sources, "service": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    scope = ("live-" if live_run else "backtest-") + signature[:20]
    run = root / "runs" / scope
    output = run / "by_symbol"
    output.mkdir(parents=True, exist_ok=True)
    store = Store(root / "scanner.sqlite3")
    summary = {
        "status": "running", "code_source": config["code_source"], "source_sha256": sources,
        "scope": scope, "start_bj": dt.datetime.fromtimestamp(start / 1000, BJ).isoformat(),
        "end_bj_exclusive": dt.datetime.fromtimestamp(end / 1000, BJ).isoformat(),
        "date_boundary": "UTC 4h，展示时间为北京时间", "closed_candles_only": True,
        "total_symbols": len(infos), "completed_symbols": 0, "failed_symbols": 0,
        "requests_this_invocation": 0, "resumed_symbols": 0,
        "notifications": "disabled", "pid": os.getpid(),
    }
    atomic_json(run / "symbols.json", infos)
    failures = 0
    start_clock = time.monotonic()
    for index, info in enumerate(infos, 1):
        symbol = info["symbol"]
        csv_path = output / (symbol + ".csv")
        if store.completed(scope, symbol) and csv_path.exists():
            summary["resumed_symbols"] += 1
            summary["completed_symbols"] += 1
            continue
        before = time.monotonic()
        store.job(scope, symbol, "running")
        try:
            candles = load_cached(store, api, symbol, start, end, info["onboard"])
            source = output / (symbol + ".input.json")
            atomic_json(source, {"symbol": symbol, "candles": candles})
            meta = output / (symbol + ".json")
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "worker", "--input", str(source), "--output", str(meta)],
                capture_output=True, text=True, timeout=config.get("symbol_timeout_seconds", 240),
            )
            if result.returncode:
                raise RuntimeError(f"识别子进程失败 code={result.returncode}: {result.stderr[-800:]}")
            source.unlink(missing_ok=True)
            if live_run:
                store.observe(scope, symbol, read_csv(csv_path))
            store.job(scope, symbol, "complete", seconds=round(time.monotonic() - before, 3))
            summary["completed_symbols"] += 1
            print(encoded({"progress": f"{index}/{len(infos)}", "symbol": symbol,
                           **json.loads(meta.read_text()), "seconds": round(time.monotonic() - before, 2)}), flush=True)
        except Exception as exc:
            failures += 1
            summary["failed_symbols"] = failures
            store.job(scope, symbol, "failed", str(exc)[:1000], round(time.monotonic() - before, 3))
            print(encoded({"symbol": symbol, "status": "failed", "error": str(exc)[:1000]}), flush=True)
        summary["current_symbol"] = symbol
        summary["elapsed_seconds"] = round(time.monotonic() - start_clock, 2)
        summary["requests_this_invocation"] = api.requests
        atomic_json(run / "summary.json", summary)
        if live_run:
            atomic_json(root / "status.json", summary)
        if index == 1 or index % 25 == 0:
            export_report(store, root, run, summary, infos, scope, live_run)
    summary["status"] = "complete" if failures == 0 else "partial"
    summary["elapsed_seconds"] = round(time.monotonic() - start_clock, 2)
    summary["requests_this_invocation"] = api.requests
    export_report(store, root, run, summary, infos, scope, live_run)
    print(encoded(summary), flush=True)
    store.db.close()
    return 1 if failures else 0


def cli() -> int:
    parser = argparse.ArgumentParser(description="qqbot 独立行情扫描服务；本机仅离线验证。")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--config", required=True)
    run.add_argument("--symbols")
    run.add_argument("--start-date")
    run.add_argument("--end-date")
    child = sub.add_parser("worker")
    child.add_argument("--input", required=True)
    child.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "worker":
        worker(Path(args.input), Path(args.output))
        return 0
    if bool(args.start_date) != bool(args.end_date):
        parser.error("历史回测必须同时指定开始和结束日期。")
    try:
        return run_scan(args)
    except Exception as exc:
        config = json.loads(Path(args.config).read_text())
        status = Path(config["state_dir"]) / "status.json"
        if args.start_date is None:
            old = json.loads(status.read_text()) if status.exists() else {}
            old.update({"status": "failed", "error": str(exc)[:1000], "updated_at_bj": now_text()})
            atomic_json(status, old)
        raise


if __name__ == "__main__":
    raise SystemExit(cli())
