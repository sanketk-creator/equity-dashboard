"""
Strategies 2 and 3 — spec §16 formula, calendar, cost and execution tests.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.s23 import costs, indicators as I
from backend.s23.book import Book, Position, execute
from backend.s23.calendar import CalendarGap, NSECalendar
from backend.s23 import universe as U

N = 420
C = [100 + i * 0.25 + 6 * math.sin(i / 9) + 2 * math.cos(i / 4) for i in range(N)]
H = [x * 1.013 for x in C]
L = [x * 0.988 for x in C]
V = [1000 + (i % 37) * 40 for i in range(N)]
GOLD = {"sma": 180.3116589795065, "adv": 318906.89144883456,
        "vol252": 0.05611955338508057, "mom": 0.45517969011293835,
        "atr": 5.106341759003529, "atr_sma": 5.184173908099928}


# ------------------------------------------------------------- §16.1 formulas

def test_indicators_match_pandas_goldens():
    assert abs(I.sma(C, 200) - GOLD["sma"]) < 1e-9
    assert abs(I.adv(C, V, 126) - GOLD["adv"]) < 1e-6
    assert abs(I.realised_vol(C, 252) - GOLD["vol252"]) < 1e-12
    assert abs(I.mom_12_1(C) - GOLD["mom"]) < 1e-12
    assert abs(I.atr_wilder(H, L, C, 14) - GOLD["atr"]) < 1e-9


def test_wilder_atr_differs_from_a_14_bar_sma_of_tr():
    assert abs(I.atr_wilder(H, L, C, 14) - GOLD["atr_sma"]) > 0.05


def test_short_windows_are_none_never_short_computed():
    assert I.sma(C[:199], 200) is None
    assert I.mom_12_1(C[:252]) is None and I.mom_12_1(C[:253]) is not None
    assert I.realised_vol(C[:252], 252) is None          # needs 253 bars
    assert I.realised_vol(C[:253], 252) is not None
    assert I.adv(C[:125], V[:125], 126) is None
    assert I.atr_wilder(H[:13], L[:13], C[:13], 14) is None


def test_adv_follows_the_backtest_code_missing_invalid_zero_counts():
    """(close*volume).rolling(126, min_periods=126).mean(): a missing volume
    voids the window; a zero-volume day counts as zero turnover."""
    v = list(V)
    v[-5] = None
    assert I.adv(C, v, 126) is None
    v[-5] = 0
    want = (sum(c * x for c, x in zip(C[-126:], V[-126:])) - C[-5] * V[-5]) / 126
    assert abs(I.adv(C, v, 126) - want) < 1e-6


def test_sector_zscore_reproduces_the_spec_worked_example():
    """§6.4: population stdev across the 8 sectors."""
    rows = {"Realty": (.55, .21, 1.26, .78), "Pharma": (.42, .14, 1.18, .92),
            "Metal": (.35, .11, 1.14, .62), "Auto": (.28, .06, 1.09, .85),
            "Media": (.31, .08, 1.11, .55), "IT": (.18, .02, 1.04, .81),
            "Energy": (.05, -.05, .97, .48), "FMCG": (-.02, -.09, .95, .40)}
    want = {"Realty": (1.61, 1.63, 1.71, .58, 1.39), "Pharma": (.88, .87, .90, 1.37, 1.00),
            "Metal": (.48, .54, .49, -.32, .30), "Auto": (.08, .00, -.03, .98, .26),
            "Media": (.25, .22, .18, -.71, -.02), "IT": (-.48, -.43, -.54, .75, -.17),
            "Energy": (-1.22, -1.19, -1.25, -1.11, -1.19),
            "FMCG": (-1.61, -1.63, -1.46, -1.56, -1.56)}
    z = [I.zscores_population({s: v[k] for s, v in rows.items()}) for k in range(4)]
    for s, w in want.items():
        got = [round(z[k][s], 2) for k in range(4)]
        assert got == pytest.approx(list(w[:4]), abs=0.011), s
        assert round(sum(z[k][s] for k in range(4)) / 4, 2) == pytest.approx(w[4], abs=0.011)
    # the walked arithmetic in the spec: Realty z1
    m1 = [v[0] for v in rows.values()]
    mean = sum(m1) / 8
    sd = math.sqrt(sum((x - mean) ** 2 for x in m1) / 8)       # ddof = 0
    assert round(mean, 3) == 0.265 and round(sd, 3) == 0.177


# ---------------------------------------------------------------- §9.5 costs

def test_costs_reproduce_the_spec_worked_examples():
    assert round(costs.buy_cost(100.0, 100, "2026-11-02"), 2) == 15.39
    assert round(costs.sell_cost(100.0, 100, "2026-11-02"), 2) == 33.89


def test_exchange_fee_switches_on_2024_09_01():
    a = costs.itemise("BUY", 100.0, 100, "2024-08-30")["exchange"]
    b = costs.itemise("BUY", 100.0, 100, "2024-09-01")["exchange"]
    assert abs(a - 0.325) < 1e-9 and abs(b - 0.297) < 1e-9


def test_dp_is_flat_per_sell_including_tiny_trims():
    assert costs.itemise("SELL", 10.0, 1, "2026-11-02")["dp"] == 20.0
    assert costs.itemise("BUY", 10.0, 1, "2026-11-02")["dp"] == 0.0


# -------------------------------------------------------------- calendar

def test_calendar_muhurat_and_week_month_ends():
    cal = NSECalendar.seed()
    assert cal.is_session("2026-11-08")               # Sunday Muhurat
    assert not cal.is_session("2026-11-10")           # Balipratipada
    assert not cal.is_session("2026-11-07")           # Saturday
    assert cal.is_last_session_of_iso_week("2026-11-08")
    assert not cal.is_last_session_of_iso_week("2026-11-06")
    assert cal.next_session("2026-11-08") == "2026-11-09"
    assert cal.next_session("2026-11-09") == "2026-11-11"
    # 2 Oct (Fri) holiday: Thursday 1 Oct ends that ISO week
    assert cal.is_last_session_of_iso_week("2026-10-01")
    # month ends from the calendar, not the 30th
    assert cal.is_last_session_of_month("2026-10-30")
    assert cal.is_last_session_of_month("2026-11-30")
    assert cal.is_last_session_of_month("2026-12-31")
    assert not cal.is_last_session_of_month("2026-12-30")


def test_calendar_refuses_beyond_coverage_and_reminds():
    cal = NSECalendar.seed()
    with pytest.raises(CalendarGap):
        cal.is_session("2027-01-01")
    with pytest.raises(CalendarGap):
        cal.is_last_session_of_month("2026-12-31") and cal.next_session("2026-12-31")
    assert cal.reminder("2026-11-30") is None
    assert "2027" in cal.reminder("2026-12-15")
    assert "MISSING" in cal.reminder("2027-01-02")


# -------------------------------------------------------------- universe

def test_seed_universe_and_sector_map_are_consistent():
    syms = U.seed_symbols()
    assert len(syms) == 500 and len(set(syms)) == 500
    smap = U.seed_sector_map()
    assert len(smap) == 208
    counts = {}
    for s, sec in smap.items():
        counts[sec] = counts.get(sec, 0) + 1
    assert counts == {"Auto": 40, "Pharma": 44, "Energy": 38, "FMCG": 27,
                      "IT": 24, "Metal": 20, "Realty": 11, "Media": 4}
    assert set(smap) <= set(syms)
    assert not (set(smap) & U.EXCLUDED)
    assert len([s for s in syms if s not in U.EXCLUDED]) == 492


def test_monthly_upload_diff_reports_joins_leaves_and_sector_moves():
    current = {s: {"active": 1, "sector": None, "industry": None}
               for s in U.seed_symbols()}
    current["INFY"].update(sector="IT", industry="Information Technology")
    lines = ["Company Name,Industry,Symbol,Series,ISIN Code"]
    for s in U.seed_symbols():
        if s in ("ZEEL",):
            continue                                   # left
        ind = "Information Technology" if s != "INFY" else "Healthcare"
        lines.append(f"Co {s},{ind},{s},EQ,INE000")
    lines.append("New Co,Power,NEWPOWER,EQ,INE999")
    d = U.diff_upload(U.parse_upload("\n".join(lines)), current)
    assert d["left"] == ["ZEEL"]
    assert d["joined"][0]["symbol"] == "NEWPOWER" and d["joined"][0]["sector"] == "Energy"
    assert {"symbol": "INFY", "from": "Information Technology",
            "to": "Healthcare"} in d["industry_changes"]


def test_upload_rejects_a_wrong_file():
    with pytest.raises(ValueError):
        U.parse_upload("a,b\n1,2\n")


# --------------------------------------------------------------- execution

def _pos(sym, shares, px, group="AGG"):
    return Position(symbol=sym, group=group, shares=shares,
                    entry_signal_date="2026-10-01", entry_fill_date="2026-10-02",
                    entry_ref_price=px, entry_price=px, atr_entry=1.0,
                    invested=shares * px)


def test_reset_sells_before_buys_trims_largest_buy_and_rounds_down():
    b = Book(100_000, 0.0, {"AAA": _pos("AAA", 100, 500.0),
                            "BBB": _pos("BBB", 100, 500.0)})
    dec = {"signal_date": "2026-11-02", "reset": True,
           "exits": [{"symbol": "AAA", "reason": "STOCK_RANK"}],
           "entries": [{"symbol": "CCC", "group": "AGG", "atr_entry": 2.0,
                        "ref_price": 99.0, "initial_build": False}],
           "weights": {"BBB": 0.5, "CCC": 0.5}}
    r = execute(b, dec, {"AAA": 500.0, "BBB": 500.0, "CCC": 99.0}, {}, "2026-11-03")
    sides = [f["side"] for f in r["fills"]]
    assert sides == sorted(sides, key=lambda x: 0 if x == "SELL" else 1)
    assert r["E"] == 100_000.0                        # pre-trade mark
    # BBB target floor(0.5*100000/500)=100 -> unchanged; CCC floor(50000/99)=505
    # but cash after AAA's sale can't cover 505 + costs: trimmed 1 at a time
    ccc = [f for f in r["fills"] if f["symbol"] == "CCC"][0]
    assert ccc["shares"] <= 505 and b.cash >= -1e-9
    assert any("trimmed CCC" in n for n in r["notes"])
    assert b.positions["CCC"].entry_price == 99.0


def test_trims_pay_dp_and_topups_keep_entry_price_and_stop():
    p = _pos("AAA", 200, 100.0)
    p.stop_level, p.armed = 90.0, True
    b = Book(100_000, 10_000.0, {"AAA": p, "BBB": _pos("BBB", 10, 100.0)})
    dec = {"signal_date": "2026-11-02", "reset": True, "exits": [],
           "entries": [], "weights": {"AAA": 0.05, "BBB": 0.5}}
    r = execute(b, dec, {"AAA": 120.0, "BBB": 120.0}, {}, "2026-11-03")
    trim = [f for f in r["fills"] if f["reason"] == "RESET_TRIM"][0]
    top = [f for f in r["fills"] if f["reason"] == "RESET_TOPUP"][0]
    assert trim["symbol"] == "AAA" and trim["costs"]["dp"] == 20.0
    assert top["symbol"] == "BBB"
    assert b.positions["BBB"].entry_price == 100.0
    assert b.positions["AAA"].stop_level == 90.0 and b.positions["AAA"].armed


def test_zero_share_slot_is_selected_and_recorded():
    b = Book(100_000, 1_000.0, {})
    dec = {"signal_date": "2026-11-02", "reset": True, "exits": [],
           "entries": [{"symbol": "MRF", "group": "AGG", "atr_entry": 900.0,
                        "ref_price": 150000.0, "initial_build": True}],
           "weights": {"MRF": 0.05}}
    r = execute(b, dec, {"MRF": 150000.0}, {}, "2026-11-03")
    assert b.positions["MRF"].shares == 0
    assert r["fills"][0]["reason"] == "ZERO_SHARE_SLOT"
    assert b.cash == 1_000.0


def test_missing_open_holds_the_order_pending_no_price():
    b = Book(100_000, 0.0, {"AAA": _pos("AAA", 10, 100.0)})
    dec = {"signal_date": "2026-11-02", "reset": True,
           "exits": [{"symbol": "AAA", "reason": "STOP_TRAIL"}],
           "entries": [{"symbol": "BBB", "group": "AGG", "atr_entry": 1.0,
                        "ref_price": 50.0, "initial_build": False}],
           "weights": {"BBB": 0.05}}
    r = execute(b, dec, {}, {"AAA": 99.0}, "2026-11-03")
    assert {p["symbol"] for p in r["pending"]} == {"AAA", "BBB"}
    assert all(p["status"] == "PENDING_NO_PRICE" for p in r["pending"])
    assert "AAA" in b.positions and "BBB" not in b.positions


def test_trade_pnl_counts_trims_and_costs_exactly():
    b = Book(100_000, 0.0, {"AAA": _pos("AAA", 100, 100.0)})
    b.positions["AAA"].invested = 10_015.39
    b.positions["AAA"].realised = 1_000.0
    r = execute(b, {"signal_date": "2026-11-02", "reset": False,
                    "exits": [{"symbol": "AAA", "reason": "STOCK_RANK"}],
                    "entries": [], "weights": {}},
                {"AAA": 110.0}, {}, "2026-11-03")
    t = r["trades"][0]
    proceeds = 100 * 110.0 - costs.sell_cost(110.0, 100, "2026-11-03")
    assert abs(t["net_pnl"] - (1_000.0 + proceeds - 10_015.39)) < 1e-9


def test_zero_share_member_later_affordable_opens_a_new_trade():
    """Backtest behaviour: a zero-share member has no trade; when a later reset
    can buy it, that is a NEW entry at that open with that signal's ATR."""
    p = _pos("MRF", 0, 150000.0)
    p.atr_entry = 900.0
    b = Book(2_000_000, 2_000_000.0, {"MRF": p})
    dec = {"signal_date": "2026-12-04", "reset": True, "exits": [], "entries": [],
           "weights": {"MRF": 0.10}, "restart_atr": {"MRF": 1200.0}}
    r = execute(b, dec, {"MRF": 140000.0}, {}, "2026-12-07")
    q = b.positions["MRF"]
    assert q.shares == 1 and q.entry_price == 140000.0 and q.atr_entry == 1200.0
    assert q.entry_fill_date == "2026-12-07" and r["fills"][0]["reason"] == "ENTRY"


