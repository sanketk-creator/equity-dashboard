"""
Strategy 2 — Momentum Barbell 20 (spec §4). Pure decision logic.

Two sleeves, 20 seats of 5.00% each at 40:60:
  AGG  8 seats, 40%, weekly rank check (last session of the ISO week),
       sort by Mom (12-1) highest first, sold when rank > 20
  DEF 12 seats, 60%, monthly rank check (last session of the month),
       sort by Vol (252 log returns) lowest first, sold when rank > 30
Eligibility gates ENTRIES only (liquidity, close > SMA200, history, not
excluded). A held stock is never sold for failing it; it is ranked against
the pool even when ineligible. No initial stop; a 3xATR trailing stop arms
at entry + 1xATR_entry and is checked on every daily close against the
level set on the PREVIOUS close (stop wins over rank on the same close;
both recorded). A stop refills its seat from that same close's ranking with
the stopped stock blocked for the day. Any membership change -> whole-book
reset at the next open.
"""
from __future__ import annotations

from .book import Position

AGG, DEF = "AGG", "DEF"
SEATS = {AGG: 8, DEF: 12}
ALLOC = {AGG: 0.40, DEF: 0.60}
EXIT_RANK = {AGG: 20, DEF: 30}


def update_trailing(p: Position, close: float, atr_t: float | None) -> dict:
    """§4.5 / §9.2. Returns {triggered, prev_stop, stop, armed}. The trigger
    compares against the level set on the PREVIOUS close, so a stop updated
    today can never fire today."""
    prev = p.stop_level
    if not p.armed and p.atr_entry is not None and \
            close >= p.entry_price + 1.0 * p.atr_entry:
        p.armed = True
    if p.armed:
        p.peak_close = close if p.peak_close is None else max(p.peak_close, close)
        if atr_t is not None:
            cand = p.peak_close - 3.0 * atr_t
            p.stop_level = cand if p.stop_level is None else max(p.stop_level, cand)
    return {"triggered": prev is not None and close <= prev,
            "prev_stop": prev, "stop": p.stop_level, "armed": p.armed}


