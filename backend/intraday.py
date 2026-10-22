"""
The HOURLY-EXIT session engine — pure logic, no I/O.

Implements "Equity 100 Momentum — Hourly Exit Specification" v1.0
(21 Oct 2026), with the operator's decisions of 22 Oct 2026:

  * Signals are daily, execution is intraday. Everything that decides WHAT
    to hold (score, rank, ATR, stop levels) is frozen at day T's close and is
    a read-only constant for the whole of T+1. Nothing is recomputed on a
    partial bar.
  * Hourly bars are labelled at their open:
        09:15 10:15 11:15 12:15 13:15 14:15 [15:15 stub, 15:15-15:30]
  * On each bar CLOSE (never the low) every open position is checked against
    its frozen stop. A close AT OR BELOW the stop triggers (operator
    decision: same inequality as the methodology's daily rule).
  * Trigger -> fill at the NEXT bar's open. Opening at-or-below the stop ->
    exit at that 09:15 open. A trigger on the last bar (15:15 stub) fills at
    the next day's open and is flagged closing-window-only (the historical
    feed has no 15:15 bar from 2026-08-03; the backtest cannot see it).
  * A freed slot refills at the next hourly bar's open AFTER the exit fill,
    from the ranked list frozen at the prior close; a gap exit at 09:15
    therefore refills at 10:15 (operator decision); an exit filled on the
    last bar refills at the next day's open. A symbol that exited today is
    never re-entered today. Nothing eligible -> the slot sits in cash.
  * Slots empty at the start of the session take the fresh daily entries at
    the 09:15 open.
  * Missing bar: the check is NEVER skipped — the last known price is
    carried forward, the check runs, and a data-health alert is raised.
    A halted stock (no print in the fill bar) keeps its exit open and fills
    at the next bar that trades; the delay is logged.

The whole session is a deterministic REPLAY of (start-of-session book,
frozen plan, recorded bars). The hub re-runs it every few seconds while the
session is live and once more, with the authoritative daily-bar open, when
the session is committed. A restart therefore loses nothing that was
recorded, and a late daily freeze produces exactly the fills an on-time one
would have.
"""
from __future__ import annotations

import copy
import math

from . import costs
from .engine import Book, Position, _entry_budget, stop_reason

SLOTS: tuple[tuple[str, str], ...] = (
    ("09:15", "10:15"), ("10:15", "11:15"), ("11:15", "12:15"),
    ("12:15", "13:15"), ("13:15", "14:15"), ("14:15", "15:15"),
    ("15:15", "15:30"))
LABELS: tuple[str, ...] = tuple(a for a, _ in SLOTS)
LAST = len(SLOTS) - 1          # the 15:15 stub


