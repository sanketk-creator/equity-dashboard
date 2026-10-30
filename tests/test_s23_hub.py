"""
Strategies 2 and 3 end to end on a real SQLite file, NO Dhan call:
go-live initial build -> fills at the next open -> daily stops -> weekly
decisions on the calendar week end (Muhurat Sunday) -> Strategy 3 decision
moved when sector-index bars are missing -> >1 missed session halts ->
operator catch-up -> restart round-trip.
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config as cfg
from backend.s23 import universe as U
from backend.s23.calendar import NSECalendar

CAL = NSECalendar.seed()


def sessions(start: str, end: str) -> list[str]:
    out, d = [], date.fromisoformat(start)
    while d.isoformat() <= end:
        if CAL.is_session(d.isoformat()):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DATES = sessions("2025-03-01", "2026-11-12")
SECTOR_STOCKS = {sec: [f"{sec[:3].upper()}{i}" for i in range(1, 7)]
                 for sec in U.SECTORS}
STOCKS = [s for v in SECTOR_STOCKS.values() for s in v] + \
    [f"OTH{i:02d}" for i in range(1, 13)]


def px(sym: str, i: int) -> float:
    k = sum(map(ord, sym)) % 17
    drift = 0.05 + k * 0.03
    return 100 + drift * i + 4 * math.sin(i / (5 + k % 4))


def bar(sym, i, d):
    c = px(sym, i)
    return {"date": d, "o": round(c * 0.998, 2), "h": round(c * 1.01, 2),
            "l": round(c * 0.99, 2), "c": round(c, 2),
            "volume": 10_000 + (sum(map(ord, sym)) % 9) * 3_000}


def idx_bar(name, i, d):
    k = sum(map(ord, name)) % 7
    c = 1000 + (2 + k) * i + 30 * math.sin(i / (6 + k))
    return {"date": d, "o": c, "h": c * 1.005, "l": c * 0.995, "c": c, "volume": 0}


def at(d, hm):
    h, m = map(int, hm.split(":"))
    return datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=cfg.IST)


def make_hub(monkeypatch, upto: str):
    from backend.s23 import hub as H
    cfg_dir = tempfile.mkdtemp()
    monkeypatch.setattr(cfg, "DB_PATH", os.path.join(cfg_dir, "equity.db"))
    h = H.StrategiesHub()
    loop = asyncio.new_event_loop()
    run = loop.run_until_complete
    smap = {s: sec for sec, v in SECTOR_STOCKS.items() for s in v}

    def seed(c):
        c.execute("DELETE FROM universe")
        for n, s in enumerate(STOCKS):
            c.execute("INSERT INTO universe(symbol,active,sector,security_id) "
                      "VALUES(?,?,?,?)", (s, 1, smap.get(s), str(1000 + n)))
        for n, name in enumerate(H.ALL_INDICES):
            c.execute("INSERT INTO index_map(name,security_id,source) VALUES(?,?,?)",
                      (name, str(10 + n), "test"))
    run(h.store.tx(seed))
    run(h.store.set_json("calendar", h.cal.to_record()))
    add_bars(h, run, [d for d in DATES if d <= upto])
    return h, loop, run, H


def add_bars(h, run, ds, skip_indices=False, only=None):
    from backend.s23.hub import ALL_INDICES, IDX_PREFIX
    for s in (only or STOCKS):
        run(h.store.upsert_bars(s, [bar(s, DATES.index(d), d) for d in ds]))
    if not skip_indices:
        for n in ALL_INDICES:
            run(h.store.upsert_bars(IDX_PREFIX + n,
                                    [idx_bar(n, DATES.index(d), d) for d in ds]))


def test_go_live_initial_build_fills_next_open_and_runs_the_week(monkeypatch):
    h, loop, run, H = make_hub(monkeypatch, "2026-11-02")
    clock = {"now": at("2026-11-03", "08:00")}
    monkeypatch.setattr(H, "now_ist", lambda: clock["now"])
    try:
        run(h.run_daily(fetch=False))
        for sid in ("barbell", "sector"):
            st = h.state[sid]
            assert st["status"] == "ACTIVE" and st["go_live"] == "2026-11-02"
            assert st["last_evaluated_bar_date"] == "2026-11-02"
            assert st["pending"] and st["pending"][0]["fill_date"] == "2026-11-03"
            assert st["pending"][0]["initial_build"] is True
        o = run(h.store.q("SELECT * FROM orders WHERE strategy_id='barbell'"))
        assert o and all(r["initial_build"] == 1 and r["reason"] == "ENTRY"
                         and r["signal_date"] == "2026-11-02"
                         and r["intended_fill_date"] == "2026-11-03" for r in o)
        sig = run(h.store.q("SELECT * FROM signals WHERE strategy_id='sector'"))
        assert sig and all(r["bar_status"] == "COMPLETE" for r in sig)
        seated = h.state["sector"]["seated"]
        assert 1 <= len(seated) <= 3
        # 11-03 bars arrive overnight -> fills at the 11-03 OPEN
        add_bars(h, run, ["2026-11-03"])
        clock["now"] = at("2026-11-04", "08:00")
        run(h.run_daily(fetch=False))
        bk = h.state["barbell"]["book"]
        assert bk["positions"] and h.state["barbell"]["pending"] == []
        for s, p in bk["positions"].items():
            assert p["entry_price"] == bar(s, DATES.index("2026-11-03"), "2026-11-03")["o"]
            assert p["entry_signal_date"] == "2026-11-02" and p["initial_build"]
            assert p["atr_entry"] and p["armed"] in (True, False)
        f = run(h.store.q("SELECT * FROM orders WHERE strategy_id='barbell' AND "
                          "status='FILLED'"))
        assert f and all(r["fill_timestamp"].startswith("2026-11-03") for r in f)
        cash = bk["cash"]
        assert cash >= 0
        # NAV identity on the book
        pd_ = run(h.store.q("SELECT * FROM portfolio_daily WHERE strategy_id='barbell' "
                            "AND date='2026-11-03'"))[0]
        assert abs(pd_["nav"] - (pd_["cash"] + pd_["invested_capital"])) < 1e-6
        # 11-04..11-06: midweek, no rank decisions for barbell; sector no decision
        for d, nxt in (("2026-11-04", "2026-11-05"), ("2026-11-05", "2026-11-06"),
                       ("2026-11-06", "2026-11-07")):
            add_bars(h, run, [d])
            clock["now"] = at(nxt, "08:00")
            run(h.run_daily(fetch=False))
        assert h.state["sector"]["last_evaluated_bar_date"] == "2026-11-06"
        n_scores = run(h.store.q("SELECT COUNT(DISTINCT date) n FROM sector_scores"))[0]["n"]
        assert n_scores == 1                       # only the go-live decision so far
        # Muhurat Sunday 8 Nov: stocks trade, sector indices have NO bar
        add_bars(h, run, ["2026-11-08"], skip_indices=True)
        clock["now"] = at("2026-11-09", "08:50")       # past the 08:45 deadline
        run(h.run_daily(fetch=False))
        aud = run(h.store.q("SELECT * FROM audit_log WHERE kind='decision_moved'"))
        assert aud and '"to": "2026-11-06"' in aud[0]["detail_json"]
        assert h.state["sector"]["last_evaluated_bar_date"] == "2026-11-08"
        dates = [r["date"] for r in run(h.store.q(
            "SELECT DISTINCT date FROM sector_scores ORDER BY date"))]
        assert dates == ["2026-11-02", "2026-11-06"]
        # barbell: 8 Nov IS the ISO-week's last session -> weekly check ran
        ev = run(h.store.q("SELECT detail_json FROM audit_log WHERE kind='evaluation' "
                           "AND strategy_id='barbell' AND bar_date='2026-11-08'"))
        assert ev and '"special_session": true' in ev[0]["detail_json"]
    finally:
        h.store.close()
        loop.close()


def test_more_than_one_missed_session_halts_until_operator_approves(monkeypatch):
    h, loop, run, H = make_hub(monkeypatch, "2026-11-02")
    clock = {"now": at("2026-11-03", "08:00")}
    monkeypatch.setattr(H, "now_ist", lambda: clock["now"])
    try:
        run(h.run_daily(fetch=False))
        add_bars(h, run, ["2026-11-03", "2026-11-04"])
        clock["now"] = at("2026-11-05", "08:00")       # two sessions due
        run(h.run_daily(fetch=False))
        for sid in ("barbell", "sector"):
            assert h.state[sid]["status"] == "HALTED"
            assert "sessions missed" in h.state[sid]["halt_reason"]
            assert h.state[sid]["last_evaluated_bar_date"] == "2026-11-02"
        run(h.approve_catchup())
        run(h.run_daily(fetch=False))
        for sid in ("barbell", "sector"):
            assert h.state[sid]["status"] == "ACTIVE"
            assert h.state[sid]["last_evaluated_bar_date"] == "2026-11-04"
    finally:
        h.store.close()
        loop.close()


def test_restart_restores_armed_stop_atr_and_peak(monkeypatch):
    h, loop, run, H = make_hub(monkeypatch, "2026-11-02")
    clock = {"now": at("2026-11-03", "08:00")}
    monkeypatch.setattr(H, "now_ist", lambda: clock["now"])
    try:
        run(h.run_daily(fetch=False))
        add_bars(h, run, ["2026-11-03"])
        clock["now"] = at("2026-11-04", "08:00")
        run(h.run_daily(fetch=False))
        before = {sid: h.state[sid]["book"] for sid in ("barbell", "sector")}
        h.store.close()
        h2 = H.StrategiesHub()
        run(h2.store.q("SELECT 1"))
        st = run(h2.store.get_json("state:barbell"))
        assert st["book"] == before["barbell"]
        for s, p in st["book"]["positions"].items():
            for k in ("atr_entry", "armed", "stop_level", "peak_close"):
                assert k in p
        # startup integrity: recomputed ATR matches the persisted one
        h2.state["barbell"] = {**h2.state["barbell"], **st}
        run(h2._startup_integrity())
        assert h2.state["barbell"]["status"] == "ACTIVE"
        # tamper with a bar -> recomputed ATR differs -> HALT
        sym = next(iter(st["book"]["positions"]))
        run(h2.store.x("UPDATE bars SET h=h*1.5 WHERE symbol=? AND date='2026-11-03'",
                       (sym,)))
        run(h2._startup_integrity())
        assert h2.state["barbell"]["status"] == "HALTED"
        h2.store.close()
    finally:
        loop.close()


def test_duplicate_evaluation_of_the_same_bar_is_a_no_op(monkeypatch):
    h, loop, run, H = make_hub(monkeypatch, "2026-11-02")
    clock = {"now": at("2026-11-03", "08:00")}
    monkeypatch.setattr(H, "now_ist", lambda: clock["now"])
    try:
        run(h.run_daily(fetch=False))
        n1 = run(h.store.q("SELECT COUNT(*) n FROM signals"))[0]["n"]
        run(h.run_daily(fetch=False))
        run(h.run_daily(fetch=False))
        assert run(h.store.q("SELECT COUNT(*) n FROM signals"))[0]["n"] == n1
        assert len(h.state["barbell"]["pending"]) == 1
    finally:
        h.store.close()
        loop.close()


def test_universe_upload_diff_then_apply(monkeypatch):
    h, loop, run, H = make_hub(monkeypatch, "2026-11-02")
    try:
        lines = ["Company Name,Industry,Symbol,Series,ISIN Code"]
        keep = STOCKS[1:]                                  # first one leaves
        for s in keep:
            lines.append(f"C {s},Power,{s},EQ,X")
        for i in range(440):
            lines.append(f"C Z{i},Capital Goods,Z{i},EQ,X")
        out = run(h.upload_universe("ind_nifty500list.csv", "\n".join(lines)))
        assert out["left"] == [STOCKS[0]] and len(out["joined"]) == 440
        run(h.apply_universe(out["upload_id"], None))
        rows = run(h.universe_rows())
        assert rows[STOCKS[0]]["active"] == 0
        assert rows["Z0"]["active"] == 1 and rows["Z0"]["sector"] is None
        assert rows[STOCKS[1]]["sector"] == "Energy"        # Power -> Energy
        with pytest.raises(ValueError):
            run(h.apply_universe(out["upload_id"], None))  # applied once only
    finally:
        h.store.close()
        loop.close()


def test_instrument_list_maps_equities_and_the_nine_indices_in_one_download(monkeypatch):
    from backend import creds
    h, loop, run, H = make_hub(monkeypatch, "2025-03-05")
    try:
        rows = ["SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,"
                "SEM_TRADING_SYMBOL,SEM_CUSTOM_SYMBOL,SEM_SERIES"]
        rows.append("NSE,E,2885,EQUITY,RELIANCE,Reliance,EQ")
        rows.append("NSE,E,9999,EQUITY,RELIANCE,Reliance,BL")       # wrong series
        names = {"NIFTY AUTO": 14, "NIFTY PHARMA": 32, "NIFTY ENERGY": 42, "NIFTY FMCG": 28,
                 "NIFTY IT": 29, "NIFTY METAL": 31, "NIFTY REALTY": 34, "NIFTY MEDIA": 30,
                 "NIFTY 500": 19}
        for n, i in names.items():
            rows.append(f"NSE,I,{i},INDEX,{n.replace(' ', '')},{n},")
        rows.append("NSE,I,77,INDEX,NIFTYAUTOX,NIFTY AUTO X,")          # near-miss name
        text = "\n".join(rows)

        class R:
            def __init__(self, t): self.text = t
            def raise_for_status(self): pass

        class Http:
            calls = 0
            async def get(self, url, timeout=None):
                Http.calls += 1
                return R(text)
            async def aclose(self): pass
        h.http = Http()
        def clear(c):
            c.execute("UPDATE universe SET security_id=NULL")
            c.execute("DELETE FROM index_map")
            c.execute("INSERT OR REPLACE INTO universe(symbol,active) VALUES('RELIANCE',1)")
        run(h.store.tx(clear))
        monkeypatch.setattr(creds.current, "client_id", "x")
        monkeypatch.setattr(creds.current, "access_token", "y")
        monkeypatch.setattr(type(creds.current), "ok", property(lambda self: True))
        run(h.map_symbols())
        assert Http.calls == 1
        u = run(h.universe_rows())
        assert u["RELIANCE"]["security_id"] == "2885"
        im = {r["name"]: r["security_id"] for r in run(h.store.q("SELECT * FROM index_map"))}
        assert im == {n: str(i) for n, i in names.items()}
        assert h.status["mapped"] is True     # run_daily won't map again
    finally:
        h.store.close()
        loop.close()
