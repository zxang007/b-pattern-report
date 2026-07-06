#!/usr/bin/env python3
from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import io
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from decimal import Decimal

import screen_aria_4h_pattern as base


ARCHIVE_BASE_URL = "https://data.binance.vision/data/futures/um"
ARCHIVE_TEXT_CACHE: dict[str, str | None] = {}


@dataclass(frozen=True)
class ArchiveDoor:
    start_time: str
    start_price: Decimal
    peak_time: str
    peak_high: Decimal
    floor_retest_time: str
    floor_retest_low: Decimal
    floor_retest_after_days: Decimal
    base_quote_volume_avg: Decimal
    up_quote_volume_avg: Decimal
    up_quote_volume_max: Decimal
    retest_quote_volume: Decimal
    up_volume_ratio: Decimal
    retest_volume_ratio: Decimal
    retest_vs_up_volume_ratio: Decimal


@dataclass(frozen=True)
class ArchiveMatch:
    four_h: base.PatternMatch
    door: ArchiveDoor
    four_h_base_quote_volume_avg: Decimal
    breakout_quote_volume: Decimal
    breakout_volume_ratio: Decimal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="用 Binance 历史归档扫描 4h + 15m 门形 + 延迟回踩结构。")
    parser.add_argument("--symbol", required=True, help="合约交易对，例如 MERLUSDT。")
    parser.add_argument("--start-year", type=int, default=2020, help="起始年份，默认 2020。")
    parser.add_argument("--end-date", default=dt.datetime.now(dt.timezone.utc).date().isoformat(), help="结束日期 UTC，默认今天。")
    parser.add_argument("--csv-file", help="CSV 输出文件，不传则用 symbol 自动生成。")
    parser.add_argument("--strict", action="store_true", help="使用 ARIA 严格参数；默认使用 VINE 校准后的宽松参数。")
    parser.add_argument("--max-pullback-bars", type=int, default=None)
    parser.add_argument("--min-breakout-candle-pct", type=Decimal, default=None)
    parser.add_argument("--min-breakout-from-anchor-pct", type=Decimal, default=None)
    parser.add_argument("--min-quote-volume", type=Decimal, default=None)
    parser.add_argument("--wash-check-bars", type=int, default=24)
    parser.add_argument("--breakout-start-date", help="只扫描该 UTC 日期之后的4小时突破，格式 YYYY-MM-DD。")
    parser.add_argument("--breakout-end-date", help="只扫描该 UTC 日期之前的4小时突破，格式 YYYY-MM-DD。")
    parser.add_argument("--door-search-days", type=Decimal, default=Decimal("20"), help="4h 结构后往后多少天内寻找15分钟门起点，默认 20。")
    parser.add_argument("--door-min-delay-days", type=Decimal, default=Decimal("0"), help="15分钟门起点距离4h结构至少延迟多少天，默认 0。")
    parser.add_argument("--door-lookahead-bars", type=int, default=None, help="兼容旧参数：4h 突破后多少根15m内允许出现门起点；不传则由 --door-search-days 计算。")
    parser.add_argument("--all-doors", action="store_true", help="输出同一个4h结构后出现的所有15分钟门；默认只取第一个。")
    parser.add_argument("--door-start-date", help="只保留该 UTC 日期之后的15分钟门起点，格式 YYYY-MM-DD。")
    parser.add_argument("--door-end-date", help="只保留该 UTC 日期之前的15分钟门起点，格式 YYYY-MM-DD。")
    parser.add_argument("--retest-start-date", help="只保留该 UTC 日期之后的回踩确认，格式 YYYY-MM-DD。")
    parser.add_argument("--retest-end-date", help="只保留该 UTC 日期之前的回踩确认，格式 YYYY-MM-DD。")
    parser.add_argument("--require-door-no-break", action="store_true", help="严格要求门形上冲和后续回踩的最低点都不低于门起点。")
    parser.add_argument(
        "--door-selection",
        choices=("first", "earliest-strong", "strongest", "latest-retest"),
        default="earliest-strong",
        help="同一个4h结构后选哪个15分钟门：first=第一个，earliest-strong=第一个强门，strongest=涨幅最大门，latest-retest=最后确认门。默认 earliest-strong。",
    )
    parser.add_argument("--strong-door-up-pct", type=Decimal, default=Decimal("15"), help="earliest-strong 模式下，门起点到高点至少上涨多少百分比才算强门，默认 15。")
    parser.add_argument("--door-peak-bars", type=int, default=192, help="门起点后多少根15m内寻找上冲高点，默认 192=48小时。")
    parser.add_argument("--delayed-retest-bars", type=int, default=960, help="上冲后多少根15m内寻找回踩门底，默认 960=10天。")
    parser.add_argument(
        "--retest-mode",
        choices=("first", "deepest", "latest"),
        default="deepest",
        help="同一个门形内选择哪个回踩：first=第一个，deepest=最低，latest=最后一个。默认 deepest。",
    )
    parser.add_argument("--min-door-up-pct", type=Decimal, default=Decimal("10"), help="门起点到高点至少上涨百分比，默认 10。")
    parser.add_argument("--door-floor-tolerance-pct", type=Decimal, default=Decimal("1"), help="允许影线低于门底的百分比，默认 1。")
    parser.add_argument("--floor-retest-near-pct", type=Decimal, default=Decimal("6"), help="延迟回踩最低点距离门底不超过多少百分比，默认 6。")
    parser.add_argument("--volume-base-bars", type=int, default=32, help="计算门前15分钟基准成交额的K线数量，默认 32。")
    parser.add_argument("--four-h-volume-base-bars", type=int, default=20, help="计算4小时突破前基准成交额的K线数量，默认 20。")
    parser.add_argument("--min-door-up-volume-ratio", type=Decimal, default=Decimal("0"), help="门形上冲平均成交额至少是门前均量的多少倍；0 表示只输出不筛选。")
    parser.add_argument("--min-breakout-volume-ratio", type=Decimal, default=Decimal("0"), help="4小时突破成交额至少是前面均量的多少倍；0 表示只输出不筛选。")
    parser.add_argument("--max-retest-vs-up-volume-ratio", type=Decimal, default=Decimal("0"), help="回踩成交额最多是上冲平均成交额的多少倍；0 表示只输出不筛选。")
    parser.add_argument("--limit", type=int, default=200, help="最多打印多少条结果，默认 200。")
    return parser.parse_args()


