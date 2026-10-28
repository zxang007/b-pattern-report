"""用后续收盘检查末次回踩低点是否失效，不改变历史形态识别。"""
from platform_door_15m import Extremes
from plot_platform_15m import bj


def retest_validity(search, door):
    candles = search.c
    episode = door.episodes[-1]
    last = min(episode, key=lambda i: candles[i].low)
    floor = candles[last].low
    # 只比较收盘，影线下探保留；收盘曾跌破后再收回也不恢复原信号。
    if not hasattr(search, 'closing_low'):
        search.closing_low = Extremes([bar.close for bar in candles], minimum=True)
    start = last + 1
    broken = len(candles)
    if start < len(candles) and search.closing_low.value(start, len(candles)) < floor:
        left, right = start, len(candles) - 1
        while left < right:
            middle = (left + right) // 2
            if search.closing_low.value(start, middle + 1) < floor:
                right = middle
            else:
                left = middle + 1
        broken = left
    later_low = search.low.value(last + 1, len(candles)) if last + 1 < len(candles) else None
    return {
        'last_retest_low': str(floor),
        'post_retest_low': str(later_low) if later_low is not None else '',
        'post_retest_min_close': str(search.closing_low.value(start, len(candles))) if start < len(candles) else '',
        'last_retest_break_bj': bj(candles[broken].open_time) if broken < len(candles) else '',
        'price_valid': broken == len(candles),
        'price_checked_through_bj': bj(candles[-1].open_time + 900000),
    }
