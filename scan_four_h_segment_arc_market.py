#!/usr/bin/env python3
from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import csv
import datetime as dt
import ssl
import sys
import time
from decimal import Decimal

import binance_archive_pattern_scan as archive
import four_h_segment_arc_scan as segment_scan
import screen_aria_4h_pattern as live


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量扫描 Binance USDⓈ-M 永续 4小时区域结构。")
    parser.add_argument("--quote", default="USDT")
    parser.add_argument("--symbols", help="只扫描指定交易对，逗号分隔，例如 ARIAUSDT,VINEUSDT。")
    parser.add_argument("--symbols-file", help="从本地文件读取交易对，每行一个 symbol。用于 Railway 等无法访问 exchangeInfo 的环境。")
    parser.add_argument("--start-date", default="2025-01-01", help="UTC 起始日期，默认 2025-01-01。")
    parser.add_argument("--end-date", default=dt.datetime.now(dt.timezone.utc).date().isoformat(), help="UTC 结束日期，默认今天。")
    parser.add_argument("--per-symbol-limit", type=int, default=5, help="每个币最多输出多少个候选，默认 5。")
    parser.add_argument("--limit", type=int, default=300, help="总输出候选上限，默认 300。")
    parser.add_argument("--csv-file", default="four_h_segment_arc_market_matches.csv")
    parser.add_argument("--archive-granularity", choices=("api", "auto", "monthly", "daily"), default="auto", help="K线来源：api=实时K线接口；auto=完整月份 monthly、未结束月份 daily。")
    parser.add_argument("--archive-sleep", type=float, default=0.12, help="每个交易对扫描后暂停秒数，默认 0.12。")
    parser.add_argument("--workers", type=int, default=1, help="并发扫描线程数，默认 1。使用 Binance archive 时可适当提高。")
    parser.add_argument("--executor", choices=("thread", "process"), default="thread", help="并发执行器：thread 适合网络瓶颈，process 适合当前 CPU 形态扫描，默认 thread。")
    parser.add_argument("--progress-every", type=int, default=1, help="无命中进度每多少个交易对打印一次，默认 1。命中和错误始终打印。")
    parser.add_argument("--max-symbols", type=int, default=0, help="最多扫描多少个交易对，0 表示全部。")
    parser.add_argument("--include-multiplier-symbols", action="store_true", help="包含 1000/1000000 等面值倍数合约，默认自动市场扫描时排除。")
    parser.add_argument("--merge-cluster-gap-bars", type=int, default=6, help="同币相邻结构窗口间隔不超过多少根4h时合并为一组，默认 6=24小时。")
    parser.add_argument("--sort", choices=("time", "score", "compact", "quality"), default="quality")
    parser.add_argument("--min-blue-drop-pct", type=Decimal, default=Decimal("6.0"), help="蓝色下杀低点相对黄色 hold 收盘价的最小跌幅，默认 6%。")
    parser.add_argument("--max-market-yellow-bars", type=int, default=48, help="市场扫描里黄色拉升区总跨度上限，避免把长周期横盘/慢涨当成拉升，默认 48。")
    parser.add_argument("--max-market-blue-bars", type=int, default=24, help="市场扫描里蓝色下杀区域总跨度上限，避免把长周期阴跌当成下杀，默认 24。")
    parser.add_argument("--max-blue-below-close-count", type=int, default=24, help="蓝区收盘低于 hold 的K线数量上限，避免把长时间阴跌当成下杀区，默认 24。")
    parser.add_argument("--min-market-wash-bars", type=int, default=4, help="市场扫描里白色洗盘区至少需要多少根4小时K，默认 4。")
    parser.add_argument("--max-market-wash-bars", type=int, default=36, help="市场扫描里白色洗盘区总跨度上限，避免把长期横盘当成洗盘，默认 36。")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=0.8)
    parser.add_argument("--failed-retry-passes", type=int, default=2, help="第一轮失败的交易对再补扫几轮，默认 2。")
    parser.add_argument("--insecure", action="store_true")
    return parser.parse_args()


