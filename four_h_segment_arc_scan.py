#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import ssl
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import market_archive_pattern_scan as archive
import screen_aria_4h_pattern as base


BJ = dt.timezone(dt.timedelta(hours=8))


@dataclass(frozen=True)
class SegmentArcMatch:
    symbol: str
    yellow_start: base.Candle
    yellow_peak: base.Candle
    yellow_end: base.Candle
    blue_start: base.Candle
    blue_low: base.Candle
    reclaim: base.Candle
    wash_start: base.Candle
    wash_end: base.Candle
    hold_line: Decimal
    wash_min_close: Decimal
    wash_peak: Decimal
    score: Decimal
    blue_below_close_count: int = 0
    breakout_volume_ratio: Decimal = Decimal("0")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="扫描4小时段结构：黄色拉升段 -> 蓝色下杀段 -> 洗盘段。")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start-date", required=True, help="UTC 起始日期 YYYY-MM-DD。")
    parser.add_argument("--end-date", required=True, help="UTC 结束日期 YYYY-MM-DD，含当天。")
    parser.add_argument("--csv-file")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--sort", choices=("time", "score", "compact", "quality"), default="time")
    return parser.parse_args()


def ms_at(day: dt.date) -> int:
    return int(dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc).timestamp() * 1000)


def bj(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).astimezone(BJ).strftime("%Y-%m-%d %H:%M")


def pct(a: Decimal, b: Decimal) -> Decimal:
    if b <= 0:
        return Decimal("0")
    return (a / b - Decimal("1")) * Decimal("100")


def highest_close(candles: list[base.Candle]) -> Decimal:
    return max(c.close for c in candles)


def dstr(value: Decimal) -> str:
    return base.decimal_to_string(value)


def monotonic_score(values: list[Decimal], direction: str) -> Decimal:
    if len(values) < 2:
        return Decimal("0")
    ok = 0
    for before, after in zip(values, values[1:]):
        if direction == "up" and after >= before:
            ok += 1
        if direction == "down" and after <= before:
            ok += 1
    return Decimal(ok) / Decimal(len(values) - 1)


def is_hold_wash(
    segment: list[base.Candle],
    hold_line: Decimal,
    args: argparse.Namespace,
) -> tuple[bool, Decimal, Decimal]:
    if not segment:
        return False, Decimal("0"), Decimal("0")
    closes = [c.close for c in segment]
    if min(closes) < hold_line:
        return False, Decimal("0"), Decimal("0")
    wash_peak = max(closes)
    wash_min_close = min(closes)
    if wash_peak <= hold_line:
        return False, Decimal("0"), Decimal("0")
    return True, wash_min_close, wash_peak


def is_valid_wash(
    segment: list[base.Candle],
    hold_line: Decimal,
    args: argparse.Namespace,
) -> tuple[bool, Decimal, Decimal]:
    return is_hold_wash(segment, hold_line, args)


def average_quote_volume(candles: list[base.Candle]) -> Decimal:
    if not candles:
        return Decimal("0")
    return sum((item.quote_volume for item in candles), Decimal("0")) / Decimal(len(candles))


def median_quote_volume(candles: list[base.Candle]) -> Decimal:
    values = sorted(item.quote_volume for item in candles)
    if not values:
        return Decimal("0")
    mid = len(values) // 2
    if len(values) % 2 == 1:
        return values[mid]
    return (values[mid - 1] + values[mid]) / Decimal("2")


def departure_indexes(
    candles: list[base.Candle],
    wash_start_i: int,
    hold_line: Decimal,
    max_wash_bars: int | None = None,
) -> list[int]:
    indexes: list[int] = []
    end_i = len(candles)
    if max_wash_bars is not None:
        end_i = min(end_i, wash_start_i + max(1, max_wash_bars) + 1)
    for index in range(wash_start_i + 1, end_i):
        segment = candles[wash_start_i:index]
        if any(candle.close < hold_line for candle in segment):
            return indexes
        candle = candles[index]
        if candle.close < hold_line:
            return indexes
        current_peak_close = max(item.close for item in segment)
        typical_wash_volume = median_quote_volume(segment)
        volume_departure = candle.quote_volume > typical_wash_volume and candle.close > current_peak_close
        if volume_departure:
            indexes.append(index)
    return indexes


