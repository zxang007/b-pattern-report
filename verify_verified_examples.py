#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path


TIME_FIELDS = (
    "yellow_start_bj",
    "yellow_end_bj",
    "blue_start_bj",
    "blue_low_bj",
    "reclaim_bj",
    "wash_start_bj",
    "wash_end_bj",
)


@dataclass(frozen=True)
class Example:
    symbol: str
    expected_yellow_start: dt.datetime
    expected_yellow_end: dt.datetime
    expected_blue_start: dt.datetime
    expected_blue_end: dt.datetime
    expected_wash_start: dt.datetime
    expected_wash_end: dt.datetime
    max_segment_gap: dt.timedelta
    notes: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that a scan CSV still covers the human-confirmed examples. "
            "This script is intentionally offline; it never fetches market data."
        )
    )
    parser.add_argument("--examples", type=Path, default=Path("verified_examples.csv"))
    parser.add_argument("--csv-file", type=Path, required=True)
    return parser.parse_args()


def parse_bj(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%d %H:%M")


def load_examples(path: Path) -> list[Example]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    examples: list[Example] = []
    for row in rows:
        max_gap_hours = float(row.get("max_segment_gap_hours") or "24")
        examples.append(
            Example(
                symbol=row["symbol"].strip().upper(),
                expected_yellow_start=parse_bj(row["expected_yellow_start_bj"]),
                expected_yellow_end=parse_bj(row["expected_yellow_end_bj"]),
                expected_blue_start=parse_bj(row["expected_blue_start_bj"]),
                expected_blue_end=parse_bj(row["expected_blue_end_bj"]),
                expected_wash_start=parse_bj(row["expected_wash_start_bj"]),
                expected_wash_end=parse_bj(row["expected_wash_end_bj"]),
                max_segment_gap=dt.timedelta(hours=max_gap_hours),
                notes=row.get("notes", ""),
            )
        )
    return examples


def load_scan_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def row_time(row: dict[str, str], field: str) -> dt.datetime | None:
    value = row.get(field, "").strip()
    if not value:
        return None
    try:
        return parse_bj(value)
    except ValueError:
        return None


def boundary_gap(
    actual_start: dt.datetime,
    actual_end: dt.datetime,
    expected_start: dt.datetime,
    expected_end: dt.datetime,
) -> dt.timedelta:
    return max(abs(actual_start - expected_start), abs(actual_end - expected_end))


def row_segment_gaps(row: dict[str, str], example: Example) -> dict[str, dt.timedelta] | None:
    if row.get("symbol", "").strip().upper() != example.symbol:
        return None
    yellow_start = row_time(row, "yellow_start_bj")
    yellow_end = row_time(row, "yellow_end_bj")
    blue_start = row_time(row, "blue_start_bj")
    blue_end = row_time(row, "reclaim_bj")
    wash_start = row_time(row, "wash_start_bj")
    wash_end = row_time(row, "wash_end_bj")
    if None in (yellow_start, yellow_end, blue_start, blue_end, wash_start, wash_end):
        return None
    return {
        "yellow": boundary_gap(yellow_start, yellow_end, example.expected_yellow_start, example.expected_yellow_end),
        "blue": boundary_gap(blue_start, blue_end, example.expected_blue_start, example.expected_blue_end),
        "wash": boundary_gap(wash_start, wash_end, example.expected_wash_start, example.expected_wash_end),
    }


def gap_hours(value: dt.timedelta) -> float:
    return value.total_seconds() / 3600


def compact_row(row: dict[str, str]) -> str:
    fields = ["symbol", *TIME_FIELDS]
    return " ".join(f"{field}={row.get(field, '')}" for field in fields)


def main() -> int:
    args = parse_args()
    examples = load_examples(args.examples)
    rows = load_scan_rows(args.csv_file)

    failures: list[Example] = []
    for example in examples:
        scored: list[tuple[dt.timedelta, dt.timedelta, dict[str, str], dict[str, dt.timedelta]]] = []
        for row in rows:
            gaps = row_segment_gaps(row, example)
            if gaps is None:
                continue
            max_gap = max(gaps.values())
            total_gap = sum(gaps.values(), dt.timedelta(0))
            scored.append((max_gap, total_gap, row, gaps))
        scored.sort(key=lambda item: (item[0], item[1]))
        if scored and scored[0][0] <= example.max_segment_gap:
            _, _, row, gaps = scored[0]
            print(
                f"verified {example.symbol}: "
                f"yellow_gap={gap_hours(gaps['yellow']):.1f}h "
                f"blue_gap={gap_hours(gaps['blue']):.1f}h "
                f"wash_gap={gap_hours(gaps['wash']):.1f}h "
                f"{compact_row(row)}",
                flush=True,
            )
            continue
        if scored:
            max_gap, _, row, gaps = scored[0]
            print(
                f"nearest {example.symbol}: max_gap={gap_hours(max_gap):.1f}h "
                f"yellow_gap={gap_hours(gaps['yellow']):.1f}h "
                f"blue_gap={gap_hours(gaps['blue']):.1f}h "
                f"wash_gap={gap_hours(gaps['wash']):.1f}h "
                f"{compact_row(row)}",
                flush=True,
            )
        failures.append(example)

    if failures:
        print("missing verified examples:", flush=True)
        for example in failures:
            print(
                f"- {example.symbol} expected wash around "
                f"{example.expected_wash_start:%Y-%m-%d %H:%M} -> {example.expected_wash_end:%Y-%m-%d %H:%M} "
                f"max_segment_gap={gap_hours(example.max_segment_gap):.1f}h {example.notes}",
                flush=True,
            )
        return 1

    print(f"all_verified_examples_present={len(examples)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