def _sec(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 3600 + int(m) * 60


SLOT_SECS = tuple((_sec(a), _sec(b)) for a, b in SLOTS)
SESSION_START, SESSION_END = SLOT_SECS[0][0], SLOT_SECS[-1][1]


def slot_index(sec_of_day: float) -> int | None:
    """Which hourly bar a trade at this IST time-of-day belongs to."""
    for i, (a, b) in enumerate(SLOT_SECS):
        if a <= sec_of_day < b:
            return i
    return None


def progress(sec_of_day: float, grace: float = 10.0) -> tuple[int, int | None]:
    """(closed, cur): how many bars are complete (their end + grace has
    passed — late packets for the bar get that long to arrive), and the bar
    currently forming (None before 09:15 or once all seven are closed)."""
    closed = sum(1 for _, b in SLOT_SECS if sec_of_day >= b + grace)
    if closed >= len(SLOTS):
        return len(SLOTS), None
    if sec_of_day >= SLOT_SECS[closed][0]:
        return closed, closed
    return closed, None


# ======================================================================

def replay_session(date: str, book_rec: dict, plan: dict,
                   bars: dict[str, dict[str, dict]],
                   opens: dict[str, float],
                   closed: int, cur: int | None, p: dict) -> dict:
    """
    Replay one session.

    book_rec  book at the START of the session (after the prior freeze)
    plan      frozen at the prior close:
                fresh        True when the freeze for the prior session is
                             done; False = stale (stops held from the last
                             freeze, NO entries or refills — ranks are old)
                stops        {symbol: frozen effective stop}
                ranked       [{symbol, atr, mom121, close}] best first
                carry_exits  exits triggered last session, due at 09:15
                prev_close   {symbol: prior daily close} (carry-forward base)
    bars      {symbol: {label: {o,h,l,c,n}}} — a label exists only if the
              symbol traded in that hour
    opens     {symbol: official 09:15 session open}
    closed    number of bars complete; cur = bar forming now (or None)
    """
    book = Book.from_record(copy.deepcopy(book_rec))
    fresh = bool(plan.get("fresh"))
    ranked: list[dict] = list(plan.get("ranked") or [])
    frozen = plan.get("stops") or {}
    stops: dict[int, float] = {}
    for pos in book.positions:
        if pos is not None:
            stops[pos.slot] = float(frozen.get(pos.symbol, pos.effective_stop))
    last_px: dict[str, float] = {s: float(v) for s, v in
                                 (plan.get("prev_close") or {}).items()
                                 if v is not None}
    px_label: dict[str, str] = {s: "prior close" for s in last_px}

    trades: list[dict] = []
    events: list[dict] = []
    alerts: list[dict] = []
    notes: list[str] = []
    exited_today: set[str] = set()
    exiting: set[int] = set()
    exits_due: dict[int, list[dict]] = {}
    refills_due: dict[int, list[int]] = {}
    carry_out: list[dict] = []
    waiting: str | None = None

    for e in plan.get("carry_exits") or []:
        e = dict(e)
        e["carried"] = True
        exits_due.setdefault(0, []).append(e)
        exiting.add(int(e["slot"]))

    start_empty = [i for i, pos in enumerate(book.positions) if pos is None]

    def open_px(sym: str, k: int) -> tuple[float | None, str]:
        if k == 0:
            px = opens.get(sym)
            if not px:
                b = (bars.get(sym) or {}).get(LABELS[0])
                px = b["o"] if b else None
        else:
            b = (bars.get(sym) or {}).get(LABELS[k])
            px = b["o"] if b else None
        if px is not None and px > 0:
            return float(px), "ok"
        return None, ("unavailable" if k < closed else "pending")

    def fill_slots(slots: list[int], k: int, why: str) -> str | None:
        """Fill each slot from the frozen ranked list at bar k's open.
        Returns a 'waiting' message if a candidate's open isn't known yet."""
        label = LABELS[k]
        for slot in sorted(slots):
            if book.positions[slot] is not None:
                continue
            if not fresh:
                notes.append(f"{label}: slot {slot + 1} left in cash — daily "
                             f"freeze not done, ranks are stale")
                continue
            filled = False
            for cand in ranked:
                s = cand["symbol"]
                if s in book.held_symbols() or s in exited_today:
                    continue
                px, st = open_px(s, k)
                if st == "pending":
                    return f"waiting for {s} {label} open print"
                if st == "unavailable":
                    notes.append(f"{label}: {s} skipped — no print in the bar")
                    continue
                budget = min(_entry_budget(book, slot, last_px, p), book.cash)
                shares = math.floor(budget / px) if px > 0 else 0
                while shares > 0 and shares * px + costs.buy_cost(px, shares) > book.cash:
                    shares -= 1
                if shares <= 0:
                    notes.append(f"{label}: {s} skipped — budget ₹{budget:,.0f} "
                                 f"below one share at ₹{px:,.2f}")
                    continue
                bc = costs.buy_cost(px, shares)
                eq = book.equity(last_px)          # pre-trade equity
                book.cash -= shares * px + bc
                atr = float(cand["atr"])
                pos = Position(
                    symbol=s, slot=slot, shares=float(shares), entry_price=px,
                    entry_date=date, entry_cost=bc, entry_atr=atr,
                    initial_stop=px - float(p["atr_mult_initial"]) * atr,
                    target_weight=round(budget / eq, 4) if eq else 0.0,
                    achieved_weight=round(shares * px / eq, 4) if eq else 0.0,
                    entry_label=label)
                book.positions[slot] = pos
                stops[slot] = pos.initial_stop
                last_px.setdefault(s, px)
                events.append({"kind": "entry", "symbol": s, "slot": slot,
                               "label": label, "price": px, "shares": shares,
                               "why": why})
                filled = True
                break
            if not filled:
                notes.append(f"{label}: slot {slot + 1} sits in cash — nothing "
                             f"eligible")
        return None

    def do_exit(pos: Position, px: float, k: int, e: dict) -> None:
        sc = costs.sell_cost(px, pos.shares)
        proceeds = pos.shares * px - sc
        gross = (px - pos.entry_price) * pos.shares
        net = gross - pos.entry_cost - sc
        book.cash += proceeds
        book.slot_proceeds[pos.slot] = proceeds
        book.positions[pos.slot] = None
        stops.pop(pos.slot, None)
        exiting.discard(pos.slot)
        exited_today.add(pos.symbol)
        fill_ts = f"{date} {LABELS[k]}"
        if e["basis"] == "gap_open":
            reason = (f"{e['reason']} — opened {px:.2f} at/below stop "
                      f"{e['stop']:.2f}; filled at that open")
        else:
            where = (f"filled at the {LABELS[k]} open"
                     if e["trigger_date"] == date
                     else f"filled at the next session's {LABELS[k]} open")
            if int(e.get("halt_bars", 0)):
                where += f" (no print for {int(e['halt_bars'])} bar(s) first)"
            reason = (f"{e['reason']} — hourly close {e['trigger_price']:.2f} "
                      f"at/below stop {e['stop']:.2f} ({e['trigger_label']} bar "
                      f"{e['trigger_date']}); {where}")
        trades.append({
            "symbol": pos.symbol, "slot": pos.slot,
            "entry_date": pos.entry_date, "entry_price": pos.entry_price,
            "shares": pos.shares, "exit_date": date, "exit_price": px,
            "entry_cost": round(pos.entry_cost, 2), "exit_cost": round(sc, 2),
            "gross_pnl": round(gross, 2), "net_pnl": round(net, 2),
            "exit_reason": reason,
            "meta": {"book": "live", "exit_basis": "hourly",
                     "trigger": e["basis"],
                     "trigger_timestamp": e["trigger_ts"],
                     "fill_timestamp": fill_ts,
                     "trigger_date": e["trigger_date"],
                     "trigger_label": e["trigger_label"],
                     "trigger_price": e["trigger_price"],
                     "stop": e["stop"],
                     "stale_price": bool(e.get("stale")),
                     "closing_window_only": bool(e.get("closing_window_only")),
                     "halt_delay_bars": int(e.get("halt_bars", 0)),
                     "entry_label": pos.entry_label,
                     "daily_rule": e.get("daily_rule")}})
        events.append({"kind": "exit", "symbol": pos.symbol, "slot": pos.slot,
                       "label": LABELS[k], "price": px,
                       "trigger": e["basis"]})

    for k in range(len(SLOTS)):
        if k >= closed and k != cur:
            break
        label = LABELS[k]

        # ---- 1. exits due at this bar's open --------------------------------
        due, still = exits_due.pop(k, []), []
        for e in due:
            pos = book.positions[int(e["slot"])]
            if pos is None or pos.symbol != e["symbol"]:
                exiting.discard(int(e["slot"]))
                continue
            px, st = open_px(pos.symbol, k)
            if st == "pending":
                still.append(e)
                continue
            if st == "unavailable":
                e["halt_bars"] = int(e.get("halt_bars", 0)) + 1
                alerts.append({"level": "warn", "symbol": pos.symbol,
                               "label": label,
                               "msg": f"{pos.symbol}: no print in the {label} "
                                      f"bar — exit held open (halt / no "
                                      f"trade), delay {e['halt_bars']} bar(s)"})
                if k < LAST:
                    exits_due.setdefault(k + 1, []).append(e)
                else:
                    carry_out.append(e)
                continue
            do_exit(pos, px, k, e)
            if k < LAST:
                refills_due.setdefault(k + 1, []).append(pos.slot)
        if still:
            exits_due[k] = still
            waiting = (f"waiting for {label} open print: "
                       + ", ".join(e["symbol"] for e in still))
            break

        if k == 0:
            # ---- 2. gap check at the session open --------------------------
            pend = []
            for pos in list(book.positions):
                if pos is None or pos.slot in exiting:
                    continue
                px, st = open_px(pos.symbol, 0)
                if st == "pending":
                    pend.append(pos.symbol)
                    continue
                if st == "unavailable":
                    alerts.append({"level": "warn", "symbol": pos.symbol,
                                   "label": label,
                                   "msg": f"{pos.symbol}: no session-open "
                                          f"print — gap check impossible"})
                    continue
                if px <= stops[pos.slot]:
                    e = {"slot": pos.slot, "symbol": pos.symbol,
                         "basis": "gap_open", "reason": stop_reason(pos),
                         "stop": round(stops[pos.slot], 2),
                         "trigger_date": date, "trigger_label": LABELS[0],
                         "trigger_ts": f"{date} {LABELS[0]}",
                         "trigger_price": px}
                    events.append({"kind": "trigger", "symbol": pos.symbol,
                                   "slot": pos.slot, "label": label,
                                   "price": px, "stop": e["stop"],
                                   "basis": "gap_open"})
                    do_exit(pos, px, 0, e)
                    refills_due.setdefault(1, []).append(pos.slot)
            if pend:
                waiting = "waiting for session-open prints: " + ", ".join(pend)
                break
            for s in set(opens) | set(bars):
                px, st = open_px(s, 0)
                if st == "ok":
                    last_px[s], px_label[s] = px, "today's open"

            # ---- 3. fresh daily entries into slots empty at the start ------
            w = fill_slots(start_empty, 0, "daily signal")
            if w:
                waiting = w
                break
        else:
            # ---- 4. refills one bar after the exit fill ---------------------
            w = fill_slots(refills_due.pop(k, []), k, "refill")
            if w:
                waiting = w
                break

        if k >= closed:
            break                           # bar still forming: no close yet

        # ---- 5. the hourly close check ---------------------------------------
        for s, sb in bars.items():
            b = sb.get(label)
            if b and b.get("c"):
                last_px[s], px_label[s] = float(b["c"]), f"{label} close"
        for pos in list(book.positions):
            if pos is None or pos.slot in exiting:
                continue
            b = (bars.get(pos.symbol) or {}).get(label)
            stale = not (b and b.get("c"))
            price = float(b["c"]) if not stale else last_px.get(pos.symbol)
            if stale:
                alerts.append({"level": "warn", "symbol": pos.symbol,
                               "label": label,
                               "msg": f"{pos.symbol}: {label} bar unavailable — "
                                      f"checked against last known price "
                                      f"{price if price is not None else '—'}"
                                      f" ({px_label.get(pos.symbol, 'none')})"})
            if price is None:
                continue
            if price <= stops[pos.slot]:
                e = {"slot": pos.slot, "symbol": pos.symbol, "basis": "hourly_close",
                     "reason": stop_reason(pos),
                     "stop": round(stops[pos.slot], 2),
                     "trigger_date": date, "trigger_label": label,
                     "trigger_ts": f"{date} {SLOTS[k][1]}",
                     "trigger_price": round(price, 2), "stale": stale,
                     "closing_window_only": k == LAST}
                exiting.add(pos.slot)
                events.append({"kind": "trigger", "symbol": pos.symbol,
                               "slot": pos.slot, "label": label,
                               "price": price, "stop": e["stop"],
                               "basis": "hourly_close", "stale": stale,
                               "closing_window_only": k == LAST})
                if k < LAST:
                    exits_due.setdefault(k + 1, []).append(e)
                else:
                    carry_out.append(e)
                    alerts.append({"level": "info", "symbol": pos.symbol,
                                   "label": label,
                                   "msg": f"{pos.symbol}: triggered ONLY in the "
                                          f"15:15 closing window — the "
                                          f"backtest cannot see this bar"})

    final = closed >= len(SLOTS)
    return {
        "date": date, "final": final, "closed": closed, "cur": cur,
        "book": book.to_record(), "trades": trades, "events": events,
        "alerts": alerts, "notes": notes, "waiting": waiting,
        "carry_exits": carry_out if final else [],
        "pending_exits": [e for lst in exits_due.values() for e in lst],
        "stops": {pos.symbol: round(stops[pos.slot], 2)
                  for pos in book.positions if pos is not None
                  and pos.slot in stops},
        "marks": last_px,
    }