def score_match(
    hold_line: Decimal,
    yellow_low: Decimal,
    blue_low: Decimal,
    wash_min_close: Decimal,
    wash_peak: Decimal,
) -> Decimal:
    return (
        pct(hold_line, yellow_low)
        + abs(pct(blue_low, hold_line))
        + pct(wash_peak, hold_line)
        + pct(wash_min_close, hold_line)
    )


def remove_nested_substructures(matches: list[SegmentArcMatch]) -> list[SegmentArcMatch]:
    """Drop small setups carved out of the blue/wash area of a larger setup."""
    kept: list[SegmentArcMatch] = []
    for match in sorted(matches, key=lambda item: (item.yellow_start.open_time, item.wash_end.open_time)):
        nested = False
        for parent in kept:
            inside_parent_setup = (
                parent.yellow_peak.open_time < match.yellow_start.open_time <= parent.wash_end.open_time
            )
            if inside_parent_setup:
                nested = True
                break
        if not nested:
            kept.append(match)
    return kept


def selection_key(match: SegmentArcMatch) -> tuple[int, Decimal, Decimal, Decimal]:
    total_bars = (match.wash_end.open_time - match.yellow_start.open_time) // (4 * 60 * 60 * 1000) + 1
    hold_gap = pct(match.wash_min_close, match.hold_line)
    yellow_up = pct(match.hold_line, match.yellow_start.low)
    return (
        abs(hold_gap),
        -yellow_up,
        total_bars,
        blue_drop_abs_pct(match),
    )


def blue_drop_abs_pct(match: SegmentArcMatch) -> Decimal:
    return abs(pct(match.blue_low.low, match.hold_line))


def wash_hold_gap_pct(match: SegmentArcMatch) -> Decimal:
    return pct(match.wash_min_close, match.hold_line)


def candle_count(start_open_time: int, end_open_time: int) -> int:
    return (end_open_time - start_open_time) // (4 * 60 * 60 * 1000) + 1


def blue_close_below_ratio(match: SegmentArcMatch) -> Decimal:
    blue_bars = candle_count(match.blue_start.open_time, match.reclaim.open_time)
    if blue_bars <= 0:
        return Decimal("0")
    return Decimal(match.blue_below_close_count) / Decimal(blue_bars)


def reclaim_delay_after_low(match: SegmentArcMatch) -> int:
    return candle_count(match.blue_low.open_time, match.reclaim.open_time)


def wash_bar_count(match: SegmentArcMatch) -> int:
    return candle_count(match.wash_start.open_time, match.wash_end.open_time)


def structure_mass(match: SegmentArcMatch) -> int:
    return match.blue_below_close_count * wash_bar_count(match)


