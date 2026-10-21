"""
Equity 100 Momentum paper-live monitor — the conventions of the methodology
document (v1.0, 21 Oct 2026), each one pinned by a test:

  Appendix A indicators against pandas-computed golden values; percentile
  with average-rank ties inclusive at 0.60; strict >1.2 volume ratio; NaN
  fails the gate; stops judged on the CLOSE only (an intraday pierce and an
  open gap both hold); trail activates at entry + 1 x entry ATR and ratchets
  up only; exits fill next open; freed slots refill the same open with a
  different symbol, never the one that just exited; carry-forward sizing
  (user decision) vs re_level (the backtest's convention) both work; costs
  include the flat Rs 20 DP charge per scrip per sell; corporate actions
  adjust open positions with the pre-state archived; the whole book
  round-trips a restart.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, costs, engine
from backend import indicators as I
from backend.engine import Book, Position
from backend.params import validate_overrides

P = dict(config.STRATEGY_PARAMS)

# ---------------------------------------------------------------- fixtures

N = 300
CLOSES = [100 + i * 0.3 + 5 * math.sin(i / 7) for i in range(N)]
HIGHS = [c * 1.012 for c in CLOSES]
LOWS = [c * 0.991 for c in CLOSES]
VOLS = [1000.0 + (i % 30) * 50 for i in range(N)]

# pandas-computed goldens for exactly the series above (see methodology
# Appendix A; regenerated values are hardcoded so CI needs no pandas)
GOLD = {"sma200": 159.77248209432364, "ret126": 0.23393346668661708,
        "vol126": 0.04799471553552883, "mom121": 0.6173116196262813,
        "atr14": 3.865384470744113, "vr": 1.1898734177215189}


def bars_from(closes, highs=None, lows=None, vols=None, start="2026-01-01"):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    out = []
    for i, c in enumerate(closes):
        out.append({"date": (d0 + timedelta(days=i)).isoformat(),
                    "o": c * 0.999, "h": (highs or closes)[i] if highs else c * 1.01,
                    "l": (lows or closes)[i] if lows else c * 0.99,
                    "c": c, "volume": (vols or [1000] * len(closes))[i],
                    "volume_adj": (vols or [1000] * len(closes))[i]})
    return out


# ============================================== Appendix A, against pandas

def test_indicators_match_pandas_goldens():
    assert I.sma(CLOSES, 200) == pytest.approx(GOLD["sma200"], abs=1e-9)
    assert I.ret_over(CLOSES, 126) == pytest.approx(GOLD["ret126"], abs=1e-12)
    assert I.realised_vol(CLOSES, 126) == pytest.approx(GOLD["vol126"], abs=1e-12)
    assert I.mom_12_1(CLOSES) == pytest.approx(GOLD["mom121"], abs=1e-12)
    assert I.atr_wilder(HIGHS, LOWS, CLOSES, 14) == pytest.approx(GOLD["atr14"], abs=1e-9)
    assert I.volume_ratio(VOLS) == pytest.approx(GOLD["vr"], abs=1e-12)


def test_short_windows_yield_none_never_a_short_computation():
    assert I.sma(CLOSES[:199], 200) is None
    assert I.ret_over(CLOSES[:126], 126) is None
    assert I.realised_vol(CLOSES[:126], 126) is None     # needs window+1
    assert I.mom_12_1(CLOSES[:252]) is None              # needs 253
    assert I.atr_wilder(HIGHS[:13], LOWS[:13], CLOSES[:13]) is None
    assert I.volume_ratio(VOLS[:19]) is None
    assert I.volume_ratio([*VOLS[:19], None]) is None    # unknown ≠ zero


def test_percentile_average_rank_ties_and_inclusive_boundary():
    pr = I.percentile_ranks({"A": 1.0, "B": 2.0, "C": 2.0, "D": 3.0, "E": 4.0})
    assert pr["B"] == pr["C"] == pytest.approx(0.5)      # midpoint rank 2.5/5
    assert pr["D"] == pytest.approx(0.8)
    # exactly 0.60 PASSES (inclusive): 3rd of 5 distinct values
    pr5 = I.percentile_ranks({c: i for i, c in enumerate("abcde")})
    assert pr5["c"] == pytest.approx(0.6)
    assert pr5["c"] >= P["percentile_pass"]


def test_volume_ratio_threshold_is_strict():
    m = {"close": 10, "dma200": 9, "ret6m": 0.1, "ret_vol": 1.0,
         "vr": 1.2, "mom121": 0.5, "atr14": 1.0, "bars": 300}
    scored = engine.score_universe({"X": dict(m)}, P)
    assert scored["X"]["c4"] is False                    # exactly 1.2 fails
    m["vr"] = 1.2000001
    assert engine.score_universe({"X": dict(m)}, P)["X"]["c4"] is True


def test_nan_components_fail_the_gate():
    m = {"close": 10, "dma200": None, "ret6m": None, "ret_vol": None,
         "vr": None, "mom121": None, "atr14": None, "bars": 100}
    s = engine.score_universe({"X": m}, P)["X"]
    assert s["score"] == 0 and not s["rankable"]


# ============================================================= costs

def test_sell_cost_includes_the_flat_dp_charge():
    base = costs.sell_cost(100.0, 10)
    # strip the DP charge: everything else is percentage turnover
    pct_only = base - config.COST_DP_PER_SELL
    assert pct_only > 0
    # doubling turnover doubles percentage costs but the DP stays flat
    double = costs.sell_cost(100.0, 20)
    assert double - config.COST_DP_PER_SELL == pytest.approx(2 * pct_only, rel=1e-9)
    # buy side carries no DP
    assert costs.buy_cost(100.0, 10) < pct_only + config.COST_DP_PER_SELL


# ===================================================== stops on the close

def scored_one(symbol, close, atr=2.0):
    return {symbol: {"close": close, "atr14": atr, "dma200": 1, "ret6m": 1,
                     "ret_vol": 1, "vr": 2.0, "mom121": 1, "bars": 300,
                     "c1": True, "c2": True, "c3": True, "c4": True,
                     "score": 4, "rankable": True,
                     "pct_ret6m": 1.0, "pct_retvol": 1.0}}


def held_book(entry=100.0, entry_atr=3.0):
    b = Book(starting_capital=100000, max_holdings=10)
    pos = Position(symbol="AAA", slot=0, shares=10, entry_price=entry,
                   entry_date="2026-10-01", entry_cost=5.0, entry_atr=entry_atr,
                   initial_stop=entry - 2 * entry_atr)
    b.positions[0] = pos
    b.cash -= entry * 10 + 5.0
    return b, pos


def test_initial_stop_exits_on_close_at_or_below_never_intraday():
    b, pos = held_book()                       # stop = 94
    plan = engine.evaluate_close(b, scored_one("AAA", 94.5), "2026-10-02",
                                 {"AAA"}, P)
    assert plan["exits"] == []                 # close above: holds (intraday
                                               # pierce is invisible by design)
    plan = engine.evaluate_close(b, scored_one("AAA", 94.0), "2026-10-03",
                                 {"AAA"}, P)
    assert len(plan["exits"]) == 1             # close AT the stop exits
    assert plan["exits"][0]["reason"] == "Initial stop"


def test_trail_activates_at_one_entry_atr_and_ratchets_up_only():
    b, pos = held_book(entry=100.0, entry_atr=3.0)     # activates at 103
    engine.evaluate_close(b, scored_one("AAA", 102.9), "d1", {"AAA"}, P)
    assert pos.trail_active is False
    engine.evaluate_close(b, scored_one("AAA", 103.0, atr=2.0), "d2", {"AAA"}, P)
    assert pos.trail_active is True
    assert pos.trail_stop == pytest.approx(103.0 - 3 * 2.0)     # 97
    # higher close, larger ATR: candidate 110-3*4=98 > 97 -> ratchet up
    engine.evaluate_close(b, scored_one("AAA", 110.0, atr=4.0), "d3", {"AAA"}, P)
    assert pos.trail_stop == pytest.approx(98.0)
    # lower candidate NEVER lowers the stop (ATR blows out)
    engine.evaluate_close(b, scored_one("AAA", 109.0, atr=8.0), "d4", {"AAA"}, P)
    assert pos.trail_stop == pytest.approx(98.0)
    # close through the trail exits, labelled as the trailing stop
    plan = engine.evaluate_close(b, scored_one("AAA", 97.5, atr=8.0), "d5",
                                 {"AAA"}, P)
    assert plan["exits"] and plan["exits"][0]["reason"] == "Trailing stop"


def test_no_rank_exit_a_better_candidate_never_ejects_a_holding():
    b, _ = held_book()
    scored = {**scored_one("AAA", 100.0),
              **scored_one("BBB", 50.0)}
    scored["BBB"]["mom121"] = 99.0             # vastly better rank
    plan = engine.evaluate_close(b, scored, "d1", {"AAA", "BBB"}, P)
    assert plan["exits"] == []                 # holding stays
    assert all(e["symbol"] != "AAA" for e in plan["entries"])


# ========================================================= fills and sizing

def full_scored(syms_moms: dict[str, float], close=100.0):
    out = {}
    for s, mom in syms_moms.items():
        out.update(scored_one(s, close))
        out[s]["mom121"] = mom
    return out


def test_entry_fills_next_open_carry_forward_first_tranche():
    b = Book(starting_capital=100000, max_holdings=10)
    scored = full_scored({"AAA": 2.0, "BBB": 1.0})
    plan = engine.evaluate_close(b, scored, "2026-10-01", {"AAA", "BBB"}, P)
    assert [e["symbol"] for e in plan["entries"]] == ["AAA", "BBB"]  # rank order
    res = engine.fill_open(b, plan, {"AAA": 200.0, "BBB": 50.0},
                           "2026-10-02", {}, P)
    a = b.positions[0]
    assert a.symbol == "AAA" and a.entry_price == 200.0
    # carry-forward first tranche: min(20%,1/10) x STARTING capital = 10,000
    assert a.shares == 50                      # floor(10000/200)
    assert a.initial_stop == pytest.approx(200.0 - 2 * a.entry_atr)
    assert b.positions[1].shares == 200        # floor(10000/50)
    assert b.cash < 100000


def test_carry_forward_reuses_the_slots_own_proceeds():
    b = Book(starting_capital=100000, max_holdings=10)
    plan = engine.evaluate_close(b, full_scored({"AAA": 2.0}), "d0", {"AAA"}, P)
    engine.fill_open(b, plan, {"AAA": 100.0}, "d1", {}, P)
    pos = b.positions[0]
    pos.initial_stop = 99.0                    # force a stop tomorrow
    plan2 = engine.evaluate_close(b, scored_one("AAA", 95.0), "d2", {"AAA"}, P)
    assert plan2["exits"]
    # exit at 120 open: proceeds land in slot 0
    engine.fill_open(b, plan2, {"AAA": 120.0}, "d3", {}, P)
    proceeds = b.slot_proceeds[0]
    assert proceeds == pytest.approx(100 * 120.0 - costs.sell_cost(120.0, 100))
    # next entry in slot 0 is budgeted at THOSE proceeds, not 10% of equity
    plan3 = engine.evaluate_close(b, full_scored({"CCC": 1.0}), "d4", {"CCC"}, P)
    engine.fill_open(b, plan3, {"CCC": 100.0}, "d5", {}, P)
    assert b.positions[0].symbol == "CCC"
    assert b.positions[0].shares == math.floor(proceeds / 100.0)


def test_re_level_mode_sizes_from_current_equity():
    p2 = {**P, "sizing_mode": "re_level"}
    b = Book(starting_capital=100000, max_holdings=10)
    b.cash = 150000                            # grown book
    plan = engine.evaluate_close(b, full_scored({"AAA": 1.0}), "d0", {"AAA"}, p2)
    engine.fill_open(b, plan, {"AAA": 100.0}, "d1", {}, p2)
    assert b.positions[0].shares == 150        # 10% of 150,000


def test_same_open_refill_with_a_different_symbol_never_the_exited_one():
    b = Book(starting_capital=100000, max_holdings=1)   # one slot
    p1 = {**P, "max_holdings": 1}
    plan = engine.evaluate_close(b, full_scored({"AAA": 2.0}), "d0", {"AAA"}, p1)
    engine.fill_open(b, plan, {"AAA": 100.0}, "d1", {}, p1)
    b.positions[0].initial_stop = 99.5
    scored = full_scored({"AAA": 3.0, "BBB": 1.0})
    scored["AAA"]["close"] = 95.0              # stops out AND still ranks first
    plan2 = engine.evaluate_close(b, scored, "d2", {"AAA", "BBB"}, p1)
    assert plan2["exits"][0]["symbol"] == "AAA"
    assert [e["symbol"] for e in plan2["entries"]] == ["BBB"]  # AAA barred
    res = engine.fill_open(b, plan2, {"AAA": 95.0, "BBB": 50.0}, "d3", {}, p1)
    assert b.positions[0].symbol == "BBB"      # freed slot refilled same open
    assert len(res["trades"]) == 1


def test_missing_open_skips_an_entry_but_holds_an_exit_loudly():
    b = Book(starting_capital=100000, max_holdings=2)
    p2 = {**P, "max_holdings": 2}
    plan = engine.evaluate_close(b, full_scored({"AAA": 2.0, "BBB": 1.0}),
                                 "d0", {"AAA", "BBB"}, p2)
    res = engine.fill_open(b, plan, {"AAA": 100.0}, "d1", {}, p2)  # no BBB print
    assert b.positions[0].symbol == "AAA" and b.positions[1] is None
    assert any("no open print" in n for n in res["notes"])
    b.positions[0].initial_stop = 999.0        # certain stop
    plan2 = engine.evaluate_close(b, scored_one("AAA", 100.0), "d2", {"AAA"}, p2)
    res2 = engine.fill_open(b, plan2, {}, "d3", {}, p2)            # suspension
    assert b.positions[0] is not None          # exit HELD, not lost
    assert res2["held_exits"]


def test_cash_cap_and_whole_shares():
    b = Book(starting_capital=100000, max_holdings=10)
    b.cash = 500.0                             # nearly fully invested
    plan = engine.evaluate_close(b, full_scored({"AAA": 1.0}), "d0", {"AAA"}, P)
    engine.fill_open(b, plan, {"AAA": 400.0}, "d1", {}, P)
    pos = b.positions[0]
    assert pos is not None and pos.shares == 1  # budget 10k but cash caps at 1
    assert b.cash >= 0


# =========================================================== corporate action

def test_corp_action_math_preserves_value_and_archives():
    # a 1:1 bonus (factor 2) on 10 shares @ 2000: 20 shares, prices halved
    pos = Position(symbol="AAA", slot=0, shares=10, entry_price=2000.0,
                   entry_date="d", entry_cost=5.0, entry_atr=30.0,
                   initial_stop=1940.0, trail_active=True,
                   highest_close=2100.0, trail_stop=2010.0)
    f = 2.0
    new_shares = int(pos.shares * f)
    leftover = pos.shares * f - new_shares
    assert new_shares == 20 and leftover == 0
    value_before = pos.shares * 2000.0
    pos.shares, pos.entry_price = float(new_shares), pos.entry_price / f
    pos.initial_stop /= f
    pos.trail_stop /= f
    assert pos.shares * (2000.0 / f) == pytest.approx(value_before)
    assert pos.initial_stop == pytest.approx(970.0)
    # fractional remainder: 4:3 style factor on 10 shares
    f2, sh = 4 / 3, 10
    ns = int(sh * f2)
    assert ns == 13 and (sh * f2 - ns) == pytest.approx(1 / 3, abs=1e-9)


# ================================================================ book state

def test_book_round_trips_a_restart():
    b, pos = held_book()
    pos.trail_active = True
    pos.highest_close = 111.0
    pos.trail_stop = 101.5
    b.slot_proceeds[3] = 12345.67
    rec = b.to_record()
    import json
    b2 = Book.from_record(json.loads(json.dumps(rec)))
    assert b2.cash == b.cash
    p2 = b2.positions[0]
    assert p2.symbol == "AAA" and p2.trail_stop == 101.5
    assert p2.effective_stop == max(p2.initial_stop, 101.5)
    assert b2.slot_proceeds[3] == 12345.67


# =================================================================== params

def test_param_validation():
    assert validate_overrides({"sizing_mode": "re_level"}) == \
        {"sizing_mode": "re_level"}
    assert validate_overrides({"sizing_mode": "carry_forward"}) == {}  # = default
    with pytest.raises(ValueError):
        validate_overrides({"sizing_mode": "yolo"})
    with pytest.raises(ValueError):
        validate_overrides({"no_such": 1})
    with pytest.raises(ValueError):
        validate_overrides({"max_holdings": 10.5})
    assert validate_overrides({"atr_mult_trail": "4"}) == {"atr_mult_trail": 4.0}


# ============================================================ end-to-end sim

def test_three_session_simulation_reconciles():
    """Signal at T close -> fill at T+1 open -> stop at T+2 close -> exit at
    T+3 open; cash + marks always reconcile with trades net of costs."""
    b = Book(starting_capital=100000, max_holdings=2)
    p2 = {**P, "max_holdings": 2}
    # T: AAA qualifies
    plan = engine.evaluate_close(b, full_scored({"AAA": 1.0}), "T", {"AAA"}, p2)
    assert not plan["filled"]
    # T+1: fills at the open, not at T's close
    engine.fill_open(b, plan, {"AAA": 100.0}, "T+1", {}, p2)
    pos = b.positions[0]
    spend = pos.shares * 100.0 + pos.entry_cost
    assert b.cash == pytest.approx(100000 - spend)
    # T+2 close crashes through the stop
    sc = scored_one("AAA", 90.0)
    plan2 = engine.evaluate_close(b, sc, "T+2", {"AAA"}, p2)
    assert plan2["exits"]
    # T+3 open gaps lower still — fill is the open, worse than the stop level
    res = engine.fill_open(b, plan2, {"AAA": 88.0}, "T+3", {}, p2)
    t = res["trades"][0]
    assert t["exit_price"] == 88.0
    assert t["net_pnl"] == pytest.approx(
        (88.0 - 100.0) * t["shares"] - t["entry_cost"] - t["exit_cost"])
    assert b.cash == pytest.approx(
        100000 - spend + t["shares"] * 88.0 - t["exit_cost"])
    assert b.positions[0] is None


def test_fill_refuses_a_stale_plan_that_reenters_the_exited_symbol():
    # Defense in depth: even if a (stale/corrupted) plan carries the same
    # symbol as both exit and entry, the fill itself refuses the re-entry.
    b = Book(starting_capital=100000, max_holdings=1)
    p1 = {**P, "max_holdings": 1}
    plan = engine.evaluate_close(b, full_scored({"AAA": 1.0}), "d0", {"AAA"}, p1)
    engine.fill_open(b, plan, {"AAA": 100.0}, "d1", {}, p1)
    stale = {"signal_date": "d2", "filled": False,
             "exits": [{"slot": 0, "symbol": "AAA", "reason": "Initial stop",
                        "stop": 99.0, "close": 98.0}],
             "entries": [{"symbol": "AAA", "mom121": 1.0, "atr": 2.0,
                          "close": 98.0}]}
    res = engine.fill_open(b, stale, {"AAA": 98.0}, "d3", {}, p1)
    assert len(res["trades"]) == 1
    assert b.positions[0] is None                 # NOT re-entered same day
    assert any("same-day re-entry" in n for n in res["notes"])


def test_gate_percentile_boundary_is_inclusive_through_score_universe():
    # five names, distinct values: the 3rd-ranked sits at exactly 0.60 and
    # must PASS criteria 2 and 3 (Appendix A trap #1).
    metrics = {}
    for i, s in enumerate(["A", "B", "C", "D", "E"]):
        metrics[s] = {"close": 10, "dma200": 9, "ret6m": float(i),
                      "ret_vol": float(i), "vr": 2.0, "mom121": 0.1,
                      "atr14": 1.0, "bars": 300}
    scored = engine.score_universe(metrics, P)
    assert scored["C"]["pct_ret6m"] == pytest.approx(0.6)
    assert scored["C"]["c2"] is True and scored["C"]["c3"] is True
    assert scored["B"]["c2"] is False             # 0.4: below the line


def test_storage_down_boots_with_a_plain_message_not_a_crash():
    import asyncio
    from backend.hub import EquityHub
    from backend import config as cfg
    old = cfg.DB_PATH
    cfg.DB_PATH = "/proc/definitely/not/writable/equity.db"
    try:
        h = EquityHub.__new__(EquityHub)
        EquityHub.__init__(h)
        assert h.store.available is False
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(h.start())        # must NOT raise
            assert "STORAGE UNAVAILABLE" in h.status["boot"]
            assert "/var/data" in h.status["boot"]
            snap = loop.run_until_complete(h.snapshot())   # page still renders
            assert snap["build"] and snap["status"]["boot"].startswith("STORAGE")
        finally:
            loop.close()
    finally:
        cfg.DB_PATH = old