def test_zero_share_member_leaving_has_no_sell_and_no_trade():
    b = Book(100_000, 1_000.0, {"MRF": _pos("MRF", 0, 150000.0)})
    r = execute(b, {"signal_date": "2026-12-04", "reset": True,
                    "exits": [{"symbol": "MRF", "reason": "STOCK_RANK"}],
                    "entries": [], "weights": {}}, {"MRF": 150000.0}, {}, "2026-12-07")
    assert r["fills"] == [] and r["trades"] == [] and "MRF" not in b.positions


def test_reset_that_sizes_a_holding_to_zero_closes_its_trade():
    """Backtest: target 0 at a reset sells everything and the trade ends
    (RESET_TO_ZERO); the stock stays a zero-share member."""
    p = _pos("MRF", 1, 100000.0)
    p.invested = 100015.39
    b = Book(200_000, 50_000.0, {"MRF": p, "AAA": _pos("AAA", 100, 100.0)})
    r = execute(b, {"signal_date": "2026-12-04", "reset": True, "exits": [],
                    "entries": [], "weights": {"MRF": 0.05, "AAA": 0.5}},
                {"MRF": 160000.0, "AAA": 100.0}, {}, "2026-12-07")
    t = r["trades"][0]
    assert t["exit_reason"] == "RESET_TO_ZERO" and t["quantity"] == 1
    assert b.positions["MRF"].shares == 0 and b.positions["MRF"].invested == 0
    assert abs(t["net_pnl"] - (160000.0 - costs.sell_cost(160000.0, 1, "2026-12-07")
                               - 100015.39)) < 1e-6


