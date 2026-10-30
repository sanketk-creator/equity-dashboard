"""
Indicators for Strategies 2 and 3 — spec §4.6, §5.2, §6.

Every window uses min_periods = its own length: a short history returns None
(NaN), which fails every screen. Indicators are computed on each
instrument's OWN rows (bar shifts, never calendar days). All functions take
oldest-first lists and return the value AS OF THE LAST ELEMENT.

Differences from Strategy 1 that the spec insists on (§4.6, §6.2):
  * realised volatility uses 252 log returns here (Strategy 1: 126);
  * ADV uses RAW volume (Strategy 1's volume ratio uses volume_adj).
"""
from __future__ import annotations

import math


def sma(closes: list[float], n: int = 200) -> float | None:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def adv(closes: list[float], volumes: list[float | None], n: int = 126) -> float | None:
    """mean(close x volume, n) on RAW volume, exactly as the backtests compute
    it ((close * volume).rolling(126, min_periods=126).mean()): a MISSING
    volume makes the window NaN; a zero-volume day counts as zero turnover.
    (Spec §6.5 says zero/negative should invalidate the window; the code —
    the stated authority — does not. Recorded as a known difference; the
    hub logs any zero/negative volume it sees.)"""
    if len(closes) < n or len(volumes) < n:
        return None
    tot = 0.0
    for c, v in zip(closes[-n:], volumes[-n:]):
        if v is None or c is None:
            return None
        tot += c * v
    return tot / n


def realised_vol(closes: list[float], n_returns: int) -> float | None:
    """stdev of the last n log returns, ddof=1, x sqrt(252). Needs n+1 bars."""
    if len(closes) < n_returns + 1:
        return None
    w = closes[-(n_returns + 1):]
    r = [math.log(w[i] / w[i - 1]) for i in range(1, len(w))]
    m = sum(r) / len(r)
    var = sum((x - m) ** 2 for x in r) / (len(r) - 1)
    return math.sqrt(var) * math.sqrt(252)


def mom_12_1(closes: list[float]) -> float | None:
    """close[t-21] / close[t-252] - 1. Needs 253 bars."""
    if len(closes) < 253:
        return None
    return closes[-22] / closes[-253] - 1


def ret_over(closes: list[float], n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    return closes[-1] / closes[-1 - n] - 1


def atr_wilder(highs: list[float], lows: list[float], closes: list[float],
               n: int = 14) -> float | None:
    """Wilder ATR: ewm(alpha=1/n, adjust=False) of TR, first TR = H - L,
    defined from the n-th bar (min_periods = n)."""
    if len(closes) < n:
        return None
    a = 1.0 / n
    atr = None
    for i in range(len(closes)):
        if i == 0:
            tr = highs[0] - lows[0]
        else:
            pc = closes[i - 1]
            tr = max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc))
        atr = tr if atr is None else atr + a * (tr - atr)
    return atr


def atr_series_last(bars: list[dict], n: int = 14) -> float | None:
    return atr_wilder([b["h"] for b in bars], [b["l"] for b in bars],
                      [b["c"] for b in bars], n)


def zscores_population(values: dict[str, float]) -> dict[str, float]:
    """(x - mean) / POPULATION stdev (ddof=0), across the given names."""
    xs = list(values.values())
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))
    if sd == 0:
        return {k: 0.0 for k in values}
    return {k: (v - m) / sd for k, v in values.items()}