def dstr(value: Decimal) -> str:
    return base.decimal_to_string(value)


def ms_at(day: dt.date) -> int:
    return int(dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc).timestamp() * 1000)


def parse_utc_time(value: str) -> int:
    return int(dt.datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def average_quote_volume(candles: list[base.Candle]) -> Decimal:
    if not candles:
        return Decimal("0")
    return sum((item.quote_volume for item in candles), Decimal("0")) / Decimal(len(candles))


def max_quote_volume(candles: list[base.Candle]) -> Decimal:
    if not candles:
        return Decimal("0")
    return max(item.quote_volume for item in candles)


def safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return numerator / denominator


def month_iter(start_year: int, end_date: dt.date):
    for year in range(start_year, end_date.year + 1):
        last_month = 12
        if year == end_date.year:
            last_month = end_date.month
        for month in range(1, last_month + 1):
            yield year, month


def parse_archive_csv(text: str, start_ms: int | None = None, end_ms: int | None = None) -> list[base.Candle]:
    candles = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].isdigit():
            continue
        open_time = int(row[0])
        if start_ms is not None and open_time < start_ms:
            continue
        if end_ms is not None and open_time > end_ms:
            continue
        candles.append(
            base.Candle(
                open_time=open_time,
                open=Decimal(row[1]),
                high=Decimal(row[2]),
                low=Decimal(row[3]),
                close=Decimal(row[4]),
                volume=Decimal(row[5]),
                quote_volume=Decimal(row[7]),
            )
        )
    return candles


def fetch_archive_text(url: str, context: ssl.SSLContext, timeout: float = 25) -> str | None:
    if url in ARCHIVE_TEXT_CACHE:
        return ARCHIVE_TEXT_CACHE[url]
    request_url = urllib.parse.quote(url, safe=":/?&=%")
    try:
        with urllib.request.urlopen(request_url, timeout=timeout, context=context) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            ARCHIVE_TEXT_CACHE[url] = None
            return None
        raise
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        text = archive.read(archive.namelist()[0]).decode("utf-8")
    ARCHIVE_TEXT_CACHE[url] = text
    return text


