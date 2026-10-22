"""
Hourly Exit Specification v1.0 (21 Oct 2026) — every rule pinned by a test,
with the operator's decisions of 22 Oct 2026 (at-or-below trigger; gap-exit
refill at 10:15; per-exit daily-rule record + shadow book).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, costs, intraday as ID
from backend.engine import Book, Position

P = dict(config.STRATEGY_PARAMS)
D = "2026-11-02"
L = ID.LABELS


def held_book(symbols_stops: dict[str, tuple[float, float]], cash=50_000.0):
    """{symbol: (entry_price, shares)} in slots 0..; rest empty."""
    b = Book(starting_capital=100_000, max_holdings=max(1, len(symbols_stops)))
    b.cash = cash
    for i, (s, (px, sh)) in enumerate(symbols_stops.items()):
        b.positions[i] = Position(symbol=s, slot=i, shares=sh, entry_price=px,
                                  entry_date="2026-10-20", entry_cost=10.0,
                                  entry_atr=5.0, initial_stop=px - 10)
    return b


def flat(o, c=None, h=None, l=None):
    c = o if c is None else c
    return {"o": o, "c": c, "h": h or max(o, c), "l": l or min(o, c), "n": 10}


def plan(stops, ranked=(), carry=(), fresh=True, prev=None):
    return {"fresh": fresh, "stops": stops,
            "ranked": [{"symbol": s, "atr": a, "mom121": 0.5, "close": 0}
                       for s, a in ranked],
            "carry_exits": list(carry), "prev_close": prev or {}}


def run(book, pl, bars, opens, closed=7, cur=None):
    return ID.replay_session(D, book.to_record(), pl, bars, opens, closed, cur, P)


def bars_all(sym, price, **over):
    """A symbol trading flat at `price` all session, with overrides per label."""
    out = {lab: flat(price) for lab in L}
    out.update(over)
    return {sym: out}


# ------------------------------------------------------------------ §3 / §4

def test_hourly_close_at_or_below_triggers_and_fills_next_bar_open():
    bk = held_book({"AAA": (100.0, 10)})
    bars = bars_all("AAA", 100.0, **{"11:15": flat(99, 90.0, l=85),
                                     "12:15": flat(91.5, 92)})
    r = run(bk, plan({"AAA": 90.0}), bars, {"AAA": 100.0})
    assert len(r["trades"]) == 1
    t = r["trades"][0]
    assert t["exit_price"] == 91.5                    # NEXT bar's open
    m = t["meta"]
    assert m["trigger_timestamp"] == f"{D} 12:15"     # 11:15 bar closes 12:15
    assert m["fill_timestamp"] == f"{D} 12:15"
    assert m["trigger_label"] == "11:15" and m["exit_basis"] == "hourly"
    assert m["trigger_price"] == 90.0 and m["stop"] == 90.0


def test_close_above_stop_holds_even_when_the_low_pierces_it():
    bk = held_book({"AAA": (100.0, 10)})
    bars = bars_all("AAA", 100.0, **{"10:15": flat(99, 90.01, l=80)})
    r = run(bk, plan({"AAA": 90.0}), bars, {"AAA": 100.0})
    assert r["trades"] == [] and r["book"]["positions"][0]["symbol"] == "AAA"


def test_gap_open_at_or_below_exits_at_that_open_and_refills_at_1015():
    bk = held_book({"AAA": (100.0, 10)})
    bars = {**bars_all("AAA", 88.0), **bars_all("BBB", 50.0,
                                                **{"10:15": flat(51.0)})}
    r = run(bk, plan({"AAA": 90.0}, ranked=[("AAA", 2.0), ("BBB", 2.0)]),
            bars, {"AAA": 90.0, "BBB": 50.0})
    t = r["trades"][0]
    assert t["exit_price"] == 90.0 and t["meta"]["trigger"] == "gap_open"
    assert t["meta"]["fill_timestamp"] == f"{D} 09:15"
    entry = [e for e in r["events"] if e["kind"] == "entry"]
    assert len(entry) == 1
    assert entry[0]["symbol"] == "BBB"                # never the exited AAA
    assert entry[0]["label"] == "10:15" and entry[0]["price"] == 51.0
    pos = r["book"]["positions"][0]
    assert pos["symbol"] == "BBB" and pos["entry_label"] == "10:15"
    assert abs(pos["initial_stop"] - (51.0 - 2 * 2.0)) < 1e-9


def test_trigger_on_the_stub_carries_to_next_open_and_is_flagged():
    bk = held_book({"AAA": (100.0, 10)})
    bars = bars_all("AAA", 100.0, **{"15:15": flat(95, 89.0)})
    r = run(bk, plan({"AAA": 90.0}), bars, {"AAA": 100.0})
    assert r["trades"] == []
    assert len(r["carry_exits"]) == 1
    c = r["carry_exits"][0]
    assert c["closing_window_only"] is True and c["trigger_label"] == "15:15"
    assert any("closing window" in a["msg"] for a in r["alerts"])
    # next session: fills at the official open, refill one bar later
    bk2 = Book.from_record(r["book"])
    nxt = {**bars_all("AAA", 87.0), **bars_all("CCC", 20.0)}
    r2 = ID.replay_session("2026-11-03", bk2.to_record(),
                           plan({"AAA": 90.0}, ranked=[("AAA", 1.0), ("CCC", 1.0)],
                                carry=r["carry_exits"]),
                           nxt, {"AAA": 86.5, "CCC": 20.0}, 7, None, P)
    t = r2["trades"][0]
    assert t["exit_price"] == 86.5 and t["meta"]["closing_window_only"] is True
    assert t["meta"]["trigger_date"] == D
    ent = [e for e in r2["events"] if e["kind"] == "entry"]
    assert [(e["symbol"], e["label"]) for e in ent] == [("CCC", "10:15")]


def test_exit_filled_on_the_last_bar_refills_next_day_not_today():
    bk = held_book({"AAA": (100.0, 10)})
    bars = {**bars_all("AAA", 100.0, **{"14:15": flat(95, 89.0),
                                        "15:15": flat(88.0)}),
            **bars_all("BBB", 10.0)}
    r = run(bk, plan({"AAA": 90.0}, ranked=[("BBB", 1.0)]), bars,
            {"AAA": 100.0, "BBB": 10.0})
    assert r["trades"][0]["exit_price"] == 88.0
    assert r["trades"][0]["meta"]["fill_timestamp"] == f"{D} 15:15"
    assert not [e for e in r["events"] if e["kind"] == "entry"]
    assert r["book"]["positions"][0] is None


# -------------------------------------------------------------------- §6/§7

def test_fresh_entries_fill_at_0915_open_and_are_checked_at_that_bars_close():
    bk = Book(starting_capital=100_000, max_holdings=10)
    bars = {**bars_all("XXX", 200.0, **{"09:15": flat(200, 189.0)}),
            **bars_all("YYY", 100.0)}
    r = run(bk, plan({}, ranked=[("XXX", 5.0), ("YYY", 4.0)]), bars,
            {"XXX": 200.0, "YYY": 100.0})
    ent = [e for e in r["events"] if e["kind"] == "entry"]
    assert {(e["symbol"], e["label"]) for e in ent} >= {("XXX", "09:15"),
                                                         ("YYY", "09:15")}
    # XXX initial stop 200 - 2x5 = 190; 09:15 bar closes 189 -> exits 10:15
    t = [t for t in r["trades"] if t["symbol"] == "XXX"][0]
    assert t["meta"]["trigger_label"] == "09:15" and t["exit_price"] == 200.0


def test_first_tranche_is_min_20pct_1_over_n_of_start_capital():
    bk = Book(starting_capital=100_000, max_holdings=10)
    r = run(bk, plan({}, ranked=[("XXX", 5.0)]), bars_all("XXX", 100.0),
            {"XXX": 100.0})
    pos = r["book"]["positions"][0]
    assert pos["shares"] == 100          # 10,000 budget (cash covers costs)
    assert pos["target_weight"] == 0.1


def test_refill_uses_the_slots_own_exit_proceeds_carry_forward():
    bk = held_book({"AAA": (100.0, 200)}, cash=10.0)
    bk.slot_proceeds[0] = None
    bars = {**bars_all("AAA", 100.0, **{"10:15": flat(99, 89.0),
                                        "11:15": flat(80.0)}),
            **bars_all("BBB", 50.0)}
    r = run(bk, plan({"AAA": 90.0}, ranked=[("BBB", 1.0)]), bars,
            {"AAA": 100.0, "BBB": 50.0})
    proceeds = 200 * 80.0 - costs.sell_cost(80.0, 200)
    pos = r["book"]["positions"][0]
    assert pos["symbol"] == "BBB" and pos["entry_label"] == "12:15"
    assert pos["shares"] * 50.0 <= proceeds + 10.0
    assert pos["shares"] >= int(proceeds / 50.0) - 1


def test_symbol_that_exited_today_is_never_reentered_today():
    bk = held_book({"AAA": (100.0, 10)})
    bars = bars_all("AAA", 100.0, **{"10:15": flat(99, 89.0)})
    r = run(bk, plan({"AAA": 90.0}, ranked=[("AAA", 1.0)]), bars,
            {"AAA": 100.0})
    assert r["book"]["positions"][0] is None
    assert any("sits in cash" in n for n in r["notes"])


def test_ranked_list_is_frozen_never_reranked():
    bk = held_book({"AAA": (100.0, 10)})
    bars = {**bars_all("AAA", 100.0, **{"10:15": flat(99, 89.0)}),
            **bars_all("LOW", 10.0), **bars_all("HIGH", 10.0)}
    r = run(bk, plan({"AAA": 90.0}, ranked=[("LOW", 1.0), ("HIGH", 1.0)]),
            bars, {"AAA": 100.0, "LOW": 10.0, "HIGH": 10.0})
    assert r["book"]["positions"][0]["symbol"] == "LOW"   # list order wins


# ---------------------------------------------------------------- §5 frozen

def test_trailing_state_never_moves_during_the_session():
    bk = held_book({"AAA": (100.0, 10)})
    bk.positions[0].trail_active = True
    bk.positions[0].highest_close = 120.0
    bk.positions[0].trail_stop = 105.0
    bars = bars_all("AAA", 150.0)
    r = run(bk, plan({"AAA": 105.0}), bars, {"AAA": 150.0})
    p0 = r["book"]["positions"][0]
    assert p0["highest_close"] == 120.0 and p0["trail_stop"] == 105.0


def test_the_frozen_stop_not_the_positions_own_is_what_is_checked():
    bk = held_book({"AAA": (100.0, 10)})          # own initial stop = 90
    bars = bars_all("AAA", 100.0, **{"10:15": flat(99, 94.0)})
    r = run(bk, plan({"AAA": 95.0}), bars, {"AAA": 100.0})
    assert r["trades"][0]["meta"]["stop"] == 95.0


# ------------------------------------------------------------------- §8

def test_missing_bar_is_checked_on_last_known_price_never_skipped():
    bk = held_book({"AAA": (100.0, 10)})
    b = bars_all("AAA", 100.0, **{"10:15": flat(99, 89.0)})["AAA"]
    del b["11:15"]                                  # bar never arrived
    b["12:15"] = flat(85.0)
    r = run(bk, plan({"AAA": 90.0}), {"AAA": b}, {"AAA": 100.0})
    # 10:15 close 89 triggers; fill due 11:15 open -> no print -> held
    t = r["trades"][0]
    assert t["exit_price"] == 85.0 and t["meta"]["halt_delay_bars"] == 1
    assert any("exit held open" in a["msg"] for a in r["alerts"])


def test_stale_carry_forward_price_still_triggers_and_is_tagged():
    bk = held_book({"AAA": (100.0, 10)})
    b = {"09:15": flat(100, 89.5)}                 # then silence all day
    r = run(bk, plan({"AAA": 90.0}), {"AAA": b}, {"AAA": 100.0})
    t = [e for e in r["events"] if e["kind"] == "trigger"]
    assert t and t[0]["label"] == "09:15" and t[0]["stale"] is False
    # Now: a bar missing BEFORE the breach; carried price is below the stop
    b2 = {"09:15": flat(100, 91.0), "10:15": flat(91, 89.0)}
    bk2 = held_book({"AAA": (100.0, 10)})
    r2 = run(bk2, plan({"AAA": 89.5}), {"AAA": b2}, {"AAA": 100.0})
    trig = [e for e in r2["events"] if e["kind"] == "trigger"]
    assert trig[0]["label"] == "10:15" and trig[0]["stale"] is False
    assert [e for e in r2["events"] if e["kind"] == "trigger"][0]["price"] == 89.0
    b3 = {"09:15": flat(100, 89.0)}
    bk3 = held_book({"AAA": (100.0, 10)})
    r3 = run(bk3, plan({"AAA": 88.0}), {"AAA": b3}, {"AAA": 100.0})
    assert not r3["trades"]
    assert sum("unavailable" in a["msg"] for a in r3["alerts"]) == 6


def test_stale_price_below_stop_triggers_with_stale_tag():
    bk = held_book({"AAA": (100.0, 10)})
    r = run(bk, plan({"AAA": 95.0}, prev={"AAA": 94.0}), {"AAA": {}}, {})
    trig = [e for e in r["events"] if e["kind"] == "trigger"]
    assert trig and trig[0]["stale"] is True and trig[0]["label"] == "09:15"


def test_stale_plan_holds_stops_but_takes_no_entries_or_refills():
    bk = held_book({"AAA": (100.0, 10)})
    bars = {**bars_all("AAA", 100.0, **{"10:15": flat(99, 89.0)}),
            **bars_all("BBB", 10.0)}
    r = run(bk, plan({"AAA": 90.0}, ranked=[("BBB", 1.0)], fresh=False), bars,
            {"AAA": 100.0, "BBB": 10.0})
    assert r["trades"] and r["trades"][0]["symbol"] == "AAA"
    assert not [e for e in r["events"] if e["kind"] == "entry"]


# --------------------------------------------------------- live / replay

def test_in_progress_replay_waits_for_open_prints_and_matches_final():
    bk = held_book({"AAA": (100.0, 10)})
    full = bars_all("AAA", 100.0, **{"10:15": flat(99, 89.0),
                                     "11:15": flat(88.0)})
    pl = plan({"AAA": 90.0})
    # 11:15 bar has started but no AAA print yet
    partial = {"AAA": {k: v for k, v in full["AAA"].items()
                       if k in ("09:15", "10:15")}}
    r = run(bk, pl, partial, {"AAA": 100.0}, closed=2, cur=2)
    assert r["trades"] == [] and "waiting" in (r["waiting"] or "")
    r_final = run(bk, pl, full, {"AAA": 100.0})
    assert r_final["trades"][0]["exit_price"] == 88.0


def test_progress_boundaries():
    s = lambda hm: ID._sec(hm)
    assert ID.progress(s("09:00")) == (0, None)
    assert ID.progress(s("09:15")) == (0, 0)
    assert ID.progress(s("10:15") + 5) == (0, 0)           # grace
    assert ID.progress(s("10:15") + 10) == (1, 1)
    assert ID.progress(s("15:30") + 10) == (7, None)
    assert ID.slot_index(s("15:29") + 59) == 6
    assert ID.slot_index(s("15:30")) is None
    assert ID.slot_index(s("09:14") + 59) is None
