#!/usr/bin/env python3
from __future__ import annotations

import csv
import datetime as dt
import html
import ssl
import argparse
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import binance_archive_pattern_scan as archive
import four_h_segment_arc_scan as scan
import screen_aria_4h_pattern as base


BJ = dt.timezone(dt.timedelta(hours=8))
OUT_DIR = Path("four_h_segment_arc_match_charts")


@dataclass(frozen=True)
class Sample:
    symbol: str
    start_date: dt.date
    end_date: dt.date
    preferred_reclaim_bj: dt.datetime
    preferred_yellow_start_bj: dt.datetime
    preferred_wash_start_bj: dt.datetime | None
    preferred_wash_end_bj: dt.datetime | None


def parse_bj(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=BJ)


def utc_ms(day: dt.date) -> int:
    return int(dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc).timestamp() * 1000)


def bj_from_ms(value: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).astimezone(BJ)


def read_samples() -> list[Sample]:
    samples: list[Sample] = []
    with open("four_h_segment_arc_samples.csv", newline="", encoding="utf-8") as file:
        for row in csv.DictReader(file):
            preferred_wash_start = row.get("preferred_wash_start_bj", "").strip()
            preferred_wash_end = row.get("preferred_wash_end_bj", "").strip()
            samples.append(
                Sample(
                    symbol=row["symbol"],
                    start_date=dt.date.fromisoformat(row["start_date"]),
                    end_date=dt.date.fromisoformat(row["end_date"]),
                    preferred_reclaim_bj=parse_bj(row["preferred_reclaim_bj"]),
                    preferred_yellow_start_bj=parse_bj(row["preferred_yellow_start_bj"]),
                    preferred_wash_start_bj=parse_bj(preferred_wash_start) if preferred_wash_start else None,
                    preferred_wash_end_bj=parse_bj(preferred_wash_end) if preferred_wash_end else None,
                )
            )
    return samples


def candle_index(candles: list[base.Candle], candle: base.Candle) -> int:
    return next(index for index, item in enumerate(candles) if item.open_time == candle.open_time)


def x_for(index: int, count: int, left: int, width: int) -> float:
    return left + width * index / max(1, count - 1)


def y_for(price: Decimal, low: Decimal, high: Decimal, top: int, height: int) -> float:
    return top + float((high - price) / (high - low)) * height


def visual_blue_start_index(
    candles: list[base.Candle],
    blue_start_i: int,
    blue_low_i: int,
    hold_line: Decimal,
) -> int:
    index = blue_start_i
    while index < blue_low_i and candles[index].close >= hold_line:
        index += 1
    return index


def select_match(
    matches: list[scan.SegmentArcMatch],
    preferred_reclaim_bj: dt.datetime,
    preferred_yellow_start_bj: dt.datetime,
    preferred_wash_start_bj: dt.datetime | None,
    preferred_wash_end_bj: dt.datetime | None,
) -> scan.SegmentArcMatch:
    target_ms = int(preferred_reclaim_bj.astimezone(dt.timezone.utc).timestamp() * 1000)
    yellow_target_ms = int(preferred_yellow_start_bj.astimezone(dt.timezone.utc).timestamp() * 1000)
    wash_target_ms = (
        int(preferred_wash_start_bj.astimezone(dt.timezone.utc).timestamp() * 1000)
        if preferred_wash_start_bj is not None
        else target_ms
    )
    wash_end_target_ms = (
        int(preferred_wash_end_bj.astimezone(dt.timezone.utc).timestamp() * 1000)
        if preferred_wash_end_bj is not None
        else wash_target_ms
    )
    return min(
        matches,
        key=lambda item: (
            abs(item.reclaim.open_time - target_ms),
            abs(item.wash_start.open_time - wash_target_ms),
            abs(scan.pct(item.wash_min_close, item.hold_line)),
            abs(item.yellow_start.open_time - yellow_target_ms),
            abs(item.wash_end.open_time - wash_end_target_ms),
            -item.score,
        ),
    )