def load_monthly(symbol: str, interval: str, start_year: int, end_date: dt.date, context: ssl.SSLContext, timeout: float = 25) -> list[base.Candle]:
    candles = []
    for year, month in month_iter(start_year, end_date):
        url = f"{ARCHIVE_BASE_URL}/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{year}-{month:02d}.zip"
        text = fetch_archive_text(url, context, timeout)
        if text is not None:
            candles.extend(parse_archive_csv(text))
    return sorted({item.open_time: item for item in candles}.values(), key=lambda item: item.open_time)


def load_daily(symbol: str, interval: str, start_ms: int, end_ms: int, context: ssl.SSLContext, timeout: float = 25) -> list[base.Candle]:
    candles = []
    start = dt.datetime.fromtimestamp(start_ms / 1000, tz=dt.timezone.utc).date()
    end = dt.datetime.fromtimestamp(end_ms / 1000, tz=dt.timezone.utc).date()
    day = start
    while day <= end:
        url = f"{ARCHIVE_BASE_URL}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{day.isoformat()}.zip"
        text = fetch_archive_text(url, context, timeout)
        if text is not None:
            candles.extend(parse_archive_csv(text, start_ms, end_ms))
        day += dt.timedelta(days=1)
    return sorted({item.open_time: item for item in candles}.values(), key=lambda item: item.open_time)


def make_pattern_args(args: argparse.Namespace) -> argparse.Namespace:
    if args.strict:
        params = {
            "max_pullback_bars": 12,
            "min_wash_bars": 3,
            "wash_check_bars": args.wash_check_bars,
            "close_tolerance_pct": Decimal("0.5"),
            "min_anchor_up_pct": Decimal("0.5"),
            "min_pullback_depth_pct": Decimal("1.0"),
            "min_breakout_candle_pct": Decimal("6.0"),
            "min_breakout_from_anchor_pct": Decimal("8.0"),
            "min_quote_volume": Decimal("500000"),
            "recent_bars": 1000000,
        }
    else:
        params = {
            "max_pullback_bars": 18,
            "min_wash_bars": 2,
            "wash_check_bars": args.wash_check_bars,
            "close_tolerance_pct": Decimal("3.0"),
            "min_anchor_up_pct": Decimal("0.0"),
            "min_pullback_depth_pct": Decimal("0.5"),
            "min_breakout_candle_pct": Decimal("2.0"),
            "min_breakout_from_anchor_pct": Decimal("3.0"),
            "min_quote_volume": Decimal("50000"),
            "recent_bars": 1000000,
        }

    if args.max_pullback_bars is not None:
        params["max_pullback_bars"] = args.max_pullback_bars
    if args.min_breakout_candle_pct is not None:
        params["min_breakout_candle_pct"] = args.min_breakout_candle_pct
    if args.min_breakout_from_anchor_pct is not None:
        params["min_breakout_from_anchor_pct"] = args.min_breakout_from_anchor_pct
    if args.min_quote_volume is not None:
        params["min_quote_volume"] = args.min_quote_volume
    return argparse.Namespace(**params)