def symbol_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        provider="binance",
        quote=args.quote,
        symbols=args.symbols,
        timeout=args.timeout,
        retries=args.retries,
        retry_delay=args.retry_delay,
        insecure=args.insecure,
        sleep=args.archive_sleep,
    )


def is_multiplier_symbol(symbol: str, quote: str) -> bool:
    base_symbol = symbol[: -len(quote)] if quote and symbol.endswith(quote) else symbol
    return base_symbol.startswith(("100000000", "10000000", "1000000", "100000", "10000", "1000"))


def filter_market_symbols(symbols: list[live.SymbolInfo], args: argparse.Namespace) -> list[live.SymbolInfo]:
    if args.include_multiplier_symbols or args.symbols:
        return symbols
    return [info for info in symbols if not is_multiplier_symbol(info.symbol, args.quote)]


def load_symbols_from_file(path: str, quote: str) -> list[live.SymbolInfo]:
    symbols: list[live.SymbolInfo] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as file:
        for line in file:
            raw = line.strip().upper()
            if not raw or raw.startswith("#"):
                continue
            symbol = raw
            if ":" in symbol:
                symbol = symbol.split(":", 1)[1]
            if symbol.endswith(".P"):
                symbol = symbol[:-2]
            if symbol in seen:
                continue
            seen.add(symbol)
            symbols.append(
                live.SymbolInfo(
                    symbol=symbol,
                    base_asset=symbol.removesuffix(quote),
                    quote_asset=quote,
                    contract_type="PERPETUAL",
                )
            )
    return symbols


def scan_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        sort=args.sort,
        min_blue_drop_pct=args.min_blue_drop_pct,
        max_market_blue_bars=args.max_market_blue_bars,
        max_blue_below_close_count=args.max_blue_below_close_count,
        min_market_wash_bars=args.min_market_wash_bars,
        max_market_wash_bars=args.max_market_wash_bars,
    )


def market_filter(matches: list[segment_scan.SegmentArcMatch], args: argparse.Namespace) -> list[segment_scan.SegmentArcMatch]:
    kept: list[segment_scan.SegmentArcMatch] = []
    for match in matches:
        blue_drop = abs(segment_scan.pct(match.blue_low.low, match.hold_line))
        if blue_drop < args.min_blue_drop_pct:
            continue
        if segment_scan.candle_count(match.yellow_start.open_time, match.yellow_end.open_time) > args.max_market_yellow_bars:
            continue
        if segment_scan.candle_count(match.blue_start.open_time, match.reclaim.open_time) > args.max_market_blue_bars:
            continue
        if match.blue_below_close_count > args.max_blue_below_close_count:
            continue
        wash_bars = segment_scan.wash_bar_count(match)
        if wash_bars < args.min_market_wash_bars:
            continue
        if wash_bars > args.max_market_wash_bars:
            continue
        kept.append(match)
    return kept


def load_archive_candles(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    start: dt.date,
    end: dt.date,
    context: ssl.SSLContext,
    granularity: str,
    insecure: bool,
    timeout: float,
    retries: int,
    retry_delay: float,
) -> list[live.Candle]:
    if granularity == "api":
        api_args = argparse.Namespace(
            provider="binance",
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
            insecure=insecure,
            sleep=0.0,
        )
        return live.load_candles(symbol, interval, start_ms, end_ms, api_args, limit=1500)

    if granularity == "daily":
        return archive.load_daily(symbol, interval, start_ms, end_ms, context, timeout)

    candles: list[live.Candle] = []
    inclusive_end = end - dt.timedelta(days=1)
    for year, month in archive.month_iter(start.year, inclusive_end):
        month_first = dt.date(year, month, 1)
        month_last = dt.date(year, month, calendar.monthrange(year, month)[1])
        if month_last < start or month_first > inclusive_end:
            continue
        range_start = max(start, month_first)
        range_end = min(inclusive_end, month_last)
        use_daily = granularity == "auto" and range_end < month_last
        if use_daily:
            candles.extend(
                archive.load_daily(
                    symbol,
                    interval,
                    segment_scan.ms_at(range_start),
                    segment_scan.ms_at(range_end + dt.timedelta(days=1)),
                    context,
                    timeout,
                )
            )
            continue
        url = f"{archive.ARCHIVE_BASE_URL}/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{year}-{month:02d}.zip"
        text = archive.fetch_archive_text(url, context, timeout)
        if text is not None:
            candles.extend(archive.parse_archive_csv(text, start_ms, end_ms))
    return sorted({item.open_time: item for item in candles}.values(), key=lambda item: item.open_time)


