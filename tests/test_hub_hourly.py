"""
Hub-level lifecycle of the hourly-exit live book next to the daily-rule
shadow book, on a real SQLite file and with NO Dhan call anywhere:

  go-live (nothing earlier simulated) -> fresh entries at the authoritative
  daily-bar open -> an hourly breach that recovers by the close (live exits,
  shadow holds, daily_rule says HOLD) -> restart -> a late freeze replayed
  exactly -> a 15:15-stub trigger carried to the next open with its
  daily-rule record.
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config as cfg, intraday as ID

SYMS = (("TCS", 0.6), ("INFY", 0.5), ("SBIN", 0.4), ("ITC", 0.3),
        ("LT", 0.2), ("HDFCBANK", 0.1))


def _weekdays(n, start=date(2025, 8, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _bar(px, vol=1500):
    return {"o": round(px * 0.999, 2), "h": round(px * 1.01, 2),
            "l": round(px * 0.99, 2), "c": round(px, 2),
            "volume": vol, "volume_adj": vol}


def _flat_hourly(px):
    return [(lab, px, px, px, px, 5) for lab in ID.LABELS]


def _rows(sym, spec):
    return [(sym, lab, o, h, l, c, n) for lab, o, h, l, c, n in spec]


def _at(d: str, hm: str) -> datetime:
    h, m = map(int, hm.split(":"))
    return datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=cfg.IST)


def _clock(monkeypatch):
    from backend import hub as H
    c = {"now": _at("2025-08-01", "08:00")}
    monkeypatch.setattr(H, "now_ist", lambda: c["now"])
    return c


def test_hourly_live_book_lifecycle_against_the_shadow_book(monkeypatch):
    from backend.hub import EquityHub
    clock = _clock(monkeypatch)
    old = cfg.DB_PATH
    cfg.DB_PATH = os.path.join(tempfile.mkdtemp(), "life.db")
    loop = asyncio.new_event_loop()
    run = loop.run_until_complete
    try:
        h = EquityHub()
        run(h._seed_constituents())
        for c in run(h.store.constituents()):
            run(h.store.upsert_constituent(
                c["symbol"], active=1 if c["symbol"] in dict(SYMS) else 0))
        dates = _weekdays(306)
        hist, S1, S2, S3, S4, S5 = dates[:-5], *dates[-5:]
        G = hist[-1]
        px = {s: 1000.0 for s, _ in SYMS}
        lines = ["symbol,date,o,h,l,c,volume,volume_adj"]
        for i, d in enumerate(hist):
            for s, drift in SYMS:
                px[s] += drift
                vol = 1000 + (500 if i >= len(hist) - 4 else 0)
                b = _bar(px[s] + 3 * math.sin(i / 3), vol)   # same wiggle for all
                lines.append(f"{s},{d},{b['o']},{b['h']},{b['l']},{b['c']},{vol},{vol}")

        # ---- GO-LIVE: one freeze, nothing earlier simulated ------------------
        clock["now"] = _at(S1, "08:00")          # deployed before S1's open
        run(h.import_bars_csv("\n".join(lines)))
        assert h.status["first_session"] == S1
        assert h.status["go_live"] == G and h.status["last_eod"] == G
        assert len(run(h.store.equity_curve())) == 1
        assert run(h.store.trades()) == []
        assert h.plan["fresh"] and h.plan["ranked"]
        ranked = [r["symbol"] for r in h.plan["ranked"]]

        # ---- S1: fresh entries at the AUTHORITATIVE daily-bar open ----------
        for s, _ in SYMS:
            run(h.store.upsert_hourly(S1, _rows(s, _flat_hourly(px[s]))))
            run(h.store.set_session_open(S1, s, px[s] + 3.0, "rest"))  # differs
            run(h.store.upsert_bars(s, [{"date": S1, **_bar(px[s])}]))
        run(h.store.upsert_bars("__NIFTY50__", [{"date": S1, **_bar(20000)}]))
        clock["now"] = _at(S2, "08:00")
        run(h.run_daily(fetch=False))
        assert h.status["last_eod"] == S1
        live = {p.symbol: p for p in h.book.positions if p}
        shadow = {p.symbol: p for p in h.shadow_book.positions if p}
        assert set(live) == set(ranked) == set(shadow)
        for s in live:
            assert live[s].entry_price == round(px[s] * 0.999, 2)   # daily open
            assert live[s].entry_price == shadow[s].entry_price
            assert live[s].entry_label == "09:15"

        # ---- S2: hourly breach that RECOVERS by the close ---------------------
        X = ranked[0]
        stop = h.plan["stops"][X]
        Z = ranked[1]                       # closes above entry + 1 ATR today
        zpos = live[Z]
        zclose = round(zpos.entry_price + 2 * zpos.entry_atr, 2)
        for s, _ in SYMS:
            if s == X:
                spec = _flat_hourly(px[s])
                spec[2] = ("11:15", px[s], px[s], stop - 1, stop - 1, 5)
                spec[3] = ("12:15", stop - 2, stop + 9, stop - 2, stop + 9, 5)
                spec[4:] = [(lab, stop + 9, stop + 9, stop + 9, stop + 9, 5)
                            for lab in ID.LABELS[4:]]
                run(h.store.upsert_hourly(S2, _rows(s, spec)))
                run(h.store.upsert_bars(s, [{"date": S2, **_bar(stop + 9), "o": px[s]}]))
            elif s == Z:
                run(h.store.upsert_hourly(S2, _rows(s, _flat_hourly(px[s]))))
                run(h.store.upsert_bars(s, [{"date": S2, **_bar(zclose),
                                             "o": px[s]}]))
            else:
                run(h.store.upsert_hourly(S2, _rows(s, _flat_hourly(px[s]))))
                run(h.store.upsert_bars(s, [{"date": S2, **_bar(px[s])}]))
        run(h.store.upsert_bars("__NIFTY50__", [{"date": S2, **_bar(20010)}]))
        clock["now"] = _at(S3, "08:00")
        run(h.run_daily(fetch=False))
        assert h.status["last_eod"] == S2
        lt = run(h.store.trades(book="live"))
        assert len(lt) == 1 and lt[0]["symbol"] == X
        m = lt[0]["meta"]
        assert lt[0]["exit_price"] == round(stop - 2, 4) or \
            abs(lt[0]["exit_price"] - (stop - 2)) < 1e-6
        assert m["trigger_label"] == "11:15" and m["exit_basis"] == "hourly"
        assert m["trigger_timestamp"] == f"{S2} 12:15"
        assert m["daily_rule"]["would_exit"] is False       # close recovered
        assert run(h.store.trades(book="shadow")) == []      # daily rule holds
        assert X in h.shadow_book.held_symbols()
        assert X not in h.book.held_symbols()
        curve = run(h.store.equity_curve())
        assert curve[-1]["shadow_equity"] is not None
        # the live trail ratcheted ONCE, from S2's daily close
        zp = [q for q in h.book.positions if q and q.symbol == Z][0]
        atr = h.scored[Z]["atr14"]
        assert zp.trail_active and zp.highest_close == zclose
        assert abs(zp.trail_stop - (zclose - 3 * atr)) < 1e-9
        assert h.plan["stops"][Z] == round(zp.effective_stop, 4)
        assert h.plan["stops"][Z] > zp.initial_stop

        # ---- restart: state round-trips ---------------------------------------
        h2 = EquityHub()
        run(h2._restore_state())
        assert h2.plan == h.plan and h2.book.to_record() == h.book.to_record()
        assert h2.shadow_book.to_record() == h.shadow_book.to_record()
        h.store.close()
        h = h2

        # ---- S3 recorded, S3 daily bars LATE; S4 recorded --------------------
        Y = [s for s in ranked if s != X][0]
        ystop = h.plan["stops"][Y]
        for s, _ in SYMS:
            spec = _flat_hourly(px[s])
            if s == Y:            # breach ONLY in the 15:15 closing window
                spec[6] = ("15:15", px[s], px[s], ystop - 1, ystop - 1, 5)
            run(h.store.upsert_hourly(S3, _rows(s, spec)))
            run(h.store.upsert_hourly(S4, _rows(s, _flat_hourly(px[s]))))
        run(h.refresh_live_view(_at(S4, "15:40")))
        assert [r["date"] for r in h.live_chain] == [S3, S4]
        assert h.live_chain[0]["plan_fresh"] is True
        assert h.live_chain[1]["plan_fresh"] is False          # stale ranks
        assert not [e for e in h.live_chain[1]["events"] if e["kind"] == "entry"]
        # the carried stub exit still happens on the stale day
        assert [t["symbol"] for t in h.live_chain[1]["trades"]] == [Y]

        # S3 daily bars land late
        for s, _ in SYMS:
            b = _bar(px[s])
            if s == Y:                       # opened normally, closed below
                b = {**_bar(ystop - 1), "o": b["o"]}
            run(h.store.upsert_bars(s, [{"date": S3, **b}]))
        run(h.store.upsert_bars("__NIFTY50__", [{"date": S3, **_bar(20020)}]))
        clock["now"] = _at(S4, "15:40")
        run(h.run_daily(fetch=False))
        assert h.status["last_eod"] == S3
        carry = h.plan["carry_exits"]
        assert len(carry) == 1 and carry[0]["symbol"] == Y
        assert carry[0]["closing_window_only"] is True
        assert carry[0]["daily_rule"]["would_exit"] is True
        run(h.refresh_live_view(_at(S4, "15:40")))
        assert [r["date"] for r in h.live_chain] == [S4]
        assert h.live_chain[0]["plan_fresh"] is True

        # S4 daily bars -> commit: Y exits at S4's daily open; X may re-enter
        for s, _ in SYMS:
            run(h.store.upsert_bars(s, [{"date": S4, **_bar(px[s])}]))
        run(h.store.upsert_bars("__NIFTY50__", [{"date": S4, **_bar(20030)}]))
        clock["now"] = _at(S5, "08:00")
        run(h.run_daily(fetch=False))
        ys = [t for t in run(h.store.trades(book="live")) if t["symbol"] == Y]
        assert ys and ys[0]["exit_price"] == round(px[Y] * 0.999, 2)
        assert ys[0]["meta"]["closing_window_only"] is True
        assert ys[0]["meta"]["daily_rule"]["would_exit"] is True
        assert ys[0]["meta"]["trigger_date"] == S3
        # shadow: S3 close at/below its stop -> exits Y at S4's open too
        sy = [t for t in run(h.store.trades(book="shadow")) if t["symbol"] == Y]
        assert sy and sy[0]["exit_price"] == ys[0]["exit_price"]

        snap = run(h.snapshot())
        assert snap["comparison_summary"]["live_exits"] == 2
        assert snap["comparison_summary"]["daily_would_hold"] == 1
        assert snap["comparison_summary"]["closing_window_only"] == 1
        h.store.close()
    finally:
        loop.close()
        cfg.DB_PATH = old


def test_freeze_waits_for_every_symbol_then_force_excludes_the_missing(monkeypatch):
    from backend.hub import EquityHub
    clock = _clock(monkeypatch)
    old = cfg.DB_PATH
    cfg.DB_PATH = os.path.join(tempfile.mkdtemp(), "wait.db")
    loop = asyncio.new_event_loop()
    run = loop.run_until_complete
    try:
        h = EquityHub()
        run(h._seed_constituents())
        for c in run(h.store.constituents()):
            run(h.store.upsert_constituent(
                c["symbol"], active=1 if c["symbol"] in dict(SYMS) else 0))
        dates = _weekdays(300)
        for s, drift in SYMS:
            p, bars = 1000.0, []
            for d in dates[:-1]:
                p += drift
                bars.append({"date": d, **_bar(p)})
            run(h.store.upsert_bars(s, bars))
        run(h.store.upsert_bars("__NIFTY50__",
                                [{"date": d, **_bar(20000)} for d in dates]))
        # benchmark knows the last session; no symbol has it yet
        run(h.store.upsert_bars("TCS", [{"date": dates[-1], **_bar(1200)}]))
        clock["now"] = _at(dates[-1], "20:00")
        run(h.run_daily(fetch=False))
        assert h.status["last_eod"] is None
        st = h.status["daily"]
        assert st["state"] == "waiting for daily bars"
        assert st["target"] == dates[-1]
        assert st["missing"] == ["HDFCBANK", "INFY", "ITC", "LT", "SBIN"]
        out = run(h.force_freeze_now(dates[-1]))
        assert out["excluded"] == ["HDFCBANK", "INFY", "ITC", "LT", "SBIN"]
        assert h.status["last_eod"] == dates[-1]
        assert h.plan["excluded_missing"] == out["excluded"]
        assert all(r["symbol"] == "TCS" for r in h.plan["ranked"])
        h.store.close()
    finally:
        loop.close()
        cfg.DB_PATH = old


def test_feed_ticks_reach_the_store_and_survive_a_restart(monkeypatch):
    from backend.hub import EquityHub
    from backend.feed import Tick
    from backend import recorder as R
    old = cfg.DB_PATH
    cfg.DB_PATH = os.path.join(tempfile.mkdtemp(), "ticks.db")
    loop = asyncio.new_event_loop()
    run = loop.run_until_complete
    try:
        h = EquityHub()
        h.symbol_ids = {"TCS": "11536"}
        h.id_to_symbol = {"11536": "TCS"}
        D = "2026-11-02"
        h.recorder.reset(D)
        clock = {"t": _at(D, "09:15").timestamp() + 2}
        monkeypatch.setattr(R.time, "time", lambda: clock["t"])
        h._on_tick(Tick(11536, "NSE_EQ", "quote", ltp=4000.0, open=3990.0))
        clock["t"] += 1800
        h._on_tick(Tick(11536, "NSE_EQ", "ticker", ltp=4010.0))
        h._on_tick(Tick(999, "NSE_EQ", "ticker", ltp=1.0))         # unknown id
        h._on_tick(Tick(11536, "NSE_EQ", "prev_close", prev_close=3900.0))
        run(h._flush_recorder())
        bars = run(h.store.hourly_for_date(D))
        assert bars == {"TCS": {"09:15": {"o": 4000.0, "h": 4010.0, "l": 4000.0,
                                          "c": 4010.0, "n": 2}}}
        assert run(h.store.session_opens(D)) == {"TCS": (3990.0, "feed")}
        h.store.close()
        h2 = EquityHub()
        run(h2._session_dates(_at(D, "10:00")))
        assert run(h2.store.hourly_dates_after("1970-01-01")) == [D]
        h2.store.close()
    finally:
        loop.close()
        cfg.DB_PATH = old


def _history_hub(name, n_extra, monkeypatch):
    from backend.hub import EquityHub
    clock = _clock(monkeypatch)
    cfg.DB_PATH = os.path.join(tempfile.mkdtemp(), name)
    h = EquityHub()
    loop = asyncio.new_event_loop()
    run = loop.run_until_complete
    run(h._seed_constituents())
    for c in run(h.store.constituents()):
        run(h.store.upsert_constituent(
            c["symbol"], active=1 if c["symbol"] in dict(SYMS) else 0))
    dates = _weekdays(301 + n_extra)
    hist = dates[:-n_extra]
    for s, drift in SYMS:
        p, bars = 1000.0, []
        for i, d in enumerate(hist):
            p += drift
            vol = 1000 + (500 if i >= len(hist) - 4 else 0)
            bars.append({"date": d, **_bar(p + 3 * math.sin(i / 3), vol)})
        run(h.store.upsert_bars(s, bars))
    run(h.store.upsert_bars("__NIFTY50__",
                            [{"date": d, **_bar(20000)} for d in hist]))
    return h, loop, run, clock, hist, dates[-n_extra:]


def test_evening_deploy_waits_for_the_day_it_missed_then_goes_live(monkeypatch):
    """Deployed after S1's session; Dhan (T-1) still ends at G. Going live at
    G would make S1 — never watched — the first traded session. So it waits
    for S1's bar; go-live = S1; first traded session = S2."""
    old = cfg.DB_PATH
    h, loop, run, clock, hist, (S1, S2) = _history_hub("eve.db", 2, monkeypatch)
    try:
        clock["now"] = _at(S1, "19:00")
        run(h.run_daily(fetch=False))
        assert h.status["go_live"] is None and h.plan is None
        assert h.status["daily"]["state"] == "waiting to go live"
        for s, _ in SYMS:
            run(h.store.upsert_bars(s, [{"date": S1, **_bar(1300)}]))
        run(h.store.upsert_bars("__NIFTY50__", [{"date": S1, **_bar(20000)}]))
        clock["now"] = _at(S2, "07:00")
        run(h.run_daily(fetch=False))
        assert h.status["go_live"] == S1 and h.status["first_session"] == S2
        assert run(h.store.trades()) == []
        h.store.close()
    finally:
        loop.close()
        cfg.DB_PATH = old