def evaluate(D: str, m: dict[str, dict], positions: dict[str, Position],
             universe: set[str], excluded: set[str], liquid: set[str],
             week_end: bool, month_end: bool, initial_build: bool,
             pending_exit: set[str] = frozenset()) -> dict:
    """
    D            the completed bar date (signal date)
    m            {symbol: {has_bar, close, sma200, adv, vol, mom, atr}} as of D
    positions    current holdings (mutated: trailing-stop state only)
    universe     current NIFTY 500 members; excluded per §4.1
    liquid       symbols passing the liquidity screen on D (computed by the
                 caller with the code-confirmed percentile rule)
    """
    signals: list[dict] = []
    exits: dict[str, dict] = {}

    def has(s):
        return (m.get(s) or {}).get("has_bar")

    # --- 1. trailing stops on today's close (every session) ------------------
    stopped: set[str] = set()
    stop_info: dict[str, dict] = {}
    for s, p in positions.items():
        # a zero-share member has no open trade, hence no stop (backtest: no pos)
        if s in pending_exit or not has(s) or p.shares == 0:
            continue
        info = update_trailing(p, m[s]["close"], m[s].get("atr"))
        stop_info[s] = info
        if info["triggered"]:
            stopped.add(s)
            exits[s] = {"symbol": s, "reason": "STOP_TRAIL",
                        "also_triggered": None, "group": p.group}

    # --- 2. eligibility (entries only) ------------------------------------------
    elig = set()
    for s in universe:
        x = m.get(s) or {}
        if s in excluded or not x.get("has_bar"):
            continue
        if x.get("vol") is None or x.get("mom") is None or x.get("sma200") is None:
            continue
        if not (x["close"] > x["sma200"]):
            continue
        if s not in liquid:
            continue
        elig.add(s)

    held = {AGG: {s for s, p in positions.items() if p.group == AGG},
            DEF: {s for s, p in positions.items() if p.group == DEF}}
    entries: list[dict] = []
    ranks: dict[str, dict] = {}

    def sleeve(g: str, check: bool, other_after: set[str]):
        # factor_test.decide_c: pool = (eligible - other sleeve) - stopped today
        pool = (elig - other_after) - stopped
        key = (lambda s: (-m[s]["mom"], s)) if g == AGG else (lambda s: (m[s]["vol"], s))
        metric = "mom" if g == AGG else "vol"

        def rank_of(s):
            v = (m.get(s) or {}).get(metric)
            if v is None:
                return None
            better = sum(1 for x in pool if (m[x][metric] > v if g == AGG
                                             else m[x][metric] < v))
            return 1 + better
        # rank exits on the sleeve's check day
        for s in sorted(held[g]):
            if s in pending_exit or not has(s):
                continue
            r = rank_of(s)
            ranks[s] = {"rank": r, "pool": len(pool), "sleeve": g}
            if check and r is not None and r > EXIT_RANK[g]:
                if s in exits:                         # stop wins, keep both
                    exits[s]["also_triggered"] = "STOCK_RANK"
                else:
                    exits[s] = {"symbol": s, "reason": "STOCK_RANK",
                                "also_triggered": None, "group": g}
        survivors = {s for s in held[g] if s not in exits}
        free = SEATS[g] - len(survivors)
        fill_now = check or initial_build or any(
            e["group"] == g for e in exits.values())
        chosen = []
        if fill_now and free > 0:
            blocked = set(positions) | stopped | {e["symbol"] for e in entries}
            for s in sorted(pool, key=key):
                if len(chosen) >= free:
                    break
                if s in blocked:
                    continue
                chosen.append(s)
        for i, s in enumerate(chosen):
            entries.append({"symbol": s, "group": g,
                            "atr_entry": m[s].get("atr"),
                            "ref_price": m[s]["close"],
                            "initial_build": initial_build,
                            "rank": 1 + sum(1 for x in pool if (
                                m[x][metric] > m[s][metric] if g == AGG
                                else m[x][metric] < m[s][metric]))})
        return survivors | set(chosen)

    agg_after = sleeve(AGG, week_end, held[DEF])
    def_after = sleeve(DEF, month_end, agg_after)

    after = agg_after | def_after
    weights = {}
    for s in after:
        g = AGG if s in agg_after else DEF
        weights[s] = ALLOC[g] / SEATS[g]

    changed = bool(exits) or bool(entries)
    for s, e in exits.items():
        x = m.get(s) or {}
        signals.append({"symbol": s, "signal_type": "EXIT", "reason": e["reason"],
                        "also_triggered": e.get("also_triggered"),
                        "ref_price": x.get("close"),
                        "rank": (ranks.get(s) or {}).get("rank"),
                        "inputs": {**_inputs(x), **(stop_info.get(s) or {}),
                                   **(ranks.get(s) or {}), "sleeve": e["group"]}})
    for e in entries:
        x = m.get(e["symbol"]) or {}
        signals.append({"symbol": e["symbol"], "signal_type": "ENTRY",
                        "reason": "ENTRY", "ref_price": x.get("close"),
                        "rank": e["rank"],
                        "inputs": {**_inputs(x), "sleeve": e["group"],
                                   "rank": e["rank"],
                                   "initial_build": initial_build}})
    restart_atr = {s: (m.get(s) or {}).get("atr") for s, p in positions.items()
                   if p.shares == 0 and s in after}
    return {"signal_date": D, "exits": list(exits.values()),
            "entries": entries, "weights": weights if changed else {},
            "reset": changed, "signals": signals, "restart_atr": restart_atr,
            "eligible": len(elig), "stop_info": stop_info, "ranks": ranks,
            "checks": {"aggressive": week_end or initial_build,
                       "defensive": month_end or initial_build}}


def _inputs(x: dict) -> dict:
    return {k: x.get(k) for k in ("close", "sma200", "adv", "adv_cut", "vol",
                                  "mom", "atr")}
