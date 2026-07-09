#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sort market report CSV by symbol and wash time.")
    parser.add_argument("csv_file", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with args.csv_file.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = reader.fieldnames

    if not fieldnames:
        raise SystemExit(f"{args.csv_file} has no CSV header")

    rows.sort(key=lambda row: (row.get("symbol", ""), row.get("wash_start_bj", ""), row.get("wash_end_bj", "")))

    with args.csv_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"final_scan_rows={len(rows)}")
    if not rows:
        raise SystemExit("No matches after API + archive fallback; refusing to deploy an empty report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