def wash_close_gap_stats(segment: list[base.Candle], hold_line: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    gaps = [pct(candle.close, hold_line) for candle in segment]
    if not gaps:
        return Decimal("0"), Decimal("0"), Decimal("0")
    average = sum(gaps, Decimal("0")) / Decimal(len(gaps))
    return min(gaps), average, max(gaps)


def is_tight_wash_above_hold(segment: list[base.Candle], hold_line: Decimal, args: argparse.Namespace) -> bool:
    min_gap, average_gap, peak_gap = wash_close_gap_stats(segment, hold_line)
    return min_gap >= Decimal("0")


def is_blue_breakout_end(
    candles: list[base.Candle],
    index: int,
    hold_line: Decimal,
    args: argparse.Namespace,
    blue_before_breakout: list[base.Candle],
) -> bool:
    if index <= 0:
        return False
    candle = candles[index]
    prev_close = candles[index - 1].close
    if candle.close <= prev_close:
        return False
    if candle.close < hold_line:
        return False
    return True


def is_coherent_yellow_rally(segment: list[base.Candle]) -> bool:
    if len(segment) < 2:
        return False
    start_close = segment[0].close
    end_close = segment[-1].close
    if end_close <= start_close:
        return False
    midway_close = start_close + (end_close - start_close) / Decimal("2")
    has_lifted = False
    for candle in segment[1:]:
        if candle.close >= midway_close:
            has_lifted = True
        if has_lifted and candle.close < start_close:
            return False
    return has_lifted


def blue_start_has_clean_boundary(
    candles: list[base.Candle],
    hold_i: int,
    blue_start_i: int,
    blue_low_i: int,
    hold_line: Decimal,
    args: argparse.Namespace,
) -> bool:
    return True


def has_failed_reclaim_before(
    candles: list[base.Candle],
    blue_low_i: int,
    reclaim_i: int,
    hold_line: Decimal,
) -> bool:
    """Once price first closes back above hold after the blue low, it should not lose hold again before the chosen reclaim."""
    saw_reclaim = False
    for index in range(blue_low_i + 1, reclaim_i):
        if candles[index].close >= hold_line:
            saw_reclaim = True
            continue
        if saw_reclaim and candles[index].close < hold_line:
            return True
    return False


def post_departure_confirms_wash_peak(
    candles: list[base.Candle],
    departure_i: int,
    wash_peak: Decimal,
    args: argparse.Namespace,
) -> bool:
    if departure_i >= len(candles):
        return False
    departure_close = candles[departure_i].close
    extension = departure_close - wash_peak
    if extension <= 0:
        return False
    follow_through_close = departure_close + extension / Decimal("2")
    for candle in candles[departure_i + 1 :]:
        if candle.close < wash_peak:
            return False
        if candle.close >= follow_through_close:
            return True
    return True


def same_bj_month(left_open_time: int, right_open_time: int) -> bool:
    left = dt.datetime.fromtimestamp(left_open_time / 1000, tz=dt.timezone.utc).astimezone(BJ)
    right = dt.datetime.fromtimestamp(right_open_time / 1000, tz=dt.timezone.utc).astimezone(BJ)
    return left.year == right.year and left.month == right.month


def month_holds_wash_peak_after_departure(
    candles: list[base.Candle],
    departure_i: int,
    wash_peak: Decimal,
    args: argparse.Namespace,
) -> bool:
    if not getattr(args, "require_month_hold_after_departure", True):
        return True
    if departure_i >= len(candles):
        return False
    departure_open_time = candles[departure_i].open_time
    for candle in candles[departure_i:]:
        if not same_bj_month(candle.open_time, departure_open_time):
            break
        if candle.close < wash_peak:
            return False
    return True


def canonical_blue_start_i(candles: list[base.Candle], yellow_peak_i: int, blue_low_i: int) -> int:
    return yellow_peak_i + 1


def normalized_blue_start_i(
    candles: list[base.Candle],
    raw_blue_start_i: int,
    blue_low_i: int,
    hold_line: Decimal,
) -> int:
    for index in range(raw_blue_start_i, blue_low_i + 1):
        if candles[index].close < hold_line:
            return index
    return raw_blue_start_i


def canonical_yellow_start_i(candles: list[base.Candle], start_i: int, yellow_end_i: int) -> int:
    if yellow_end_i <= start_i:
        return start_i
    hold_line = candles[yellow_end_i].close
    local_start_i = start_i
    for index in range(yellow_end_i - 1, start_i - 1, -1):
        if candles[index].close > hold_line:
            local_start_i = index + 1
            break
    if local_start_i >= yellow_end_i:
        local_start_i = start_i
    low_close_i = min(range(local_start_i, yellow_end_i), key=lambda i: candles[i].close)
    base_start_i = min(low_close_i + 1, yellow_end_i)
    if base_start_i < yellow_end_i:
        base_close = candles[base_start_i].close
        dipped_after_base = False
        for index in range(base_start_i + 1, yellow_end_i):
            candle = candles[index]
            if candle.close < base_close:
                dipped_after_base = True
                continue
            if not dipped_after_base:
                continue
            prefix = candles[base_start_i:index]
            prefix_high = max(item.high for item in prefix)
            if (
                candle.close > prefix_high
                and candle.close > candle.open
                and candle.quote_volume >= median_quote_volume(prefix)
            ):
                return index
    return base_start_i


def structure_rank_key(match: SegmentArcMatch) -> tuple[Decimal, Decimal, int, Decimal, Decimal, Decimal, Decimal]:
    total_bars = (match.wash_end.open_time - match.yellow_start.open_time) // (4 * 60 * 60 * 1000) + 1
    blue_drop = blue_drop_abs_pct(match)
    peak_gap = pct(match.wash_peak, match.hold_line)
    min_gap = wash_hold_gap_pct(match)
    return (
        abs(min_gap),
        peak_gap,
        total_bars,
        -match.breakout_volume_ratio,
        -blue_close_below_ratio(match),
        blue_drop,
        -Decimal(structure_mass(match)),
    )


def keep_rally_candidates(matches: list[SegmentArcMatch]) -> list[SegmentArcMatch]:
    grouped: dict[
        tuple[int, int, int, int, Decimal],
        dict[int, tuple[int, SegmentArcMatch]],
    ] = {}
    for match in matches:
        key = (
            match.yellow_peak.open_time,
            match.blue_low.open_time,
            match.reclaim.open_time,
            match.wash_start.open_time,
            match.wash_end.open_time,
            match.hold_line,
        )
        by_start = grouped.setdefault(key, {})
        start_key = match.yellow_start.open_time
        count, representative = by_start.get(start_key, (0, match))
        by_start[start_key] = (count + 1, representative)

    kept: list[SegmentArcMatch] = []
    for by_start in grouped.values():
        _, representative = max(
            by_start.values(),
            key=lambda item: (item[0], -item[1].yellow_start.open_time),
        )
        kept.append(representative)
    return kept


def keep_tightest_hold_for_wash(matches: list[SegmentArcMatch]) -> list[SegmentArcMatch]:
    best_by_wash: dict[tuple[int, int, int, int], SegmentArcMatch] = {}
    for match in matches:
        key = (
            match.blue_low.open_time,
            match.reclaim.open_time,
            match.wash_start.open_time,
            match.wash_end.open_time,
        )
        current = best_by_wash.get(key)
        if current is None:
            best_by_wash[key] = match
            continue
        current_key = (
            abs(wash_hold_gap_pct(current)),
            pct(current.wash_peak, current.hold_line),
            -current.hold_line,
            current.yellow_start.open_time,
            structure_rank_key(current),
        )
        match_key = (
            abs(wash_hold_gap_pct(match)),
            pct(match.wash_peak, match.hold_line),
            -match.hold_line,
            match.yellow_start.open_time,
            structure_rank_key(match),
        )
        if match_key < current_key:
            best_by_wash[key] = match
    return list(best_by_wash.values())


def keep_first_wash_for_blue(matches: list[SegmentArcMatch]) -> list[SegmentArcMatch]:
    best_by_blue: dict[tuple[int, int, Decimal], SegmentArcMatch] = {}
    for match in matches:
        key = (match.yellow_peak.open_time, match.blue_low.open_time, match.hold_line)
        current = best_by_blue.get(key)
        if current is None:
            best_by_blue[key] = match
            continue
        current_key = (
            current.wash_start.open_time,
            current.wash_end.open_time,
            structure_rank_key(current),
        )
        match_key = (
            match.wash_start.open_time,
            match.wash_end.open_time,
            structure_rank_key(match),
        )
        if match_key < current_key:
            best_by_blue[key] = match
    return list(best_by_blue.values())


def remove_preempted_broad_matches(matches: list[SegmentArcMatch]) -> list[SegmentArcMatch]:
    kept: list[SegmentArcMatch] = []
    for match in matches:
        preempted = False
        match_mass = structure_mass(match)
        for other in matches:
            if other is match:
                continue
            if not (match.yellow_peak.open_time < other.yellow_start.open_time < match.wash_start.open_time):
                continue
            if other.wash_start.open_time > match.wash_start.open_time:
                continue
            if structure_mass(other) * 2 < match_mass:
                continue
            if pct(other.wash_peak, other.hold_line) > pct(match.wash_peak, match.hold_line):
                continue
            preempted = True
            break
        if not preempted:
            kept.append(match)
    return kept


def remove_dominated_substructures(matches: list[SegmentArcMatch]) -> list[SegmentArcMatch]:
    kept: list[SegmentArcMatch] = []
    for match in matches:
        match_mass = structure_mass(match)
        match_peak_gap = pct(match.wash_peak, match.hold_line)
        match_blue_drop = blue_drop_abs_pct(match)
        dominated = False
        for other in matches:
            if other is match:
                continue
            if other.blue_low.open_time > match.blue_low.open_time:
                continue
            same_downkill_and_wash_end = (
                other.blue_low.open_time == match.blue_low.open_time
                and other.wash_end.open_time == match.wash_end.open_time
            )
            later_rally_high = (
                match.yellow_peak.open_time < other.yellow_peak.open_time < other.blue_low.open_time
                and other.hold_line > match.hold_line
            )
            if same_downkill_and_wash_end and later_rally_high:
                dominated = True
                break
            if structure_mass(other) <= match_mass:
                continue
            same_blue_low = (
                other.blue_low.open_time == match.blue_low.open_time
                and other.hold_line >= match.hold_line
                and other.yellow_peak.open_time >= match.yellow_peak.open_time
            )
            earlier_or_same_wash = (
                other.wash_start.open_time <= match.wash_start.open_time
                and other.hold_line >= match.hold_line
                and other.yellow_peak.open_time >= match.yellow_peak.open_time
            )
            weak_blue_against_its_wash = (
                match_blue_drop < match_peak_gap
                and other.hold_line >= match.hold_line
                and other.yellow_peak.open_time >= match.yellow_peak.open_time
            )
            if same_blue_low or earlier_or_same_wash or weak_blue_against_its_wash:
                dominated = True
                break
        if not dominated:
            kept.append(match)
    return kept


def find_matches(candles: list[base.Candle], symbol: str, args: argparse.Namespace) -> list[SegmentArcMatch]:
    matches: list[SegmentArcMatch] = []
    n = len(candles)
    max_market_yellow_bars = getattr(args, "max_market_yellow_bars", None)
    max_market_blue_bars = getattr(args, "max_market_blue_bars", None)
    max_market_wash_bars = getattr(args, "max_market_wash_bars", None)

    for yellow_start_i in range(0, n - 3):
        blue_start_max = n - 3
        if max_market_yellow_bars is not None:
            blue_start_max = min(blue_start_max, yellow_start_i + max(1, max_market_yellow_bars))
        for blue_start_i in range(yellow_start_i + 1, blue_start_max + 1):
            yellow_end_i = blue_start_i - 1
            effective_yellow_peak_i = yellow_end_i
            if effective_yellow_peak_i <= yellow_start_i:
                continue
            effective_yellow_start_i = canonical_yellow_start_i(candles, yellow_start_i, effective_yellow_peak_i)
            if effective_yellow_start_i >= effective_yellow_peak_i:
                continue
            yellow_rally_span = candles[effective_yellow_start_i : effective_yellow_peak_i + 1]
            if not is_coherent_yellow_rally(yellow_rally_span):
                continue
            yellow_low = min(c.low for c in yellow_rally_span)
            yellow_max_close = max(c.close for c in yellow_rally_span)
            hold_line = candles[effective_yellow_peak_i].close
            if hold_line <= yellow_low:
                continue

            breakout_max = n - 2
            if max_market_blue_bars is not None:
                breakout_max = min(breakout_max, blue_start_i + max(1, max_market_blue_bars))
            for breakout_i in range(blue_start_i + 1, breakout_max + 1):
                probe_blue_low_i = min(range(blue_start_i, breakout_i + 1), key=lambda i: candles[i].low)
                effective_blue_start_i = blue_start_i
                if effective_blue_start_i >= breakout_i:
                    continue
                blue_low_i = min(range(effective_blue_start_i, breakout_i + 1), key=lambda i: candles[i].low)
                if blue_low_i < effective_blue_start_i:
                    continue
                blue_low_candle = candles[blue_low_i]
                blue_low = blue_low_candle.low
                if blue_low >= hold_line:
                    continue
                effective_blue_start_i = normalized_blue_start_i(
                    candles,
                    effective_blue_start_i,
                    blue_low_i,
                    hold_line,
                )
                if effective_blue_start_i >= breakout_i:
                    continue
                yellow_end_i = effective_blue_start_i - 1
                min_blue_drop_pct = getattr(args, "min_blue_drop_pct", None)
                if min_blue_drop_pct is not None and abs(pct(blue_low, hold_line)) < min_blue_drop_pct:
                    continue
                blue_before_breakout = candles[effective_blue_start_i:breakout_i]
                if not blue_start_has_clean_boundary(
                    candles,
                    effective_yellow_peak_i,
                    effective_blue_start_i,
                    blue_low_i,
                    hold_line,
                    args,
                ):
                    continue
                if not is_blue_breakout_end(candles, breakout_i, hold_line, args, blue_before_breakout):
                    continue
                if has_failed_reclaim_before(candles, blue_low_i, breakout_i, hold_line):
                    continue
                blue_region = candles[effective_blue_start_i : breakout_i + 1]
                if max_market_blue_bars is not None and len(blue_region) > max_market_blue_bars:
                    continue
                blue_below_close_count = sum(1 for candle in blue_region if candle.close < hold_line)
                max_blue_below_close_count = getattr(args, "max_blue_below_close_count", None)
                if max_blue_below_close_count is not None and blue_below_close_count > max_blue_below_close_count:
                    continue
                blue_average_volume = average_quote_volume(blue_before_breakout)
                breakout_volume_ratio = (
                    candles[breakout_i].quote_volume / blue_average_volume
                    if blue_average_volume > 0
                    else Decimal("0")
                )

                wash_start_i = breakout_i
                if wash_start_i >= n - 1:
                    continue
                wash_start = candles[wash_start_i]
                if wash_start.close < hold_line:
                    continue

                departures = departure_indexes(candles, wash_start_i, hold_line, max_market_wash_bars)
                for departure_pos, departure_i in enumerate(departures):
                    has_later_departure = departure_pos < len(departures) - 1
                    if (
                        has_later_departure
                        and departure_i + 1 < len(candles)
                        and candles[departure_i + 1].close <= candles[departure_i].close
                    ):
                        continue
                    wash = candles[wash_start_i:departure_i]
                    min_market_wash_bars = getattr(args, "min_market_wash_bars", None)
                    if min_market_wash_bars is not None and len(wash) < min_market_wash_bars:
                        continue
                    if max_market_wash_bars is not None and len(wash) > max_market_wash_bars:
                        continue
                    ok, wash_min_close, wash_peak = is_valid_wash(wash, hold_line, args)
                    if ok and is_tight_wash_above_hold(wash, hold_line, args):
                        if wash_min_close < yellow_max_close:
                            continue
                        if not post_departure_confirms_wash_peak(candles, departure_i, wash_peak, args):
                            continue
                        if not month_holds_wash_peak_after_departure(candles, departure_i, wash_peak, args):
                            continue
                        wash_end = candles[departure_i - 1]
                        score = score_match(hold_line, yellow_low, blue_low, wash_min_close, wash_peak)
                        matches.append(
                            SegmentArcMatch(
                                symbol=symbol,
                                yellow_start=candles[effective_yellow_start_i],
                                yellow_peak=candles[effective_yellow_peak_i],
                                yellow_end=candles[yellow_end_i],
                                blue_start=candles[effective_blue_start_i],
                                blue_low=blue_low_candle,
                                reclaim=candles[breakout_i],
                                wash_start=wash_start,
                                wash_end=wash_end,
                                hold_line=hold_line,
                                wash_min_close=wash_min_close,
                                wash_peak=wash_peak,
                                score=score,
                                blue_below_close_count=blue_below_close_count,
                                breakout_volume_ratio=breakout_volume_ratio,
                            )
                        )
                        break

    matches = keep_rally_candidates(matches)
    matches = keep_tightest_hold_for_wash(matches)
    matches = keep_first_wash_for_blue(matches)
    matches = remove_preempted_broad_matches(matches)
    matches = remove_dominated_substructures(matches)
    sort_mode = getattr(args, "sort", "time")
    if sort_mode == "score":
        return sorted(matches, key=lambda m: m.score, reverse=True)
    if sort_mode == "compact":
        return sorted(matches, key=selection_key)
    if sort_mode == "quality":
        return sorted(matches, key=structure_rank_key)
    return sorted(
        matches,
        key=lambda m: (
            m.reclaim.open_time,
            m.wash_start.open_time,
            m.wash_end.open_time,
            -m.score,
        ),
    )


def write_csv(path: str, matches: list[SegmentArcMatch]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "symbol",
                "score",
                "yellow_start_bj",
                "yellow_start_low",
                "yellow_peak_bj",
                "yellow_peak_high_hold",
                "yellow_up_pct",
                "blue_low_bj",
                "blue_low",
                "blue_drop_pct",
                "reclaim_bj",
                "reclaim_close",
                "blue_below_close_count",
                "breakout_volume_ratio",
                "wash_start_bj",
                "wash_start_close",
                "wash_end_bj",
                "wash_min_close",
                "wash_peak_close",
            ]
        )
        for match in matches:
            writer.writerow(
                [
                    match.symbol,
                    f"{match.score:.4f}",
                    bj(match.yellow_start.open_time),
                    dstr(match.yellow_start.low),
                    bj(match.yellow_peak.open_time),
                    dstr(match.hold_line),
                    f"{pct(match.hold_line, match.yellow_start.low):.4f}",
                    bj(match.blue_low.open_time),
                    dstr(match.blue_low.low),
                    f"{pct(match.blue_low.low, match.hold_line):.4f}",
                    bj(match.reclaim.open_time),
                    dstr(match.reclaim.close),
                    match.blue_below_close_count,
                    f"{match.breakout_volume_ratio:.4f}",
                    bj(match.wash_start.open_time),
                    dstr(match.wash_start.close),
                    bj(match.wash_end.open_time),
                    dstr(match.wash_min_close),
                    dstr(match.wash_peak),
                ]
            )


def main() -> int:
    args = parse_args()
    context = ssl._create_unverified_context()
    start = dt.date.fromisoformat(args.start_date)
    end = dt.date.fromisoformat(args.end_date) + dt.timedelta(days=1)
    candles = archive.load_daily(args.symbol, "4h", ms_at(start), ms_at(end), context)
    matches = find_matches(candles, args.symbol, args)
    if args.csv_file:
        write_csv(args.csv_file, matches)
    for match in matches[: args.limit]:
        print(
            match.symbol,
            f"score={match.score:.2f}",
            f"yellow={bj(match.yellow_start.open_time)}->{bj(match.yellow_peak.open_time)} high={dstr(match.hold_line)}",
            f"blue_low={bj(match.blue_low.open_time)} {dstr(match.blue_low.low)}",
            f"reclaim={bj(match.reclaim.open_time)} close={dstr(match.reclaim.close)}",
            f"wash_start={bj(match.wash_start.open_time)} close={dstr(match.wash_start.close)}",
            f"wash_end={bj(match.wash_end.open_time)} min_close={dstr(match.wash_min_close)} peak={dstr(match.wash_peak)}",
        )
    print(f"matches={len(matches)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