def dedupe(matches: list[segment_scan.SegmentArcMatch], cluster_gap_bars: int = 6) -> list[segment_scan.SegmentArcMatch]:
    best: dict[tuple[str, int, int], segment_scan.SegmentArcMatch] = {}
    for match in matches:
        key = (
            match.symbol,
            match.blue_low.open_time,
            match.wash_end.open_time,
        )
        current = best.get(key)
        if current is None or segment_scan.structure_rank_key(match) < segment_scan.structure_rank_key(current):
            best[key] = match
    collapsed = sorted(best.values(), key=lambda item: (item.symbol, item.yellow_start.open_time, item.wash_end.open_time))
    gap_ms = max(0, cluster_gap_bars) * 4 * 60 * 60 * 1000
    clustered: list[segment_scan.SegmentArcMatch] = []
    cluster: list[segment_scan.SegmentArcMatch] = []
    cluster_end = 0
    current_symbol = ""

    def choose_cluster(items: list[segment_scan.SegmentArcMatch]) -> segment_scan.SegmentArcMatch:
        return min(items, key=lambda item: (item.yellow_start.open_time, segment_scan.structure_rank_key(item)))

    for match in collapsed:
        start = match.yellow_start.open_time
        end = match.wash_end.open_time
        if not cluster or match.symbol != current_symbol or start > cluster_end + gap_ms:
            if cluster:
                clustered.append(choose_cluster(cluster))
            cluster = [match]
            current_symbol = match.symbol
            cluster_end = end
            continue
        cluster.append(match)
        cluster_end = max(cluster_end, end)
    if cluster:
        clustered.append(choose_cluster(cluster))
    return clustered


