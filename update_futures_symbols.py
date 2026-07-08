#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_BASE_URLS = (
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
    "https://fapi4.binance.com",
    "https://www.binance.com",
)


class SymbolUpdateError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update the futures symbol list used by the market report.")
    parser.add_argument("--symbols-file", default="futures_symbols_2026.txt")
    parser.add_argument("--quote", default="USDT")
    parser.add_argument("--min-symbol-count", type=int, default=400)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument("--base-url", action="append", help="Override or add an exchangeInfo base URL.")
    parser.add_argument("--insecure", action="store_true", help="Disable HTTPS certificate verification.")
    return parser.parse_args()


def request_json(base_url: str, args: argparse.Namespace) -> dict:
    url = f"{base_url.rstrip('/')}/fapi/v1/exchangeInfo"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "market-pattern-symbol-updater/1.0",
        },
    )
    context = ssl._create_unverified_context() if args.insecure else None
    last_error = None
    for attempt in range(1, args.retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=args.timeout, context=context) as response:
                body = response.read().decode("utf-8")
            data = json.loads(body)
            if not isinstance(data, dict):
                raise SymbolUpdateError(f"{url}: response is not an object")
            return data
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")
            last_error = f"HTTP {exc.code}: {message[:500]}"
            if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                break
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError, json.JSONDecodeError) as exc:
            last_error = str(exc)

        if attempt < args.retries:
            time.sleep(args.retry_delay * attempt)

    raise SymbolUpdateError(f"{url}: {last_error}")


def fetch_symbols(args: argparse.Namespace) -> list[str]:
    base_urls = tuple(args.base_url) if args.base_url else DEFAULT_BASE_URLS
    errors: list[str] = []
    for base_url in base_urls:
        try:
            data = request_json(base_url, args)
        except SymbolUpdateError as exc:
            errors.append(str(exc))
            continue

        symbols = []
        for raw in data.get("symbols", []):
            if raw.get("status") != "TRADING":
                continue
            if raw.get("contractType") != "PERPETUAL":
                continue
            if raw.get("quoteAsset") != args.quote:
                continue
            symbol = str(raw.get("symbol", "")).strip().upper()
            if not symbol or not symbol.endswith(args.quote):
                continue
            symbols.append(symbol)

        symbols = sorted(set(symbols))
        if symbols:
            print(f"fetched {len(symbols)} symbols from {base_url}")
            return symbols
        errors.append(f"{base_url}: exchangeInfo returned zero usable symbols")

    raise SymbolUpdateError("failed to fetch symbols:\n" + "\n".join(errors))


def main() -> int:
    args = parse_args()
    symbols_path = Path(args.symbols_file)
    symbols = fetch_symbols(args)

    if not symbols:
        raise SystemExit("Refusing to write: fetched symbol list is empty.")
    if len(symbols) < args.min_symbol_count:
        raise SystemExit(
            f"Refusing to write: fetched {len(symbols)} symbols, below minimum {args.min_symbol_count}."
        )

    new_text = "\n".join(symbols) + "\n"
    if not new_text.strip():
        raise SystemExit("Refusing to write: generated symbol file is empty.")

    old_text = symbols_path.read_text(encoding="utf-8") if symbols_path.exists() else ""
    if old_text == new_text:
        print(f"No changes: {symbols_path} already contains {len(symbols)} symbols.")
        return 0

    symbols_path.write_text(new_text, encoding="utf-8")
    print(f"Updated {symbols_path}: {len(symbols)} symbols.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SymbolUpdateError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
