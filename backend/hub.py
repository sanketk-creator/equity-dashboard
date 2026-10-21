"""
EquityHub — orchestration around the pure engine.

The clock: one asyncio loop. At OPEN_FILL_TIME on a session day it fills the
frozen plan at live opens; at EOD_TIME it ingests the day's completed bars
and runs the close pipeline. Everything is also runnable by hand from the
Diagnostics page, and every step is idempotent per date, so a service that
was down simply CATCHES UP from stored daily bars the next time it runs —
a missed morning's fills use that day's recorded open, which is the same
price the live run would have used (recorded as a late fill).

Session calendar = days the benchmark index printed a daily bar. No bar, no
session: holidays never create phantom evaluation days.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date as _date
from datetime import datetime, timedelta

from . import config, creds, engine
from .engine import Book
from .rest import DhanRest
from .storage import Storage

log = logging.getLogger("equity.hub")

BENCH = "__NIFTY50__"


def now_ist() -> datetime:
    return datetime.now(config.IST)


class EquityHub:
    def __init__(self):
        self.store = Storage(config.DB_PATH)
        self.rest = DhanRest()
        self.params: dict = dict(config.STRATEGY_PARAMS)
        self.book: Book = Book(starting_capital=config.STARTING_CAPITAL,
                               max_holdings=int(self.params["max_holdings"]))
        self.plan: dict | None = None
        self.scored: dict = {}
        self.symbol_ids: dict[str, str] = {}
        self.unmapped: list[str] = []
        self.status: dict = {"boot": "starting", "last_eod": None,
                             "last_open_fill": None, "errors": []}
        self._tasks: list[asyncio.Task] = []

    # ----------------------------------------------------------------- boot

    async def start(self) -> None:
        if not self.store.available:
            # The paper book MUST persist. Running memory-only would silently
            # lose positions on the next restart, so nothing starts; the page
            # itself says exactly what to fix.
            self.status["boot"] = (
                f"STORAGE UNAVAILABLE: {self.store.error} — the database "
                f"cannot be opened. On Render: add a Disk mounted at "
                f"/var/data (Disks section, 1 GB) so DB_PATH="
                f"{config.DB_PATH} is writable, then redeploy.")
            self.status["errors"] = [self.status["boot"]]
            log.error(self.status["boot"])
            return
        await self._load_params()
        await self._seed_constituents()
        await self._restore_state()
        await self._load_saved_token()
        self._tasks.append(asyncio.create_task(self._token_loop(), name="token"))
        self._tasks.append(asyncio.create_task(self._boot_data(), name="bootdata"))
        self._tasks.append(asyncio.create_task(self._clock(), name="clock"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.rest.aclose()
        self.store.close()

    async def _load_params(self) -> None:
        ov = await self.store.get_json("params", {}) or {}
        clean = {}
        for k, v in ov.items():
            if k in config.STRATEGY_PARAMS:
                clean[k] = v
        self.params = {**config.STRATEGY_PARAMS, **clean}

    async def set_params(self, overrides: dict | None) -> None:
        from .params import validate_overrides
        clean = validate_overrides(overrides or {})
        await self.store.set_json("params", clean)
        self.params = {**config.STRATEGY_PARAMS, **clean}

    async def _seed_constituents(self) -> None:
        existing = {c["symbol"] for c in await self.store.constituents()}
        if existing:
            return
        today = now_ist().date().isoformat()
        for s in config.UNIVERSE_SEED:
            note = config.SEED_EXCLUDED.get(s)
            await self.store.upsert_constituent(
                s, active=0 if note else 1, added_date=today, note=note)
        log.info("Seeded %d constituents (%d excluded)",
                 len(config.UNIVERSE_SEED), len(config.SEED_EXCLUDED))

    async def _restore_state(self) -> None:
        rec = await self.store.get_json("book")
        if rec:
            self.book = Book.from_record(rec)
            log.info("Restored book: cash ₹%.0f, %d holdings", self.book.cash,
                     len(self.book.held_symbols()))
        self.plan = await self.store.get_json("plan")
        self.status["last_eod"] = await self.store.get_meta("last_eod")

    async def _persist(self) -> None:
        await self.store.set_json("book", self.book.to_record())
        await self.store.set_json("plan", self.plan)

    # ----------------------------------------------------------------- token

    async def _load_saved_token(self) -> None:
        """A token saved earlier (pasted or auto-fetched) survives restarts;
        it beats the env token unless it is already expired."""
        from .auth import seconds_until_expiry
        saved = await self.store.get_meta("dhan_access_token")
        saved_cid = await self.store.get_meta("dhan_client_id")
        if not saved:
            return
        left = seconds_until_expiry(saved)
        if left is not None and left <= 0:
            log.info("Saved token already expired; keeping the environment one")
            return
        creds.current.set(saved_cid or creds.current.client_id, saved, "saved")
        log.info("Using the saved access token (%.1f hours left)",
                 (left or 0) / 3600)

    async def update_credentials(self, access_token: str,
                                 client_id: str | None = None,
                                 source: str = "saved") -> dict:
        access_token = (access_token or "").strip()
        if not access_token:
            raise ValueError("Access token is empty")
        creds.current.set(client_id or creds.current.client_id,
                          access_token, source)
        creds.current.last_refresh_at = __import__("time").time()
        creds.current.last_refresh_error = None
        await self.store.set_meta("dhan_access_token", access_token)
        await self.store.set_meta("dhan_client_id", creds.current.client_id)
        log.info("Access token updated (source=%s)", source)
        if self.status["boot"].startswith("boot error") or self.unmapped:
            self._tasks.append(asyncio.create_task(self._boot_data(),
                                                   name="bootdata-retry"))
        return creds.current.status()

    async def refresh_token_now(self) -> dict:
        """Generate a fresh 24h token from the stored PIN + TOTP secret —
        the same mechanism the Nifty dashboard runs (backend/auth.py)."""
        from .auth import TokenRefreshError, fetch_access_token
        from .creds import auto_refresh_configured, dhan_pin, totp_secret
        if not auto_refresh_configured():
            raise TokenRefreshError(
                "Automatic refresh isn't set up. Add DHAN_PIN and "
                "DHAN_TOTP_SECRET in Render, or paste a token manually.")
        result = await fetch_access_token(creds.current.client_id,
                                          dhan_pin(), totp_secret())
        await self.update_credentials(result["access_token"], source="auto")
        return creds.current.status()

    async def _token_loop(self) -> None:
        """Every 5 minutes: if auto-refresh is configured and the token is
        missing, expired or within 3 hours of expiry, fetch a fresh one. A
        failure is recorded and retried — and even a whole missed evening
        costs nothing, because run_eod catches up from stored bars."""
        from .auth import TokenRefreshError
        from .creds import auto_refresh_configured
        await asyncio.sleep(5)
        while True:
            try:
                if auto_refresh_configured() and (
                        not creds.current.access_token
                        or creds.current.expired()
                        or creds.current.expires_within(3 * 3600)):
                    log.info("Token missing or expiring soon — refreshing")
                    await self.refresh_token_now()
            except TokenRefreshError as exc:
                creds.current.last_refresh_error = str(exc)
                creds.current.last_refresh_at = __import__("time").time()
                log.warning("Automatic token refresh failed: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                creds.current.last_refresh_error = f"{type(exc).__name__}: {exc}"
                log.exception("Token loop error")
            await asyncio.sleep(300)

    # ------------------------------------------------------------ boot data

    async def _boot_data(self) -> None:
        self.status["boot"] = "mapping symbols"
        try:
            await self._map_symbols()
            self.status["boot"] = "backfilling bars"
            await self._backfill_missing()
            self.status["boot"] = "ready"
            # Catch up any sessions that completed while the service was down.
            await self.run_eod()
        except Exception as exc:
            self.status["boot"] = f"boot error: {exc}"
            log.exception("boot data failed")

    async def _map_symbols(self) -> None:
        cons = await self.store.constituents()
        missing = [c["symbol"] for c in cons if not c["security_id"]]
        if not missing:
            self.symbol_ids = {c["symbol"]: c["security_id"] for c in cons
                               if c["security_id"]}
            return
        if not creds.current.ok:
            self.status["errors"].append("No Dhan credentials — cannot map symbols")
            return
        master = await self.rest.scrip_master_nse_eq()
        self.unmapped = []
        for c in cons:
            sid = c["security_id"] or master.get(c["symbol"])
            if sid:
                await self.store.upsert_constituent(c["symbol"], security_id=sid)
                self.symbol_ids[c["symbol"]] = sid
            else:
                self.unmapped.append(c["symbol"])
        if self.unmapped:
            log.warning("Unmapped symbols (no NSE_EQ scrip-master row): %s",
                        ", ".join(self.unmapped))

    async def _active_symbols(self) -> list[str]:
        return [c["symbol"] for c in await self.store.constituents()
                if c["active"] and c["security_id"]]

    async def _backfill_missing(self, force: list[str] | None = None) -> None:
        """252-bar rule: a symbol needs ~330 calendar-fetched sessions before it
        can be ranked. Fetch whatever is missing; pace the API."""
        if not creds.current.ok:
            return
        # benchmark first — it IS the session calendar
        bench = await self.rest.index_history(config.BENCHMARK_SECURITY_ID,
                                              config.BACKFILL_SESSIONS)
        await self.store.upsert_bars(BENCH, bench)
        syms = force if force is not None else await self._active_symbols()
        for s in syms:
            sid = self.symbol_ids.get(s)
            if not sid:
                continue
            have = await self.store.dates_with_bars(s)
            if force is None and len(have) >= 260:
                continue
            try:
                bars = await self.rest.daily_history(sid, config.BACKFILL_SESSIONS)
                for b in bars:
                    b["volume_adj"] = b["volume"]
                await self.store.upsert_bars(s, bars)
                await self._reapply_volume_factors(s)
            except Exception as exc:
                log.warning("backfill %s failed: %s", s, exc)
            await asyncio.sleep(0.35)

    async def _reapply_volume_factors(self, symbol: str) -> None:
        """After any full refetch, volume_adj starts as raw volume; re-apply
        every APPLIED, non-skip corporate-action factor to pre-ex dates."""
        for a in await self.store.corp_actions("applied"):
            if a["symbol"] == symbol and a["factor"] and not a["skip_volume"]:
                await self.store.scale_volume_adj_before(
                    symbol, a["ex_date"], float(a["factor"]))

    # ---------------------------------------------------------------- clock

    async def _clock(self) -> None:
        fired: set[str] = set()
        while True:
            try:
                now = now_ist()
                hm = now.strftime("%H:%M")
                key_open = f"open:{now.date()}"
                key_eod = f"eod:{now.date()}"
                if hm >= config.OPEN_FILL_TIME and hm < "15:30" \
                        and key_open not in fired and self.status["boot"] == "ready":
                    fired.add(key_open)
                    await self.run_open_fill()
                if hm >= config.EOD_TIME and key_eod not in fired \
                        and self.status["boot"] == "ready":
                    fired.add(key_eod)
                    await self.run_eod()
            except Exception:
                log.exception("clock tick failed")
            await asyncio.sleep(20)

    # --------------------------------------------------------- the pipeline

    async def _bench_dates(self) -> list[str]:
        bars = await self.store.bars_for(BENCH, 500)
        return [b["date"] for b in bars]

    async def _bars_asof(self, symbol: str, date_iso: str,
                         limit: int = 400) -> list[dict]:
        bars = await self.store.bars_for(symbol, limit + 60)
        return [b for b in bars if b["date"] <= date_iso][-limit:]

    async def process_session(self, date_iso: str) -> None:
        """Fill the pending plan at this session's open (from the stored bar
        when live fills didn't run), then evaluate this session's close."""
        p = self.params
        symbols = await self._active_symbols()

        # -- late fill from stored opens, if the 09:20 job never ran ----------
        if self.plan and not self.plan.get("filled") \
                and self.plan["signal_date"] < date_iso:
            opens: dict[str, float] = {}
            for s in {x["symbol"] for x in
                      self.plan["exits"] + self.plan["entries"]}:
                bs = await self._bars_asof(s, date_iso, 2)
                if bs and bs[-1]["date"] == date_iso:
                    opens[s] = bs[-1]["o"]
            closes_prev = await self._latest_closes(symbols, before=date_iso)
            result = engine.fill_open(self.book, self.plan, opens, date_iso,
                                      closes_prev, p)
            for t in result["trades"]:
                await self.store.add_trade(t)
            if result["notes"]:
                log.info("late fill %s: %s", date_iso, "; ".join(result["notes"]))
            self.plan["late_fill"] = True
            self.status["last_open_fill"] = f"{date_iso} (late, from stored open)"

        # -- evaluate this close ----------------------------------------------
        metrics: dict[str, dict] = {}
        closes: dict[str, float] = {}
        eligible: set[str] = set()
        for s in symbols:
            bars = await self._bars_asof(s, date_iso)
            if not bars or bars[-1]["date"] != date_iso:
                continue                     # no completed bar today: ineligible
            m = engine.compute_metrics(bars, p)
            metrics[s] = m
            closes[s] = m["close"]
            eligible.add(s)
        # held symbols that lost membership still need closes for marking/stops
        for pos in self.book.positions:
            if pos is not None and pos.symbol not in metrics:
                bars = await self._bars_asof(pos.symbol, date_iso)
                if bars and bars[-1]["date"] == date_iso:
                    m = engine.compute_metrics(bars, p)
                    metrics[pos.symbol] = m
                    closes[pos.symbol] = m["close"]

        self.scored = engine.score_universe(metrics, p)
        held_exit_carry = []
        if self.plan and self.plan.get("filled"):
            # exits that found no print carry into the new plan verbatim
            held_exit_carry = [e for e in self.plan.get("held", [])]
        new_plan = engine.evaluate_close(self.book, self.scored, date_iso,
                                         eligible, p)
        for e in held_exit_carry:
            if e["symbol"] in self.book.held_symbols() \
                    and e["symbol"] not in {x["symbol"] for x in new_plan["exits"]}:
                new_plan["exits"].insert(0, e)
        self.plan = new_plan

        bench_bar = await self._bars_asof(BENCH, date_iso, 1)
        bench_close = bench_bar[-1]["c"] if bench_bar and \
            bench_bar[-1]["date"] == date_iso else None
        eq = self.book.equity(closes)
        await self.store.upsert_equity_point(
            date_iso, round(eq, 2), round(self.book.cash, 2),
            len(self.book.held_symbols()), bench_close)
        await self.store.set_meta("last_eod", date_iso)
        self.status["last_eod"] = date_iso
        await self._persist()
        log.info("EOD %s: equity ₹%.0f, %d held, %d qualified, plan %d exits "
                 "/ %d entries", date_iso, eq, len(self.book.held_symbols()),
                 new_plan["qualified"], len(new_plan["exits"]),
                 len(new_plan["entries"]))

    async def _latest_closes(self, symbols: list[str],
                             before: str) -> dict[str, float]:
        out = {}
        for s in set(symbols) | self.book.held_symbols():
            bars = await self._bars_asof(s, before, 3)
            bars = [b for b in bars if b["date"] < before]
            if bars:
                out[s] = bars[-1]["c"]
        return out

    async def run_eod(self) -> None:
        """Ingest today's bars, then process every session not yet processed,
        in order — this IS the catch-up mechanism."""
        if not creds.current.ok:
            self.status["errors"] = ["EOD skipped: Dhan token missing/expired "
                                     "— renew on Diagnostics"][:]
            return
        try:
            bench = await self.rest.index_history(config.BENCHMARK_SECURITY_ID, 12)
            await self.store.upsert_bars(BENCH, bench)
            for s in await self._active_symbols():
                sid = self.symbol_ids.get(s)
                if not sid:
                    continue
                try:
                    bars = await self.rest.daily_history(sid, 12)
                    for b in bars:
                        b["volume_adj"] = b["volume"]
                    have = await self.store.dates_with_bars(s)
                    new = [b for b in bars if b["date"] not in have]
                    if new:
                        await self.store.upsert_bars(s, new)
                except Exception as exc:
                    log.warning("EOD bars %s: %s", s, exc)
                await asyncio.sleep(0.25)
        except Exception as exc:
            self.status["errors"] = [f"EOD bar ingest failed: {exc}"]
            return

        last = await self.store.get_meta("last_eod") or "1970-01-01"
        today = now_ist().date().isoformat()
        cutoff = today if now_ist().strftime("%H:%M") >= config.EOD_TIME \
            else (now_ist().date() - timedelta(days=1)).isoformat()
        for d in [d for d in await self._bench_dates()
                  if last < d <= cutoff]:
            await self.process_session(d)
        self.status["errors"] = []

    async def run_open_fill(self) -> None:
        """09:20 on a session day: fill the frozen plan at live opens."""
        if not self.plan or self.plan.get("filled"):
            return
        if not creds.current.ok:
            self.status["errors"] = ["Open fill skipped: token missing — plan "
                                     "will late-fill from the stored open"]
            return
        syms = {x["symbol"] for x in self.plan["exits"] + self.plan["entries"]}
        ids = [self.symbol_ids[s] for s in syms if s in self.symbol_ids]
        try:
            quotes = await self.rest.ohlc(ids)
        except Exception as exc:
            self.status["errors"] = [f"Open fill failed: {exc} — will "
                                     f"late-fill from the stored open"]
            return
        opens: dict[str, float] = {}
        for s in syms:
            q = quotes.get(str(self.symbol_ids.get(s)))
            if q and q.get("open"):
                opens[s] = float(q["open"])
        if not opens:
            return                           # holiday: no prints, plan waits
        today = now_ist().date().isoformat()
        closes_prev = await self._latest_closes(list(syms), before=today)
        result = engine.fill_open(self.book, self.plan, opens, today,
                                  closes_prev, self.params)
        for t in result["trades"]:
            await self.store.add_trade(t)
        self.plan["held"] = result["held_exits"]
        self.status["last_open_fill"] = today
        await self._persist()
        log.info("Open fill %s: %d trades, notes: %s", today,
                 len(result["trades"]), "; ".join(result["notes"]) or "none")

    # ------------------------------------------------------- corporate actions

    async def apply_corp_action(self, action_id: int) -> dict:
        acts = {a["id"]: a for a in await self.store.corp_actions()}
        a = acts.get(action_id)
        if not a or a["status"] != "pending":
            raise ValueError("unknown or already-resolved action")
        f = float(a["factor"] or 0)
        if f <= 0:
            raise ValueError("a positive factor is required to apply")
        sym, ex = a["symbol"], a["ex_date"]
        archived = None
        for pos in self.book.positions:
            if pos is not None and pos.symbol == sym:
                archived = pos.as_dict()
                last = await self._bars_asof(sym, now_ist().date().isoformat(), 1)
                last_close = last[-1]["c"] if last else pos.entry_price
                new_shares = int(pos.shares * f)
                leftover = pos.shares * f - new_shares
                cash_credit = leftover * (last_close / f)
                pos.shares = float(new_shares)
                pos.entry_price /= f
                pos.entry_atr /= f
                pos.initial_stop /= f
                if pos.trail_stop is not None:
                    pos.trail_stop /= f
                if pos.highest_close is not None:
                    pos.highest_close /= f
                self.book.cash += cash_credit
                await self.store.set_json(
                    f"corp_action_prestate:{action_id}",
                    {"position": archived, "cash_credit": round(cash_credit, 2)})
        if not a["skip_volume"]:
            await self.store.scale_volume_adj_before(sym, ex, f)
        await self.store.update_corp_action(
            action_id, status="applied", applied_at=now_ist().isoformat())
        # Dhan back-adjusts history at the ex-date: the stored series is stale.
        await self.store.delete_bars(sym)
        await self._backfill_missing(force=[sym])
        await self._persist()
        return {"adjusted_position": archived is not None}

    # --------------------------------------------------------------- snapshot

    async def snapshot(self) -> dict:
        if not self.store.available:
            return {"build": config.BUILD, "time_ist": now_ist().isoformat(),
                    "status": self.status, "credentials": creds.current.status(),
                    "params": self.params,
                    "params_default": dict(config.STRATEGY_PARAMS),
                    "param_choices": config.PARAM_CHOICES,
                    "book": self.book.to_record(), "equity": None, "plan": None,
                    "candidates": [], "trades": [], "curve": [],
                    "benchmark_label": config.BENCHMARK_LABEL,
                    "known_differences": config.KNOWN_DIFFERENCES,
                    "unmapped": [], "corp_actions": [], "constituents": [],
                    "stats": {}, "rates_verified": config.RATES_VERIFIED}
        curve = await self.store.equity_curve()
        trades = await self.store.trades(300)
        closes = {}
        if self.scored:
            closes = {s: m["close"] for s, m in self.scored.items()
                      if m.get("close") is not None}
        candidates = sorted(
            (dict(symbol=s, score=m["score"], mom121=m.get("mom121"),
                  c1=m["c1"], c2=m["c2"], c3=m["c3"], c4=m["c4"],
                  close=m.get("close"), rankable=m["rankable"])
             for s, m in self.scored.items()),
            key=lambda d: (-(d["score"]), -(d["mom121"] or -9)))[:40]
        return {
            "build": config.BUILD,
            "time_ist": now_ist().isoformat(),
            "status": self.status,
            "credentials": creds.current.status(),
            "params": self.params,
            "params_default": dict(config.STRATEGY_PARAMS),
            "param_choices": config.PARAM_CHOICES,
            "book": self.book.to_record(),
            "equity": self.book.equity(closes) if closes else None,
            "plan": self.plan,
            "candidates": candidates,
            "trades": trades,
            "curve": curve,
            "benchmark_label": config.BENCHMARK_LABEL,
            "known_differences": config.KNOWN_DIFFERENCES,
            "unmapped": self.unmapped,
            "corp_actions": await self.store.corp_actions(),
            "constituents": await self.store.constituents(),
            "stats": self._stats(curve, trades),
            "rates_verified": config.RATES_VERIFIED,
        }

    @staticmethod
    def _stats(curve: list[dict], trades: list[dict]) -> dict:
        out: dict = {"trades": len([t for t in trades if t.get("exit_date")])}
        closed = [t for t in trades if t.get("net_pnl") is not None]
        wins = [t for t in closed if t["net_pnl"] > 0]
        out["win_rate"] = round(100 * len(wins) / len(closed), 1) if closed else None
        if curve:
            eq = [c["equity"] for c in curve]
            peak, dd = eq[0], 0.0
            for v in eq:
                peak = max(peak, v)
                dd = min(dd, v / peak - 1)
            out["max_drawdown_pct"] = round(dd * 100, 2)
            out["total_return_pct"] = round((eq[-1] / eq[0] - 1) * 100, 2)
        return out


hub = EquityHub()