def test_forced_go_live_still_never_trades_an_unwatched_session(monkeypatch):
    """Operator forces go-live at G after S1 started: S1 is re-frozen only —
    neither book trades it; the first traded session is S2."""
    old = cfg.DB_PATH
    h, loop, run, clock, hist, (S1, S2) = _history_hub("force2.db", 2, monkeypatch)
    try:
        clock["now"] = _at(S1, "19:00")
        run(h.force_freeze_now(hist[-1]))
        assert h.status["go_live"] == hist[-1]
        assert h.status["first_session"] == S2
        assert h.plan["ranked"]
        # the feed recorded part of S1 (deployed mid-session): not replayed
        run(h.store.upsert_hourly(S1, _rows("TCS", _flat_hourly(1300.0))))
        h.status["last_eod"] = hist[-1]
        assert S1 not in run(h._session_dates(_at(S1, "19:30")))
        for s, _ in SYMS:
            run(h.store.upsert_bars(s, [{"date": S1, **_bar(1300)}]))
        run(h.store.upsert_bars("__NIFTY50__", [{"date": S1, **_bar(20000)}]))
        clock["now"] = _at(S2, "07:00")
        run(h.run_daily(fetch=False))
        assert h.status["last_eod"] == S1
        assert run(h.store.trades()) == []
        assert not h.book.held_symbols() and not h.shadow_book.held_symbols()
        assert h.plan["signal_date"] == S1 and h.plan["fresh"]
        h.store.close()
    finally:
        loop.close()
        cfg.DB_PATH = old