def bj_dt(open_time: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(open_time / 1000, tz=dt.timezone.utc).astimezone(segment_scan.BJ)


def month_key_score(open_time: int) -> tuple[str, int, int]:
    value = bj_dt(open_time).date()
    last_day = calendar.monthrange(value.year, value.month)[1]
    nodes = [
        ("month_start", 1),
        ("month_mid", 15),
        ("month_end", last_day),
    ]
    bucket, node_day = min(nodes, key=lambda item: abs(value.day - item[1]))
    distance = abs(value.day - node_day)
    score = max(0, 10 - distance * 2)
    if distance > 3:
        bucket = "off_node"
    return bucket, distance, score


def structure_calendar_score(match: segment_scan.SegmentArcMatch) -> int:
    return (
        month_key_score(match.yellow_peak.open_time)[2]
        + month_key_score(match.blue_low.open_time)[2]
        + month_key_score(match.wash_start.open_time)[2]
        + month_key_score(match.wash_end.open_time)[2]
    )


def wick_ratio(candle, side: str) -> Decimal:
    price_range = candle.high - candle.low
    if price_range <= 0:
        return Decimal("0")
    if side == "upper":
        wick = candle.high - max(candle.open, candle.close)
    else:
        wick = min(candle.open, candle.close) - candle.low
    if wick <= 0:
        return Decimal("0")
    return wick / price_range


def wash_wick_boundary_score(match: segment_scan.SegmentArcMatch) -> Decimal:
    upper = wick_ratio(match.wash_start, "upper")
    lower = wick_ratio(match.wash_end, "lower")
    return (upper + lower) * Decimal("10")


def write_rows(path: str, rows: list[segment_scan.SegmentArcMatch]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "symbol",
                "score",
                "yellow_start_bj",
                "yellow_peak_bj",
                "yellow_end_bj",
                "yellow_bars",
                "yellow_hold_close",
                "blue_start_bj",
                "blue_low_bj",
                "blue_low",
                "reclaim_bj",
                "reclaim_close",
                "blue_below_close_count",
                "breakout_volume_ratio",
                "wash_hold_gap_pct",
                "wash_start_bj",
                "wash_start_close",
                "wash_end_bj",
                "wash_min_close",
                "wash_peak_close",
                "wash_start_calendar_bucket",
                "wash_start_calendar_distance_days",
                "structure_calendar_score",
                "wash_wick_boundary_score",
            ]
        )
        for match in rows:
            wash_bucket, wash_distance, _ = month_key_score(match.wash_start.open_time)
            writer.writerow(
                [
                    match.symbol,
                    f"{match.score:.4f}",
                    segment_scan.bj(match.yellow_start.open_time),
                    segment_scan.bj(match.yellow_peak.open_time),
                    segment_scan.bj(match.yellow_end.open_time),
                    segment_scan.candle_count(match.yellow_start.open_time, match.yellow_end.open_time),
                    segment_scan.dstr(match.hold_line),
                    segment_scan.bj(match.blue_start.open_time),
                    segment_scan.bj(match.blue_low.open_time),
                    segment_scan.dstr(match.blue_low.low),
                    segment_scan.bj(match.reclaim.open_time),
                    segment_scan.dstr(match.reclaim.close),
                    match.blue_below_close_count,
                    f"{match.breakout_volume_ratio:.4f}",
                    f"{segment_scan.wash_hold_gap_pct(match):.4f}",
                    segment_scan.bj(match.wash_start.open_time),
                    segment_scan.dstr(match.wash_start.close),
                    segment_scan.bj(match.wash_end.open_time),
                    segment_scan.dstr(match.wash_min_close),
                    segment_scan.dstr(match.wash_peak),
                    wash_bucket,
                    wash_distance,
                    structure_calendar_score(match),
                    f"{wash_wick_boundary_score(match):.4f}",
                ]
            )


def scan_symbol(
    index: int,
    total: int,
    info: live.SymbolInfo,
    args: argparse.Namespace,
    start: dt.date,
    end: dt.date,
    start_ms: int,
    end_ms: int,
    params: argparse.Namespace,
) -> tuple[int, str, list[segment_scan.SegmentArcMatch], int, str | None]:
    last_error = ""
    for attempt in range(1, max(1, args.retries) + 1):
        try:
            context = ssl._create_unverified_context() if args.insecure else ssl._create_unverified_context()
            candles = load_archive_candles(
                info.symbol,
                "4h",
                start_ms,
                end_ms,
                start,
                end,
                context,
                args.archive_granularity,
                args.insecure,
                args.timeout,
                args.retries,
                args.retry_delay,
            )
            raw_matches = segment_scan.find_matches(candles, info.symbol, params)
            matches = dedupe(market_filter(raw_matches, args), args.merge_cluster_gap_bars)
            matches = sorted(
                matches,
                key=lambda item: (
                    segment_scan.structure_rank_key(item),
                    item.wash_start.open_time,
                ),
            )
            return index, info.symbol, matches[: args.per_symbol_limit], len(raw_matches), None
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            if attempt < max(1, args.retries):
                time.sleep(max(0.0, args.retry_delay) * attempt)
    return index, info.symbol, [], 0, last_error


def record_result(
    index: int,
    total: int,
    symbol: str,
    selected: list[segment_scan.SegmentArcMatch],
    raw_count: int,
    error: str | None,
    progress_every: int,
    completed: int | None = None,
) -> bool:
    prefix = f"[{completed}/{total} done; index {index}]" if completed is not None else f"[{index}/{total}]"
    if error:
        print(f"{prefix} {symbol}: error={error}", file=sys.stderr, flush=True)
        return False
    if selected:
        print(f"{prefix} {symbol}: {len(selected)} raw={raw_count}", flush=True)
    elif (completed if completed is not None else index) % progress_every == 0:
        print(f"{prefix} {symbol}: 0 raw={raw_count}", file=sys.stderr, flush=True)
    return True


