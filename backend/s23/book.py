"""
Execution simulator for Strategies 2 and 3 — spec §4.4, §5.5, §8.3, §8.5,
§10.2, §10.3. Pure: no I/O.

At a fill open:
  1. E = equity at the fill open (see E_MODE below)
  2. exits sell their whole position
  3. if the decision is a reset, every surviving holding and every entry
     gets target_shares = floor(weight x E / open)  (rounded DOWN)
  4. ALL sells (exits, RESET_TRIM) before ALL buys (ENTRY, RESET_TOPUP)
  5. cash short -> trim the largest buy one share at a time
  6. an entry whose target is 0 shares is still selected: it holds 0 shares
     and its slot sits in cash (ZERO_SHARE_SLOT)
A symbol with no open print: its order is held (PENDING_NO_PRICE) and
retried at the next session, at most 3 sessions, then cancelled + alerted.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from . import costs

MAX_PENDING_SESSIONS = 3

# How E is measured at the fill open: BEFORE that open's sells, exactly as
# both backtests do (nav_open = cash + sum(shares x open, last close when a
# name has no open) in factor_test.simulate_f and sector_rotation.simulate).
E_MODE = "pre_trade"


@dataclass
class Position:
    symbol: str
    group: str                       # Barbell: "AGG"/"DEF"; Sector: sector name
    shares: int
    entry_signal_date: str
    entry_fill_date: str
    entry_ref_price: float           # close of the signal bar
    entry_price: float               # fill open (kept through top-ups)
    atr_entry: float | None          # ATR(14) on the signal close
    armed: bool = False
    stop_level: float | None = None
    peak_close: float | None = None
    invested: float = 0.0            # rupees paid incl. buy costs
    realised: float = 0.0            # net rupees received from trims
    initial_build: bool = False
    target_weight: float = 0.0
    last_atr: float | None = None    # ATR at the last evaluation (restart check)
    costs_paid: float = 0.0          # every charge on this position's fills
    dp_paid: float = 0.0
    last_eval_date: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Book:
    starting_capital: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    def to_record(self) -> dict:
        return {"starting_capital": self.starting_capital, "cash": self.cash,
                "positions": {s: p.as_dict() for s, p in self.positions.items()}}

    @classmethod
    def from_record(cls, r: dict) -> "Book":
        return cls(r["starting_capital"], r["cash"],
                   {s: Position.from_dict(p) for s, p in (r.get("positions") or {}).items()})

    def nav(self, prices: dict[str, float]) -> float:
        tot = self.cash
        for p in self.positions.values():
            px = prices.get(p.symbol)
            tot += p.shares * (px if px is not None else p.entry_price)
        return tot


def execute(book: Book, decision: dict, opens: dict[str, float],
            fallback_prices: dict[str, float], fill_date: str,
            slippage_bps: float = 0.0) -> dict:
    """
    decision = {signal_date, reset: bool,
                exits:   [{symbol, reason, also_triggered?, signal_id?}],
                entries: [{symbol, group, atr_entry, ref_price,
                           initial_build, signal_id?}],
                weights: {symbol: target weight for EVERY holding after the
                          decision, entries included}}
    Returns {fills, trades, pending, notes, E}.
    """
    fills: list[dict] = []
    trades: list[dict] = []
    pending: list[dict] = []
    notes: list[str] = []

    def px_of(s):
        v = opens.get(s)
        return v if v is not None and v > 0 else None

    # 1. equity at the fill open, before any trade
    e_marks = {}
    for s, p in book.positions.items():
        e_marks[s] = px_of(s) if px_of(s) is not None else fallback_prices.get(s, p.entry_price)
        if px_of(s) is None:
            notes.append(f"{s}: no open print — marked at last close for E")
    E = book.cash + sum(p.shares * e_marks[s] for s, p in book.positions.items())

    def fill(side, sym, shares, price, reason, extra=None):
        it = costs.itemise(side, price, shares, fill_date)
        slip = price * shares * slippage_bps / 1e4
        f = {"side": side, "symbol": sym, "shares": int(shares), "price": price,
             "reason": reason, "fill_date": fill_date, "costs": it,
             "slippage_assumed": round(slip, 4), **(extra or {})}
        fills.append(f)
        p = book.positions.get(sym)
        if p is not None:
            p.costs_paid += it["total"]
            p.dp_paid += it["dp"]
        return it["total"]

    exiting = {e["symbol"]: e for e in decision.get("exits", [])}

    # 2. exits — whole position
    for sym, e in exiting.items():
        p = book.positions.get(sym)
        if p is None:
            continue
        if p.shares == 0:
            # a zero-share member leaving: nothing to sell and, as in the
            # backtests, no trade record (it never held shares)
            del book.positions[sym]
            continue
        px = px_of(sym)
        if px is None:
            pending.append({"side": "SELL", "symbol": sym, "reason": e["reason"],
                            "status": "PENDING_NO_PRICE", **e})
            continue
        c = fill("SELL", sym, p.shares, px, e["reason"],
                 {"also_triggered": e.get("also_triggered"),
                  "signal_id": e.get("signal_id")}) if p.shares > 0 else 0.0
        proceeds = p.shares * px - c
        book.cash += proceeds
        trades.append({
            "symbol": sym, "group": p.group,
            "entry_signal_date": p.entry_signal_date,
            "entry_fill_date": p.entry_fill_date, "entry_price": p.entry_price,
            "exit_signal_date": decision.get("signal_date"),
            "exit_fill_date": fill_date, "exit_price": px,
            "quantity": p.shares, "exit_reason": e["reason"],
            "also_triggered": e.get("also_triggered"),
            "invested": p.invested, "realised_trims": p.realised,
            "final_proceeds": proceeds,
            "net_pnl": p.realised + proceeds - p.invested,
            "total_costs": p.costs_paid, "dp_charge": p.dp_paid,
            "initial_build": p.initial_build})
        del book.positions[sym]

    if not decision.get("reset"):
        return {"fills": fills, "trades": trades, "pending": pending,
                "notes": notes, "E": E}

    weights = decision.get("weights") or {}

    # 3. targets for survivors and entries. A retried order (after
    # PENDING_NO_PRICE) executes alone: resize=False leaves survivors as-is.
    targets: dict[str, int] = {}
    for sym, p in (book.positions.items() if decision.get("resize", True) else ()):
        px = px_of(sym)
        if px is None:
            notes.append(f"{sym}: no open print — not resized this reset")
            continue
        w = weights.get(sym)
        if w is None:
            continue
        p.target_weight = w
        targets[sym] = math.floor(w * E / px)
    entries = []
    for ent in decision.get("entries", []):
        sym = ent["symbol"]
        px = px_of(sym)
        if px is None:
            pending.append({"side": "BUY", "symbol": sym, "reason": "ENTRY",
                            "status": "PENDING_NO_PRICE", **ent})
            continue
        targets[sym] = math.floor(weights[sym] * E / px)
        entries.append(ent)

    # 4a. sells first: trims
    for sym, p in list(book.positions.items()):
        if sym in targets and targets[sym] < p.shares:
            q = p.shares - targets[sym]
            px = px_of(sym)
            to_zero = targets[sym] == 0
            c = fill("SELL", sym, q, px, "RESET_TO_ZERO" if to_zero else "RESET_TRIM")
            net = q * px - c
            book.cash += net
            p.realised += net                # final P&L = realised + last sale - invested
            p.shares -= q
            if to_zero:
                # the slot is now worth less than one share: as in the
                # backtests the TRADE closes here (exit reason RESET_TO_ZERO)
                # and the stock stays a zero-share member of its seat
                trades.append({
                    "symbol": sym, "group": p.group,
                    "entry_signal_date": p.entry_signal_date,
                    "entry_fill_date": p.entry_fill_date,
                    "entry_price": p.entry_price,
                    "exit_signal_date": decision.get("signal_date"),
                    "exit_fill_date": fill_date, "exit_price": px,
                    "quantity": q, "exit_reason": "RESET_TO_ZERO",
                    "also_triggered": None, "invested": p.invested,
                    "realised_trims": p.realised - net, "final_proceeds": net,
                    "net_pnl": p.realised - p.invested,
                    "total_costs": p.costs_paid, "dp_charge": p.dp_paid,
                    "initial_build": p.initial_build})
                p.invested = p.realised = p.costs_paid = p.dp_paid = 0.0
                p.armed, p.stop_level, p.peak_close = False, None, None

    # 4b. buys: top-ups and entries
    buys: list[dict] = []
    for sym, p in book.positions.items():
        if sym in targets and targets[sym] > p.shares:
            buys.append({"symbol": sym, "q": targets[sym] - p.shares,
                         "px": px_of(sym), "reason": "RESET_TOPUP"})
    for ent in entries:
        sym = ent["symbol"]
        buys.append({"symbol": sym, "q": targets[sym], "px": px_of(sym),
                     "reason": "ENTRY", "ent": ent})

    def need():
        return sum(b["q"] * b["px"] + costs.buy_cost(b["px"], b["q"], fill_date)
                   for b in buys)

    # 5. cash short -> trim the largest buy one share at a time
    while buys and need() > book.cash + 1e-9:
        big = max((b for b in buys if b["q"] > 0),
                  key=lambda b: (b["q"] * b["px"], b["symbol"]), default=None)
        if big is None:
            break
        big["q"] -= 1
        notes.append(f"cash short: trimmed {big['symbol']} by 1 share")

    for b in buys:
        sym, q, px = b["symbol"], b["q"], b["px"]
        if b["reason"] == "ENTRY":
            ent = b["ent"]
            reason = "ENTRY" if q > 0 else "ZERO_SHARE_SLOT"
            c = fill("BUY", sym, q, px, reason,
                     {"initial_build": bool(ent.get("initial_build")),
                      "signal_id": ent.get("signal_id")}) if q > 0 else 0.0
            if q == 0:
                fills.append({"side": "BUY", "symbol": sym, "shares": 0,
                              "price": px, "reason": "ZERO_SHARE_SLOT",
                              "fill_date": fill_date, "costs": costs.itemise("BUY", px, 0, fill_date),
                              "slippage_assumed": 0.0,
                              "initial_build": bool(ent.get("initial_build")),
                              "signal_id": ent.get("signal_id")})
            book.cash -= q * px + c
            book.positions[sym] = Position(
                symbol=sym, group=ent["group"], shares=q,
                entry_signal_date=decision["signal_date"], entry_fill_date=fill_date,
                entry_ref_price=ent.get("ref_price"), entry_price=px,
                atr_entry=ent.get("atr_entry"),
                invested=q * px + c, initial_build=bool(ent.get("initial_build")),
                target_weight=weights.get(sym, 0.0), costs_paid=c)
        else:
            if q <= 0:
                continue
            p = book.positions[sym]
            if p.shares == 0:
                # a zero-share member that can now afford shares: the
                # backtests open a NEW trade here (entry = this open, ATR from
                # this signal close, trail dormant), not a top-up
                c = fill("BUY", sym, q, px, "ENTRY",
                         {"initial_build": False, "from_zero_share": True})
                book.cash -= q * px + c
                p.shares = q
                p.entry_signal_date = decision["signal_date"]
                p.entry_fill_date = fill_date
                p.entry_price = px
                ra = (decision.get("restart_atr") or {}).get(sym)
                if ra is not None:
                    p.atr_entry = ra
                p.armed, p.stop_level, p.peak_close = False, None, None
                p.invested, p.realised, p.costs_paid = q * px + c, 0.0, c
                p.dp_paid, p.initial_build = 0.0, False
                continue
            c = fill("BUY", sym, q, px, "RESET_TOPUP")
            book.cash -= q * px + c
            p.shares += q
            p.invested += q * px + c        # entry_price and stop unchanged
    return {"fills": fills, "trades": trades, "pending": pending,
            "notes": notes, "E": E}