def render(symbol: str, candles: list[base.Candle], match: scan.SegmentArcMatch) -> str:
    width = 1600
    height = 900
    left = 96
    right = 44
    price_top = 82
    price_h = 590
    vol_top = 730
    vol_h = 115
    chart_w = width - left - right

    low = min(c.low for c in candles)
    high = max(c.high for c in candles)
    padding = (high - low) * Decimal("0.08")
    low -= padding
    high += padding
    max_vol = max((c.quote_volume for c in candles), default=Decimal("1"))

    yellow_start_i = candle_index(candles, match.yellow_start)
    yellow_peak_i = candle_index(candles, match.yellow_peak)
    yellow_end_i = candle_index(candles, match.yellow_end)
    blue_start_i = candle_index(candles, match.blue_start)
    blue_low_i = candle_index(candles, match.blue_low)
    reclaim_i = candle_index(candles, match.reclaim)
    wash_start_i = candle_index(candles, match.wash_start)
    wash_end_i = candle_index(candles, match.wash_end)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<defs>",
        '<marker id="arrow-yellow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">',
        '<path d="M0,0 L0,6 L9,3 z" fill="#facc15"/>',
        "</marker>",
        '<marker id="arrow-blue" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">',
        '<path d="M0,0 L0,6 L9,3 z" fill="#0ea5e9"/>',
        "</marker>",
        '<marker id="arrow-red" markerWidth="12" markerHeight="12" refX="10" refY="3" orient="auto" markerUnits="strokeWidth">',
        '<path d="M0,0 L0,6 L10,3 z" fill="#ff4d57"/>',
        "</marker>",
        "</defs>",
        '<rect width="100%" height="100%" fill="#111827"/>',
        f'<text x="{left}" y="38" fill="#f9fafb" font-family="Arial" font-size="26" font-weight="700">{html.escape(symbol)} 4h segment arc match</text>',
        f'<text x="{left}" y="65" fill="#9ca3af" font-family="Arial" font-size="15">Logic-found: yellow rally area, blue down-kill area, white wash area. Beijing time.</text>',
        f'<rect x="{left}" y="{price_top}" width="{chart_w}" height="{price_h}" fill="#172033" stroke="#263244"/>',
        f'<rect x="{left}" y="{vol_top}" width="{chart_w}" height="{vol_h}" fill="#111827" stroke="#263244"/>',
    ]

    for tick in range(6):
        y = price_top + price_h * tick / 5
        parts.append(f'<line x1="{left}" x2="{left + chart_w}" y1="{y:.2f}" y2="{y:.2f}" stroke="#263244"/>')

    candle_w = max(4.0, min(14.0, chart_w / max(1, len(candles)) * 0.62))
    for idx, candle in enumerate(candles):
        x = x_for(idx, len(candles), left, chart_w)
        color = "#10b981" if candle.close >= candle.open else "#ef4444"
        y_high = y_for(candle.high, low, high, price_top, price_h)
        y_low = y_for(candle.low, low, high, price_top, price_h)
        y_open = y_for(candle.open, low, high, price_top, price_h)
        y_close = y_for(candle.close, low, high, price_top, price_h)
        parts.append(f'<line x1="{x:.2f}" x2="{x:.2f}" y1="{y_high:.2f}" y2="{y_low:.2f}" stroke="{color}" stroke-width="1.5"/>')
        parts.append(f'<rect x="{x - candle_w / 2:.2f}" y="{min(y_open, y_close):.2f}" width="{candle_w:.2f}" height="{max(1.0, abs(y_close - y_open)):.2f}" fill="{color}"/>')
        vol_height = float(candle.quote_volume / max_vol) * vol_h if max_vol > 0 else 0
        parts.append(f'<rect x="{x - candle_w / 2:.2f}" y="{vol_top + vol_h - vol_height:.2f}" width="{candle_w:.2f}" height="{vol_height:.2f}" fill="{color}" opacity="0.72"/>')

    yx1 = x_for(yellow_start_i, len(candles), left, chart_w) - candle_w * 1.0
    yx2 = x_for(yellow_end_i, len(candles), left, chart_w) + candle_w * 1.0
    yellow_span = candles[yellow_start_i : yellow_end_i + 1]
    yy1 = y_for(max(c.high for c in yellow_span), low, high, price_top, price_h) - 12
    yy2 = y_for(min(c.low for c in yellow_span), low, high, price_top, price_h) + 12
    parts.append(
        f'<rect x="{yx1:.2f}" y="{min(yy1, yy2):.2f}" width="{max(1, yx2 - yx1):.2f}" height="{max(1, abs(yy2 - yy1)):.2f}" fill="#facc15" fill-opacity="0.04" stroke="#facc15" stroke-width="5" rx="2"/>'
    )

    x1 = x_for(wash_start_i, len(candles), left, chart_w) - candle_w * 1.5
    x2 = x_for(wash_end_i, len(candles), left, chart_w) + candle_w * 1.5
    wash_span = candles[wash_start_i : wash_end_i + 1]
    y1 = y_for(max(c.high for c in wash_span), low, high, price_top, price_h) - 16
    y2 = y_for(match.hold_line, low, high, price_top, price_h) + 16
    parts.append(
        f'<rect x="{x1:.2f}" y="{min(y1, y2):.2f}" width="{max(1, x2 - x1):.2f}" height="{max(1, abs(y2 - y1)):.2f}" fill="none" stroke="#f9fafb" stroke-width="5" rx="2"/>'
    )

    down_start_i = blue_start_i
    down_end_i = max(down_start_i, reclaim_i)
    down_span = candles[down_start_i : down_end_i + 1]
    if down_span:
        dx1 = x_for(down_start_i, len(candles), left, chart_w) - candle_w / 2
        dx2 = x_for(down_end_i, len(candles), left, chart_w) + candle_w / 2
        dy1 = y_for(max(c.high for c in down_span), low, high, price_top, price_h) - 14
        dy2 = y_for(min(c.low for c in down_span), low, high, price_top, price_h) + 14
        parts.append(
            f'<rect x="{dx1:.2f}" y="{min(dy1, dy2):.2f}" width="{max(1, dx2 - dx1):.2f}" height="{max(1, abs(dy2 - dy1)):.2f}" fill="#0ea5e9" fill-opacity="0.05" stroke="#0ea5e9" stroke-width="4" rx="2"/>'
        )

    bx = x_for(reclaim_i, len(candles), left, chart_w)
    body_top = min(match.reclaim.open, match.reclaim.close)
    by = y_for(body_top, low, high, price_top, price_h)
    parts.append(
        f'<line x1="{bx - 18:.2f}" x2="{bx - 2:.2f}" y1="{by - 110:.2f}" y2="{by - 12:.2f}" stroke="#ff4d57" stroke-width="5" marker-end="url(#arrow-red)"/>'
    )

    for idx in [0, len(candles) // 2, len(candles) - 1]:
        candle = candles[idx]
        x = x_for(idx, len(candles), left, chart_w)
        parts.append(f'<text x="{x - 62:.2f}" y="{height - 28}" fill="#9ca3af" font-family="Arial" font-size="13">{bj_from_ms(candle.open_time):%m-%d %H:%M}</text>')

    parts.append("</svg>")
    return "\n".join(parts)


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    context = ssl._create_unverified_context()
    index = ["<html><body style='background:#111827;color:#f9fafb;font-family:Arial'>"]
    scan_args = argparse.Namespace(
        min_yellow_bars=3,
        max_yellow_bars=24,
        min_yellow_up_pct=Decimal("2.0"),
        max_yellow_start_bearish_pct=Decimal("3.0"),
        min_blue_bars=2,
        max_blue_bars=40,
        max_blue_low_to_reclaim_bars=11,
        min_blue_drop_pct=Decimal("2.0"),
        max_blue_drop_pct=Decimal("28.0"),
        max_reclaim_bars=4,
        min_breakout_volume_ratio=Decimal("1.0"),
        max_wash_start_delay_bars=10,
        hold_close_tolerance_pct=Decimal("0.8"),
        min_wash_bars=4,
        max_wash_bars=40,
        min_wash_arc_lift_pct=Decimal("1.0"),
        max_wash_hold_gap_pct=Decimal("3.0"),
        max_wash_average_gap_pct=Decimal("8.0"),
        max_wash_peak_gap_pct=Decimal("15.0"),
        min_reclaim_close_pct=Decimal("0.0"),
        max_reclaim_close_pct=Decimal("20.0"),
        departure_close_pct=Decimal("10.0"),
        departure_volume_ratio=Decimal("4.0"),
        yellow_launch_close_pct=Decimal("6.0"),
        yellow_launch_volume_ratio=Decimal("4.0"),
        sort="quality",
    )
    summary_rows: list[list[str]] = []
    for sample in read_samples():
        start_ms = utc_ms(sample.start_date)
        end_ms = utc_ms(sample.end_date + dt.timedelta(days=1))
        candles = archive.load_daily(sample.symbol, "4h", start_ms, end_ms, context)
        matches = scan.find_matches(candles, sample.symbol, scan_args)
        if not matches:
            print(f"{sample.symbol}: no match")
            continue
        match = select_match(
            matches,
            sample.preferred_reclaim_bj,
            sample.preferred_yellow_start_bj,
            sample.preferred_wash_start_bj,
            sample.preferred_wash_end_bj,
        )
        svg = render(sample.symbol, candles, match)
        path = OUT_DIR / f"{sample.symbol.lower()}_4h_segment_arc_match.svg"
        path.write_text(svg, encoding="utf-8")
        index.append(f"<h2>{html.escape(sample.symbol)}</h2><img src='{path.name}' style='max-width:100%;border:1px solid #263244'>")
        summary_rows.append(
            [
                sample.symbol,
                f"{match.score:.4f}",
                scan.bj(match.yellow_start.open_time),
                scan.dstr(match.yellow_start.low),
                scan.bj(match.yellow_peak.open_time),
                scan.dstr(match.hold_line),
                scan.bj(match.blue_low.open_time),
                scan.dstr(match.blue_low.low),
                scan.bj(match.reclaim.open_time),
                scan.dstr(match.reclaim.close),
                scan.bj(match.wash_start.open_time),
                scan.dstr(match.wash_start.close),
                scan.bj(match.wash_end.open_time),
                scan.dstr(match.wash_min_close),
                scan.dstr(match.wash_peak),
            ]
        )
        print(path)
    index.append("</body></html>")
    (OUT_DIR / "index.html").write_text("\n".join(index), encoding="utf-8")
    with open(OUT_DIR / "summary.csv", "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "symbol",
                "score",
                "yellow_start_bj",
                "yellow_start_low",
                "yellow_peak_bj",
                "yellow_peak_high_hold",
                "blue_low_bj",
                "blue_low",
                "reclaim_bj",
                "reclaim_close",
                "wash_start_bj",
                "wash_start_close",
                "wash_end_bj",
                "wash_min_close",
                "wash_peak_close",
            ]
        )
        writer.writerows(summary_rows)
    print(OUT_DIR / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
