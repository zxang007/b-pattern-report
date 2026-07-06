#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


BINANCE_FAPI_BASE_URL = "https://fapi.binance.com"
OKX_BASE_URL = "https://www.okx.com"


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal

    @property
    def day_time(self) -> str:
        value = dt.datetime.fromtimestamp(self.open_time / 1000, tz=dt.timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M")

    @property
    def change_pct(self) -> Decimal:
        if self.open == 0:
            return Decimal("0")
        return (self.close / self.open - Decimal("1")) * Decimal("100")


@dataclass(frozen=True)
class DoorPattern:
    start_time: str
    start_price: Decimal
    peak_time: str
    peak_high: Decimal
    drop_time: str
    drop_low: Decimal
    retest_time: str
    retest_low: Decimal


@dataclass(frozen=True)
class PatternMatch:
    symbol: str
    breakout_open_time: int
    anchor_time: str
    anchor_close: Decimal
    pullback_low_time: str
    pullback_low: Decimal
    breakout_time: str
    breakout_close: Decimal
    breakout_gain_pct: Decimal
    wash_bars: int
    min_close_after_breakout: Decimal
    latest_time: str
    latest_close: Decimal
    bars_since_breakout: int
    score: Decimal
    door_15m: DoorPattern | None = None
    anchor_high: Decimal = Decimal("0")


class BinanceError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "筛选币安 USDⓈ-M 永续合约 4小时K 中类似 ARIA 2026-03-13 "
            "前后形态的币：先有向上锚点，随后下杀但收盘守住锚点，"
            "再一根阳线拉回，之后洗盘收盘不破锚点。"
        )
    )
    parser.add_argument("--quote", default="USDT", help="报价资产，默认 USDT。")
    parser.add_argument("--provider", choices=("binance", "okx"), default="binance", help="行情来源，默认 binance。")
    parser.add_argument("--symbols", help="只扫描指定交易对，多个用英文逗号分隔。")
    parser.add_argument("--lookback-days", type=int, default=45, help="不指定日期时，回看最近多少天，默认 45。")
    parser.add_argument("--start-date", help="UTC 起始日期，格式 YYYY-MM-DD。")
    parser.add_argument("--end-date", help="UTC 结束日期，格式 YYYY-MM-DD，不传则到当前时间。")
    parser.add_argument("--recent-bars", type=int, default=84, help="只保留突破阳线距离最新K线不超过多少根，默认 84 根=14天。")
    parser.add_argument("--max-pullback-bars", type=int, default=12, help="锚点后最多等待多少根K线出现突破阳线，默认 12。")
    parser.add_argument("--min-wash-bars", type=int, default=3, help="突破阳线后至少洗盘多少根K线，默认 3。")
    parser.add_argument("--wash-check-bars", type=int, default=24, help="突破后只检查多少根4小时K的洗盘不破；0 表示一直检查到最新。默认 24。")
    parser.add_argument("--close-tolerance-pct", type=Decimal, default=Decimal("0.5"), help="洗盘收盘允许低于锚点的容差百分比，默认 0.5。")
    parser.add_argument("--min-anchor-up-pct", type=Decimal, default=Decimal("0.5"), help="锚点K线收盘相对开盘至少上涨百分比，默认 0.5。")
    parser.add_argument("--min-pullback-depth-pct", type=Decimal, default=Decimal("1.0"), help="下杀低点至少低于锚点收盘多少百分比，默认 1。")
    parser.add_argument("--min-breakout-candle-pct", type=Decimal, default=Decimal("6.0"), help="拉回阳线自身涨幅至少多少百分比，默认 6。")
    parser.add_argument("--min-breakout-from-anchor-pct", type=Decimal, default=Decimal("8.0"), help="拉回阳线收盘至少高于锚点多少百分比，默认 8。")
    parser.add_argument("--min-quote-volume", type=Decimal, default=Decimal("500000"), help="锚点到突破段每根K线最低成交额，默认 50万 USDT。")
    parser.add_argument("--no-15m-door", action="store_true", help="只筛 4小时结构，不要求后续15分钟门形。")
    parser.add_argument("--door-lookahead-bars", type=int, default=96, help="4小时突破后，在多少根15分钟K内寻找门形，默认 96 根=24小时。")
    parser.add_argument("--door-peak-bars", type=int, default=32, help="门底起点后，最多多少根15分钟K内出现上冲高点，默认 32。")
    parser.add_argument("--door-drop-bars", type=int, default=48, help="上冲高点后，最多多少根15分钟K内回落到门底附近，默认 48。")
    parser.add_argument("--door-retest-bars", type=int, default=64, help="第一次回落后，最多多少根15分钟K内出现二次回踩，默认 64。")
    parser.add_argument("--min-door-up-pct", type=Decimal, default=Decimal("10.0"), help="15分钟门形从起点到高点至少上涨百分比，默认 10。")
    parser.add_argument("--door-floor-tolerance-pct", type=Decimal, default=Decimal("1.0"), help="门底允许被影线低于起点的容差百分比，默认 1。")
    parser.add_argument("--door-near-start-pct", type=Decimal, default=Decimal("0"), help="回落/回踩低点距离门底起点不超过多少百分比；0 表示不限制，只要求不破门底。默认 0。")
    parser.add_argument("--workers", type=int, default=1, help="并发请求数，默认 1，避免触发 Binance 限频。")
    parser.add_argument("--timeout", type=float, default=15.0, help="HTTP 超时时间，默认 15 秒。")
    parser.add_argument("--retries", type=int, default=3, help="HTTP 重试次数，默认 3。")
    parser.add_argument("--retry-delay", type=float, default=0.8, help="HTTP 重试基础等待秒数，默认 0.8。")
    parser.add_argument("--sleep", type=float, default=0.15, help="每个请求后的暂停秒数，默认 0.15。")
    parser.add_argument("--limit", type=int, default=50, help="最多打印多少条结果，默认 50。")
    parser.add_argument("--csv-file", default="aria_4h_pattern_matches.csv", help="CSV 输出文件。默认 aria_4h_pattern_matches.csv。")
    parser.add_argument("--insecure", action="store_true", help="关闭 HTTPS 证书校验。")
    return parser.parse_args()


