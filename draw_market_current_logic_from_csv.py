#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hashlib
import html
import os
import secrets
import ssl
import time
from decimal import Decimal
from pathlib import Path

import draw_four_h_segment_arc_matches_svg as chart
import four_h_segment_arc_scan as scan
import scan_four_h_segment_arc_market as market_scan


BJ = dt.timezone(dt.timedelta(hours=8))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按市场扫描 CSV 固定候选生成4小时结构图。")
    parser.add_argument("--csv-file", default="market_first30_2026_06_07_current_logic.csv")
    parser.add_argument("--out-dir", default="market_first30_current_logic_charts")
    parser.add_argument("--start-date", default="2026-06-01")
    parser.add_argument("--end-date", default="2026-07-06")
    parser.add_argument("--archive-granularity", choices=["api", "auto", "monthly", "daily"], default="auto")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--retry-delay", type=float, default=1.2)
    parser.add_argument("--password", default=os.environ.get("REPORT_PASSWORD", ""))
    parser.add_argument("--password-hash", default=os.environ.get("REPORT_PASSWORD_SHA256", ""))
    return parser.parse_args()


def ms_at(day: dt.date) -> int:
    return int(dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc).timestamp() * 1000)


def parse_bj_ms(value: str) -> int:
    parsed = dt.datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=BJ)
    return int(parsed.astimezone(dt.timezone.utc).timestamp() * 1000)


def candle_at(candles_by_time, value: str):
    open_time = parse_bj_ms(value)
    try:
        return candles_by_time[open_time]
    except KeyError as exc:
        raise ValueError(f"CSV时间没有对应K线: {value}") from exc


def row_to_match(row: dict[str, str], candles_by_time: dict[int, object]) -> scan.SegmentArcMatch:
    return scan.SegmentArcMatch(
        symbol=row["symbol"],
        yellow_start=candle_at(candles_by_time, row["yellow_start_bj"]),
        yellow_peak=candle_at(candles_by_time, row["yellow_peak_bj"]),
        yellow_end=candle_at(candles_by_time, row["yellow_end_bj"]),
        blue_start=candle_at(candles_by_time, row["blue_start_bj"]),
        blue_low=candle_at(candles_by_time, row["blue_low_bj"]),
        reclaim=candle_at(candles_by_time, row["reclaim_bj"]),
        wash_start=candle_at(candles_by_time, row["wash_start_bj"]),
        wash_end=candle_at(candles_by_time, row["wash_end_bj"]),
        hold_line=Decimal(row["yellow_hold_close"]),
        wash_min_close=Decimal(row["wash_min_close"]),
        wash_peak=Decimal(row["wash_peak_close"]),
        score=Decimal(row["score"]),
        blue_below_close_count=int(row.get("blue_below_close_count") or 0),
        breakout_volume_ratio=Decimal(row.get("breakout_volume_ratio") or "0"),
    )