def find_delayed_doors(candles: list[base.Candle], args: argparse.Namespace) -> list[ArchiveDoor]:
    if len(candles) < 8:
        return []
    floor_ratio = Decimal("1") - base.pct(args.door_floor_tolerance_pct)
    retest_near_ratio = Decimal("1") + base.pct(args.floor_retest_near_pct)
    up_ratio = Decimal("1") + base.pct(args.min_door_up_pct)
    doors = []

    if args.door_lookahead_bars is not None:
        search_bars = args.door_lookahead_bars
    else:
        search_bars = int(args.door_search_days * Decimal(24 * 4))
    min_delay_ms = int(args.door_min_delay_days * Decimal(24 * 60 * 60 * 1000))
    reference_time = candles[0].open_time

    max_start = min(len(candles) - 4, search_bars)
    for start_index in range(max_start):
        start = candles[start_index]
        if start.open_time - reference_time < min_delay_ms:
            continue
        if start.low <= 0:
            continue
        start_price = start.low
        floor = start_price * floor_ratio

        peak_end = min(len(candles) - 3, start_index + args.door_peak_bars)
        for peak_index in range(start_index + 1, peak_end + 1):
            peak = candles[peak_index]
            if peak.high < start_price * up_ratio:
                continue
            if any(item.low < floor for item in candles[start_index + 1 : peak_index + 1]):
                continue
            if args.require_door_no_break and any(item.low < start_price for item in candles[start_index + 1 : peak_index + 1]):
                continue

            retest_end = min(len(candles) - 1, peak_index + args.delayed_retest_bars)
            valid_retests = []
            for retest_index in range(peak_index + 1, retest_end + 1):
                retest = candles[retest_index]
                if retest.low < floor:
                    break
                if args.require_door_no_break and retest.low < start_price:
                    break
                if retest.low <= start_price * retest_near_ratio:
                    valid_retests.append(retest)
            if not valid_retests:
                continue
            if args.retest_mode == "first":
                selected_retest = valid_retests[0]
            elif args.retest_mode == "latest":
                selected_retest = valid_retests[-1]
            else:
                selected_retest = min(valid_retests, key=lambda item: item.low)
            days = Decimal(selected_retest.open_time - start.open_time) / Decimal(24 * 60 * 60 * 1000)
            base_start = max(0, start_index - args.volume_base_bars)
            base_volume_avg = average_quote_volume(candles[base_start:start_index])
            if base_volume_avg <= 0:
                base_volume_avg = average_quote_volume(candles[:start_index + 1])
            up_window = candles[start_index : peak_index + 1]
            up_volume_avg = average_quote_volume(up_window)
            up_volume_max = max_quote_volume(up_window)
            retest_volume = selected_retest.quote_volume
            up_volume_ratio = safe_ratio(up_volume_avg, base_volume_avg)
            retest_volume_ratio = safe_ratio(retest_volume, base_volume_avg)
            retest_vs_up_volume_ratio = safe_ratio(retest_volume, up_volume_avg)
            if args.min_door_up_volume_ratio > 0 and up_volume_ratio < args.min_door_up_volume_ratio:
                continue
            if args.max_retest_vs_up_volume_ratio > 0 and retest_vs_up_volume_ratio > args.max_retest_vs_up_volume_ratio:
                continue
            doors.append(
                ArchiveDoor(
                    start_time=start.day_time,
                    start_price=start_price,
                    peak_time=peak.day_time,
                    peak_high=peak.high,
                    floor_retest_time=selected_retest.day_time,
                    floor_retest_low=selected_retest.low,
                    floor_retest_after_days=days,
                    base_quote_volume_avg=base_volume_avg,
                    up_quote_volume_avg=up_volume_avg,
                    up_quote_volume_max=up_volume_max,
                    retest_quote_volume=retest_volume,
                    up_volume_ratio=up_volume_ratio,
                    retest_volume_ratio=retest_volume_ratio,
                    retest_vs_up_volume_ratio=retest_vs_up_volume_ratio,
                )
            )
    return doors


def find_delayed_door(candles: list[base.Candle], args: argparse.Namespace) -> ArchiveDoor | None:
    doors = find_delayed_doors(candles, args)
    return select_door(doors, args)


def door_up_pct(door: ArchiveDoor) -> Decimal:
    if door.start_price <= 0:
        return Decimal("0")
    return (door.peak_high / door.start_price - Decimal("1")) * Decimal("100")


def retest_gap_pct(door: ArchiveDoor) -> Decimal:
    if door.start_price <= 0:
        return Decimal("999999")
    return (door.floor_retest_low / door.start_price - Decimal("1")) * Decimal("100")


def dedupe_doors_by_start(doors: list[ArchiveDoor]) -> list[ArchiveDoor]:
    best_by_start: dict[tuple[str, Decimal], ArchiveDoor] = {}
    for door in doors:
        key = (door.start_time, door.start_price)
        current = best_by_start.get(key)
        if current is None:
            best_by_start[key] = door
            continue
        current_score = door_quality_score(current)
        next_score = door_quality_score(door)
        if next_score > current_score:
            best_by_start[key] = door
    return sorted(best_by_start.values(), key=lambda item: parse_utc_time(item.start_time))


def door_quality_score(door: ArchiveDoor) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    return (
        door_up_pct(door),
        door.up_volume_ratio,
        -door.retest_vs_up_volume_ratio,
        -retest_gap_pct(door),
    )


def select_door(doors: list[ArchiveDoor], args: argparse.Namespace) -> ArchiveDoor | None:
    if not doors:
        return None
    candidates = dedupe_doors_by_start(doors)
    if args.door_selection == "first":
        return candidates[0]
    if args.door_selection == "strongest":
        return max(candidates, key=door_quality_score)
    if args.door_selection == "latest-retest":
        return max(candidates, key=lambda item: parse_utc_time(item.floor_retest_time))

    strong = [door for door in candidates if door_up_pct(door) >= args.strong_door_up_pct]
    if strong:
        return strong[0]
    return candidates[0]


