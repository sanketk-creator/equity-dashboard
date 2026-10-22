"""
Hourly bars from the live Dhan feed.

One bar per symbol per hour, labelled at its open (09:15 ... 14:15, plus
the 15:15-15:30 stub). open = first trade in the hour, close = last trade
before it ends. The 09:15 bar's open is replaced by the OFFICIAL session
open (REST /marketfeed/ohlc, or the quote packet's day-open) — the pre-open
auction sets it, and spec §7 fills fresh entries there.

Which hour a trade belongs to is decided by its LAST TRADE TIME, not by when
the packet reached us — a packet delayed across the hour boundary must not
move a trade into the next bar. Dhan's LTT field is epoch seconds; whether
it is UTC-epoch or IST-shifted is calibrated from the live stream itself
(the first ticks whose LTT lands within two minutes of the wall clock under
one reading or the other). Until calibrated, receipt time is used.

A bar that has been CLOSED (its end + grace passed and it was checked) is
locked: a straggler can never rewrite a close that a decision was made on.
"""
from __future__ import annotations

import time
from datetime import datetime

from . import config
from .intraday import LABELS, SESSION_END, SESSION_START, slot_index

IST_SHIFT = 19800            # 5h30m
_CALIB_WINDOW = 120
_CALIB_VOTES = 20


def _ist(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, config.IST)


class HourlyRecorder:
    def __init__(self) -> None:
        self.date: str | None = None
        self.bars: dict[str, dict[str, dict]] = {}
        self.opens: dict[str, tuple[float, str]] = {}
        self.locked = 0                         # bars [0, locked) are frozen
        self.dirty: set[tuple[str, str]] = set()
        self.dirty_opens: set[str] = set()
        self.ltt_mode: str | None = None        # "utc" | "ist" | None
        self._votes: list[str] = []
        self.ticks = 0
        self.late_dropped = 0
        self.last_tick_epoch: float | None = None

    # ------------------------------------------------------------ lifecycle

    def reset(self, date: str, bars: dict | None = None,
              opens: dict | None = None) -> None:
        self.date = date
        self.bars = {s: {lab: dict(b) for lab, b in sb.items()}
                     for s, sb in (bars or {}).items()}
        self.opens = dict(opens or {})
        self.locked = 0
        self.dirty.clear()
        self.dirty_opens.clear()
        self.ticks = 0
        self.late_dropped = 0

    def lock_through(self, closed: int) -> None:
        self.locked = max(self.locked, closed)

    # ---------------------------------------------------------------- time

    def trade_time(self, ltt: int | None, now: float) -> float | None:
        """Best estimate of when the trade happened (true epoch seconds)."""
        if ltt and ltt > 946684800:
            if self.ltt_mode is None:
                if abs(ltt - now) <= _CALIB_WINDOW:
                    self._votes.append("utc")
                elif abs(ltt - IST_SHIFT - now) <= _CALIB_WINDOW:
                    self._votes.append("ist")
                if len(self._votes) >= _CALIB_VOTES:
                    top = max(set(self._votes), key=self._votes.count)
                    if self._votes.count(top) >= 0.8 * len(self._votes):
                        self.ltt_mode = top
                    else:
                        self._votes.clear()
            if self.ltt_mode == "utc":
                t = float(ltt)
            elif self.ltt_mode == "ist":
                t = float(ltt - IST_SHIFT)
            elif (abs(ltt - now) <= _CALIB_WINDOW
                  or abs(ltt - IST_SHIFT - now) <= _CALIB_WINDOW):
                return now                      # fresh trade: receipt time
            else:
                # Not calibrated yet and this trade is NOT fresh under either
                # reading (e.g. the on-subscribe snapshot of a halted stock's
                # last trade): it cannot be placed in an hour, so it must not
                # invent a print in the current one.
                return None
            return min(t, now)
        return now

    # --------------------------------------------------------------- ticks

    def on_trade(self, symbol: str, ltp: float | None, ltt: int | None,
                 day_open: float | None, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self.date is None or not ltp or ltp <= 0:
            return
        self.last_tick_epoch = now
        self.ticks += 1
        wall = _ist(now)
        if wall.date().isoformat() != self.date:
            return
        wsec = wall.hour * 3600 + wall.minute * 60 + wall.second
        if (day_open and day_open > 0 and wsec >= SESSION_START
                and symbol not in self.opens):
            self.opens[symbol] = (float(day_open), "feed")
            self.dirty_opens.add(symbol)
        tt = self.trade_time(ltt, now)
        if tt is None:
            return
        t = _ist(tt)
        if t.date().isoformat() != self.date:
            return                              # a snapshot of an old trade
        sec = t.hour * 3600 + t.minute * 60 + t.second
        if not (SESSION_START <= sec < SESSION_END):
            return
        k = slot_index(sec)
        if k is None:
            return
        if k < self.locked:
            self.late_dropped += 1
            return
        label = LABELS[k]
        sb = self.bars.setdefault(symbol, {})
        b = sb.get(label)
        px = float(ltp)
        if b is None:
            sb[label] = {"o": px, "h": px, "l": px, "c": px, "n": 1}
        else:
            b["c"] = px
            b["h"] = max(b["h"], px)
            b["l"] = min(b["l"], px)
            b["n"] = int(b.get("n") or 0) + 1
        self.dirty.add((symbol, label))

    def set_official_open(self, symbol: str, price: float, src: str) -> None:
        """REST /marketfeed/ohlc beats the packet's day-open; nothing else
        overwrites a REST value."""
        if not price or price <= 0:
            return
        cur = self.opens.get(symbol)
        if cur and cur[1] == "rest":
            return
        self.opens[symbol] = (float(price), src)
        self.dirty_opens.add(symbol)

    # ------------------------------------------------------------- flushing

    def take_dirty(self) -> tuple[list[tuple], list[tuple]]:
        rows = []
        for s, lab in self.dirty:
            b = self.bars[s][lab]
            rows.append((s, lab, b["o"], b["h"], b["l"], b["c"], b["n"]))
        opens = [(s, *self.opens[s]) for s in self.dirty_opens]
        self.dirty.clear()
        self.dirty_opens.clear()
        return rows, opens

    def open_prices(self) -> dict[str, float]:
        return {s: v[0] for s, v in self.opens.items()}

    def status(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        age = (now - self.last_tick_epoch) if self.last_tick_epoch else None
        return {"date": self.date, "ticks": self.ticks,
                "symbols_with_bars": len(self.bars),
                "ltt_mode": self.ltt_mode or "calibrating (using receipt time)",
                "late_dropped": self.late_dropped,
                "last_tick_age_s": round(age, 1) if age is not None else None,
                "official_opens": len(self.opens)}