def xor_stream(data: bytes, password: str, salt: bytes) -> bytes:
    key = password.encode("utf-8")
    output = bytearray()
    counter = 0
    while len(output) < len(data):
        output.extend(hashlib.sha256(key + salt + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(item ^ output[index] for index, item in enumerate(data))


def protected_html(inner_html: str, password: str) -> str:
    salt = secrets.token_bytes(16)
    cipher = xor_stream(inner_html.encode("utf-8"), password, salt)
    check = hashlib.sha256(password.encode("utf-8") + salt).hexdigest()
    salt_b64 = base64.b64encode(salt).decode("ascii")
    cipher_b64 = base64.b64encode(cipher).decode("ascii")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Binance Pattern Report</title>
  <style>
    body {{ margin: 0; min-height: 100vh; background: #111827; color: #f9fafb; font-family: Arial, sans-serif; }}
    .gate {{ min-height: 100vh; display: grid; place-items: center; padding: 24px; box-sizing: border-box; }}
    .panel {{ width: min(420px, 100%); border: 1px solid #334155; background: #0f172a; padding: 24px; }}
    h1 {{ margin: 0 0 18px; font-size: 22px; }}
    input {{ width: 100%; box-sizing: border-box; background: #020617; color: #f9fafb; border: 1px solid #475569; padding: 12px; font-size: 16px; }}
    button {{ width: 100%; margin-top: 12px; padding: 12px; font-size: 16px; background: #38bdf8; border: 0; color: #082f49; font-weight: 700; }}
    .error {{ margin-top: 12px; min-height: 20px; color: #fb7185; }}
  </style>
</head>
<body>
  <div class="gate">
    <form class="panel" id="gate-form">
      <h1>Binance Pattern Report</h1>
      <input id="password" type="password" autocomplete="current-password" placeholder="输入密码">
      <button type="submit">打开</button>
      <div class="error" id="error"></div>
    </form>
  </div>
  <script>
    const saltB64 = "{salt_b64}";
    const cipherB64 = "{cipher_b64}";
    const checkHex = "{check}";

    function fromB64(value) {{
      const binary = atob(value);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
      return bytes;
    }}

    function toHex(bytes) {{
      return Array.from(bytes).map((item) => item.toString(16).padStart(2, "0")).join("");
    }}

    function concatBytes(parts) {{
      const size = parts.reduce((total, item) => total + item.length, 0);
      const out = new Uint8Array(size);
      let offset = 0;
      for (const item of parts) {{
        out.set(item, offset);
        offset += item.length;
      }}
      return out;
    }}

    async function sha256(bytes) {{
      return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
    }}

    async function decrypt(password) {{
      const encoder = new TextEncoder();
      const key = encoder.encode(password);
      const salt = fromB64(saltB64);
      const check = await sha256(concatBytes([key, salt]));
      if (toHex(check) !== checkHex) throw new Error("密码错误");
      const cipher = fromB64(cipherB64);
      const stream = new Uint8Array(cipher.length);
      let offset = 0;
      let counter = 0;
      while (offset < cipher.length) {{
        const counterBytes = new Uint8Array(4);
        new DataView(counterBytes.buffer).setUint32(0, counter);
        const block = await sha256(concatBytes([key, salt, counterBytes]));
        stream.set(block.slice(0, Math.min(block.length, cipher.length - offset)), offset);
        offset += block.length;
        counter += 1;
      }}
      const plain = new Uint8Array(cipher.length);
      for (let i = 0; i < cipher.length; i += 1) plain[i] = cipher[i] ^ stream[i];
      return new TextDecoder().decode(plain);
    }}

    document.getElementById("gate-form").addEventListener("submit", async (event) => {{
      event.preventDefault();
      const error = document.getElementById("error");
      error.textContent = "";
      try {{
        document.body.innerHTML = await decrypt(document.getElementById("password").value);
      }} catch (exc) {{
        error.textContent = "密码错误";
      }}
    }});
  </script>
</body>
</html>"""


def gated_html(inner_html: str, password_hash: str) -> str:
    escaped = inner_html.replace("</script", "<\\/script")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Binance Pattern Report</title>
  <style>
    body {{ margin: 0; min-height: 100vh; background: #111827; color: #f9fafb; font-family: Arial, sans-serif; }}
    .gate {{ min-height: 100vh; display: grid; place-items: center; padding: 24px; box-sizing: border-box; }}
    .panel {{ width: min(420px, 100%); border: 1px solid #334155; background: #0f172a; padding: 24px; }}
    h1 {{ margin: 0 0 18px; font-size: 22px; }}
    input {{ width: 100%; box-sizing: border-box; background: #020617; color: #f9fafb; border: 1px solid #475569; padding: 12px; font-size: 16px; }}
    button {{ width: 100%; margin-top: 12px; padding: 12px; font-size: 16px; background: #38bdf8; border: 0; color: #082f49; font-weight: 700; }}
    .error {{ margin-top: 12px; min-height: 20px; color: #fb7185; }}
  </style>
</head>
<body>
  <div class="gate">
    <form class="panel" id="gate-form">
      <h1>Binance Pattern Report</h1>
      <input id="password" type="password" autocomplete="current-password" placeholder="输入密码">
      <button type="submit">打开</button>
      <div class="error" id="error"></div>
    </form>
  </div>
  <template id="report-content">{escaped}</template>
  <script>
    const expectedHash = "{html.escape(password_hash.strip().lower())}";

    function toHex(bytes) {{
      return Array.from(bytes).map((item) => item.toString(16).padStart(2, "0")).join("");
    }}

    async function sha256(text) {{
      const bytes = new TextEncoder().encode(text);
      return toHex(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)));
    }}

    document.getElementById("gate-form").addEventListener("submit", async (event) => {{
      event.preventDefault();
      const error = document.getElementById("error");
      error.textContent = "";
      const actualHash = await sha256(document.getElementById("password").value);
      if (actualHash !== expectedHash) {{
        error.textContent = "密码错误";
        return;
      }}
      document.body.innerHTML = document.getElementById("report-content").innerHTML;
    }});
  </script>
</body>
</html>"""


def load_candles_with_retry(
    symbol: str,
    start_ms: int,
    end_ms: int,
    start: dt.date,
    end: dt.date,
    context: ssl.SSLContext,
    args: argparse.Namespace,
):
    last_error: Exception | None = None
    for attempt in range(1, max(1, args.retries) + 1):
        try:
            return market_scan.load_archive_candles(
                symbol,
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
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < max(1, args.retries):
                time.sleep(max(0.0, args.retry_delay) * attempt)
    if args.archive_granularity != "api":
        return market_scan.load_archive_candles(
            symbol,
            "4h",
            start_ms,
            end_ms,
            start,
            end,
            context,
            "api",
            args.insecure,
            args.timeout,
            args.retries,
            args.retry_delay,
        )
    raise RuntimeError(f"{symbol} K线读取失败: {last_error}") from last_error


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(exist_ok=True)
    with open(args.csv_file, newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))

    start = dt.date.fromisoformat(args.start_date)
    end = dt.date.fromisoformat(args.end_date) + dt.timedelta(days=1)
    start_ms = ms_at(start)
    end_ms = ms_at(end)
    context = ssl._create_unverified_context()
    candle_cache = {}
    index_parts = ["<body style='background:#111827;color:#f9fafb;font-family:Arial'>"]

    for idx, row in enumerate(rows, start=1):
        symbol = row["symbol"]
        if symbol not in candle_cache:
            candle_cache[symbol] = load_candles_with_retry(symbol, start_ms, end_ms, start, end, context, args)
        candles = candle_cache[symbol]
        candles_by_time = {item.open_time: item for item in candles}
        match = row_to_match(row, candles_by_time)
        svg = chart.render(symbol, candles, match)
        wash_stamp = row["wash_start_bj"].replace(" ", "_").replace(":", "")
        path = out_dir / f"{idx:02d}_{symbol.lower()}_{wash_stamp}.svg"
        path.write_text(svg, encoding="utf-8")
        index_parts.append(
            f"<h2>{idx}. {html.escape(symbol)} wash={html.escape(row['wash_start_bj'])}</h2>"
            f"<p>yellow_bars={html.escape(row.get('yellow_bars', ''))} "
            f"blue_low={html.escape(row['blue_low_bj'])} wash_end={html.escape(row['wash_end_bj'])}</p>"
            f"<img src='{html.escape(path.name)}' style='max-width:100%;border:1px solid #263244'>"
        )
        print(path)

    index_parts.append("</body>")
    body = "\n".join(index_parts)
    if args.password_hash:
        output = gated_html(body, args.password_hash)
    elif args.password:
        output = protected_html(body, args.password)
    else:
        output = f"<html>{body}</html>"
    (out_dir / "index.html").write_text(output, encoding="utf-8")
    print(out_dir / "index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
