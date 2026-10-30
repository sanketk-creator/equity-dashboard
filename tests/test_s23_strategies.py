"""
Strategy 2 (Barbell 20) and Strategy 3 (Sector 12) decision rules — spec
§4, §5, §9, §16.2, §16.4.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.s23 import barbell as B, sector as S
from backend.s23.book import Position


def pos(sym, group, entry=100.0, atr=5.0, shares=10):
    return Position(symbol=sym, group=group, shares=shares,
                    entry_signal_date="2026-09-01", entry_fill_date="2026-09-02",
                    entry_ref_price=entry, entry_price=entry, atr_entry=atr)


# ============================================================ Strategy 2

def test_trailing_stop_reproduces_the_spec_worked_example():
    """§9.3 path (trail part): entry 500, ATR 15, constant ATR."""
    p = pos("X", "AGG", entry=500.0, atr=15.0)
    path = [(500, None, False), (508, None, False), (518, 473, False),
            (560, 515, False), (540, 515, False), (600, 555, False),
            (552, 555, True)]
    for close, want_stop, want_trig in path:
        info = B.update_trailing(p, close, 15.0)
        assert info["stop"] == want_stop
        assert info["triggered"] is want_trig


def test_no_initial_stop_and_no_trigger_on_the_arming_close():
    p = pos("X", "AGG", entry=100.0, atr=5.0)
    assert B.update_trailing(p, 60.0, 5.0)["triggered"] is False   # no stop yet
    info = B.update_trailing(p, 105.0, 5.0)            # arms: stop 90
    assert info["armed"] and info["stop"] == 90.0 and not info["triggered"]


def test_trail_uses_current_atr_and_highest_close_and_never_falls():
    p = pos("X", "AGG", entry=100.0, atr=5.0)
    B.update_trailing(p, 110.0, 5.0)                    # stop 95
    B.update_trailing(p, 111.0, 2.0)                    # 111-6 = 105
    assert p.stop_level == 105.0
    B.update_trailing(p, 108.0, 1.0)                    # peak 111 -> 108 cand
    assert p.stop_level == 108.0 and p.peak_close == 111.0
    B.update_trailing(p, 109.0, 9.0)                    # cand 84: no fall
    assert p.stop_level == 108.0


def _m(n_agg=30, n_def=30):
    """Universe: A01..A30 high momentum, D01..D30 low volatility."""
    m = {}
    for i in range(1, n_agg + 1):
        m[f"A{i:02d}"] = {"has_bar": True, "close": 110.0, "sma200": 100.0,
                          "adv": 1e7, "vol": 0.50 + i / 100, "mom": 1.0 - i / 100,
                          "atr": 2.0}
    for i in range(1, n_def + 1):
        m[f"D{i:02d}"] = {"has_bar": True, "close": 110.0, "sma200": 100.0,
                          "adv": 1e7, "vol": 0.10 + i / 1000, "mom": 0.01 + i / 10000,
                          "atr": 2.0}
    return m


def test_initial_build_fills_8_aggressive_and_12_defensive_exclusively():
    m = _m()
    u = set(m)
    r = B.evaluate("2026-11-06", m, {}, u, set(), u, False, False, True)
    agg = [e["symbol"] for e in r["entries"] if e["group"] == "AGG"]
    dfn = [e["symbol"] for e in r["entries"] if e["group"] == "DEF"]
    assert agg == [f"A{i:02d}" for i in range(1, 9)]
    assert dfn == [f"D{i:02d}" for i in range(1, 13)]
    assert not set(agg) & set(dfn)
    assert all(abs(w - 0.05) < 1e-12 for w in r["weights"].values())
    assert len(r["weights"]) == 20 and all(e["initial_build"] for e in r["entries"])


def test_rank_exit_only_on_the_sleeves_check_day():
    m = _m()
    u = set(m)
    held = {s: pos(s, "AGG") for s in [f"A{i:02d}" for i in range(1, 8)] + ["A25"]}
    r = B.evaluate("2026-11-04", m, held, u, set(), u, False, False, False)
    assert r["exits"] == [] and r["entries"] == []        # midweek: nothing
    r = B.evaluate("2026-11-08", m, held, u, set(), u, True, False, False)
    assert [e["symbol"] for e in r["exits"]] == ["A25"]   # rank 25 > 20
    assert [e["symbol"] for e in r["entries"]] == ["A08"]


def test_held_stock_failing_eligibility_is_not_sold():
    m = _m()
    u = set(m)
    m["A03"]["close"] = 90.0                               # below SMA200
    held = {"A03": pos("A03", "AGG")}
    r = B.evaluate("2026-11-08", m, held, u, set(), u, True, False, False)
    assert "A03" not in [e["symbol"] for e in r["exits"]]


def test_stop_wins_over_rank_and_both_are_recorded():
    m = _m()
    u = set(m)
    p = pos("A25", "AGG")
    p.stop_level, p.armed, p.peak_close = 120.0, True, 125.0
    r = B.evaluate("2026-11-08", m, {"A25": p}, u, set(), u, True, False, False)
    e = [x for x in r["exits"] if x["symbol"] == "A25"][0]
    assert e["reason"] == "STOP_TRAIL" and e["also_triggered"] == "STOCK_RANK"


def test_midweek_stop_refills_that_seat_from_the_same_close_blocking_the_stock():
    m = _m()
    u = set(m)
    held = {s: pos(s, "AGG") for s in [f"A{i:02d}" for i in range(2, 10)]}
    held["A02"].stop_level, held["A02"].armed = 120.0, True
    r = B.evaluate("2026-11-04", m, held, u, set(), u, False, False, False)
    assert [e["symbol"] for e in r["exits"]] == ["A02"]
    # seat refilled from today's AGG ranking: A01 (best not held), never A02
    assert [e["symbol"] for e in r["entries"]] == ["A01"]
    assert r["reset"] is True and len(r["weights"]) == 8


def test_defensive_pool_excludes_aggressive_holdings_and_same_day_entries():
    m = _m()
    u = set(m)
    # make D01 the best momentum name too: aggressive takes it first
    m["D01"]["mom"] = 5.0
    r = B.evaluate("2026-10-30", m, {}, u, set(), u, True, True, True)
    agg = {e["symbol"] for e in r["entries"] if e["group"] == "AGG"}
    dfn = {e["symbol"] for e in r["entries"] if e["group"] == "DEF"}
    assert "D01" in agg and "D01" not in dfn


def test_ties_break_alphabetically_and_repeatably():
    m = _m()
    for s in ("A05", "A06"):
        m[s]["mom"] = 0.5
    u = set(m)
    r1 = B.evaluate("2026-11-06", m, {}, u, set(), u, False, False, True)
    r2 = B.evaluate("2026-11-06", m, {}, u, set(), u, False, False, True)
    assert [e["symbol"] for e in r1["entries"]] == [e["symbol"] for e in r2["entries"]]


def test_excluded_and_illiquid_never_enter():
    m = _m()
    u = set(m)
    r = B.evaluate("2026-11-06", m, {}, u, {"A01"}, u - {"A02"}, False, False, True)
    names = {e["symbol"] for e in r["entries"]}
    assert "A01" not in names and "A02" not in names


# ============================================================ Strategy 3

def ranking(order, above=None):
    above = above or {}
    rows = []
    for i, s in enumerate(order):
        rows.append({"sector": s, "score": 10 - i, "rank": i + 1,
                     "above_sma200": above.get(s, True), "m1": 0, "m2": 0,
                     "m3": 1, "m4": 0, "z1": 0, "z2": 0, "z3": 0, "z4": 0,
                     "close_vs_sma200": 1.0})
    return rows


SECS = ["Pharma", "Media", "Metal", "Realty", "Energy", "Auto", "IT", "FMCG"]


def stocks(per=12):
    m, smap = {}, {}
    for sec in SECS:
        for i in range(1, per + 1):
            s = f"{sec[:3].upper()}{i:02d}"
            m[s] = {"has_bar": True, "close": 110.0, "sma200": 100.0,
                    "mom": 1.0 - i / 100, "adv": 1e7, "vol": 0.3, "rows": 300}
            smap[s] = sec
    return m, smap


def test_spec_worked_example_auto_leaves_media_takes_the_seat():
    """§5.8: seats Pharma(1) Metal(3) Auto(6) -> Auto leaves; Media (rank 2)
    takes the seat."""
    order = ["Pharma", "Media", "Metal", "Realty", "Energy", "Auto", "IT", "FMCG"]
    m, smap = stocks()
    held = {}
    for sec, pre in (("Pharma", "PHA"), ("Metal", "MET"), ("Auto", "AUT")):
        for i in range(1, 5):
            held[f"{pre}{i:02d}"] = pos(f"{pre}{i:02d}", sec)
    u = set(m)
    r = S.evaluate("2026-11-08", ranking(order), ["Pharma", "Metal", "Auto"],
                   m, smap, held, u, set(), u, False)
    assert r["leaving"] == ["Auto"] and r["joining"] == ["Media"]
    assert {e["symbol"] for e in r["exits"]} == {f"AUT{i:02d}" for i in range(1, 5)}
    assert all(e["reason"] == "SECTOR_EXIT" for e in r["exits"])
    assert {e["symbol"] for e in r["entries"]} == {f"MED{i:02d}" for i in range(1, 5)}
    # weights by ORDER among held: Pharma 50, Media 33, Metal 17
    assert abs(r["weights"]["PHA01"] - 0.125) < 1e-12
    assert abs(r["weights"]["MED01"] - 0.0825) < 1e-12
    assert abs(r["weights"]["MET01"] - 0.0425) < 1e-12


def test_rank_4_stays_rank_5_leaves():
    m, smap = stocks()
    u = set(m)
    held = {"REA01": pos("REA01", "Realty")}
    r = S.evaluate("d", ranking(SECS), ["Realty"], m, smap, held, u, set(), u, False)
    assert "Realty" not in r["leaving"]                  # rank 4
    order = ["Pharma", "Media", "Metal", "Energy", "Realty", "Auto", "IT", "FMCG"]
    r = S.evaluate("d", ranking(order), ["Realty"], m, smap, held, u, set(), u, False)
    assert r["leaving"] == ["Realty"]                    # rank 5


def test_seated_sector_below_sma200_stays_unseated_below_cannot_enter():
    m, smap = stocks()
    u = set(m)
    r = S.evaluate("d", ranking(SECS, above={"Metal": False, "Media": False}),
                   ["Metal"], m, smap, {}, u, set(), u, False)
    assert "Metal" in r["seated_after"]                  # stays below SMA
    assert "Media" not in r["seated_after"]              # can't enter below
    assert r["joining"] == ["Pharma", "Realty"]


def test_no_swapping_when_seats_are_full():
    """§5.9.1 state: Pharma 1 / Metal 3 / Realty 4 seated; Media (2) waits."""
    m, smap = stocks()
    u = set(m)
    held = {}
    for sec, pre in (("Pharma", "PHA"), ("Metal", "MET"), ("Realty", "REA")):
        for i in range(1, 5):
            held[f"{pre}{i:02d}"] = pos(f"{pre}{i:02d}", sec)
    r = S.evaluate("2026-10-16", ranking(SECS, above={"Metal": False}),
                   ["Pharma", "Metal", "Realty"], m, smap, held, u, set(), u, False)
    assert r["reset"] is False and r["exits"] == [] and r["entries"] == []
    assert r["weights"] == {}                            # nothing traded


def test_stock_rank_worse_than_8_exits_and_is_replaced_by_the_best():
    m, smap = stocks()
    u = set(m)
    held = {s: pos(s, "Pharma") for s in ("PHA01", "PHA02", "PHA03", "PHA10")}
    r = S.evaluate("d", ranking(SECS), ["Pharma", "Media", "Metal"], m, smap,
                   held, u, set(), u, False)
    assert [e["symbol"] for e in r["exits"]] == ["PHA10"]
    assert r["stock_ranks"]["PHA10"] == 10
    assert "PHA04" in {e["symbol"] for e in r["entries"]}


def test_held_stock_failing_stock_eligibility_is_not_sold():
    m, smap = stocks()
    u = set(m)
    m["PHA02"]["close"] = 90.0                            # below SMA200
    held = {s: pos(s, "Pharma") for s in ("PHA01", "PHA02", "PHA03", "PHA04")}
    r = S.evaluate("d", ranking(SECS), ["Pharma", "Media", "Metal"], m, smap,
                   held, u, set(), u, False)
    assert "PHA02" not in {e["symbol"] for e in r["exits"]}


def test_fewer_qualifying_sectors_leaves_the_seat_in_cash():
    m, smap = stocks()
    u = set(m)
    r = S.evaluate("d", ranking(SECS, above={s: False for s in SECS[2:]}), [],
                   m, smap, {}, u, set(), u, True)
    assert r["seated_after"] == ["Pharma", "Media"]
    total = sum(r["weights"].values())
    assert abs(total - 0.83) < 1e-9                       # 17% stays in cash


def test_sector_measures_and_ranking_math():
    import math
    n = 400
    idx = [(f"d{i:04d}", 100 + i * 0.3 + 5 * math.sin(i / 11)) for i in range(n)]
    n500 = [(d, 200 + i * 0.1) for i, (d, _) in enumerate(idx)]
    mm = S.sector_measures(idx, n500)
    closes = [c for _, c in idx]
    assert abs(mm["m1"] - (closes[-22] / closes[-253] - 1)) < 1e-12
    want_m2 = (closes[-1] / closes[-127] - 1) - (n500[-1][1] / n500[-127][1] - 1)
    assert abs(mm["m2"] - want_m2) < 1e-12
    assert 0.0 <= mm["m4"] <= 1.0
    assert S.sector_measures(idx[:324], n500[:324]) is None   # m4 needs 325
    assert S.sector_measures(idx[:325], n500[:325]) is not None


def test_m2_uses_nifty500_own_bars_and_needs_its_bar_on_the_day():
    """sector_rotation.index_indicators: NIFTY 500's 6m return on ITS OWN
    126 bars (a gap shifts the anchor), matched by date, never forward-filled."""
    import math
    idx = [(f"d{i:04d}", 100 + i * 0.3 + 5 * math.sin(i / 11)) for i in range(400)]
    full = [(d, 200 + i * 0.1) for i, (d, _) in enumerate(idx)]
    gapped = [x for x in full if x[0] != "d0350"]          # one N500 gap
    mm = S.sector_measures(idx, gapped)
    closes = [c for _, c in idx]
    b6 = gapped[-1][1] / gapped[-127][1] - 1               # 126 OWN bars back
    assert abs(mm["m2"] - ((closes[-1] / closes[-127] - 1) - b6)) < 1e-12
    assert gapped[-127][0] == "d0272"                      # anchor moved a day
    assert S.sector_measures(idx, full[:-1]) is None       # no N500 bar today


def test_a_rank_5_to_8_sector_can_take_a_free_seat():
    """No rank cutoff on entry (sector_rotation.decide walks the whole list)."""
    m, smap = stocks()
    u = set(m)
    above = {s: (s in ("Pharma", "Energy")) for s in SECS}  # Energy is rank 5
    r = S.evaluate("d", ranking(SECS, above=above), [], m, smap, {}, u, set(),
                   u, True)
    assert r["seated_after"] == ["Pharma", "Energy"]


def test_a_falling_seated_sector_is_repicked_when_seats_stay_free():
    """Realty seated, falls to rank 5; every better unseated sector is below
    its SMA200, so the walk reaches Realty (above) and it stays."""
    m, smap = stocks()
    u = set(m)
    order = ["Pharma", "Media", "Metal", "Energy", "Realty", "Auto", "IT", "FMCG"]
    above = {s: s in ("Pharma", "Realty") for s in order}
    held = {f"REA{i:02d}": pos(f"REA{i:02d}", "Realty") for i in range(1, 5)}
    r = S.evaluate("d", ranking(order, above=above), ["Realty"], m, smap, held,
                   u, set(), u, False)
    assert "Realty" in r["seated_after"] and r["repicked"] == ["Realty"]
    assert not [e for e in r["exits"] if e["reason"] == "SECTOR_EXIT"]


def test_zero_share_member_has_no_stop():
    m = _m()
    u = set(m)
    p = pos("A01", "AGG", shares=0)
    p.stop_level, p.armed = 500.0, True                 # would trigger if checked
    r = B.evaluate("2026-11-04", m, {"A01": p}, u, set(), u, False, False, False)
    assert r["exits"] == []


def test_stopped_name_is_excluded_from_the_rank_pool_that_close():
    """decide_c: pool = (eligible - other sleeve) - stopped today, so another
    holding's rank improves by one when a better name is stopped."""
    m = _m()
    u = set(m)
    held = {"A01": pos("A01", "AGG"), "A21": pos("A21", "AGG")}
    held["A01"].stop_level, held["A01"].armed = 120.0, True   # stops today
    r = B.evaluate("2026-11-08", m, held, u, set(), u, True, False, False)
    assert r["ranks"]["A21"]["rank"] == 20                    # not 21
    assert "A21" not in {e["symbol"] for e in r["exits"]}