def test_liquidity_cut_is_strictly_above_the_linear_20th_percentile():
    from backend.s23.hub import liquidity_pass
    vals = {"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0, "e": 1.0, "f": 5.0,
            "z": 0.0, "n": None}                     # zero and missing dropped
    ok, cut = liquidity_pass(vals)
    assert cut == 1.0 and ok == {"f"}               # 1.0 itself does not pass
    ok, cut = liquidity_pass({"a": 10.0, "b": 20.0, "c": 30.0, "d": 40.0, "e": 50.0})
    assert abs(cut - 18.0) < 1e-12 and ok == {"b", "c", "d", "e"}   # linear


def test_liquidity_cut_matches_pandas_quantile():
    pd = pytest.importorskip("pandas")
    from backend.s23.hub import liquidity_pass
    x = {f"s{i}": float(i * i % 37 + 1) for i in range(1, 60)}
    ok, cut = liquidity_pass(x)
    s = pd.Series(x)
    assert abs(cut - s.quantile(0.2)) < 1e-12 and ok == set(s[s > s.quantile(0.2)].index)


def test_cash_short_trims_the_largest_buy_by_value():
    b = Book(100_000, 10_000.0, {})
    dec = {"signal_date": "2026-11-02", "reset": True, "exits": [],
           "entries": [{"symbol": "BIG", "group": "AGG", "atr_entry": 1.0, "ref_price": 1.0},
                       {"symbol": "SML", "group": "AGG", "atr_entry": 1.0, "ref_price": 1.0}],
           "weights": {"BIG": 0.6, "SML": 0.4}}
    r = execute(b, dec, {"BIG": 100.0, "SML": 100.0}, {}, "2026-11-03")
    # targets 60 and 40 = Rs 10,000 + costs > Rs 10,000 cash -> trim BIG
    assert b.positions["SML"].shares == 40 and b.positions["BIG"].shares < 60


def test_pasted_list_from_excel_is_tab_separated_and_parses_the_same():
    """IT blocks file upload, so the list is pasted. From Excel the columns
    arrive TAB-separated (company names may contain commas); from Notepad
    they arrive comma-separated. Both must give identical rows."""
    syms = U.seed_symbols()
    comma = ["Company Name,Industry,Symbol,Series,ISIN Code"] + \
        [f'"Co {s}, Ltd.",Capital Goods,{s},EQ,INE{i:06d}' for i, s in enumerate(syms)]
    tab = ["Company Name\tIndustry\tSymbol\tSeries\tISIN Code"] + \
        [f"Co {s}, Ltd.\tCapital Goods\t{s}\tEQ\tINE{i:06d}" for i, s in enumerate(syms)]
    a = U.parse_upload("\r\n".join(comma) + "\r\n")
    b = U.parse_upload("﻿" + "\r\n".join(tab) + "\r\n\r\n")
    assert a == b and a["format"] == "ind_nifty500list" and len(a["rows"]) == 500
    assert a["rows"]["INFY"]["company"] == "Co INFY, Ltd."