def scan(args: argparse.Namespace) -> list[ArchiveMatch]:
    symbol = args.symbol.upper()
    end_date = dt.date.fromisoformat(args.end_date)
    context = ssl._create_unverified_context()
    pattern_args = make_pattern_args(args)

    four_h = load_monthly(symbol, "4h", args.start_year, end_date, context)
    if not four_h:
        raise RuntimeError(f"No 4h archive data found for {symbol}")

    # Add daily files for the current month in case the monthly archive is not ready yet.
    month_start = dt.date(end_date.year, end_date.month, 1)
    daily_4h = load_daily(symbol, "4h", ms_at(month_start), ms_at(end_date), context)
    four_h = sorted({item.open_time: item for item in four_h + daily_4h}.values(), key=lambda item: item.open_time)
    print(f"4h_range={four_h[0].day_time}->{four_h[-1].day_time} bars={len(four_h)}", file=sys.stderr)

    raw_matches = base.find_symbol_matches(symbol, four_h, pattern_args)
    four_h_by_time = {candle.open_time: (idx, candle) for idx, candle in enumerate(four_h)}
    if args.breakout_start_date:
        start_ms = ms_at(dt.date.fromisoformat(args.breakout_start_date))
        raw_matches = [match for match in raw_matches if match.breakout_open_time >= start_ms]
    if args.breakout_end_date:
        end_ms = ms_at(dt.date.fromisoformat(args.breakout_end_date) + dt.timedelta(days=1))
        raw_matches = [match for match in raw_matches if match.breakout_open_time < end_ms]
    print(f"raw_4h_matches={len(raw_matches)}", file=sys.stderr)

    matches = []
    for index, four_h_match in enumerate(raw_matches, start=1):
        breakout_index, breakout_candle = four_h_by_time[four_h_match.breakout_open_time]
        volume_base_start = max(0, breakout_index - args.four_h_volume_base_bars)
        four_h_base_volume_avg = average_quote_volume(four_h[volume_base_start:breakout_index])
        breakout_volume_ratio = safe_ratio(breakout_candle.quote_volume, four_h_base_volume_avg)
        if args.min_breakout_volume_ratio > 0 and breakout_volume_ratio < args.min_breakout_volume_ratio:
            continue
        start_ms = four_h_match.breakout_open_time
        if args.door_lookahead_bars is not None:
            search_bars = args.door_lookahead_bars
        else:
            search_bars = int(args.door_search_days * Decimal(24 * 4))
        lookahead = (
            search_bars
            + args.door_peak_bars
            + args.delayed_retest_bars
            + 16
        )
        end_ms = start_ms + lookahead * 15 * 60 * 1000
        fifteen_m = load_daily(symbol, "15m", start_ms, end_ms, context)
        doors = find_delayed_doors(fifteen_m, args)
        if args.door_start_date:
            door_start_ms = ms_at(dt.date.fromisoformat(args.door_start_date))
            doors = [door for door in doors if parse_utc_time(door.start_time) >= door_start_ms]
        if args.door_end_date:
            door_end_ms = ms_at(dt.date.fromisoformat(args.door_end_date) + dt.timedelta(days=1))
            doors = [door for door in doors if parse_utc_time(door.start_time) < door_end_ms]
        if args.retest_start_date:
            retest_start_ms = ms_at(dt.date.fromisoformat(args.retest_start_date))
            doors = [door for door in doors if parse_utc_time(door.floor_retest_time) >= retest_start_ms]
        if args.retest_end_date:
            retest_end_ms = ms_at(dt.date.fromisoformat(args.retest_end_date) + dt.timedelta(days=1))
            doors = [door for door in doors if parse_utc_time(door.floor_retest_time) < retest_end_ms]
        if doors:
            selected_doors = doors if args.all_doors else [select_door(doors, args)]
            for door in selected_doors:
                if door is not None:
                    matches.append(
                        ArchiveMatch(
                            four_h=four_h_match,
                            door=door,
                            four_h_base_quote_volume_avg=four_h_base_volume_avg,
                            breakout_quote_volume=breakout_candle.quote_volume,
                            breakout_volume_ratio=breakout_volume_ratio,
                        )
                    )
        if index % 20 == 0:
            print(f"checked_4h_matches={index}/{len(raw_matches)} confirmed={len(matches)}", file=sys.stderr)
        time.sleep(0.03)

    best_by_breakout: dict[str, ArchiveMatch] = {}
    for match in matches:
        if args.all_doors:
            key = f"{match.four_h.breakout_time}|{match.door.start_time}|{match.door.peak_time}|{match.door.floor_retest_time}"
        else:
            key = match.four_h.breakout_time
        current = best_by_breakout.get(key)
        if current is None or match.four_h.score > current.four_h.score:
            best_by_breakout[key] = match
    return sorted(best_by_breakout.values(), key=lambda item: item.four_h.breakout_open_time)