def test_trigger_compares_with_the_previous_close_level_not_todays_update():
    p = pos("X", "AGG", entry=100.0, atr=5.0)
    p.armed, p.peak_close, p.stop_level = True, 120.0, 100.0
    info = B.update_trailing(p, 112.0, 2.0)        # today's level 120-6 = 114
    assert info["stop"] == 114.0 and info["triggered"] is False   # 112 > 100
    assert B.update_trailing(p, 113.0, 2.0)["triggered"] is True  # 113 <= 114


def test_aggressive_rank_exactly_21_exits_rank_20_stays():
    m = _m()
    u = set(m)
    held = {s: pos(s, "AGG") for s in ("A20", "A21")}
    r = B.evaluate("2026-11-08", m, held, u, set(), u, True, False, False)
    ex = {e["symbol"] for e in r["exits"]}
    assert r["ranks"]["A21"]["rank"] == 21 and "A21" in ex
    assert r["ranks"]["A20"]["rank"] == 20 and "A20" not in ex


def test_sector_stock_without_252_day_vol_is_not_eligible():
    m, smap = stocks()
    u = set(m)
    m["PHA01"]["vol"] = None
    r = S.evaluate("d", ranking(SECS), [], m, smap, {}, u, set(), u, True)
    assert "PHA01" not in {e["symbol"] for e in r["entries"]}
