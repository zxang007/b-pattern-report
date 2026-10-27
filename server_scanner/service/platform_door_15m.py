#!/usr/bin/env python3
"""平台、放量突破及多次回踩的通用十五分钟结构，不按币名或日期特判。"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass


class Extremes:
    """区间极值索引；索引层级只用于计算加速。"""
    def __init__(self, values, minimum=False):
        self.values, self.minimum = values, minimum
        self.levels = [list(range(len(values)))]
        span = 1
        while span * 2 <= len(values):
            old = self.levels[-1]
            self.levels.append([self.choose(old[i], old[i + span]) for i in range(len(values) - span * 2 + 1)])
            span *= 2

    def choose(self, a, b):
        va, vb = self.values[a], self.values[b]
        return a if va == vb or ((va < vb) == self.minimum) else b

    def at(self, left, right):
        assert left < right
        k = (right - left).bit_length() - 1
        return self.choose(self.levels[k][left], self.levels[k][right - (1 << k)])

    def value(self, left, right):
        return self.values[self.at(left, right)]


def previous_ge(values):
    stack, result = [], []
    for i, value in enumerate(values):
        while stack and values[stack[-1]] < value:
            stack.pop()
        result.append(stack[-1] if stack else -1)
        stack.append(i)
    return result


@dataclass(frozen=True)
class PlatformDoor:
    platform: int
    support: int
    launch: int
    peak: int
    retests: tuple[int, ...]
    episodes: tuple[tuple[int, ...], ...]
    invalidated_at: int


class PlatformSearch:
    def __init__(self, candles):
        self.c = candles
        self.times = [c.open_time for c in candles]
        self.low = Extremes([c.low for c in candles], True)
        self.high = Extremes([c.high for c in candles])
        self.close = Extremes([c.close for c in candles])
        self.amount = Extremes([c.quote_volume for c in candles])
        self.prev_close = previous_ge([c.close for c in candles])
        self.prev_amount = previous_ge([c.quote_volume for c in candles])
        self.prev_lower = previous_ge([-c.low for c in candles])
        self.up, self.down = [0], [0]
        self.last_up, self.last_down = [-1], [-1]
        for i in range(1, len(candles)):
            self.up.append(self.up[-1] + (candles[i].close > candles[i - 1].close))
            self.down.append(self.down[-1] + (candles[i].close < candles[i - 1].close))
            self.last_up.append(i if candles[i].close > candles[i - 1].close else self.last_up[-1])
            self.last_down.append(i if candles[i].close < candles[i - 1].close else self.last_down[-1])
        # 同时识别影线低点和收盘转折，不把 VVV 的影线节点漏掉。
        self.pivots = [i for i in range(1, len(candles) - 1)
            if (candles[i].low <= candles[i - 1].low and candles[i].low < candles[i + 1].low)
            or (candles[i].close <= candles[i - 1].close and candles[i].close < candles[i + 1].close)]

    def platforms(self, launch, context=0):
        c = self.c
        boundary = max(context, self.prev_close[launch] + 1, self.prev_amount[launch] + 1)
        turning = min(self.last_up[launch - 1], self.last_down[launch - 1]) - 1
        probe, seen = launch - 1, set()
        while probe >= boundary:
            left = min(probe, turning)
            if left < boundary:
                break
            floor = self.low.at(left, launch)
            if floor not in seen:
                seen.add(floor)
                yield left, floor
            previous = self.prev_lower[floor]
            # 同价支撑不重复展开，继续寻找更早的独立低点。
            while previous >= boundary and c[previous].low >= c[floor].low:
                previous = self.prev_lower[previous]
            probe = previous

    def first_break(self, start, floor):
        if start >= len(self.c) or self.low.value(start, len(self.c)) >= floor:
            return len(self.c)
        left, right = start, len(self.c) - 1
        while left < right:
            middle = (left + right) // 2
            if self.low.value(start, middle + 1) < floor:
                right = middle
            else:
                left = middle + 1
        return left

    def describe_all(self, launch, context=0):
        c = self.c
        if c[launch].close <= c[launch].open:
            return []
        found = []
        seen_geometry = set()
        for base, floor in self.platforms(launch, context):
            support, ceiling = c[floor].low, self.close.value(base, launch)
            stop = self.first_break(launch, support)
            geometry = (ceiling, stop)
            if geometry in seen_geometry:
                continue
            # 相同平台上沿和支撑失效时刻产生同一回踩序列，保留更接近现价的平台。
            seen_geometry.add(geometry)
            episodes = []
            first = bisect_left(self.pivots, launch + 1)
            for i in self.pivots[first:]:
                if i >= stop or i + 1 >= stop:
                    break
                if c[i].low > ceiling or c[i].close >= c[launch].close:
                    continue
                if not episodes:
                    episodes.append([i])
                    continue
                previous = episodes[-1][-1]
                separate = i > previous + 1 and self.close.value(previous + 1, i) > ceiling
                if not separate:
                    episodes[-1].append(i)
                    continue
                # 后续主要回踩须回到第一次下杀影线触及的价格带。
                initial = c[episodes[0][0]]
                if c[i].low <= min(initial.open, initial.close):
                    episodes.append([i])
            if len(episodes) < 2:
                continue
            # 同一回踩保留影线和收盘两个低点，避免强行选成同一种单根定义。
            representatives = [tuple(sorted({min(episode, key=lambda i: c[i].low),
                                              min(episode, key=lambda i: c[i].close)}))
                               for episode in episodes]
            returns = tuple(i for episode in representatives for i in episode)
            peak = self.high.at(launch, returns[0])
            found.append(PlatformDoor(base, floor, launch, peak, returns,
                tuple(representatives), stop))
        return found

    def find(self, after_ms, context_ms=None):
        after = bisect_left(self.times, after_ms)
        context = bisect_left(self.times, context_ms if context_ms is not None else after_ms)
        return [door for i in range(max(1, after), len(self.c) - 1)
                for door in self.describe_all(i, context)]