def write_csv(path: str, rows: list[ArchiveMatch]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "symbol",
                "anchor_time_utc",
                "anchor_close",
                "pullback_low_time_utc",
                "pullback_low",
                "breakout_time_utc",
                "breakout_close",
                "breakout_gain_pct",
                "four_h_base_quote_volume_avg",
                "breakout_quote_volume",
                "breakout_volume_ratio",
                "wash_bars",
                "min_close_after_breakout",
                "door_start_time_utc",
                "door_start_price",
                "door_peak_time_utc",
                "door_peak_high",
                "floor_retest_time_utc",
                "floor_retest_low",
                "floor_retest_after_days",
                "base_quote_volume_avg",
                "up_quote_volume_avg",
                "up_quote_volume_max",
                "retest_quote_volume",
                "up_volume_ratio",
                "retest_volume_ratio",
                "retest_vs_up_volume_ratio",
            ]
        )
        for item in rows:
            m = item.four_h
            d = item.door
            writer.writerow(
                [
                    m.symbol,
                    m.anchor_time,
                    dstr(m.anchor_close),
                    m.pullback_low_time,
                    dstr(m.pullback_low),
                    m.breakout_time,
                    dstr(m.breakout_close),
                    f"{m.breakout_gain_pct:.2f}",
                    dstr(item.four_h_base_quote_volume_avg),
                    dstr(item.breakout_quote_volume),
                    f"{item.breakout_volume_ratio:.2f}",
                    m.wash_bars,
                    dstr(m.min_close_after_breakout),
                    d.start_time,
                    dstr(d.start_price),
                    d.peak_time,
                    dstr(d.peak_high),
                    d.floor_retest_time,
                    dstr(d.floor_retest_low),
                    f"{d.floor_retest_after_days:.2f}",
                    dstr(d.base_quote_volume_avg),
                    dstr(d.up_quote_volume_avg),
                    dstr(d.up_quote_volume_max),
                    dstr(d.retest_quote_volume),
                    f"{d.up_volume_ratio:.2f}",
                    f"{d.retest_volume_ratio:.2f}",
                    f"{d.retest_vs_up_volume_ratio:.2f}",
                ]
            )


def print_rows(rows: list[ArchiveMatch], limit: int) -> None:
    print(
        "symbol anchor_utc anchor_close breakout_utc breakout_close breakout% breakout_vol_ratio "
        "door_start door_floor door_peak peak_high retest_utc retest_low retest_days "
        "up_vol_ratio retest_vs_up_vol"
    )
    for item in rows[:limit]:
        m = item.four_h
        d = item.door
        print(
            m.symbol,
            m.anchor_time,
            dstr(m.anchor_close),
            m.breakout_time,
            dstr(m.breakout_close),
            f"{m.breakout_gain_pct:.2f}",
            f"{item.breakout_volume_ratio:.2f}",
            d.start_time,
            dstr(d.start_price),
            d.peak_time,
            dstr(d.peak_high),
            d.floor_retest_time,
            dstr(d.floor_retest_low),
            f"{d.floor_retest_after_days:.2f}",
            f"{d.up_volume_ratio:.2f}",
            f"{d.retest_vs_up_volume_ratio:.2f}",
        )


def main() -> int:
    args = parse_args()
    rows = scan(args)
    csv_file = args.csv_file or f"{args.symbol.lower()}_archive_pattern_matches.csv"
    write_csv(csv_file, rows)
    print_rows(rows, args.limit)
    print(f"\nmatched={len(rows)} csv={csv_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