def pct(value: Decimal) -> Decimal:
    return value / Decimal("100")


def decimal_to_string(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal(1)))
    return format(normalized, "f")


def request_json(path: str, params: dict[str, object], args: argparse.Namespace):
    query = urllib.parse.urlencode(params)
    base_url = OKX_BASE_URL if args.provider == "okx" else BINANCE_FAPI_BASE_URL
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "aria-4h-pattern-screener/1.0",
        },
    )
    context = ssl._create_unverified_context() if args.insecure else None
    last_error = None
    for attempt in range(1, args.retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=args.timeout, context=context) as response:
                body = response.read().decode("utf-8")
            return json.loads(body)
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {exc.code}: {message}"
            if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                break
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            last_error = str(exc)

        if attempt < args.retries:
            time.sleep(args.retry_delay * attempt)

    raise BinanceError(f"request failed: {url}: {last_error}")


def ms_at_utc_date(value: str) -> int:
    day = dt.date.fromisoformat(value)
    midnight = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
    return int(midnight.timestamp() * 1000)


def scan_start_end(args: argparse.Namespace) -> tuple[int, int]:
    if args.start_date:
        start_ms = ms_at_utc_date(args.start_date)
    else:
        start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.lookback_days)
        start_ms = int(start.timestamp() * 1000)

    if args.end_date:
        end_day = dt.date.fromisoformat(args.end_date) + dt.timedelta(days=1)
        end = dt.datetime.combine(end_day, dt.time.min, tzinfo=dt.timezone.utc)
    else:
        end = dt.datetime.now(dt.timezone.utc)
    return start_ms, int(end.timestamp() * 1000)


def load_symbols(args: argparse.Namespace) -> list[SymbolInfo]:
    if args.symbols:
        requested = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
        return [
            SymbolInfo(
                symbol=normalize_symbol(symbol, args),
                base_asset=normalize_symbol(symbol, args).split("-")[0] if args.provider == "okx" else symbol.removesuffix(args.quote),
                quote_asset=args.quote,
                contract_type="PERPETUAL",
            )
            for symbol in requested
        ]

    if args.provider == "okx":
        return load_okx_symbols(args)

    data = request_json("/fapi/v1/exchangeInfo", {}, args)
    symbols = []
    for raw in data.get("symbols", []):
        if raw.get("status") != "TRADING":
            continue
        if raw.get("contractType") != "PERPETUAL":
            continue
        quote_asset = raw.get("quoteAsset") or raw.get("marginAsset") or ""
        symbol = raw.get("symbol", "")
        if quote_asset != args.quote:
            continue
        symbols.append(
            SymbolInfo(
                symbol=symbol,
                base_asset=raw.get("baseAsset", ""),
                quote_asset=quote_asset,
                contract_type=raw.get("contractType", ""),
            )
        )
    return sorted(symbols, key=lambda item: item.symbol)


