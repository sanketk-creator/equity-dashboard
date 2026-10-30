"""
Strategy 3 — Sector Momentum 12 (spec §5). Pure decision logic.

Weekly, on the last session of the ISO week:
  * score the 8 sector indices: mean of population z-scores of
      m1 = 12-1 momentum of the index
      m2 = index 6M return minus NIFTY 500 6M return
      m3 = index close / its SMA200
      m4 = fraction of the last 126 index closes above their SMA200
    rank by score (ties alphabetical);
  * a seated sector LEAVES at rank worse than 4 (all its stocks sold);
    it STAYS at ranks 1-4 even below its SMA200;
  * free seats are filled walking the WHOLE ranking (no rank cutoff) with
    sectors whose index closes above its SMA200 (no swapping); a seated
    sector that fell to rank 5+ can be picked again by that walk, and then
    stays (sector_rotation.decide);
  * inside each seated sector 4 stock seats: enter by highest 12-1 among
    eligible names, leave when rank within the sector is worse than 8;
  * weights by ORDER among seated sectors: 50% / 33% / 17%, divided by 4;
  * any sector or stock membership change -> whole-book reset; no stop-loss,
    no take-profit.
"""
from __future__ import annotations

from . import indicators as I
from .book import Position

SEATS = 3
STOCK_SEATS = 4
SECTOR_EXIT_RANK = 4
STOCK_EXIT_RANK = 8
ORDER_WEIGHTS = (0.50, 0.33, 0.17)

# Read from sector_rotation.py (decide) + d1_exit4.py (SECTOR_EXIT_RANK = 4):
# the free-seat walk runs down the WHOLE ranking (no rank cutoff) taking the
# first gated (close > SMA200) sectors not already seated. A seated sector
# that just fell to rank 5+ is NOT excluded from that walk: if seats are
# still free when the walk reaches it and it is above its SMA200, it is
# picked again and simply stays (its stocks continue, ranked as usual).
ENTRY_RANK_CUTOFF: int | None = None
# index_indicators(): above = (close > SMA200) only where SMA200 exists,
# then rolling(126, min_periods=126).mean() -> 325 rows (spec said 326).
M4_MIN_ROWS = 325
REL_WIN = 126


def sector_measures(idx: list[tuple[str, float]],
                    n500: list[tuple[str, float]]) -> dict | None:
    """idx: [(date, close)] of the sector index, oldest first, up to the
    decision date. n500: [(date, close)] of NIFTY 500, oldest first, up to
    the decision date.

    m2 exactly as index_indicators(): the NIFTY 500 6-month return is
    computed on NIFTY 500's OWN bars (its close / its close 126 of ITS bars
    earlier - 1) and matched to the sector's date with NO forward fill. A
    NIFTY 500 gap inside the window just moves that anchor back one day; no
    NIFTY 500 bar ON the decision date -> m2 undefined (the hub moves the
    week's decision to the last session where all 9 index files have a bar).
    """
    dates = [d for d, _ in idx]
    closes = [c for _, c in idx]
    if len(closes) < max(M4_MIN_ROWS, 253):
        return None
    m1 = I.mom_12_1(closes)
    if not n500 or n500[-1][0] != dates[-1] or len(n500) < REL_WIN + 1:
        return None
    b6 = n500[-1][1] / n500[-1 - REL_WIN][1] - 1
    m2 = (closes[-1] / closes[-1 - REL_WIN] - 1) - b6
    sma_t = I.sma(closes, 200)
    m3 = closes[-1] / sma_t
    above = 0
    for k in range(126):
        end = len(closes) - k
        sm = sum(closes[end - 200:end]) / 200
        if closes[end - 1] > sm:
            above += 1
    m4 = above / 126
    return {"m1": m1, "m2": m2, "m3": m3, "m4": m4, "sma200": sma_t,
            "close": closes[-1], "above_sma200": closes[-1] > sma_t,
            "rows": len(closes), "n500_6m": b6}


