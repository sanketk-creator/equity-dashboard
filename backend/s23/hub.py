"""
StrategiesHub — orchestration for Strategy 2 (Momentum Barbell 20) and
Strategy 3 (Sector Momentum 12). Spec §7, §8, §11, §15.

Daily cycle (Dhan's daily API is T-1, so a session's completed bar arrives
overnight; the cycle runs every few minutes until it can):

  1. data: fetch the completed bar of the next unprocessed session for the
     universe (~500 stocks) and the 9 indices; validate (§6.5, §7.6)
  2. for each strategy, in order, for that session T:
       a. fill orders due at T's OPEN (sells before buys; costs; retries)
       b. Strategy 2: trailing stops on T's close (every session); weekly /
          monthly rank checks on calendar week / month ends
          Strategy 3: weekly decision on the calendar week end
       c. persist signals (immutable), orders for the next open, positions,
          portfolio_daily, audit row — one transaction
  3. more than one session waiting -> HALT, human review (§11.5)

Signals use completed bars only; fills are always the NEXT session's open
(the daily bar's official open). Intraday quotes are display-only.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import os
import time
from datetime import date, datetime, timedelta

import httpx

from .. import config, creds
from ..rest import DhanRest
from . import barbell as BB, sector as SC, universe as U
from . import indicators as I
from .book import MAX_PENDING_SESSIONS, Book, Position, execute
from .calendar import CalendarGap, NSECalendar
from .store import Store

log = logging.getLogger("s23")

VERSION = "spec-1.0 (2026-10-29)"
S23_BUILD = "2026-10-30-s23-v1"
STRATS = {
    "barbell": {"name": "momentum_barbell_20", "label": "Momentum Barbell 20",
                "capital": 200_000.0},
    "sector": {"name": "sector_momentum_12", "label": "Sector Momentum 12",
               "capital": 200_000.0},
}
# Read from the backtest code (handover pack, 30 Oct 2026) — where the spec
# and the code differ the code wins, and the difference is recorded here and
# shown on the Data & Audit tab.
KNOWN_DIFFERENCES = [
    ("Strategy 3 liquidity screen", "Code: ADV above the 20th percentile (pandas "
     "quantile, linear) of ALL ~492 tradable NIFTY 500 names, strictly above. Spec "
     "§5.4.1 says 'of the 208 tradable'. Implemented as the code. Strategy 2 uses "
     "the identical rule."),
    ("Strategy 3 sector entry", "Code: a free seat walks the WHOLE ranking — a "
     "sector ranked 5-8 can take it if every better unseated sector is below its "
     "SMA200; and a seated sector that fell to rank 5+ is picked again by that walk "
     "(so it stays) when seats are still free and it is above its SMA200. Spec "
     "§5.3 does not say either. Implemented as the code."),
    ("Strategy 3 relative strength (m2)", "Code: NIFTY 500's 6-month return on its "
     "OWN bars (126 of its bars back), matched by date, never forward-filled. If "
     "NIFTY 500 has no bar on the week's last session, the decision moves (the "
     "'all index files' rule covers 9 files: 8 sectors + NIFTY 500)."),
    ("Strategy 3 persistence (m4)", "Code needs 325 index rows (spec says 326)."),
    ("ADV with zero volume", "Code counts a zero-volume day as zero turnover; only "
     "a MISSING volume voids the 126-bar window. Spec §6.5 says zero/negative "
     "should void it. Implemented as the code."),
    ("Zero-share members", "Code: a selected stock priced above its slot holds a "
     "seat with no trade and no stop; if a later reset can afford it, that is a new "
     "trade at that open with that signal's ATR. Implemented as the code."),
    ("Orders without an open price", "Live follows spec §8.3/§15.2: held "
     "PENDING_NO_PRICE and retried up to 3 sessions, then cancelled with an alert "
     "(the backtests never met this case for entries)."),
    ("Data source", "Live bars from Dhan (stocks and the 9 indices); the backtests "
     "read the local parquet store and the ETF index files. Small vendor "
     "differences are possible."),
    ("Universe", "Live membership follows your monthly NSE upload; the backtests "
     "applied the 26-Oct-2026 list to every date. A held stock that leaves the "
     "index is kept until its own exit (operator decision)."),
]
IDX_PREFIX = "IDX:"
ALL_INDICES = [U.BENCH_INDEX] + [U.SECTOR_INDEX[s] for s in U.SECTORS]
HISTORY_BARS = 400                     # spec §7.3: load at least 400 days
READY_DEADLINE = "08:45"               # degrade (proceed, record missing) after
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
BASE = "https://api.dhan.co/v2"


def now_ist() -> datetime:
    return datetime.now(config.IST)


def _sec(dt: datetime) -> int:
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def quantile_linear(vals: list[float], q: float) -> float:
    """numpy / pandas default ('linear') quantile."""
    xs = sorted(vals)
    if not xs:
        return math.inf
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


# The 20th-percentile liquidity screen — IDENTICAL in both backtests
# (barbell20.eligible_on and sector_rotation.eligible_on):
#     a = adv.dropna(); a = a[a > 0]; liquid = a[a > a.quantile(0.20)]
# pandas' default (linear) quantile, over EVERY tradable NIFTY 500 name with
# a valid ADV that day (the ~492, not Strategy 3's 208 as spec §5.4.1 says),
# passing STRICTLY above.
def liquidity_pass(adv_map: dict[str, float | None]) -> tuple[set[str], float]:
    valid = {s: v for s, v in adv_map.items() if v is not None and v > 0}
    cut = quantile_linear(list(valid.values()), 0.20)
    return {s for s, v in valid.items() if v > cut}, cut


def blank_state(sid: str) -> dict:
    cap = STRATS[sid]["capital"]
    return {"status": "WAITING_GO_LIVE", "halt_reason": None,
            "book": Book(cap, cap).to_record(), "last_evaluated_bar_date": None,
            "go_live": None, "pending": [], "retry": [], "seated": [],
            "realised_cum": 0.0, "slippage_cum": 0.0, "peak_nav": cap,
            "last_fill_ts": None, "last_eval_ts": None,
            "config": {"version": VERSION, "capital": cap}}


class StrategiesHub:
    def __init__(self):
        base = os.path.dirname(config.DB_PATH) or "."
        self.db_path = os.path.join(base, "strategies.db")
        self.store = Store(self.db_path)
        self.rest = DhanRest()                 # same class as Strategy 1, own instance
        self.http = httpx.AsyncClient(timeout=60.0)
        self.cal = NSECalendar.seed()
        self.state: dict[str, dict] = {sid: blank_state(sid) for sid in STRATS}
        self.status: dict = {"boot": "starting", "data": {}, "errors": [],
                             "mapped": False}
        self.prices: dict[str, tuple[float, float]] = {}
        self.slippage_bps = float(os.getenv("S23_SLIPPAGE_BPS", "0") or 0)
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []

    # ================================================================ boot

    async def start(self) -> None:
        if not self.store.available:
            self.status["boot"] = f"STORAGE UNAVAILABLE: {self.store.error}"
            return
        rec = await self.store.get_json("calendar")
        if rec:
            self.cal = NSECalendar.from_record(rec)
        else:
            await self.store.set_json("calendar", self.cal.to_record())
        await self._seed_universe()
        for sid in STRATS:
            st = await self.store.get_json(f"state:{sid}")
            if st:
                self.state[sid] = {**blank_state(sid), **st}
        await self._startup_integrity()
        self.status["boot"] = "ready"
        self._tasks.append(asyncio.create_task(self._loop(), name="s23-daily"))
        self._tasks.append(asyncio.create_task(self._price_loop(), name="s23-ltp"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.http.aclose()
        await self.rest.aclose()
        self.store.close()

    async def _seed_universe(self) -> None:
        rows = await self.store.q("SELECT COUNT(*) n FROM universe")
        if rows[0]["n"]:
            return
        smap = U.seed_sector_map()
        today = now_ist().date().isoformat()

        def ins(c):
            for s in U.seed_symbols():
                c.execute("INSERT INTO universe(symbol,active,sector,added_date,note) "
                          "VALUES(?,?,?,?,?)",
                          (s, 1, smap.get(s), today,
                           "excluded (spec §4.1/§5.1.1)" if s in U.EXCLUDED else None))
        await self.store.tx(ins)
        await self._audit(None, None, "universe_seeded",
                          {"symbols": 500, "sector_mapped": len(smap)})

    async def _audit(self, sid, bar_date, kind, detail) -> None:
        await self.store.x("INSERT INTO audit_log(ts,strategy_id,bar_date,kind,"
                           "detail_json) VALUES(?,?,?,?,?)",
                           (now_ist().isoformat(), sid, bar_date, kind,
                            json.dumps(detail, default=str)))

    async def _save_state(self, sid: str, extra_tx=None) -> None:
        st = self.state[sid]
        book = st["book"]

        def fn(c):
            c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) "
                      "DO UPDATE SET value=excluded.value",
                      (f"state:{sid}", json.dumps(st, default=str)))
            c.execute("DELETE FROM positions WHERE strategy_id=?", (sid,))
            for s, p in book["positions"].items():
                c.execute("INSERT INTO positions(strategy_id,symbol,json) VALUES(?,?,?)",
                          (sid, s, json.dumps(p)))
            if extra_tx:
                extra_tx(c)
        await self.store.tx(fn)

    def _halt(self, sid: str, reason: str) -> None:
        st = self.state[sid]
        st["status"] = "HALTED"
        st["halt_reason"] = reason
        log.error("Strategy %s HALTED: %s", sid, reason)

    # ======================================================= universe data

    async def universe_rows(self) -> dict[str, dict]:
        return {r["symbol"]: r for r in await self.store.q("SELECT * FROM universe")}

    async def _tradable(self) -> set[str]:
        return {s for s, r in (await self.universe_rows()).items()
                if r["active"] and s not in U.EXCLUDED}

    async def _sector_map(self) -> dict[str, str]:
        return {s: r["sector"] for s, r in (await self.universe_rows()).items()
                if r["active"] and r["sector"] and s not in U.EXCLUDED}

    def _held(self) -> set[str]:
        out = set()
        for st in self.state.values():
            out |= set(st["book"]["positions"])
        return out

    async def _master(self) -> tuple[dict[str, str], dict[str, list[tuple[str, str]]]]:
        """ONE download of Dhan's instrument list, parsed for both NSE
        equities (symbol -> id, same rule as Strategy 1) and the 9 indices."""
        r = await self.http.get(SCRIP_MASTER_URL, timeout=120.0)
        r.raise_for_status()
        want = {U.norm_index(n): n for n in ALL_INDICES}
        found: dict[str, list[tuple[str, str]]] = {n: [] for n in ALL_INDICES}
        eq: dict[str, str] = {}
        for row in csv.DictReader(io.StringIO(r.text)):
            if (row.get("SEM_EXM_EXCH_ID") or "").strip() != "NSE":
                continue
            inst = (row.get("SEM_INSTRUMENT_NAME") or "").strip().upper()
            sid = (row.get("SEM_SMST_SECURITY_ID") or "").strip()
            if not sid:
                continue
            if inst == "EQUITY":
                if (row.get("SEM_SERIES") or "").strip() not in ("EQ", "BE", ""):
                    continue
                sym = (row.get("SEM_TRADING_SYMBOL") or "").strip().upper()
                if sym and sym not in eq:
                    eq[sym] = sid
            elif inst == "INDEX":
                for col in ("SEM_TRADING_SYMBOL", "SEM_CUSTOM_SYMBOL"):
                    key = U.norm_index(row.get(col) or "")
                    if key in want and sid not in [p[0] for p in found[want[key]]]:
                        found[want[key]].append((sid, (row.get(col) or "").strip()))
        del r
        return eq, found

    async def map_symbols(self) -> None:
        if not creds.current.ok:
            return
        rows = await self.universe_rows()
        missing = [s for s, r in rows.items() if not r["security_id"]]
        imap = {r["name"]: r for r in await self.store.q("SELECT * FROM index_map")}
        need_idx = any(n not in imap or not imap[n]["security_id"] for n in ALL_INDICES)
        if not missing and not need_idx:
            self.status["mapped"] = True
            return
        master, found = await self._master()
        if missing:

            def fn(c):
                for s in missing:
                    if master.get(s):
                        c.execute("UPDATE universe SET security_id=? WHERE symbol=?",
                                  (master[s], s))
            await self.store.tx(fn)
        if need_idx:

            def fn2(c):
                for n, hits in found.items():
                    if n in imap and imap[n]["source"] == "manual":
                        continue
                    if len(hits) == 1:
                        c.execute("INSERT INTO index_map(name,security_id,dhan_symbol,"
                                  "source) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE "
                                  "SET security_id=excluded.security_id,"
                                  "dhan_symbol=excluded.dhan_symbol,source=excluded.source",
                                  (n, hits[0][0], hits[0][1], "auto"))
                    else:
                        c.execute("INSERT INTO index_map(name,security_id,dhan_symbol,"
                                  "source) VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE "
                                  "SET security_id=NULL,dhan_symbol=excluded.dhan_symbol,"
                                  "source=excluded.source",
                                  (n, None, json.dumps(hits),
                                   "ambiguous" if hits else "not found"))
            await self.store.tx(fn2)
        self.status["mapped"] = True

    async def set_index_id(self, name: str, security_id: str) -> None:
        if name not in ALL_INDICES:
            raise ValueError(f"unknown index {name}")
        await self.store.x("INSERT INTO index_map(name,security_id,dhan_symbol,source) "
                           "VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                           "security_id=excluded.security_id,source='manual'",
                           (name, str(security_id), None, "manual"))
        await self.store.delete_bars(IDX_PREFIX + name)

    # ======================================================== bar fetching

    @staticmethod
    def _validate(sym: str, bars: list[dict]) -> tuple[list[dict], list[str], str | None]:
        """(clean bars, warnings, fatal). Spec §6.5 / §7.6."""
        seen, warn = set(), []
        for b in bars:
            if b["date"] in seen:
                return [], warn, f"{sym}: duplicate bars for {b['date']}"
            seen.add(b["date"])
            if min(b["o"], b["h"], b["l"], b["c"]) <= 0:
                return [], warn, f"{sym}: price <= 0 on {b['date']}"
        clean = []
        for b in bars:
            if not (b["l"] <= b["o"] <= b["h"] and b["l"] <= b["c"] <= b["h"]):
                warn.append(f"{sym} {b['date']}: OHLC inconsistent — bar dropped")
                continue
            clean.append(b)
        return clean, warn, None

    async def _fetch(self, sym: str, sid: str, n: int, is_index: bool) -> list[str]:
        if is_index:
            bars = await self.rest.index_history(int(sid), n)
        else:
            bars = await self.rest.daily_history(sid, n)
        clean, warn, fatal = self._validate(sym, bars)
        if fatal:
            raise RuntimeError(fatal)
        await self.store.upsert_bars(sym, clean)
        return warn

    async def _instruments(self) -> list[tuple[str, str, bool]]:
        out = []
        rows = await self.universe_rows()
        need = {s for s, r in rows.items() if r["active"]} | self._held()
        for s in sorted(need):
            sid = (rows.get(s) or {}).get("security_id")
            if sid:
                out.append((s, sid, False))
        for r in await self.store.q("SELECT * FROM index_map WHERE security_id IS NOT NULL"):
            out.append((IDX_PREFIX + r["name"], r["security_id"], True))
        return out

    async def backfill(self) -> None:
        if not creds.current.ok:
            return
        counts = await self.store.bar_counts()
        for sym, sid, is_idx in await self._instruments():
            if counts.get(sym, 0) >= HISTORY_BARS - 20:
                continue
            try:
                for w in await self._fetch(sym, sid, HISTORY_BARS, is_idx):
                    self.status["data"].setdefault("warnings", []).append(w)
            except RuntimeError as exc:
                if "duplicate" in str(exc) or "<= 0" in str(exc):
                    for sid2 in STRATS:
                        self._halt(sid2, f"data: {exc}")
                log.warning("backfill %s: %s", sym, exc)
            except Exception as exc:
                log.warning("backfill %s: %s", sym, exc)
            await asyncio.sleep(0.25)              # Dhan data APIs: 5/s

    async def _ensure_session(self, T: str, now: datetime) -> dict:
        """Fetch T's completed bar where missing. Returns readiness info."""
        have = await self.store.symbols_with_bar(T)
        inst = await self._instruments()
        missing = [x for x in inst if x[0] not in have]
        if missing and creds.current.ok:
            for sym, sid, is_idx in missing:
                try:
                    for w in await self._fetch(sym, sid, 12, is_idx):
                        self.status["data"].setdefault("warnings", []).append(w)
                except RuntimeError as exc:
                    if "duplicate" in str(exc) or "<= 0" in str(exc):
                        for s2 in STRATS:
                            self._halt(s2, f"data: {exc}")
                except Exception as exc:
                    log.warning("daily bar %s: %s", sym, exc)
                await asyncio.sleep(0.25)
            have = await self.store.symbols_with_bar(T)
        stocks = [x[0] for x in inst if not x[2]]
        miss_stocks = [s for s in stocks if s not in have]
        miss_idx = [n for n in ALL_INDICES if IDX_PREFIX + n not in have]
        nxt = self.cal.next_session(T)
        deadline = datetime.fromisoformat(f"{nxt}T{READY_DEADLINE}:00").replace(
            tzinfo=config.IST)
        complete = not miss_stocks and not miss_idx
        past_deadline = now >= deadline
        coverage = (len(stocks) - len(miss_stocks)) / max(1, len(stocks))
        info = {"session": T, "complete": complete, "missing_stocks": miss_stocks,
                "missing_indices": miss_idx, "coverage": round(coverage, 4),
                "deadline": deadline.isoformat(), "past_deadline": past_deadline}
        info["ready"] = complete or (past_deadline and coverage >= 0.5)
        info["fatal"] = past_deadline and coverage < 0.5
        self.status["data"]["last_session_check"] = info
        return info

    # ======================================================== daily cycle

    async def _loop(self) -> None:
        await asyncio.sleep(45)
        while True:
            try:
                await self.run_daily()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status["errors"] = [f"{type(exc).__name__}: {exc}"]
                log.exception("s23 daily cycle failed")
            await asyncio.sleep(60 * config.DAILY_POLL_MINUTES)

    def _last_started(self, now: datetime) -> str:
        today = now.date().isoformat()
        if self.cal.is_session(today) and _sec(now) >= 9 * 3600 + 15 * 60:
            return today
        return self.cal.prev_session(today)

    async def run_daily(self, now: datetime | None = None,
                        fetch: bool = True) -> None:
        async with self._lock:
            now = now or now_ist()
            today = now.date().isoformat()
            if not self.cal.covers(today):
                self.status["errors"] = [self.cal.reminder(today)]
                return
            if fetch and creds.current.ok:
                if not self.status["mapped"]:
                    try:
                        await self.map_symbols()
                    except Exception as exc:
                        self.status["errors"] = [f"symbol mapping: {exc}"]
                await self.backfill()
            for sid in STRATS:
                if self.state[sid]["status"] == "WAITING_GO_LIVE":
                    await self._try_go_live(sid, now, fetch)
            live = [sid for sid in STRATS if self.state[sid]["status"] == "ACTIVE"]
            if not live:
                return
            last = min(self.state[s]["last_evaluated_bar_date"] for s in live)
            due = self.cal.sessions_between(last, today)
            allow = int(await self.store.get_json("catchup_approved", 0) or 0)
            if len(due) > 1 and allow < len(due):
                for sid in live:
                    self._halt(sid, f"{len(due)} sessions missed ({', '.join(due)}) "
                                    f"— §11.5 requires manual review before "
                                    f"catching up")
                    await self._save_state(sid)
                await self._audit(None, due[0], "halt_missed_sessions", {"due": due})
                return
            for T in due:
                info = await self._ensure_session(T, now) if fetch else \
                    await self._readiness_no_fetch(T, now)
                if info.get("fatal"):
                    for sid in live:
                        self._halt(sid, f"no usable daily bars for {T} by the "
                                        f"deadline (coverage {info['coverage']:.0%})")
                        await self._save_state(sid)
                    return
                if not info["ready"]:
                    return
                for sid in live:
                    st = self.state[sid]
                    if st["status"] != "ACTIVE" or st["last_evaluated_bar_date"] >= T:
                        continue
                    try:
                        await self.process_session(sid, T, info)
                    except CalendarGap as exc:
                        self.status["errors"] = [str(exc)]
                        return
                    except Exception as exc:
                        log.exception("process %s %s", sid, T)
                        self._halt(sid, f"evaluation of {T} failed: {exc}")
                        await self._save_state(sid)
                if allow:
                    allow = max(0, allow - 1)
                    await self.store.set_json("catchup_approved", allow)

    async def _readiness_no_fetch(self, T: str, now: datetime) -> dict:
        have = await self.store.symbols_with_bar(T)
        inst = await self._instruments()
        stocks = [x[0] for x in inst if not x[2]]
        miss = [s for s in stocks if s not in have]
        miss_idx = [n for n in ALL_INDICES if IDX_PREFIX + n not in have]
        cov = (len(stocks) - len(miss)) / max(1, len(stocks))
        nxt = self.cal.next_session(T)
        deadline = datetime.fromisoformat(f"{nxt}T{READY_DEADLINE}:00").replace(
            tzinfo=config.IST)
        complete = not miss and not miss_idx
        return {"session": T, "complete": complete, "missing_stocks": miss,
                "missing_indices": miss_idx, "coverage": cov,
                "ready": complete or (now >= deadline and cov >= 0.5),
                "fatal": now >= deadline and cov < 0.5}

    async def approve_catchup(self) -> dict:
        live = [s for s in STRATS if self.state[s]["last_evaluated_bar_date"]]
        if not live:
            return {"due": []}
        last = min(self.state[s]["last_evaluated_bar_date"] for s in live)
        due = self.cal.sessions_between(last, now_ist().date().isoformat())
        await self.store.set_json("catchup_approved", len(due))
        for sid in live:
            st = self.state[sid]
            if st["status"] == "HALTED" and "sessions missed" in (st["halt_reason"] or ""):
                st["status"], st["halt_reason"] = "ACTIVE", None
                await self._save_state(sid)
        await self._audit(None, None, "catchup_approved", {"due": due})
        return {"due": due}

    async def resume(self, sid: str) -> None:
        st = self.state[sid]
        if st["status"] == "HALTED":
            st["status"] = "ACTIVE" if st["last_evaluated_bar_date"] else "WAITING_GO_LIVE"
            st["halt_reason"] = None
            await self._save_state(sid)
            await self._audit(sid, None, "resumed_by_operator", {})

    # ============================================================ go-live

    async def _try_go_live(self, sid: str, now: datetime, fetch: bool) -> None:
        G = self._last_started(now)
        info = await self._ensure_session(G, now) if fetch else \
            await self._readiness_no_fetch(G, now)
        if not info["ready"]:
            self.status["data"]["go_live_wait"] = info
            return
        counts = await self.store.bar_counts()
        short = [s for s in await self._tradable() if counts.get(s, 0) < 253]
        if len(short) > 0.2 * max(1, len(await self._tradable())):
            self.status["data"]["go_live_wait"] = {
                "message": f"history backfill incomplete ({len(short)} symbols "
                           f"under 253 bars)"}
            return
        st = self.state[sid]
        st["go_live"] = G
        st["status"] = "ACTIVE"
        try:
            await self._decide(sid, G, info, initial_build=True)
        except Exception as exc:
            # e.g. sector indices short of the 325 rows m4 needs: not live yet
            self.state[sid] = blank_state(sid)
            self.status["data"]["go_live_wait"] = {
                "message": f"{STRATS[sid]['label']}: cannot go live on {G}: {exc}"}
            log.warning("go-live %s on %s refused: %s", sid, G, exc)
            return
        await self._audit(sid, G, "go_live", {"initial_build_signal_date": G,
                                              "fills_at": self.cal.next_session(G)})

    # ================================================== session processing

    async def _metrics(self, symbols: set[str], D: str) -> dict[str, dict]:
        out = {}
        specials = set(self.cal.special)
        for s in symbols:
            bars = await self.store.bars_upto(s, D, 420)
            if not bars or bars[-1]["date"] != D:
                out[s] = {"has_bar": False}
                continue
            closes = [b["c"] for b in bars]
            vols = [b["volume"] for b in bars]
            out[s] = {
                "has_bar": True, "close": closes[-1], "open": bars[-1]["o"],
                "sma200": I.sma(closes, 200), "adv": I.adv(closes, vols, 126),
                "vol": I.realised_vol(closes, 252), "mom": I.mom_12_1(closes),
                "atr": I.atr_series_last(bars, 14), "rows": len(bars),
                "special_in_adv_window": sorted(
                    {b["date"] for b in bars[-126:]} & specials),
            }
        return out

    async def _opens(self, symbols: set[str], D: str) -> dict[str, float]:
        out = {}
        for s in symbols:
            rows = await self.store.q("SELECT o FROM bars WHERE symbol=? AND date=?",
                                      (s, D))
            if rows and rows[0]["o"] and rows[0]["o"] > 0:
                out[s] = rows[0]["o"]
        return out

    async def _closes(self, symbols: set[str], D: str) -> dict[str, float]:
        out = {}
        for s in symbols:
            rows = await self.store.q("SELECT c FROM bars WHERE symbol=? AND date<=? "
                                      "ORDER BY date DESC LIMIT 1", (s, D))
            if rows:
                out[s] = rows[0]["c"]
        return out

    async def process_session(self, sid: str, T: str, info: dict) -> None:
        st = self.state[sid]
        # Strategy 3: week-end decision without all 8 sector-index bars moves
        # to the last session where all 8 have bars (spec §5.7, §11.6).
        if sid == "sector" and self.cal.is_last_session_of_iso_week(T):
            have = await self.store.symbols_with_bar(T)
            if not all(IDX_PREFIX + n in have for n in ALL_INDICES):
                Dp = await self._last_all_index_date(T)
                if Dp is None or self.cal.next_session(Dp) != T:
                    self._halt(sid, f"week-end {T}: sector index bars missing and "
                                    f"the fallback session {Dp} does not fill at "
                                    f"{T}'s open — review")
                    await self._save_state(sid)
                    return
                await self._audit(sid, T, "decision_moved",
                                  {"from": T, "to": Dp, "fills_at": T,
                                   "reason": "an index bar (8 sectors + NIFTY 500) missing"})
                await self._decide(sid, Dp, info, initial_build=False,
                                   moved_from=T)
                st["_moved_done"] = T
        await self._fill_due(sid, T)
        await self._decide(sid, T, info, initial_build=False)

    async def _last_all_index_date(self, before: str) -> str | None:
        # all 9 index files: the 8 sectors AND NIFTY 500 (d1_exit4.weekly_calendar)
        names = [IDX_PREFIX + n for n in ALL_INDICES]
        rows = await self.store.q(
            "SELECT date, COUNT(*) n FROM bars WHERE symbol IN (%s) AND date < ? "
            "GROUP BY date HAVING n = ? ORDER BY date DESC LIMIT 1"
            % ",".join("?" * len(names)), (*names, before, len(names)))
        return rows[0]["date"] if rows else None

    async def _fill_due(self, sid: str, T: str) -> None:
        st = self.state[sid]
        book = Book.from_record(st["book"])
        due = [p for p in st["pending"] if p["fill_date"] <= T]
        st["pending"] = [p for p in st["pending"] if p["fill_date"] > T]
        retry, st["retry"] = st["retry"], []
        syms = set(book.positions)
        for d in due:
            syms |= {e["symbol"] for e in d["decision"]["exits"]}
            syms |= {e["symbol"] for e in d["decision"]["entries"]}
        syms |= {r["symbol"] for r in retry}
        opens = await self._opens(syms, T)
        prev = await self._closes(syms, self.cal.prev_session(T))
        all_fills, all_trades, new_pending = [], [], []

        # retries of PENDING_NO_PRICE orders execute alone (no resize)
        if retry:
            dec = {"signal_date": retry[0].get("signal_date"), "reset": True,
                   "resize": False,
                   "exits": [r for r in retry if r["side"] == "SELL"],
                   "entries": [r for r in retry if r["side"] == "BUY"],
                   "weights": {r["symbol"]: r.get("weight", 0.05) for r in retry}}
            res = execute(book, dec, opens, prev, T, self.slippage_bps)
            all_fills += [(f, r) for f in res["fills"] for r in retry
                          if r["symbol"] == f["symbol"]]
            all_trades += res["trades"]
            for p in res["pending"]:
                src = next(r for r in retry if r["symbol"] == p["symbol"])
                src["attempts"] = src.get("attempts", 1) + 1
                if src["attempts"] > MAX_PENDING_SESSIONS:
                    await self._order_status(sid, src, "CANCELLED")
                    await self._audit(sid, T, "order_cancelled_no_price", src)
                    self.status["errors"].append(
                        f"{STRATS[sid]['label']}: {src['side']} {src['symbol']} "
                        f"cancelled after {MAX_PENDING_SESSIONS} sessions without "
                        f"an open price")
                else:
                    new_pending.append(src)
        for d in due:
            res = execute(book, d["decision"], opens, prev, T, self.slippage_bps)
            all_fills += [(f, d) for f in res["fills"]]
            all_trades += res["trades"]
            for p in res["pending"]:
                new_pending.append({**p, "side": p["side"], "attempts": 1,
                                    "signal_date": d["signal_date"],
                                    "weight": d["decision"]["weights"].get(p["symbol"])})
            if res["notes"]:
                await self._audit(sid, T, "fill_notes", {"notes": res["notes"]})
        st["retry"] = new_pending
        st["book"] = book.to_record()
        if all_fills:
            st["last_fill_ts"] = f"{T} 09:15 IST (official open)"
        st["slippage_cum"] += sum(f["slippage_assumed"] for f, _ in all_fills)
        await self._record_fills(sid, T, all_fills, all_trades, new_pending)

    async def _order_status(self, sid, item, status) -> None:
        await self.store.x("UPDATE orders SET status=? WHERE strategy_id=? AND "
                           "symbol=? AND signal_date=? AND side=?",
                           (status, sid, item["symbol"], item.get("signal_date"),
                            item["side"]))

    async def _record_fills(self, sid, T, fills, trades, pending) -> None:
        st = self.state[sid]

        def fn(c):
            for f, d in fills:
                sd = d.get("signal_date")
                exists = c.execute(
                    "SELECT order_id FROM orders WHERE strategy_id=? AND symbol=? AND "
                    "signal_date=? AND side=?", (sid, f["symbol"], sd, f["side"])).fetchone()
                vals = (f["price"], f"{T} 09:15 IST (official open)", f["shares"],
                        json.dumps(f["costs"]), f["slippage_assumed"])
                if exists:
                    c.execute("UPDATE orders SET status=?, fill_price=?, fill_timestamp=?, "
                              "filled_shares=?, costs_json=?, slippage_assumed=?, "
                              "reason=CASE WHEN ?='ZERO_SHARE_SLOT' THEN 'ZERO_SHARE_SLOT' "
                              "ELSE reason END WHERE order_id=?",
                              ("FILLED", *vals, f["reason"], exists[0]))
                else:
                    c.execute("INSERT INTO orders(strategy_id,symbol,side,signal_date,"
                              "intended_fill_date,status,reason,target_shares,fill_price,"
                              "fill_timestamp,filled_shares,costs_json,slippage_assumed) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (sid, f["symbol"], f["side"], sd, T, "FILLED", f["reason"],
                               f["shares"], *vals))
            for p in pending:
                c.execute("UPDATE orders SET status='PENDING_NO_PRICE', attempts=? "
                          "WHERE strategy_id=? AND symbol=? AND signal_date=? AND side=?",
                          (p.get("attempts", 1), sid, p["symbol"], p.get("signal_date"),
                           p["side"]))
            for t in trades:
                st["realised_cum"] += t["net_pnl"]
                d0 = date.fromisoformat(t["entry_fill_date"])
                d1 = date.fromisoformat(t["exit_fill_date"])
                sess = len(self.cal.sessions_between(t["entry_fill_date"],
                                                     t["exit_fill_date"])) + 1
                gross = t["net_pnl"] + t["total_costs"]
                ret = t["net_pnl"] / t["invested"] * 100 if t["invested"] else None
                c.execute("INSERT INTO trades(strategy_id,symbol,sleeve_or_sector,"
                          "entry_signal_date,entry_fill_timestamp,entry_price,"
                          "exit_signal_date,exit_fill_timestamp,exit_price,quantity,"
                          "holding_days_calendar,holding_days_sessions,exit_reason,"
                          "also_triggered,gross_pnl,total_costs,dp_charge,net_pnl,"
                          "return_pct,initial_build,json) VALUES(?,?,?,?,?,?,?,?,?,?,?,"
                          "?,?,?,?,?,?,?,?,?,?)",
                          (sid, t["symbol"], t["group"], t["entry_signal_date"],
                           f"{t['entry_fill_date']} 09:15 IST", t["entry_price"],
                           t["exit_signal_date"], f"{t['exit_fill_date']} 09:15 IST",
                           t["exit_price"], t["quantity"], (d1 - d0).days, sess,
                           t["exit_reason"], t.get("also_triggered"), gross,
                           t["total_costs"], t["dp_charge"], t["net_pnl"], ret,
                           int(bool(t.get("initial_build"))), json.dumps(t, default=str)))
        await self.store.tx(fn)

    async def _decide(self, sid: str, D: str, info: dict, initial_build: bool,
                      moved_from: str | None = None) -> None:
        st = self.state[sid]
        if st.get("_moved_done") == D and not moved_from:
            # this week's decision already ran on the fallback session
            st.pop("_moved_done", None)
            await self._close_session(sid, D, None, info)
            return
        book = Book.from_record(st["book"])
        tradable = await self._tradable()
        pend_exit = {r["symbol"] for r in st["retry"] if r["side"] == "SELL"}
        signals_meta = {"week_end": self.cal.is_last_session_of_iso_week(D),
                        "month_end": self.cal.is_last_session_of_month(D),
                        "special_session": self.cal.is_special(D),
                        "moved_from": moved_from}
        if sid == "barbell":
            syms = tradable | set(book.positions)
            m = await self._metrics(syms, D)
            liquid, cut = liquidity_pass({s: m[s].get("adv") for s in tradable
                                          if m.get(s, {}).get("has_bar")})
            for s in m:
                m[s]["adv_cut"] = cut
            res = BB.evaluate(D, m, book.positions, tradable, U.EXCLUDED, liquid,
                              signals_meta["week_end"], signals_meta["month_end"],
                              initial_build, pend_exit)
            for s, p in book.positions.items():
                if m.get(s, {}).get("has_bar"):
                    p.last_atr, p.last_eval_date = m[s]["atr"], D
            st["book"] = book.to_record()
        else:
            smap = await self._sector_map()
            syms = tradable | set(smap) | set(book.positions)
            m = await self._metrics(syms, D)
            liquid, cut = liquidity_pass({s: m[s].get("adv") for s in tradable
                                          if m.get(s, {}).get("has_bar")})
            for s in m:
                m[s]["adv_cut"] = cut
            decision_day = initial_build or moved_from or signals_meta["week_end"]
            res = None
            if decision_day:
                ranking = await self._sector_ranking(D)
                if ranking is None:
                    raise RuntimeError(f"sector measures unavailable for {D}")
                res = SC.evaluate(D, ranking, st["seated"], m, smap,
                                  book.positions, set(smap), U.EXCLUDED, liquid,
                                  initial_build, pend_exit)
                st["seated"] = res["seated_after"]
                await self._save_sector_scores(D, ranking, res)
        await self._persist_decision(sid, D, res, m, signals_meta, initial_build)
        if not moved_from:
            await self._close_session(sid, D, res, info)
        else:
            await self._save_state(sid)

    async def _sector_ranking(self, D: str) -> list[dict] | None:
        bench = await self.store.bars_upto(IDX_PREFIX + U.BENCH_INDEX, D, 420)
        n500 = [(b["date"], b["c"]) for b in bench]
        meas = {}
        for sec in U.SECTORS:
            bars = await self.store.bars_upto(IDX_PREFIX + U.SECTOR_INDEX[sec], D, 420)
            if not bars or bars[-1]["date"] != D:
                return None
            mm = SC.sector_measures([(b["date"], b["c"]) for b in bars], n500)
            if mm is None:
                return None
            meas[sec] = mm
        return SC.rank_sectors(meas)

    async def _save_sector_scores(self, D, ranking, res) -> None:
        order = res["weight_order"]

        def fn(c):
            for r in ranking:
                c.execute("INSERT OR REPLACE INTO sector_scores(date,sector,m1,m2,m3,m4,"
                          "z1,z2,z3,z4,score,rank,close_vs_sma200,above_sma200,seated,"
                          "weight_order) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (D, r["sector"], r["m1"], r["m2"], r["m3"], r["m4"], r["z1"],
                           r["z2"], r["z3"], r["z4"], r["score"], r["rank"],
                           r["close_vs_sma200"], int(r["above_sma200"]),
                           int(r["sector"] in res["seated_after"]),
                           order.index(r["sector"]) + 1 if r["sector"] in order else None))
        await self.store.tx(fn)

    async def _persist_decision(self, sid, D, res, m, meta, initial_build) -> None:
        st = self.state[sid]
        if res is None or not res["reset"]:
            return
        fill_date = self.cal.next_session(D)
        ts = now_ist().isoformat()
        ids: dict[tuple, int] = {}

        def fn(c):
            for sgl in res["signals"]:
                x = m.get(sgl["symbol"]) or {}
                flags = {**meta, "initial_build": initial_build,
                         "special_in_adv_window": x.get("special_in_adv_window") or []}
                cur = c.execute(
                    "INSERT INTO signals(strategy_id,strategy_version,symbol,signal_type,"
                    "signal_date,signal_timestamp,bar_status,signal_reference_price,"
                    "reason,also_triggered,indicator_values_json,rank_at_signal,"
                    "data_timestamp,flags_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, VERSION, sgl["symbol"], sgl["signal_type"], D, ts, "COMPLETE",
                     sgl.get("ref_price"), sgl["reason"], sgl.get("also_triggered"),
                     json.dumps(sgl.get("inputs"), default=str), sgl.get("rank"), ts,
                     json.dumps(flags)))
                ids[(sgl["symbol"], sgl["signal_type"])] = cur.lastrowid
            for e in res["exits"]:
                c.execute("INSERT OR IGNORE INTO orders(signal_id,strategy_id,symbol,side,"
                          "signal_date,intended_fill_date,status,reason,also_triggered,"
                          "target_weight,initial_build) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (ids.get((e["symbol"], "EXIT")), sid, e["symbol"], "SELL", D,
                           fill_date, "PENDING", e["reason"], e.get("also_triggered"),
                           0.0, 0))
            for e in res["entries"]:
                c.execute("INSERT OR IGNORE INTO orders(signal_id,strategy_id,symbol,side,"
                          "signal_date,intended_fill_date,status,reason,target_weight,"
                          "initial_build) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (ids.get((e["symbol"], "ENTRY")), sid, e["symbol"], "BUY", D,
                           fill_date, "PENDING", "ENTRY", res["weights"].get(e["symbol"]),
                           int(initial_build)))
        await self.store.tx(fn)
        dec = {"signal_date": D, "reset": True, "exits": res["exits"],
               "entries": res["entries"], "weights": res["weights"],
               "restart_atr": res.get("restart_atr") or {}}
        st["pending"].append({"signal_date": D, "fill_date": fill_date,
                              "decision": dec, "initial_build": initial_build})

    async def _close_session(self, sid, D, res, info) -> None:
        st = self.state[sid]
        book = Book.from_record(st["book"])
        closes = await self._closes(set(book.positions), D)
        nav = book.nav(closes)
        st["peak_nav"] = max(st["peak_nav"], nav)
        invested = sum(p.shares * closes.get(s, p.entry_price)
                       for s, p in book.positions.items())
        unreal = sum(p.shares * closes.get(s, p.entry_price) + p.realised - p.invested
                     for s, p in book.positions.items())
        bench = await self._closes({IDX_PREFIX + U.BENCH_INDEX}, D)
        st["last_evaluated_bar_date"] = D
        st["last_eval_ts"] = now_ist().isoformat()
        n_sec = len(st["seated"]) if sid == "sector" else None
        missing = (info or {}).get("missing_stocks") or []

        def fn(c):
            c.execute("INSERT OR REPLACE INTO portfolio_daily(date,strategy_id,nav,cash,"
                      "invested_capital,n_positions,n_sectors,realised_pnl_cumulative,"
                      "unrealised_pnl,drawdown_from_peak,data_feed_status,strategy_status,"
                      "benchmark,nav_after_slippage) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (D, sid, nav, book.cash, invested,
                       sum(1 for p in book.positions.values()), n_sec,
                       st["realised_cum"], unreal, nav / st["peak_nav"] - 1,
                       "complete" if not missing else f"degraded: {len(missing)} missing",
                       st["status"], bench.get(IDX_PREFIX + U.BENCH_INDEX),
                       nav - st["slippage_cum"]))
            c.execute("INSERT INTO audit_log(ts,strategy_id,bar_date,kind,detail_json) "
                      "VALUES(?,?,?,?,?)",
                      (now_ist().isoformat(), sid, D, "evaluation", json.dumps({
                          "decision": None if res is None else {
                              "exits": [(e["symbol"], e["reason"]) for e in res["exits"]],
                              "entries": [e["symbol"] for e in res["entries"]],
                              "reset": res["reset"]},
                          "eligible": (res or {}).get("eligible"),
                          "missing_symbols": missing,
                          "special_session": self.cal.is_special(D)}, default=str)))
        await self._save_state(sid, fn)

    # ===================================================== integrity (§11.3)

    async def _startup_integrity(self) -> None:
        for sid, st in self.state.items():
            if st["status"] != "ACTIVE":
                continue
            book = Book.from_record(st["book"])
            for s, p in book.positions.items():
                if sid == "barbell" and p.last_atr is not None and p.last_eval_date:
                    bars = await self.store.bars_upto(s, p.last_eval_date, 420)
                    if bars and bars[-1]["date"] == p.last_eval_date:
                        a = I.atr_series_last(bars, 14)
                        if a is None or abs(a - p.last_atr) > 1e-6 * max(1.0, abs(a)):
                            self._halt(sid, f"startup check: {s} ATR on "
                                            f"{p.last_eval_date} recomputes to {a} "
                                            f"but {p.last_atr} was persisted — bars "
                                            f"changed underneath the book")
                            break
                    if p.armed and p.stop_level is None:
                        self._halt(sid, f"startup check: {s} armed without a stop")
                        break

    # ======================================================= live prices

    async def _price_loop(self) -> None:
        while True:
            try:
                now = now_ist()
                today = now.date().isoformat()
                if self.cal.covers(today) and self.cal.is_session(today) and \
                        9 * 3600 + 15 * 60 <= _sec(now) <= 15 * 3600 + 31 * 60 \
                        and creds.current.ok:
                    await self._refresh_prices()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status["ltp_error"] = str(exc)
            await asyncio.sleep(60)

    async def _refresh_prices(self) -> None:
        rows = await self.universe_rows()
        ids = {rows[s]["security_id"]: s for s in self._held()
               if (rows.get(s) or {}).get("security_id")}
        if not ids:
            return
        r = await self.http.post(f"{BASE}/marketfeed/ltp",
                                 json={"NSE_EQ": [int(i) for i in ids]},
                                 headers={"access-token": creds.current.access_token,
                                          "client-id": creds.current.client_id,
                                          "Content-Type": "application/json"})
        r.raise_for_status()
        data = (r.json().get("data") or {}).get("NSE_EQ") or {}
        t = time.time()
        for sid_, q in data.items():
            s = ids.get(str(sid_))
            if s and q.get("last_price"):
                self.prices[s] = (float(q["last_price"]), t)
        self.status.pop("ltp_error", None)

    # ===================================================== operator edits

    async def save_calendar(self, holidays: dict, special: dict,
                            covered_through: str) -> None:
        for d in list(holidays) + list(special) + [covered_through]:
            date.fromisoformat(d)
        self.cal = NSECalendar(holidays, special, covered_through)
        await self.store.set_json("calendar", self.cal.to_record())
        await self._audit(None, None, "calendar_saved", self.cal.to_record())

    async def upload_universe(self, filename: str, text: str) -> dict:
        parsed = U.parse_upload(text)
        rows = await self.universe_rows()
        diff = U.diff_upload(parsed, rows)
        held = self._held()
        diff["left_but_held"] = [s for s in diff["left"] if s in held]
        cur = await self.store.tx(lambda c: c.execute(
            "INSERT INTO uploads(created_at,filename,format,diff_json,rows_json,status) "
            "VALUES(?,?,?,?,?,?)", (now_ist().isoformat(), filename, parsed["format"],
                                   json.dumps(diff), json.dumps(parsed["rows"]),
                                   "pending")).lastrowid)
        return {"upload_id": cur, **diff}

    async def apply_universe(self, upload_id: int, assignments: dict | None) -> dict:
        ups = await self.store.q("SELECT * FROM uploads WHERE id=?", (upload_id,))
        if not ups or ups[0]["status"] != "pending":
            raise ValueError("unknown or already-applied upload")
        up = ups[0]
        diff = json.loads(up["diff_json"])
        newrows = json.loads(up["rows_json"])
        assignments = assignments or {}
        today = now_ist().date().isoformat()

        def fn(c):
            for s in diff["left"]:
                c.execute("UPDATE universe SET active=0, removed_date=? WHERE symbol=?",
                          (today, s))
            for s, r in newrows.items():
                ind = r.get("industry")
                sec = (U.INDUSTRY_TO_SECTOR.get(U.norm_label(ind)) if ind is not None
                       else None)
                if s in assignments:
                    sec = assignments[s] or None
                exists = c.execute("SELECT symbol, sector FROM universe WHERE symbol=?",
                                   (s,)).fetchone()
                if exists:
                    if ind is None and s not in assignments:
                        sec = exists["sector"]
                    c.execute("UPDATE universe SET active=1, removed_date=NULL, "
                              "sector=?, industry=COALESCE(?, industry), "
                              "company=COALESCE(?, company), isin=COALESCE(?, isin) "
                              "WHERE symbol=?", (sec, ind, r.get("company"),
                                                 r.get("isin"), s))
                else:
                    c.execute("INSERT INTO universe(symbol,active,sector,industry,company,"
                              "isin,added_date) VALUES(?,?,?,?,?,?,?)",
                              (s, 1, sec, ind, r.get("company"), r.get("isin"), today))
            c.execute("UPDATE uploads SET status='applied', applied_at=? WHERE id=?",
                      (now_ist().isoformat(), upload_id))
        await self.store.tx(fn)
        await self.store.set_json("universe_last_applied", today)
        await self._audit(None, None, "universe_applied",
                          {"upload_id": upload_id, "joined": len(diff["joined"]),
                           "left": diff["left"], "assignments": assignments})
        self.status["mapped"] = False              # map + backfill the new names
        return {"applied": True, "joined": len(diff["joined"]),
                "left": len(diff["left"])}

    # --------------------------------------------- corporate actions (§B-SPEC-3)

    async def corp_add(self, symbol, ex_date, kind, factor, note) -> int:
        date.fromisoformat(ex_date)
        if factor <= 0:
            raise ValueError("factor must be positive")
        return await self.store.tx(lambda c: c.execute(
            "INSERT INTO corp_actions(symbol,ex_date,kind,factor,note,created_at) "
            "VALUES(?,?,?,?,?,?)", (symbol.upper(), ex_date, kind, factor, note,
                                    now_ist().isoformat())).lastrowid)

    async def corp_apply(self, action_id: int) -> dict:
        acts = await self.store.q("SELECT * FROM corp_actions WHERE id=?", (action_id,))
        if not acts or acts[0]["status"] != "pending":
            raise ValueError("unknown or already-resolved action")
        a = acts[0]
        f, sym = float(a["factor"]), a["symbol"]
        pre = {}
        last = await self._closes({sym}, now_ist().date().isoformat())
        for sid, st in self.state.items():
            book = Book.from_record(st["book"])
            p = book.positions.get(sym)
            if p:
                pre[sid] = p.as_dict()
                new_sh = int(p.shares * f)
                ref = last.get(sym, p.entry_price)
                book.cash += (p.shares * f - new_sh) * (ref / f)
                p.shares = new_sh
                for k in ("entry_price", "entry_ref_price", "atr_entry", "stop_level",
                          "peak_close", "last_atr"):
                    v = getattr(p, k)
                    if v is not None:
                        setattr(p, k, v / f)
                st["book"] = book.to_record()
            for d in st["pending"]:
                for e in d["decision"]["entries"]:
                    if e["symbol"] == sym:
                        for k in ("atr_entry", "ref_price"):
                            if e.get(k) is not None:
                                e[k] /= f
            await self._save_state(sid)
        await self.store.x("UPDATE corp_actions SET status='applied', applied_at=?, "
                           "prestate_json=? WHERE id=?",
                           (now_ist().isoformat(), json.dumps(pre), action_id))
        await self.store.delete_bars(sym)          # Dhan back-adjusts history
        if creds.current.ok:
            rows = await self.universe_rows()
            sid_ = (rows.get(sym) or {}).get("security_id")
            if sid_:
                await self._fetch(sym, sid_, HISTORY_BARS, False)
        await self._audit(None, None, "corp_action_applied",
                          {"id": action_id, "symbol": sym, "factor": f,
                           "prestate": pre})
        return {"adjusted": list(pre)}

    async def corp_reject(self, action_id: int) -> None:
        await self.store.x("UPDATE corp_actions SET status='rejected' WHERE id=?",
                           (action_id,))

    # ============================================================ snapshot

    async def snapshot(self, sid: str) -> dict:
        if not self.store.available:
            return {"boot": self.status["boot"]}
        st = self.state[sid]
        book = Book.from_record(st["book"])
        last = st["last_evaluated_bar_date"] or now_ist().date().isoformat()
        closes = await self._closes(set(book.positions), last)
        rows_u = await self.universe_rows()
        nav_px, positions = {}, []
        for s, p in book.positions.items():
            live = self.prices.get(s)
            px = live[0] if live else closes.get(s)
            nav_px[s] = px
            positions.append({
                **p.as_dict(), "current_price": px,
                "current_price_ts": (datetime.fromtimestamp(live[1], config.IST).isoformat()
                                     if live else f"{last} close"),
                "value": p.shares * (px or 0),
                "unrealised_pnl": p.shares * (px or 0) + p.realised - p.invested,
                "unrealised_pct": ((p.shares * (px or 0) + p.realised - p.invested)
                                   / p.invested * 100) if p.invested else None,
                "left_index": not (rows_u.get(s) or {}).get("active", 0)})
        nav = book.nav(nav_px)
        for x in positions:
            x["actual_weight"] = x["value"] / nav if nav else None
        curve = await self.store.q("SELECT * FROM portfolio_daily WHERE strategy_id=? "
                                   "ORDER BY date", (sid,))
        trades = await self.store.q("SELECT * FROM trades WHERE strategy_id=? "
                                    "ORDER BY trade_id DESC LIMIT 300", (sid,))
        orders = await self.store.q("SELECT * FROM orders WHERE strategy_id=? AND status "
                                    "IN ('PENDING','PENDING_NO_PRICE') ORDER BY order_id",
                                    (sid,))
        fills = await self.store.q("SELECT * FROM orders WHERE strategy_id=? AND status="
                                   "'FILLED' ORDER BY order_id DESC LIMIT 200", (sid,))
        signals = await self.store.q("SELECT * FROM signals WHERE strategy_id=? ORDER BY "
                                     "signal_id DESC LIMIT 300", (sid,))
        out = {
            "strategy": sid, **STRATS[sid], "version": VERSION,
            "status": st["status"], "halt_reason": st["halt_reason"],
            "go_live": st["go_live"], "last_evaluated_bar_date": st["last_evaluated_bar_date"],
            "last_eval_ts": st["last_eval_ts"], "last_fill_ts": st["last_fill_ts"],
            "cash": book.cash, "nav": nav, "starting_capital": book.starting_capital,
            "invested": sum(x["value"] for x in positions),
            "realised_cum": st["realised_cum"],
            "unrealised": sum(x["unrealised_pnl"] for x in positions),
            "drawdown": (nav / st["peak_nav"] - 1) if st["peak_nav"] else None,
            "slippage_bps": self.slippage_bps, "slippage_cum": st["slippage_cum"],
            "positions": sorted(positions, key=lambda x: (x["group"], x["symbol"])),
            "pending_orders": orders, "recent_fills": fills,
            "trades": trades, "signals": signals, "curve": curve,
            "seated": st["seated"], "health": self._health(sid),
        }
        if sid == "sector":
            last_scores = await self.store.q(
                "SELECT * FROM sector_scores WHERE date=(SELECT MAX(date) FROM "
                "sector_scores) ORDER BY rank")
            out["sector_ranking"] = last_scores
        return out

    def _health(self, sid: str) -> list[dict]:
        st = self.state[sid]
        out = []
        today = now_ist().date().isoformat()
        if st["last_evaluated_bar_date"]:
            try:
                behind = self.cal.sessions_between(st["last_evaluated_bar_date"], today)
                if len(behind) > 1:
                    out.append({"level": "critical",
                                "msg": f"{len(behind)} sessions behind ({', '.join(behind)})"})
            except CalendarGap as exc:
                out.append({"level": "critical", "msg": str(exc)})
        for s, p in st["book"]["positions"].items():
            if p.get("armed") and p.get("stop_level") is None:
                out.append({"level": "critical", "msg": f"{s}: armed without a stop"})
        for r in st["retry"]:
            if r.get("attempts", 1) >= MAX_PENDING_SESSIONS:
                out.append({"level": "warn",
                            "msg": f"{r['side']} {r['symbol']} pending "
                                   f"{r['attempts']} sessions without a price"})
        return out

    async def overview(self) -> dict:
        today = now_ist().date().isoformat()
        rows = await self.universe_rows()
        imap = await self.store.q("SELECT * FROM index_map ORDER BY name")
        idx_last = {}
        for n in ALL_INDICES:
            r = await self.store.q("SELECT date, c FROM bars WHERE symbol=? ORDER BY "
                                   "date DESC LIMIT 1", (IDX_PREFIX + n,))
            idx_last[n] = r[0] if r else None
        ups = await self.store.q("SELECT id,created_at,filename,format,diff_json,status "
                                 "FROM uploads ORDER BY id DESC LIMIT 5")
        for u in ups:
            u["diff"] = json.loads(u.pop("diff_json") or "{}")
        last_applied = await self.store.get_json("universe_last_applied")
        reminders = []
        r = self.cal.reminder(today)
        if r:
            reminders.append(r)
        if not last_applied or last_applied[:7] != today[:7]:
            seen = last_applied or "never - using the 30 Oct 2026 seed list"
            reminders.append(f"Upload this month's NSE ind_nifty500list "
                             f"(last applied: {seen})")
        active = [s for s, r_ in rows.items() if r_["active"]]
        audit = await self.store.q("SELECT * FROM audit_log ORDER BY id DESC LIMIT 100")
        corp = await self.store.q("SELECT id,symbol,ex_date,kind,factor,status,note "
                                  "FROM corp_actions ORDER BY id DESC")
        return {
            "boot": self.status["boot"], "errors": self.status["errors"],
            "data": self.status["data"], "ltp_error": self.status.get("ltp_error"),
            "calendar": self.cal.to_record(), "reminders": reminders,
            "universe": {"active": len(active),
                         "tradable": len([s for s in active if s not in U.EXCLUDED]),
                         "sector_mapped": len([s for s in active if rows[s]["sector"]
                                               and s not in U.EXCLUDED]),
                         "unmapped_ids": sorted(s for s in active
                                                if not rows[s]["security_id"]),
                         "left_but_held": sorted(s for s in self._held()
                                                 if not (rows.get(s) or {}).get("active")),
                         "last_applied": last_applied},
            "index_map": imap, "index_last_bar": idx_last, "uploads": ups,
            "audit": audit, "corp_actions": corp, "db_path": self.db_path,
            "known_differences": KNOWN_DIFFERENCES, "build": S23_BUILD,
            "strategies": {sid: {"status": st["status"], "halt_reason": st["halt_reason"],
                                 "last_evaluated_bar_date": st["last_evaluated_bar_date"],
                                 "go_live": st["go_live"]}
                           for sid, st in self.state.items()},
        }


s23hub = StrategiesHub()