def test_go_live_waits_when_the_history_is_too_short(monkeypatch):
    from backend.hub import EquityHub
    clock = _clock(monkeypatch)
    old = cfg.DB_PATH
    cfg.DB_PATH = os.path.join(tempfile.mkdtemp(), "short.db")
    loop = asyncio.new_event_loop()
    run = loop.run_until_complete
    try:
        h = EquityHub()
        run(h._seed_constituents())
        for c in run(h.store.constituents()):
            run(h.store.upsert_constituent(
                c["symbol"], active=1 if c["symbol"] in dict(SYMS) else 0))
        dates = _weekdays(12)                  # Dhan returned only 12 days
        for s, _ in SYMS:
            run(h.store.upsert_bars(s, [{"date": d, **_bar(1000)} for d in dates]))
        run(h.store.upsert_bars("__NIFTY50__",
                                [{"date": d, **_bar(20000)} for d in dates]))
        clock["now"] = _at(dates[-1], "20:00")
        run(h.run_daily(fetch=False))
        assert h.status["go_live"] is None and h.plan is None
        assert h.status["daily"]["state"] == "waiting for history"
        run(h.force_freeze_now(dates[-1]))        # operator's call
        assert h.status["go_live"] == dates[-1]
        h.store.close()
    finally:
        loop.close()
        cfg.DB_PATH = old