def rank_sectors(measures: dict[str, dict]) -> list[dict]:
    names = sorted(measures)
    z = {k: I.zscores_population({s: measures[s][k] for s in names})
         for k in ("m1", "m2", "m3", "m4")}
    rows = []
    for s in names:
        sc = (z["m1"][s] + z["m2"][s] + z["m3"][s] + z["m4"][s]) / 4
        rows.append({"sector": s, **{k: measures[s][k] for k in ("m1", "m2", "m3", "m4")},
                     "z1": z["m1"][s], "z2": z["m2"][s], "z3": z["m3"][s],
                     "z4": z["m4"][s], "score": sc,
                     "close_vs_sma200": measures[s]["m3"],
                     "above_sma200": measures[s]["above_sma200"]})
    rows.sort(key=lambda r: (-r["score"], r["sector"]))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def evaluate(D: str, ranking: list[dict], seated: list[str],
             m: dict[str, dict], sector_map: dict[str, str],
             positions: dict[str, Position], universe: set[str],
             excluded: set[str], liquid: set[str], initial_build: bool,
             pending_exit: set[str] = frozenset()) -> dict:
    """
    ranking   rank_sectors() output for D (all 8 sectors)
    seated    sectors holding a seat before this decision
    m         {symbol: {has_bar, close, sma200, mom, adv}} as of D
    sector_map  current symbol -> sector (entries use it)
    positions   current holdings; Position.group = the seat's sector
    """
    by = {r["sector"]: r for r in ranking}
    exits: dict[str, dict] = {}
    signals: list[dict] = []

    # --- sectors -----------------------------------------------------------
    keep = [s for s in seated if by[s]["rank"] <= SECTOR_EXIT_RANK]
    picked = list(keep)
    for r in ranking:                                  # rank order, whole list
        if len(picked) >= SEATS:
            break
        if ENTRY_RANK_CUTOFF is not None and r["rank"] > ENTRY_RANK_CUTOFF:
            break
        if r["sector"] not in picked and r["above_sma200"]:
            picked.append(r["sector"])
    # a seated sector that fell below the exit rank but was re-picked stays
    staying = [s for s in seated if s in picked]
    leaving = [s for s in seated if s not in picked]
    repicked = [s for s in staying if s not in keep]
    new_secs = [s for s in picked if s not in seated]
    for sec in leaving:
        for s, p in positions.items():
            if p.group == sec:
                exits[s] = {"symbol": s, "reason": "SECTOR_EXIT",
                            "also_triggered": None, "group": sec}

    # --- stock eligibility (entries only) -------------------------------------
    def eligible(s):
        # sector_rotation.eligible_on: liquid, close > SMA200, vol and mom
        # defined, >= 253 bars, not excluded
        x = m.get(s) or {}
        return (s in universe and s not in excluded and x.get("has_bar")
                and x.get("mom") is not None and x.get("vol") is not None
                and x.get("sma200") is not None and (x.get("rows") or 0) >= 253
                and x["close"] > x["sma200"] and s in liquid)

    elig_by_sec: dict[str, list[str]] = {}
    for s, sec in sector_map.items():
        if eligible(s):
            elig_by_sec.setdefault(sec, []).append(s)

    entries: list[dict] = []
    stock_ranks: dict[str, int] = {}
    after_by_sec: dict[str, list[str]] = {}
    for sec in staying + new_secs:
        held = sorted(s for s, p in positions.items() if p.group == sec)
        pool = elig_by_sec.get(sec, [])
        survivors = []
        for s in held:
            if s in pending_exit or not (m.get(s) or {}).get("has_bar") \
                    or (m.get(s) or {}).get("mom") is None:
                survivors.append(s)             # no signal today: untouched
                continue
            r = 1 + sum(1 for x in pool if m[x]["mom"] > m[s]["mom"])
            stock_ranks[s] = r
            if r > STOCK_EXIT_RANK:
                exits[s] = {"symbol": s, "reason": "STOCK_RANK",
                            "also_triggered": None, "group": sec}
            else:
                survivors.append(s)
        free = STOCK_SEATS - len(survivors)
        chosen = [s for s in sorted(pool, key=lambda s: (-m[s]["mom"], s))
                  if s not in positions][:max(0, free)]
        for s in chosen:
            r = 1 + sum(1 for x in pool if m[x]["mom"] > m[s]["mom"])
            entries.append({"symbol": s, "group": sec, "atr_entry": None,
                            "ref_price": m[s]["close"],
                            "initial_build": initial_build, "rank": r})
        after_by_sec[sec] = survivors + chosen

    seated_after = sorted(staying + new_secs, key=lambda s: by[s]["rank"])
    order = sorted(seated_after, key=lambda s: (-by[s]["score"], s))
    weights = {}
    for i, sec in enumerate(order):
        for s in after_by_sec.get(sec, []):
            weights[s] = ORDER_WEIGHTS[i] / STOCK_SEATS
    changed = bool(exits) or bool(entries) or set(seated_after) != set(seated)

    for s, e in exits.items():
        x = m.get(s) or {}
        signals.append({"symbol": s, "signal_type": "EXIT", "reason": e["reason"],
                        "ref_price": x.get("close"), "rank": stock_ranks.get(s),
                        "inputs": {"sector": e["group"],
                                   "sector_rank": by[e["group"]]["rank"],
                                   "sector_score": by[e["group"]]["score"],
                                   "stock_rank": stock_ranks.get(s),
                                   "mom": x.get("mom"), "close": x.get("close")}})
    for e in entries:
        x = m.get(e["symbol"]) or {}
        signals.append({"symbol": e["symbol"], "signal_type": "ENTRY",
                        "reason": "ENTRY", "ref_price": x.get("close"),
                        "rank": e["rank"],
                        "inputs": {"sector": e["group"],
                                   "sector_rank": by[e["group"]]["rank"],
                                   "sector_score": by[e["group"]]["score"],
                                   "stock_rank": e["rank"], "mom": x.get("mom"),
                                   "close": x.get("close"), "sma200": x.get("sma200"),
                                   "adv": x.get("adv"), "adv_cut": x.get("adv_cut"),
                                   "initial_build": initial_build}})
    return {"signal_date": D, "exits": list(exits.values()), "entries": entries,
            "weights": weights if changed else {}, "reset": changed,
            "signals": signals, "seated_before": list(seated),
            "seated_after": seated_after, "weight_order": order,
            "leaving": leaving, "joining": new_secs, "repicked": repicked,
            "stock_ranks": stock_ranks}