def main() -> int:
    args = parse_args()
    symbols = load_symbols_from_file(args.symbols_file, args.quote) if args.symbols_file else live.load_symbols(symbol_args(args))
    symbols = filter_market_symbols(symbols, args)
    if args.max_symbols > 0:
        symbols = symbols[: args.max_symbols]
    start = dt.date.fromisoformat(args.start_date)
    end = dt.date.fromisoformat(args.end_date) + dt.timedelta(days=1)
    start_ms = segment_scan.ms_at(start)
    end_ms = segment_scan.ms_at(end)
    params = scan_args(args)

    rows: list[segment_scan.SegmentArcMatch] = []
    failed: list[live.SymbolInfo] = []
    workers = max(1, args.workers)
    progress_every = max(1, args.progress_every)
    if workers == 1:
        for index, info in enumerate(symbols, start=1):
            _, symbol, selected, raw_count, error = scan_symbol(index, len(symbols), info, args, start, end, start_ms, end_ms, params)
            rows.extend(selected)
            if not record_result(index, len(symbols), symbol, selected, raw_count, error, progress_every):
                failed.append(info)
            if args.archive_sleep > 0:
                time.sleep(args.archive_sleep)
    else:
        executor_class = concurrent.futures.ProcessPoolExecutor if args.executor == "process" else concurrent.futures.ThreadPoolExecutor
        with executor_class(max_workers=workers) as executor:
            futures = {
                executor.submit(scan_symbol, index, len(symbols), info, args, start, end, start_ms, end_ms, params)
                for index, info in enumerate(symbols, start=1)
            }
            info_by_symbol = {info.symbol: info for info in symbols}
            completed = 0
            for future in concurrent.futures.as_completed(futures):
                completed += 1
                index, symbol, selected, raw_count, error = future.result()
                rows.extend(selected)
                if not record_result(index, len(symbols), symbol, selected, raw_count, error, progress_every, completed):
                    failed.append(info_by_symbol[symbol])
                if args.archive_sleep > 0:
                    time.sleep(args.archive_sleep)

    for retry_pass in range(1, max(0, args.failed_retry_passes) + 1):
        if not failed:
            break
        retry_items = failed
        failed = []
        print(f"retry pass {retry_pass}: {len(retry_items)} symbols", file=sys.stderr, flush=True)
        for offset, info in enumerate(retry_items, start=1):
            _, symbol, selected, raw_count, error = scan_symbol(offset, len(retry_items), info, args, start, end, start_ms, end_ms, params)
            rows.extend(selected)
            if not record_result(offset, len(retry_items), symbol, selected, raw_count, error, progress_every):
                failed.append(info)
            if args.archive_sleep > 0:
                time.sleep(args.archive_sleep)

    if failed:
        print("unfinished_symbols=" + ",".join(info.symbol for info in failed), file=sys.stderr, flush=True)

    rows = sorted(
        rows,
        key=lambda item: (
            item.symbol,
            item.wash_start.open_time,
            segment_scan.structure_rank_key(item),
        ),
    )[: args.limit]
    write_rows(args.csv_file, rows)
    for match in rows:
        print(
            match.symbol,
            f"yellow={segment_scan.bj(match.yellow_start.open_time)}->{segment_scan.bj(match.yellow_peak.open_time)} hold={segment_scan.dstr(match.hold_line)}",
            f"blue_low={segment_scan.bj(match.blue_low.open_time)} {segment_scan.dstr(match.blue_low.low)}",
            f"wash={segment_scan.bj(match.wash_start.open_time)}->{segment_scan.bj(match.wash_end.open_time)} min_close={segment_scan.dstr(match.wash_min_close)}",
        )
    print(f"wrote {args.csv_file} rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
