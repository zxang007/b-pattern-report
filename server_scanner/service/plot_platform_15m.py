"""绘制自动识别的平台、放量启动与全部主要回踩证据。"""
from decimal import Decimal
import datetime as dt
import html


BJ = dt.timezone(dt.timedelta(hours=8))


def bj(value):
    return dt.datetime.fromtimestamp(value / 1000, BJ).strftime('%Y-%m-%d %H:%M')


def render(symbol, candles, door):
    start, final = door.platform, max(door.retests)
    end = min(len(candles), final + 2)
    values = candles[start:end]
    low, high = min(c.low for c in values), max(c.high for c in values)
    pad = (high - low) * Decimal('.08')
    low, high = low - pad, high + pad
    width, height, x0, x1 = 1600, 960, 90, 1485
    top, bottom, vt, vb = 100, 650, 735, 845
    x = lambda i: x0 + (i - start + .5) * (x1 - x0) / len(values)
    y = lambda p: top + float((high - p) / (high - low)) * (bottom - top)
    bar_w = max(.25, min(13, (x1 - x0) / len(values) * .65))
    maxq = max(c.quote_volume for c in values)
    support = candles[door.support].low
    platform_top = max(c.close for c in candles[start:door.launch])
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="960" viewBox="0 0 1600 960">',
        '<rect width="100%" height="100%" fill="#111827"/>',
        f'<text x="90" y="38" fill="#f9fafb" font-family="Arial" font-size="26">{html.escape(symbol)} | 15m platform and retests | {len(door.episodes)} retest episodes</text>',
        '<text x="90" y="70" fill="#cbd5e1" font-family="Arial" font-size="16">Grey: platform | Green: volume rise and first return | Yellow: retest lows | Beijing time</text>',
        f'<rect x="{x0}" y="{top}" width="{x1-x0}" height="{bottom-top}" fill="#172033"/>']
    ceiling = max(c.high for c in candles[start:door.launch])
    cell = (x1 - x0) / len(values)
    parts.append(f'<rect x="{x(start)-cell/2:.2f}" y="{y(ceiling):.2f}" width="{(door.launch-start)*cell:.2f}" height="{y(support)-y(ceiling):.2f}" fill="#cbd5e1" fill-opacity=".14" stroke="#94a3b8"/>')
    for k in range(6):
        price = high - (high - low) * Decimal(k) / 5
        parts.append(f'<line x1="{x0}" x2="{x1}" y1="{y(price):.2f}" y2="{y(price):.2f}" stroke="#334155"/>')
        parts.append(f'<text x="1495" y="{y(price)+5:.2f}" fill="#cbd5e1" font-family="Arial" font-size="14">{price:.7f}</text>')
    for i in range(start, end):
        c = candles[i]
        color = '#10b981' if c.close >= c.open else '#ef4444'
        parts.append(f'<line x1="{x(i):.2f}" x2="{x(i):.2f}" y1="{y(c.high):.2f}" y2="{y(c.low):.2f}" stroke="{color}"/>')
        parts.append(f'<rect x="{x(i)-bar_w/2:.2f}" y="{min(y(c.open),y(c.close)):.2f}" width="{bar_w:.2f}" height="{max(.6,abs(y(c.close)-y(c.open))):.2f}" fill="{color}"/>')
        volume_h = float(c.quote_volume / maxq) * (vb - vt) if maxq else 0
        parts.append(f'<rect x="{x(i)-bar_w/2:.2f}" y="{vb-volume_h:.2f}" width="{bar_w:.2f}" height="{volume_h:.2f}" fill="{color}"/>')
    first = min(door.episodes[0], key=lambda i: candles[i].low)
    last = min(door.episodes[-1], key=lambda i: candles[i].low)
    roof = candles[door.peak].high
    parts.append(f'<polyline points="{x(door.launch):.2f},{y(support):.2f} {x(door.launch):.2f},{y(roof):.2f} {x(first):.2f},{y(roof):.2f} {x(first):.2f},{y(candles[first].low):.2f}" fill="none" stroke="#34d399" stroke-width="3"/>')
    for level, color in [(support, '#94a3b8'), (platform_top, '#64748b'), (candles[first].low, '#facc15')]:
        parts.append(f'<line x1="{x(start):.2f}" x2="{x(end-1):.2f}" y1="{y(level):.2f}" y2="{y(level):.2f}" stroke="{color}" stroke-dasharray="7 5"/>')
    for episode in door.episodes:
        for i in episode:
            parts.append(f'<circle cx="{x(i):.2f}" cy="{y(candles[i].low):.2f}" r="4" fill="#facc15"/>')
    for number, i in enumerate([door.launch, first, last], 1):
        yy = min(bottom - 18, y(candles[i].low) + 24)
        parts.append(f'<circle cx="{x(i):.2f}" cy="{yy:.2f}" r="14" fill="#f9fafb" stroke="#34d399" stroke-width="2"/>')
        parts.append(f'<text x="{x(i):.2f}" y="{yy+5:.2f}" text-anchor="middle" font-family="Arial" font-size="16" fill="#111827">{number}</text>')
    # 标签仅为绘图排版抽稀，所有回踩仍画在图上并保存在证据表。
    previous, row = None, 0
    for i in sorted({start, door.launch, first, last, end - 1}):
        row = 1 - row if previous is not None and x(i) - previous < 155 else 0
        parts.append(f'<text x="{x(i):.2f}" y="{880+row*24}" text-anchor="middle" fill="#cbd5e1" font-family="Arial" font-size="14">{bj(candles[i].open_time)[5:]}</text>')
        previous = x(i)
    parts.append('<text x="90" y="710" fill="#cbd5e1" font-family="Arial" font-size="14">Quote turnover (USDT), zero baseline</text>')
    parts.append('<text x="90" y="940" fill="#cbd5e1" font-family="Arial" font-size="14">1: launch | 2: first retest | 3: latest major retest | all wick/close witnesses retained as yellow dots</text></svg>')
    return '\n'.join(parts)
