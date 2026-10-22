"""
The strategy engine — pure logic, no I/O, so every convention is testable.

Since v6 this module serves two roles:
  * the SIGNAL side for both books — metrics, the four-criteria score, the
    12-1 ranking, and the once-a-day trail ratchet (ratchet_trails);
  * the complete DAILY-CLOSE rule (evaluate_close + fill_open) that the
    SHADOW book runs. The LIVE book's exits are hourly — backend/intraday.py.
The rules below describe the daily-close rule as the backtest ran it.

Replicates the methodology document's Section 12 bridge rules:
  1. Scores, ranks and stop levels are computed ONCE, from completed daily
     bars, and frozen as a Plan for the following session.
  2. Signals detected at day T's close fill at day T+1's open.
  3. Stops are checked on the daily close only — never intraday, and a
     session opening below the stop does NOT exit at that open (no open-gap
     exit; the stop still requires a close at-or-below it).
  4. Universe eligibility is whatever the constituents table said at signal
     time (point-in-time from go-live forward).
  5. Costs include the flat Rs 20 DP charge per scrip per sell.
  6. No regime filter.

Exit precedence inside a session plan: stop exits first, then refills — a
slot freed at the open may be refilled at the same open with a DIFFERENT
symbol; the symbol that exited cannot be re-entered the same day.

"Closes above the stop stays" (document §6) — therefore a close exactly AT
the stop exits.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from . import costs, indicators as I


# ================================================================= metrics

def compute_metrics(bars: list[dict], p: dict) -> dict[str, Any]:
    """All per-symbol numbers from a completed daily history (oldest first).
    Any window short of its own length yields None — NaN fails the gate."""
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    vols = [b.get("volume_adj") for b in bars]
    out = {
        "bars": len(bars),
        "close": closes[-1] if closes else None,
        "dma200": I.sma(closes, int(p["dma_window"])),
        "ret6m": I.ret_over(closes, int(p["ret6m_window"])),
        "vol": I.realised_vol(closes, int(p["vol_window"])),
        "vr": I.volume_ratio(vols, int(p["vol_avg_short"]), int(p["vol_avg_long"])),
        "mom121": I.mom_12_1(closes, int(p["mom_skip"]), int(p["mom_window"])),
        "atr14": I.atr_wilder(highs, lows, closes, int(p["atr_window"])),
    }
    rv = out["ret6m"], out["vol"]
    out["ret_vol"] = (rv[0] / rv[1]) if (rv[0] is not None and rv[1]) else None
    return out


def score_universe(metrics: dict[str, dict], p: dict) -> dict[str, dict]:
    """
    The four-criteria confidence score, cross-sectional parts ranked within
    the eligible universe THAT DAY with average-rank ties, inclusive at the
    percentile_pass boundary (Appendix A trap #1).
    """
    pct2 = I.percentile_ranks({s: m["ret6m"] for s, m in metrics.items()
                               if m["ret6m"] is not None})
    pct3 = I.percentile_ranks({s: m["ret_vol"] for s, m in metrics.items()
                               if m["ret_vol"] is not None})
    gate_pct = float(p["percentile_pass"])
    vr_min = float(p["vol_ratio_threshold"])
    out: dict[str, dict] = {}
    for s, m in metrics.items():
        c1 = (m["close"] is not None and m["dma200"] is not None
              and m["close"] > m["dma200"])
        c2 = pct2.get(s) is not None and pct2[s] >= gate_pct
        c3 = pct3.get(s) is not None and pct3[s] >= gate_pct
        c4 = m["vr"] is not None and m["vr"] > vr_min       # strict
        rankable = (m["bars"] >= int(p["min_bars_to_rank"])
                    and m["mom121"] is not None)
        out[s] = {**m, "c1": c1, "c2": c2, "c3": c3, "c4": c4,
                  "score": int(c1) + int(c2) + int(c3) + int(c4),
                  "pct_ret6m": pct2.get(s), "pct_retvol": pct3.get(s),
                  "rankable": rankable}
    return out


# ==================================================================== book

@dataclass
class Position:
    symbol: str
    slot: int
    shares: float
    entry_price: float
    entry_date: str
    entry_cost: float
    entry_atr: float
    initial_stop: float
    trail_active: bool = False
    highest_close: float | None = None
    trail_stop: float | None = None
    target_weight: float = 0.0
    achieved_weight: float = 0.0
    entry_label: str = "09:15"      # hourly book: bar whose OPEN filled it

    @property
    def effective_stop(self) -> float:
        if self.trail_active and self.trail_stop is not None:
            return max(self.initial_stop, self.trail_stop)
        return self.initial_stop

    def as_dict(self) -> dict:
        d = asdict(self)
        d["effective_stop"] = self.effective_stop
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


@dataclass
class Book:
    starting_capital: float
    max_holdings: int
    cash: float = 0.0
    positions: list[Position | None] = field(default_factory=list)
    # Carry-forward state: the rupee proceeds each slot last exited with.
    # None = the slot has never been used (first fill takes the initial
    # tranche: min(20%, 1/N) of STARTING equity).
    slot_proceeds: list[float | None] = field(default_factory=list)

    def __post_init__(self):
        if not self.positions:
            self.positions = [None] * self.max_holdings
        if not self.slot_proceeds:
            self.slot_proceeds = [None] * self.max_holdings
        if self.cash == 0.0 and all(pos is None for pos in self.positions):
            self.cash = self.starting_capital

    def held_symbols(self) -> set[str]:
        return {pos.symbol for pos in self.positions if pos is not None}

    def equity(self, closes: dict[str, float]) -> float:
        total = self.cash
        for pos in self.positions:
            if pos is not None:
                px = closes.get(pos.symbol)
                total += pos.shares * (px if px is not None else pos.entry_price)
        return total

    def to_record(self) -> dict:
        return {"starting_capital": self.starting_capital,
                "max_holdings": self.max_holdings, "cash": self.cash,
                "positions": [p.as_dict() if p else None for p in self.positions],
                "slot_proceeds": self.slot_proceeds}

    @classmethod
    def from_record(cls, d: dict) -> "Book":
        b = cls(starting_capital=d["starting_capital"],
                max_holdings=d["max_holdings"], cash=d["cash"],
                positions=[Position.from_dict(p) if p else None
                           for p in d["positions"]],
                slot_proceeds=list(d["slot_proceeds"]))
        return b


# ==================================================================== plan

def ratchet_trails(book: Book, scored: dict[str, dict], p: dict) -> None:
    """
    Day T close: trail activation and ratchet from today's COMPLETED daily
    bar (hourly spec §5: once per day, from the daily close, up only).
    Activation = first daily close at or above entry + 1 x entry ATR; level =
    highest daily close since activation - 3 x current ATR. A position with
    no bar today is left untouched.
    """
    for pos in book.positions:
        if pos is None:
            continue
        m = scored.get(pos.symbol) or {}
        close, atr = m.get("close"), m.get("atr14")
        if close is None:
            continue                       # no bar today: nothing to ratchet
        act_level = pos.entry_price + float(p["trail_activation_mult"]) * pos.entry_atr
        if not pos.trail_active and close >= act_level:
            pos.trail_active = True
            pos.highest_close = close
        if pos.trail_active:
            pos.highest_close = max(pos.highest_close or close, close)
            if atr is not None:
                cand = pos.highest_close - float(p["atr_mult_trail"]) * atr
                if pos.trail_stop is None or cand > pos.trail_stop:
                    pos.trail_stop = cand          # ratchets up only


def stop_reason(pos: Position) -> str:
    return ("Trailing stop" if pos.trail_active and pos.trail_stop is not None
            and pos.trail_stop >= pos.initial_stop else "Initial stop")


def ranked_candidates(scored: dict[str, dict], eligible: set[str],
                      p: dict) -> list[dict]:
    """Every name passing the gate THAT DAY, best 12-1 momentum first. The
    hourly book freezes this whole list for the next session's 09:15 entries
    and intraday refills (spec §6: never re-ranked during the session)."""
    names = sorted(
        (s for s, m in scored.items()
         if s in eligible and m["score"] >= int(p["score_gate"])
         and m["rankable"] and m.get("atr14") is not None),
        key=lambda s: scored[s]["mom121"], reverse=True)
    return [{"symbol": s, "mom121": round(scored[s]["mom121"], 6),
             "atr": scored[s]["atr14"], "close": scored[s]["close"]}
            for s in names]


def evaluate_close(book: Book, scored: dict[str, dict], date: str,
                   eligible: set[str], p: dict) -> dict:
    """
    DAILY-CLOSE RULE (the backtest's convention; used by the SHADOW book).
    Day T close: update trail state on today's close, queue stop exits
    (close at-or-below the effective stop), rank candidates, and freeze the
    Plan for T+1's open. Mutates only trail state; fills happen in fill_open.
    """
    ratchet_trails(book, scored, p)
    exits: list[dict] = []
    for pos in book.positions:
        if pos is None:
            continue
        close = (scored.get(pos.symbol) or {}).get("close")
        if close is None:
            continue                       # no bar today: stop cannot be judged
        if close <= pos.effective_stop:
            exits.append({"slot": pos.slot, "symbol": pos.symbol,
                          "reason": stop_reason(pos),
                          "stop": round(pos.effective_stop, 2),
                          "close": close})

    exiting = {e["symbol"] for e in exits}
    held_after = book.held_symbols() - exiting
    free = sum(1 for pos in book.positions if pos is None) + len(exits)

    candidates = sorted(
        (s for s, m in scored.items()
         if s in eligible and m["score"] >= int(p["score_gate"])
         and m["rankable"] and s not in held_after and s not in exiting),
        key=lambda s: scored[s]["mom121"], reverse=True)

    entries = [{"symbol": s, "mom121": round(scored[s]["mom121"], 6),
                "atr": scored[s]["atr14"], "close": scored[s]["close"]}
               for s in candidates[:free]
               if scored[s]["atr14"] is not None]

    return {"signal_date": date, "exits": exits, "entries": entries,
            "qualified": len(candidates), "filled": False}


def _entry_budget(book: Book, slot: int, closes: dict[str, float], p: dict) -> float:
    """Carry-forward (user decision) or re-level (the backtest's convention)."""
    if str(p["sizing_mode"]) == "re_level":
        frac = min(float(p["target_weight_cap"]), 1.0 / int(p["max_holdings"]))
        return frac * book.equity(closes)
    prior = book.slot_proceeds[slot]
    if prior is None:                       # first-ever use of this slot
        frac = min(float(p["target_weight_cap"]), 1.0 / int(p["max_holdings"]))
        return frac * book.starting_capital
    return prior


def fill_open(book: Book, plan: dict, opens: dict[str, float], fill_date: str,
              closes_for_equity: dict[str, float], p: dict) -> dict:
    """
    T+1 open: exits first (their proceeds land in their slots), then entries
    into free slots in rank order. A missing open quote skips an ENTRY
    (recorded) but never an exit — an exit with no print holds to the next
    session, loudly.
    """
    trades: list[dict] = []
    notes: list[str] = []
    held_exits: list[dict] = []

    for e in plan["exits"]:
        pos = book.positions[e["slot"]]
        if pos is None or pos.symbol != e["symbol"]:
            notes.append(f"{e['symbol']}: exit skipped — slot changed")
            continue
        px = opens.get(pos.symbol)
        if px is None or px <= 0:
            held_exits.append(e)
            notes.append(f"{pos.symbol}: no open print — exit HELD to next session")
            continue
        sc = costs.sell_cost(px, pos.shares)
        proceeds = pos.shares * px - sc
        gross = (px - pos.entry_price) * pos.shares
        net = gross - pos.entry_cost - sc
        book.cash += proceeds
        book.slot_proceeds[pos.slot] = proceeds
        book.positions[pos.slot] = None
        trades.append({"symbol": pos.symbol, "slot": pos.slot,
                       "entry_date": pos.entry_date,
                       "entry_price": pos.entry_price, "shares": pos.shares,
                       "exit_date": fill_date, "exit_price": px,
                       "entry_cost": round(pos.entry_cost, 2),
                       "exit_cost": round(sc, 2),
                       "gross_pnl": round(gross, 2), "net_pnl": round(net, 2),
                       "exit_reason": f"{e['reason']} — close {e['close']:.2f} "
                                      f"at/below stop {e['stop']:.2f}",
                       "meta": {"signal_date": plan["signal_date"]}})

    exited_today = {t["symbol"] for t in trades}
    free_slots = [i for i, pos in enumerate(book.positions) if pos is None]

    for ent in plan["entries"]:
        if not free_slots:
            break
        s = ent["symbol"]
        if s in exited_today or s in book.held_symbols():
            notes.append(f"{s}: entry skipped — same-day re-entry / duplicate")
            continue
        px = opens.get(s)
        if px is None or px <= 0:
            notes.append(f"{s}: entry skipped — no open print")
            continue
        slot = free_slots[0]
        budget = _entry_budget(book, slot, closes_for_equity, p)
        budget = min(budget, book.cash)
        shares = math.floor(budget / px)
        while shares > 0 and shares * px + costs.buy_cost(px, shares) > book.cash:
            shares -= 1
        if shares <= 0:
            notes.append(f"{s}: entry skipped — budget ₹{budget:,.0f} below one "
                         f"share at ₹{px:,.2f}")
            continue
        bc = costs.buy_cost(px, shares)
        eq = book.equity(closes_for_equity)        # pre-trade equity
        book.cash -= shares * px + bc
        pos = Position(
            symbol=s, slot=slot, shares=float(shares), entry_price=px,
            entry_date=fill_date, entry_cost=bc, entry_atr=float(ent["atr"]),
            initial_stop=px - float(p["atr_mult_initial"]) * float(ent["atr"]),
            target_weight=round(budget / eq, 4) if eq else 0.0,
            achieved_weight=round(shares * px / eq, 4) if eq else 0.0,
        )
        book.positions[slot] = pos
        free_slots.pop(0)

    plan["filled"] = True
    plan["fill_date"] = fill_date
    return {"trades": trades, "notes": notes, "held_exits": held_exits}