def normalize_symbol(symbol: str, args: argparse.Namespace) -> str:
    if args.provider != "okx":
        return symbol
    if "-" in symbol:
        return symbol
    if symbol.endswith(args.quote):
        base = symbol[: -len(args.quote)]
        return f"{base}-{args.quote}-SWAP"
    return symbol


def load_okx_symbols(args: argparse.Namespace) -> list[SymbolInfo]:
    data = request_json("/api/v5/public/instruments", {"instType": "SWAP"}, args)
    if data.get("code") != "0":
        raise BinanceError(f"OKX instruments error: {data}")

    symbols = []
    for raw in data.get("data", []):
        if raw.get("state") != "live":
            continue
        if raw.get("ctType") != "linear":
            continue
        if raw.get("settleCcy") != args.quote:
            continue
        inst_id = raw.get("instId", "")
        if not inst_id.endswith(f"-{args.quote}-SWAP"):
            continue
        symbols.append(
            SymbolInfo(
                symbol=inst_id,
                base_asset=inst_id.split("-")[0],
                quote_asset=args.quote,
                contract_type="SWAP",
            )
        )
    return sorted(symbols, key=lambda item: item.symbol)


def load_candles(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    args: argparse.Namespace,
    limit: int = 1500,
) -> list[Candle]:
    if args.provider == "okx":
        return load_okx_candles(symbol, interval, start_ms, end_ms, args, limit)

    candles = []
    interval_step = interval_to_ms(interval)
    next_start = start_ms
    while next_start < end_ms:
        data = request_json(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": next_start,
                "endTime": end_ms,
                "limit": limit,
            },
            args,
        )
        if not data:
            break

        for row in data:
            if int(row[6]) > int(time.time() * 1000):
                continue
            candles.append(
                Candle(
                    open_time=int(row[0]),
                    open=Decimal(str(row[1])),
                    high=Decimal(str(row[2])),
                    low=Decimal(str(row[3])),
                    close=Decimal(str(row[4])),
                    volume=Decimal(str(row[5])),
                    quote_volume=Decimal(str(row[7])),
                )
            )

        last_open_time = int(data[-1][0])
        next_start = last_open_time + interval_step
        if len(data) < limit:
            break
        if args.sleep > 0:
            time.sleep(args.sleep)

    deduped = {item.open_time: item for item in candles}
    return [deduped[key] for key in sorted(deduped)]


def interval_to_ms(interval: str) -> int:
    if interval == "4h":
        return 4 * 60 * 60 * 1000
    if interval == "15m":
        return 15 * 60 * 1000
    raise ValueError(f"unsupported interval: {interval}")


def okx_bar(interval: str) -> str:
    if interval == "4h":
        return "4H"
    if interval == "15m":
        return "15m"
    return interval


def load_okx_candles(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    args: argparse.Namespace,
    limit: int = 1500,
) -> list[Candle]:
    data = request_json(
        "/api/v5/market/history-candles",
        {
            "instId": symbol,
            "bar": okx_bar(interval),
            "limit": min(limit, 300),
        },
        args,
    )
    if data.get("code") != "0":
        raise BinanceError(f"OKX candles error for {symbol}: {data}")

    candles = []
    for row in data.get("data", []):
        open_time = int(row[0])
        if open_time < start_ms or open_time > end_ms:
            continue
        candles.append(
            Candle(
                open_time=open_time,
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=Decimal(str(row[5])),
                quote_volume=Decimal(str(row[7])),
            )
        )
    return sorted(candles, key=lambda item: item.open_time)


def load_4h_candles(symbol: str, args: argparse.Namespace) -> list[Candle]:
    start_ms, end_ms = scan_start_end(args)
    return load_candles(symbol, "4h", start_ms, end_ms, args)


def load_15m_candles_after(symbol: str, start_ms: int, args: argparse.Namespace) -> list[Candle]:
    total_bars = (
        args.door_lookahead_bars
        + args.door_peak_bars
        + args.door_drop_bars
        + args.door_retest_bars
        + 8
    )
    end_ms = start_ms + total_bars * 15 * 60 * 1000
    return load_candles(symbol, "15m", start_ms, end_ms, args)


