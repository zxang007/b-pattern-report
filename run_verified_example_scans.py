#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the generic market scanner over each verified example's own date range "
            "and merge the generated CSV files. This script does not contain any "
            "symbol-specific pattern logic; verified_examples.csv is only a backtest target list."
        )
    )
    parser.add_argument("--examples", type=Path, default=Path("verified_examples.csv"))
    parser.add_argument("--scanner", type=Path, default=Path("scan_four_h_segment_arc_market.py"))
    parser.add_argument("--out", type=Path, default=Path("regression/verified_examples_scan.csv"))
    parser.add_argument("--work-dir", type=Path, default=Path("regression/by_example"))
    return parser.parse_args()


def load_examples(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_scan_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        return fieldnames, list(reader)


def write_merged(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    examples = load_examples(args.examples)
    args.work_dir.mkdir(parents=True, exist_ok=True)

    merged_fieldnames: list[str] = []
    merged_rows: list[dict[str, str]] = []
    for index, example in enumerate(examples, start=1):
        symbol = example["symbol"].strip().upper()
        start_date = example["scan_start_date"].strip()
        end_date = example["scan_end_date"].strip()
        if not symbol or not start_date or not end_date:
            raise ValueError(f"bad example row #{index}: {example}")

        out_file = args.work_dir / f"{index:02d}_{symbol.lower()}_scan.csv"
        failed_file = args.work_dir / f"{index:02d}_{symbol.lower()}_failed.txt"
        command = [
            sys.executable,
            str(args.scanner),
            "--symbols",
            symbol,
            "--start-date",
            start_date,
            "--end-date",
            end_date,
            "--csv-file",
            str(out_file),
            "--failed-symbols-file",
            str(failed_file),
            "--archive-granularity",
            "auto",
            "--workers",
            "1",
            "--progress-every",
            "1",
            "--archive-sleep",
            "0",
            "--limit",
            "200",
            "--per-symbol-limit",
            "200",
            "--timeout",
            "20",
            "--retries",
            "2",
            "--failed-retry-passes",
            "1",
            "--insecure",
        ]
        print(f"scan_example {symbol} {start_date}..{end_date}", flush=True)
        subprocess.run(command, check=True)

        fieldnames, rows = read_scan_csv(out_file)
        if not fieldnames:
            raise RuntimeError(f"scanner did not write CSV headers: {out_file}")
        if not merged_fieldnames:
            merged_fieldnames = fieldnames
        elif merged_fieldnames != fieldnames:
            raise RuntimeError(f"scanner CSV header changed in {out_file}")
        merged_rows.extend(rows)
        print(f"scan_example_rows {symbol} rows={len(rows)}", flush=True)

    if not merged_fieldnames:
        raise RuntimeError("no verified examples to scan")
    write_merged(args.out, merged_fieldnames, merged_rows)
    print(f"wrote {args.out} rows={len(merged_rows)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
