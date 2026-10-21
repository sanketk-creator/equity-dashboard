"""
Indicators, EXACTLY as Appendix A of the methodology defines them.

The truncation test the backtest passed was against these precise
definitions; a live engine differing by one window length diverges for
reasons that look like noise. The two named divergence traps are honoured:

  1. Percentile with AVERAGE-RANK ties, inclusive at the boundary (>= 0.60
     passes) — not "top 40 names by count".
  2. Realised volatility = standard deviation of 126 LOG returns, ddof=1,
     annualised by sqrt(252).

Window handling: every function returns None when fewer bars exist than its
own window needs (pandas min_periods semantics) — a short history is never
computed on a short window; it fails the gate instead.
"""
from __future__ import annotations

import math
from typing import Sequence


def sma(closes: Sequence[float], window: int) -> float | None:
    """Simple moving average of the LAST `window` closes."""
    if len(closes) < window:
        return None
    tail = closes[-window:]
    return sum(tail) / window


def ret_over(closes: Sequence[float], shift: int) -> float | None:
    """close / close.shift(shift) - 1, evaluated at the last bar."""
    if len(closes) < shift + 1:
        return None
    past = closes[-(shift + 1)]
    if not past:
        return None
    return closes[-1] / past - 1.0


def realised_vol(closes: Sequence[float], window: int = 126) -> float | None:
    """Std of the last `window` LOG returns, ddof=1, annualised x sqrt(252)."""
    if len(closes) < window + 1:
        return None
    tail = closes[-(window + 1):]
    rets = []
    for a, b in zip(tail, tail[1:]):
        if a <= 0 or b <= 0:
            return None
        rets.append(math.log(b / a))
    n = len(rets)                      # == window
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(252.0)


def mom_12_1(closes: Sequence[float], skip: int = 21, window: int = 252) -> float | None:
    """close.shift(skip) / close.shift(window) - 1, at the last bar."""
    if len(closes) < window + 1:
        return None
    a = closes[-(window + 1)]
    b = closes[-(skip + 1)]
    if not a:
        return None
    return b / a - 1.0


def atr_wilder(highs: Sequence[float], lows: Sequence[float],
               closes: Sequence[float], window: int = 14) -> float | None:
    """
    ATR(14), Wilder smoothing: exponential with alpha = 1/window,
    adjust=False, first true range = high - low, minimum `window` bars.
    Matches pandas tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean().
    """
    n = len(closes)
    if n < window or not (len(highs) == len(lows) == n):
        return None
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    alpha = 1.0 / window
    val = trs[0]
    for tr in trs[1:]:
        val = alpha * tr + (1 - alpha) * val
    return val


def volume_ratio(volumes: Sequence[float], short: int = 5, long: int = 20) -> float | None:
    """Mean of last `short` volume_adj / mean of last `long`. None on any
    missing value in the window (unknown is not zero) or a zero denominator."""
    if len(volumes) < long:
        return None
    tail = volumes[-long:]
    if any(v is None for v in tail):
        return None
    long_avg = sum(tail) / long
    short_avg = sum(tail[-short:]) / short
    if long_avg <= 0:
        return None
    return short_avg / long_avg


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """
    Percentile of each value among ALL supplied values, with AVERAGE-RANK tie
    handling (pandas rank(pct=True)): tied values get the midpoint rank and
    pass or fail together. Returned as rank/N in (0, 1].
    """
    if not values:
        return {}
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0        # 1-based average rank
        for k in range(i, j + 1):
            out[items[k][0]] = avg_rank / n
        i = j + 1
    return out