def score_match(match: PatternMatch) -> Decimal:
    hold_line = match.anchor_high if match.anchor_high > 0 else match.anchor_close
    if hold_line == 0:
        return Decimal("0")
    support_gap = (match.min_close_after_breakout / hold_line - Decimal("1")) * Decimal("100")
    breakout_strength = (match.breakout_close / hold_line - Decimal("1")) * Decimal("100")
    recency_penalty = Decimal(match.bars_since_breakout) * Decimal("0.08")
    return breakout_strength + support_gap - recency_penalty


def has_inverted_u_wash(candles: list[Candle], hold_line: Decimal) -> bool:
    if len(candles) < 5:
        return False
    closes = [item.close for item in candles]
    peak_index = max(range(len(candles)), key=lambda index: closes[index])
    if peak_index < 1 or peak_index > len(candles) - 2:
        return False

    start_close = closes[0]
    peak_close = closes[peak_index]
    end_close = closes[-1]
    if peak_close < hold_line * Decimal("1.01"):
        return False
    if peak_close <= start_close * Decimal("1.005"):
        return False
    if end_close > peak_close * Decimal("0.995"):
        return False

    left = closes[: peak_index + 1]
    right = closes[peak_index:]
    left_up_count = sum(1 for before, after in zip(left, left[1:]) if after >= before)
    right_down_count = sum(1 for before, after in zip(right, right[1:]) if after <= before)
    return left_up_count >= max(1, len(left) // 2) and right_down_count >= max(1, len(right) // 2)


def find_inverted_u_wash(
    candles: list[Candle],
    start_index: int,
    hold_line: Decimal,
    max_bars: int,
    min_bars: int,
    close_floor: Decimal,
) -> tuple[list[Candle], Decimal] | None:
    search_end = min(len(candles), start_index + max_bars)
    for end_index in range(start_index + min_bars, search_end + 1):
        segment = candles[start_index:end_index]
        if any(item.close < close_floor for item in segment):
            break
        if has_inverted_u_wash(segment, hold_line):
            return segment, min(item.close for item in segment)
    return None


def find_symbol_matches(symbol: str, candles: list[Candle], args: argparse.Namespace) -> list[PatternMatch]:
    matches: list[PatternMatch] = []
    if len(candles) < args.max_pullback_bars + args.min_wash_bars + 3:
        return matches

    close_floor_ratio = Decimal("1") - pct(args.close_tolerance_pct)
    pullback_ratio = Decimal("1") - pct(args.min_pullback_depth_pct)
    breakout_anchor_ratio = Decimal("1") + pct(args.min_breakout_from_anchor_pct)

    latest_index = len(candles) - 1
    for anchor_index in range(1, len(candles) - args.min_wash_bars - 1):
        anchor = candles[anchor_index]
        if anchor.quote_volume < args.min_quote_volume:
            continue
        if anchor.open <= 0 or anchor.close / anchor.open - Decimal("1") < pct(args.min_anchor_up_pct):
            continue
        if anchor.close <= candles[anchor_index - 1].close:
            continue

        hold_line = anchor.high
        floor = hold_line * close_floor_ratio
        found_pullback = False
        pullback_low = anchor.low
        pullback_low_index = anchor_index

        search_end = min(latest_index - args.min_wash_bars, anchor_index + args.max_pullback_bars)
        for breakout_index in range(anchor_index + 2, search_end + 1):
            candidate_span = candles[anchor_index + 1 : breakout_index]
            if not candidate_span:
                continue
            if any(item.quote_volume < args.min_quote_volume for item in candidate_span):
                continue

            span_low_index, span_low = min(
                enumerate(candidate_span, start=anchor_index + 1),
                key=lambda item: item[1].low,
            )
            if span_low.low <= hold_line * pullback_ratio:
                found_pullback = True
                pullback_low = span_low.low
                pullback_low_index = span_low_index
            if not found_pullback:
                continue

            breakout = candles[breakout_index]
            if breakout.quote_volume < args.min_quote_volume:
                continue
            if breakout.close <= breakout.open:
                continue
            if breakout.open <= 0 or breakout.close / breakout.open - Decimal("1") < pct(args.min_breakout_candle_pct):
                continue
            if breakout.close < hold_line * breakout_anchor_ratio:
                continue

            # The hold rule starts after the reclaim/breakout candle. The pullback
            # itself is allowed to shake below the yellow rally high; later wash
            # closes are what must defend that yellow high.
            max_wash_bars = args.wash_check_bars if args.wash_check_bars > 0 else len(candles) - breakout_index - 1
            wash = find_inverted_u_wash(
                candles,
                breakout_index,
                hold_line,
                max_wash_bars,
                args.min_wash_bars,
                floor,
            )
            if wash is None:
                continue
            after, min_after_close = wash
            if latest_index - breakout_index > args.recent_bars:
                continue

            match = PatternMatch(
                symbol=symbol,
                breakout_open_time=breakout.open_time,
                anchor_time=anchor.day_time,
                anchor_close=anchor.close,
                pullback_low_time=candles[pullback_low_index].day_time,
                pullback_low=pullback_low,
                breakout_time=breakout.day_time,
                breakout_close=breakout.close,
                breakout_gain_pct=breakout.change_pct,
                wash_bars=len(after),
                min_close_after_breakout=min_after_close,
                latest_time=candles[-1].day_time,
                latest_close=candles[-1].close,
                bars_since_breakout=latest_index - breakout_index,
                score=Decimal("0"),
                anchor_high=anchor.high,
            )
            matches.append(
                PatternMatch(
                    **{
                        **match.__dict__,
                        "score": score_match(match),
                    }
                )
            )

    return matches


def find_15m_door(candles: list[Candle], args: argparse.Namespace) -> DoorPattern | None:
    if len(candles) < 8:
        return None

    max_start_index = min(len(candles) - 4, args.door_lookahead_bars)
    floor_ratio = Decimal("1") - pct(args.door_floor_tolerance_pct)
    near_ceiling = None
    if args.door_near_start_pct > 0:
        near_ceiling = Decimal("1") + pct(args.door_near_start_pct)
    up_ratio = Decimal("1") + pct(args.min_door_up_pct)

    for start_index in range(0, max_start_index):
        start = candles[start_index]
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

            drop_end = min(len(candles) - 2, peak_index + args.door_drop_bars)
            for drop_index in range(peak_index + 1, drop_end + 1):
                drop = candles[drop_index]
                if drop.low < floor:
                    break
                if near_ceiling is not None and drop.low > start_price * near_ceiling:
                    continue

                retest_end = min(len(candles) - 1, drop_index + args.door_retest_bars)
                for retest_index in range(drop_index + 2, retest_end + 1):
                    retest = candles[retest_index]
                    if retest.low < floor:
                        break
                    if near_ceiling is not None and retest.low > start_price * near_ceiling:
                        continue
                    if retest.close < floor:
                        continue
                    return DoorPattern(
                        start_time=start.day_time,
                        start_price=start_price,
                        peak_time=peak.day_time,
                        peak_high=peak.high,
                        drop_time=drop.day_time,
                        drop_low=drop.low,
                        retest_time=retest.day_time,
                        retest_low=retest.low,
                    )

    return None


def scan_one(info: SymbolInfo, args: argparse.Namespace) -> tuple[list[PatternMatch], str | None]:
    try:
        candles = load_4h_candles(info.symbol, args)
        matches = find_symbol_matches(info.symbol, candles, args)
        if args.no_15m_door or not matches:
            return matches, None

        confirmed_matches = []
        for match in matches:
            door_candles = load_15m_candles_after(info.symbol, match.breakout_open_time, args)
            door = find_15m_door(door_candles, args)
            if door is None:
                continue
            confirmed_matches.append(
                PatternMatch(
                    **{
                        **match.__dict__,
                        "door_15m": door,
                    }
                )
            )
        return confirmed_matches, None
    except (BinanceError, InvalidOperation, ValueError) as exc:
        return [], f"{info.symbol}: {exc}"
    finally:
        if args.sleep > 0:
            time.sleep(args.sleep)


def write_csv(path: str, matches: list[PatternMatch]) -> None:
    fields = [
        "symbol",
        "anchor_time_utc",
        "anchor_close",
        "pullback_low_time_utc",
        "pullback_low",
        "breakout_time_utc",
        "breakout_close",
        "breakout_gain_pct",
        "wash_bars",
        "min_close_after_breakout",
        "latest_time_utc",
        "latest_close",
        "bars_since_breakout",
        "score",
        "door_start_time_utc",
        "door_start_price",
        "door_peak_time_utc",
        "door_peak_high",
        "door_drop_time_utc",
        "door_drop_low",
        "door_retest_time_utc",
        "door_retest_low",
    ]
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for item in matches:
            writer.writerow(
                {
                    "symbol": item.symbol,
                    "anchor_time_utc": item.anchor_time,
                    "anchor_close": decimal_to_string(item.anchor_close),
                    "pullback_low_time_utc": item.pullback_low_time,
                    "pullback_low": decimal_to_string(item.pullback_low),
                    "breakout_time_utc": item.breakout_time,
                    "breakout_close": decimal_to_string(item.breakout_close),
                    "breakout_gain_pct": f"{item.breakout_gain_pct:.2f}",
                    "wash_bars": item.wash_bars,
                    "min_close_after_breakout": decimal_to_string(item.min_close_after_breakout),
                    "latest_time_utc": item.latest_time,
                    "latest_close": decimal_to_string(item.latest_close),
                    "bars_since_breakout": item.bars_since_breakout,
                    "score": f"{item.score:.2f}",
                    "door_start_time_utc": item.door_15m.start_time if item.door_15m else "",
                    "door_start_price": decimal_to_string(item.door_15m.start_price) if item.door_15m else "",
                    "door_peak_time_utc": item.door_15m.peak_time if item.door_15m else "",
                    "door_peak_high": decimal_to_string(item.door_15m.peak_high) if item.door_15m else "",
                    "door_drop_time_utc": item.door_15m.drop_time if item.door_15m else "",
                    "door_drop_low": decimal_to_string(item.door_15m.drop_low) if item.door_15m else "",
                    "door_retest_time_utc": item.door_15m.retest_time if item.door_15m else "",
                    "door_retest_low": decimal_to_string(item.door_15m.retest_low) if item.door_15m else "",
                }
            )


def print_table(matches: list[PatternMatch], limit: int) -> None:
    rows = matches[:limit]
    if not rows:
        print("No matches.")
        return

    print(
        "symbol anchor_utc anchor_close pullback_low breakout_utc breakout_close "
        "breakout% wash_bars min_after_close latest_close bars score door_start door_peak door_retest"
    )
    for item in rows:
        door_start = item.door_15m.start_time if item.door_15m else "-"
        door_peak = decimal_to_string(item.door_15m.peak_high) if item.door_15m else "-"
        door_retest = decimal_to_string(item.door_15m.retest_low) if item.door_15m else "-"
        print(
            item.symbol,
            item.anchor_time,
            decimal_to_string(item.anchor_close),
            decimal_to_string(item.pullback_low),
            item.breakout_time,
            decimal_to_string(item.breakout_close),
            f"{item.breakout_gain_pct:.2f}",
            item.wash_bars,
            decimal_to_string(item.min_close_after_breakout),
            decimal_to_string(item.latest_close),
            item.bars_since_breakout,
            f"{item.score:.2f}",
            door_start,
            door_peak,
            door_retest,
        )


def main() -> int:
    args = parse_args()
    symbols = load_symbols(args)
    if not symbols:
        print("No symbols to scan.", file=sys.stderr)
        return 1

    all_matches: list[PatternMatch] = []
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {executor.submit(scan_one, info, args): info.symbol for info in symbols}
        for index, future in enumerate(concurrent.futures.as_completed(future_map), start=1):
            matches, error = future.result()
            all_matches.extend(matches)
            if error:
                errors.append(error)
            if index % 50 == 0:
                print(f"scanned={index}/{len(symbols)} matches={len(all_matches)} errors={len(errors)}", file=sys.stderr)

    best_by_symbol: dict[str, PatternMatch] = {}
    for match in all_matches:
        current = best_by_symbol.get(match.symbol)
        if current is None or (match.bars_since_breakout, -match.score) < (current.bars_since_breakout, -current.score):
            best_by_symbol[match.symbol] = match

    matches = sorted(
        best_by_symbol.values(),
        key=lambda item: (item.bars_since_breakout, -item.score, item.symbol),
    )
    write_csv(args.csv_file, matches)
    print_table(matches, args.limit)
    print(f"\nmatched_symbols={len(matches)} csv={args.csv_file} errors={len(errors)}")
    if errors:
        print("first_errors:", file=sys.stderr)
        for error in errors[:10]:
            print(error, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
