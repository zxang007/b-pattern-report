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
    expected_wash_start: dt.datetime
    expected_wash_end: dt.datetime
    window: dt.timedelta
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
        window_days = float(row.get("window_days") or "3")
        examples.append(
            Example(
                symbol=row["symbol"].strip().upper(),
                expected_wash_start=parse_bj(row["expected_wash_start_bj"]),
                expected_wash_end=parse_bj(row["expected_wash_end_bj"]),
                window=dt.timedelta(days=window_days),
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


def row_overlaps_example(row: dict[str, str], example: Example) -> bool:
    if row.get("symbol", "").strip().upper() != example.symbol:
        return False
    wash_start = row_time(row, "wash_start_bj")
    wash_end = row_time(row, "wash_end_bj")
    if wash_start is None or wash_end is None:
        return False
    start_floor = example.expected_wash_start - example.window
    end_ceiling = example.expected_wash_end + example.window
    return wash_start <= end_ceiling and wash_end >= start_floor


def compact_row(row: dict[str, str]) -> str:
    fields = ["symbol", *TIME_FIELDS]
    return " ".join(f"{field}={row.get(field, '')}" for field in fields)


def main() -> int:
    args = parse_args()
    examples = load_examples(args.examples)
    rows = load_scan_rows(args.csv_file)

    failures: list[Example] = []
    for example in examples:
        candidates = [row for row in rows if row_overlaps_example(row, example)]
        if candidates:
            print(f"verified {example.symbol}: {compact_row(candidates[0])}", flush=True)
            continue
        failures.append(example)

    if failures:
        print("missing verified examples:", flush=True)
        for example in failures:
            print(
                f"- {example.symbol} expected wash around "
                f"{example.expected_wash_start:%Y-%m-%d %H:%M} -> {example.expected_wash_end:%Y-%m-%d %H:%M} "
                f"window=+/-{example.window.days}d {example.notes}",
                flush=True,
            )
        return 1

    print(f"all_verified_examples_present={len(examples)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
