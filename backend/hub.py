"""
EquityHub — orchestration around the pure engines.

Two books run on the SAME frozen daily signals:

  LIVE    the Hourly Exit Specification: stops checked on hourly closes,
          fills at the next hourly bar's open (backend/intraday.py).
  SHADOW  the backtest's daily-close rule (backend/engine.py, unchanged) —
          so the cost of the exit convention is visible on its own (§9).

The day, in order:

  overnight / pre-open   DAILY FREEZE for session T. Dhan's daily API is
                         T-1, so the job polls every few minutes until every
                         symbol has T's completed bar, then: commits session
                         T of the live book (replayed once more with the
                         authoritative daily-bar open), ratchets trails from
                         T's close, scores and ranks, freezes T+1's plan,
                         and runs the shadow book's T open + T close.
  09:00-15:30            the live feed builds hourly bars; the session is
                         replayed every few seconds from (committed book,
                         frozen plan, recorded bars) — so a restart loses
                         nothing that was recorded.

Nothing before the first freeze is ever simulated: the first freeze is
GO-LIVE, and both books start there with the starting capital and no
positions (operator decision: fresh book).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from . import config, creds, engine, intraday
from .engine import Book
from .recorder import HourlyRecorder
from .rest import DhanRest
from .storage import Storage

log = logging.getLogger("equity.hub")

BENCH = "__NIFTY50__"


def now_ist() -> datetime:
    return datetime.now(config.IST)


def _sec_of_day(dt: datetime) -> float:
    return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6


def stale_plan_from(result: dict) -> dict:
    """The plan for a session whose prior freeze has NOT landed: stops held
    at their last frozen levels, no entries or refills."""
    return {"signal_date": None, "fresh": False, "stops": result["stops"],
            "ranked": [], "carry_exits": result["carry_exits"],
            "prev_close": result["marks"]}


class EquityHub:
    def __init__(self):
        self.store = Storage(config.DB_PATH)
        self.rest = DhanRest()
        self.params: dict = dict(config.STRATEGY_PARAMS)
        mh = int(self.params["max_holdings"])
        self.book: Book = Book(starting_capital=config.STARTING_CAPITAL,
                               max_holdings=mh)
        self.plan: dict | None = None                  # live plan, next session
        self.shadow_book: Book = Book(starting_capital=config.STARTING_CAPITAL,
                                      max_holdings=mh)
        self.shadow_plan: dict | None = None
        self.scored: dict = {}
        self.symbol_ids: dict[str, str] = {}
        self.id_to_symbol: dict[str, str] = {}
        self.unmapped: list[str] = []
        self.recorder = HourlyRecorder()
        self.feed = None
        self.live_chain: list[dict] = []
        self.force_freeze: dict[str, set[str]] = {}
        self._opens_fetched: str | None = None
        self._opens_last_try = 0.0
        self._daily_lock = asyncio.Lock()
        self._freezing = False                     # mid-commit: don't redraw
        self.status: dict = {"boot": "starting", "last_eod": None,
                             "go_live": None, "first_session": None,
                             "errors": [],
                             "daily": {"state": "idle"}}
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
        self._tasks.append(asyncio.create_task(self._session_loop(), name="session"))
        self._tasks.append(asyncio.create_task(self._daily_loop(), name="daily"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.feed is not None:
            try:
                await self.feed.stop()
            except Exception:
                pass
        try:
            await self._flush_recorder()
        except Exception:
            pass
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
        plan = await self.store.get_json("plan")
        # A v1-v5 plan (daily-fill shape: exits/entries, no 'fresh') is not
        # an hourly plan. Those builds never froze a session on Render (no
        # daily data), so it is dropped, loudly, rather than misread.
        if plan and "fresh" not in plan:
            log.warning("Dropping a pre-v6 plan record (%s)", plan.get("signal_date"))
            plan = None
        self.plan = plan
        srec = await self.store.get_json("shadow_book")
        if srec:
            self.shadow_book = Book.from_record(srec)
        self.shadow_plan = await self.store.get_json("shadow_plan")
        self.status["last_eod"] = await self.store.get_meta("last_eod")
        self.status["go_live"] = await self.store.get_meta("go_live")
        self.status["first_session"] = await self.store.get_meta("first_session")
        if self.plan is None and self.status["last_eod"] and not self.status["go_live"]:
            # legacy last_eod without a v6 freeze: start clean
            self.status["last_eod"] = None
        log.info("Restored: live cash ₹%.0f / %d held; shadow %d held; "
                 "last freeze %s", self.book.cash, len(self.book.held_symbols()),
                 len(self.shadow_book.held_symbols()), self.status["last_eod"])

    async def _persist(self) -> None:
        await self.store.set_json("book", self.book.to_record())
        await self.store.set_json("plan", self.plan)
        await self.store.set_json("shadow_book", self.shadow_book.to_record())
        await self.store.set_json("shadow_plan", self.shadow_plan)

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
        creds.current.last_refresh_at = time.time()
        creds.current.last_refresh_error = None
        await self.store.set_meta("dhan_access_token", access_token)
        await self.store.set_meta("dhan_client_id", creds.current.client_id)
        log.info("Access token updated (source=%s)", source)
        if self.feed is not None:
            await self.feed.reconnect()           # the socket carries the token
        elif self.symbol_ids:
            self._tasks.append(asyncio.create_task(self._start_feed(),
                                                   name="feed-start"))
        if self.status["boot"].startswith("boot error") or self.unmapped \
                or not self.symbol_ids:
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
        missing, expired or within 3 hours of expiry, fetch a fresh one."""
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
                    if self.status["boot"] == "ready":
                        await self.run_daily()
            except TokenRefreshError as exc:
                creds.current.last_refresh_error = str(exc)
                creds.current.last_refresh_at = time.time()
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
            await self._start_feed()
            self.status["boot"] = "backfilling bars"
            await self._backfill_missing()
            self.status["boot"] = "ready"
            await self.run_daily()
        except Exception as exc:
            self.status["boot"] = f"boot error: {exc}"
            log.exception("boot data failed")

    async def _map_symbols(self) -> None:
        cons = await self.store.constituents()
        missing = [c["symbol"] for c in cons if not c["security_id"]]
        if missing and creds.current.ok:
            master = await self.rest.scrip_master_nse_eq()
            self.unmapped = []
            for c in cons:
                sid = c["security_id"] or master.get(c["symbol"])
                if sid:
                    if not c["security_id"]:
                        await self.store.upsert_constituent(c["symbol"],
                                                            security_id=sid)
                else:
                    self.unmapped.append(c["symbol"])
            if self.unmapped:
                log.warning("Unmapped symbols (no NSE_EQ scrip-master row): %s",
                            ", ".join(self.unmapped))
        elif missing:
            self.status["errors"] = ["No Dhan credentials — cannot map symbols"]
        cons = await self.store.constituents()
        self.symbol_ids = {c["symbol"]: str(c["security_id"]) for c in cons
                           if c["security_id"]}
        self.id_to_symbol = {sid: s for s, sid in self.symbol_ids.items()}

    async def _active_symbols(self) -> list[str]:
        return [c["symbol"] for c in await self.store.constituents()
                if c["active"]]

    def _held_anywhere(self) -> set[str]:
        return self.book.held_symbols() | self.shadow_book.held_symbols()

    async def _backfill_missing(self, force: list[str] | None = None) -> None:
        """252-bar rule: a symbol needs ~330 sessions before it can be
        ranked. Fetch whatever is missing; pace the API."""
        if not creds.current.ok:
            return
        try:
            bench = await self.rest.index_history(config.BENCHMARK_SECURITY_ID,
                                                  config.BACKFILL_SESSIONS)
            await self.store.upsert_bars(BENCH, bench)
        except Exception as exc:
            self.status["errors"] = [self._friendly_fetch_error(exc)]
            log.warning("benchmark backfill failed: %s", exc)
            return
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
        for a in await self.store.corp_actions("applied"):
            if a["symbol"] == symbol and a["factor"] and not a["skip_volume"]:
                await self.store.scale_volume_adj_before(
                    symbol, a["ex_date"], float(a["factor"]))

    # ------------------------------------------------------------ live feed

    async def _start_feed(self) -> None:
        if not creds.current.ok or not self.symbol_ids:
            return
        from .feed import DhanFeed, Instrument
        if self.feed is None:
            self.feed = DhanFeed(self._on_tick)
            self.feed.start()
        wanted = set(await self._active_symbols()) | self._held_anywhere()
        ins = [Instrument("NSE_EQ", int(self.symbol_ids[s])) for s in wanted
               if s in self.symbol_ids]
        await self.feed.subscribe(ins, config.REQ_SUBSCRIBE_QUOTE)
        log.info("Feed: %d equities on quote packets", len(ins))

    def _on_tick(self, tick) -> None:
        if tick.kind not in ("ticker", "quote", "full"):
            return
        sym = self.id_to_symbol.get(str(tick.security_id))
        if not sym:
            return
        self.recorder.on_trade(sym, tick.ltp, tick.last_trade_time,
                               tick.open if tick.kind != "ticker" else None)

    async def _flush_recorder(self) -> None:
        if self.recorder.date is None:
            return
        rows, opens = self.recorder.take_dirty()
        if rows:
            await self.store.upsert_hourly(self.recorder.date, rows)
        for s, px, src in opens:
            await self.store.set_session_open(self.recorder.date, s, px, src)

    async def _fetch_official_opens(self, today: str) -> None:
        """Once per session, just after 09:15: the exchange's official open
        for every subscribed symbol (REST beats the feed's day-open)."""
        ids = [self.symbol_ids[s] for s in self.symbol_ids]
        quotes = await self.rest.ohlc(ids)
        got = 0
        for sid, q in quotes.items():
            s = self.id_to_symbol.get(str(sid))
            o = q.get("open")
            if s and o:
                self.recorder.set_official_open(s, float(o), "rest")
                got += 1
        if got >= max(1, int(0.9 * len(ids))):
            self._opens_fetched = today
        log.info("Official opens via REST: %d/%d", got, len(ids))

    # ---------------------------------------------------------- the session

    async def _session_loop(self) -> None:
        while True:
            try:
                await self._session_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("session tick failed")
            await asyncio.sleep(5)

    async def _session_tick(self) -> None:
        if not self.store.available:
            return
        now = now_ist()
        today = now.date().isoformat()
        sec = _sec_of_day(now)
        in_window = now.weekday() < 5 and \
            intraday.SESSION_START - 900 <= sec < intraday.SESSION_END + 900
        if in_window:
            if self.recorder.date != today:
                bars = await self.store.hourly_for_date(today)
                opens = await self.store.session_opens(today)
                self.recorder.reset(today, bars, opens)
            closed, _cur = intraday.progress(sec, config.BAR_GRACE_SECONDS)
            self.recorder.lock_through(closed)
            await self._flush_recorder()
            if (sec >= intraday.SESSION_START + 20
                    and sec < intraday.SESSION_START + 1800
                    and self._opens_fetched != today and creds.current.ok
                    and time.time() - self._opens_last_try > 30):
                self._opens_last_try = time.time()
                try:
                    await self._fetch_official_opens(today)
                except Exception as exc:
                    log.warning("official opens fetch failed: %s", exc)
            await self.refresh_live_view(now)
        elif int(sec) % 60 < 5:
            await self.refresh_live_view(now)

    async def _session_dates(self, now: datetime) -> list[str]:
        """Sessions after the last freeze that the live book must replay."""
        last = self.status["last_eod"] or "1970-01-01"
        dates = set(await self.store.hourly_dates_after(last))
        today = now.date().isoformat()
        if now.weekday() < 5 and _sec_of_day(now) >= intraday.SESSION_START \
                and today > last:
            dates.add(today)
        first = self.status.get("first_session") or ""
        return sorted(d for d in dates if d >= first)

    async def refresh_live_view(self, now: datetime | None = None) -> None:
        now = now or now_ist()
        if self._freezing:
            return                       # book/plan are mid-update
        if self.plan is None:
            self.live_chain = []
            return
        today = now.date().isoformat()
        book_rec, plan = self.book.to_record(), self.plan
        chain = []
        for d in await self._session_dates(now):
            if d == self.recorder.date:
                bars, opens = self.recorder.bars, self.recorder.open_prices()
            else:
                bars = await self.store.hourly_for_date(d)
                opens = {s: v[0] for s, v in (await self.store.session_opens(d)).items()}
            if d == today:
                closed, cur = intraday.progress(_sec_of_day(now),
                                                config.BAR_GRACE_SECONDS)
            else:
                closed, cur = len(intraday.SLOTS), None
            r = intraday.replay_session(d, book_rec, plan, bars, opens,
                                        closed, cur, self.params)
            r["plan_fresh"] = bool(plan.get("fresh"))
            chain.append(r)
            book_rec, plan = r["book"], stale_plan_from(r)
        self.live_chain = chain

    # ------------------------------------------------------------ daily job

    async def _daily_loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                if self.status["boot"] == "ready":
                    await self.run_daily()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("daily job failed")
            await asyncio.sleep(60 * config.DAILY_POLL_MINUTES)

    async def run_eod(self) -> None:          # kept for the old button/API
        await self.run_daily()

    async def _bench_dates(self) -> list[str]:
        bars = await self.store.bars_for(BENCH, 500)
        if bars:
            return [b["date"] for b in bars]
        # No benchmark series (imported data without an index file): the
        # session calendar is majority vote — a date is a session when at
        # least 30% of active symbols printed a bar.
        active = await self._active_symbols()
        need = max(2, int(0.3 * max(1, len(active))))
        return (await self.store.dates_by_coverage(need))[-500:]

    async def _missing_for(self, date_iso: str) -> tuple[list[str], list[str]]:
        """(missing, no_data). A symbol with recent bars but none for this
        session blocks the freeze; a symbol with no data in the last 30 days
        cannot be ranked anyway and is only reported."""
        latest = await self.store.latest_bar_dates()
        floor = (datetime.fromisoformat(date_iso) - timedelta(days=30)).date().isoformat()
        need = set(await self._active_symbols()) | self._held_anywhere()
        missing, no_data = [], []
        for s in sorted(need):
            d = latest.get(s)
            if d is None or d < floor:
                no_data.append(s)
            elif d < date_iso:
                missing.append(s)
        return missing, no_data

    @staticmethod
    def _last_started_session_day(now: datetime) -> str:
        """The most recent weekday whose 09:15 open has passed (a holiday
        only delays go-live by one session — never trades an unwatched one)."""
        d = now.date()
        if not (d.weekday() < 5 and _sec_of_day(now) >= intraday.SESSION_START):
            d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d.isoformat()

    async def _short_history(self, date_iso: str) -> tuple[list[str], int]:
        """Active symbols with a bar on date_iso but fewer than
        min_bars_to_rank bars up to it (listed too recently, or the backfill
        has not run)."""
        need = int(self.params["min_bars_to_rank"])
        short, total = [], 0
        for s in await self._active_symbols():
            bars = await self._bars_asof(s, date_iso, need)
            if not bars or bars[-1]["date"] != date_iso:
                continue
            total += 1
            if len(bars) < need:
                short.append(s)
        return short, total

    async def _fetch_daily(self, symbols: list[str]) -> None:
        for s in symbols:
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
                log.warning("daily bars %s: %s", s, exc)
            await asyncio.sleep(0.25)

    @staticmethod
    def _friendly_fetch_error(exc: Exception) -> str:
        msg = str(exc)
        if "DH-902" in msg or "PLUS subscription" in msg:
            return ("Dhan historical API is not on this client ID's plan "
                    "(DH-902: needs the paid Data API subscription, ₹499 + tax "
                    "a month). Until it is active, no daily freeze can run.")
        return f"Daily bar fetch failed: {msg}"

    async def run_daily(self, fetch: bool = True) -> None:
        async with self._daily_lock:
            await self._run_daily(fetch)

    async def _run_daily(self, fetch: bool) -> None:
        now = now_ist()
        today = now.date().isoformat()
        st = {"state": "checking", "checked_at": now.isoformat()}
        if fetch and creds.current.ok:
            try:
                bench = await self.rest.index_history(config.BENCHMARK_SECURITY_ID, 12)
                await self.store.upsert_bars(BENCH, bench)
                self.status["errors"] = []
            except Exception as exc:
                self.status["errors"] = [self._friendly_fetch_error(exc)]
                fetch = False
        elif fetch:
            self.status["errors"] = ["Daily fetch skipped: Dhan token missing/"
                                     "expired — renew on Diagnostics"]
            fetch = False
        last = self.status["last_eod"]
        done_today = _sec_of_day(now) >= intraday.SESSION_END + 300
        targets = [d for d in await self._bench_dates()
                   if (last is None or d > last)
                   and (d < today or (d == today and done_today))]
        if last is None and targets:
            targets = targets[-1:]              # GO-LIVE at the latest session
            if fetch:
                # e.g. the Data API was switched on after boot: the ~330
                # session history every rank needs is fetched first
                await self._backfill_missing()
        if not targets:
            st.update(state="up to date", target=None, missing=[])
            self.status["daily"] = st
            return
        for t in targets:
            missing, no_data = await self._missing_for(t)
            if missing and fetch:
                await self._fetch_daily(missing)
                missing, no_data = await self._missing_for(t)
            allowed = self.force_freeze.get(t, set())
            blocking = [s for s in missing if s not in allowed]
            if blocking:
                st.update(state="waiting for daily bars", target=t,
                          missing=blocking, no_data=no_data)
                self.status["daily"] = st
                return
            if last is None and "__history__" not in allowed:
                started = self._last_started_session_day(now)
                if t < started:
                    # Go-live at t would make the FIRST traded session one
                    # that already started unwatched (Dhan's data is T-1, so
                    # an evening deploy only sees yesterday). Wait for that
                    # day's bar; the first traded session is then the next
                    # one, watched from its 09:15 open.
                    st.update(state="waiting to go live", target=t, missing=[],
                              message=f"go-live waits for the {started} daily "
                                      f"bar (Dhan publishes it overnight) so the "
                                      f"first traded session is fully watched")
                    self.status["daily"] = st
                    return
                short, total = await self._short_history(t)
                if total and len(short) > 0.2 * total:
                    st.update(state="waiting for history", target=t,
                              missing=[], short_history=short[:40],
                              message=f"only {total - len(short)} of {total} "
                                      f"symbols have the "
                                      f"{int(self.params['min_bars_to_rank'])} "
                                      f"daily bars ranking needs — go-live waits "
                                      f"for the backfill")
                    self.status["daily"] = st
                    return
            await self.freeze(t, excluded=sorted(set(missing)))
        st.update(state="up to date", target=None, missing=[],
                  no_data=no_data)
        self.status["daily"] = st

    async def _bars_asof(self, symbol: str, date_iso: str,
                         limit: int = 400) -> list[dict]:
        bars = await self.store.bars_for(symbol, limit + 60)
        return [b for b in bars if b["date"] <= date_iso][-limit:]

    async def _latest_closes(self, symbols, before: str) -> dict[str, float]:
        out = {}
        for s in set(symbols) | self._held_anywhere():
            bars = await self._bars_asof(s, before, 3)
            bars = [b for b in bars if b["date"] < before]
            if bars:
                out[s] = bars[-1]["c"]
        return out

    @staticmethod
    def _daily_rule(stop: float | None, close: float | None) -> dict:
        if stop is None or close is None:
            return {"stop": stop, "daily_close": close, "would_exit": None,
                    "note": "no daily close for the trigger day"}
        would = close <= stop
        return {"stop": round(stop, 2), "daily_close": round(close, 2),
                "would_exit": would,
                "note": ("daily rule would ALSO exit (next day's open)" if would
                         else "daily rule would HOLD — the close recovered "
                              "above the stop")}

    async def freeze(self, date_iso: str, excluded: list[str] | None = None) -> None:
        """Session T is complete and its daily bars are in: commit the live
        session, ratchet, score, rank, freeze T+1; run the shadow book."""
        self._freezing = True
        try:
            await self._freeze(date_iso, excluded)
        finally:
            self._freezing = False
        await self.refresh_live_view()

    async def _freeze(self, date_iso: str, excluded: list[str] | None) -> None:
        p = self.params
        symbols = await self._active_symbols()
        day_bars = await self.store.bars_on(date_iso)
        closes_T = {s: b["c"] for s, b in day_bars.items()}
        opens_T = {s: b["o"] for s, b in day_bars.items()}

        # A session that started before go-live (deployed mid-session or in
        # the evening, while Dhan's T-1 data still ended the day before) was
        # never watched: it is NOT traded by either book.
        first = self.status.get("first_session")
        observed = first is not None and date_iso >= first
        if not observed and self.status.get("go_live"):
            log.info("Session %s precedes the first observed session %s — "
                     "not traded (go-live re-freeze only)", date_iso, first)
            self.shadow_plan = None

        # -- 1. commit the live session T -------------------------------------
        session_summary = None
        if observed and self.plan is not None and self.plan.get("signal_date") \
                and self.plan["signal_date"] < date_iso:
            if self.recorder.date == date_iso:
                await self._flush_recorder()
            hbars = await self.store.hourly_for_date(date_iso)
            rec_opens = await self.store.session_opens(date_iso)
            opens = {s: v[0] for s, v in rec_opens.items()}
            opens.update({s: o for s, o in opens_T.items() if o})  # authority
            r = intraday.replay_session(date_iso, self.book.to_record(), self.plan,
                                        hbars, opens, len(intraday.SLOTS), None, p)
            for t in r["trades"]:
                m = t["meta"]
                if m.get("trigger_date") == date_iso and not m.get("daily_rule"):
                    m["daily_rule"] = self._daily_rule(m.get("stop"),
                                                       closes_T.get(t["symbol"]))
                t["book"] = "live"
                await self.store.add_trade(t)
            carry = []
            for c in r["carry_exits"]:
                c = dict(c)
                c.setdefault("daily_rule", self._daily_rule(
                    c.get("stop"), closes_T.get(c["symbol"])))
                carry.append(c)
            self.book = Book.from_record(r["book"])
            session_summary = {
                "date": date_iso, "trades": len(r["trades"]),
                "entries": sum(1 for e in r["events"] if e["kind"] == "entry"),
                "alerts": r["alerts"][-50:], "notes": r["notes"][-50:],
                "no_hourly_data": not hbars}
            await self.store.set_json(f"session:{date_iso}", session_summary)
        else:
            carry = []

        # -- 2. score the completed day ------------------------------------------
        metrics: dict[str, dict] = {}
        eligible: set[str] = set()
        for s in set(symbols) | self._held_anywhere():
            bars = await self._bars_asof(s, date_iso)
            if not bars or bars[-1]["date"] != date_iso:
                continue
            metrics[s] = engine.compute_metrics(bars, p)
            if s in symbols:
                eligible.add(s)
        self.scored = engine.score_universe(metrics, p)
        closes = {s: m["close"] for s, m in metrics.items()}

        # -- 3. live: ratchet from T's close, freeze T+1 -------------------------
        engine.ratchet_trails(self.book, self.scored, p)
        ranked = engine.ranked_candidates(self.scored, eligible, p)
        self.plan = {
            "signal_date": date_iso, "fresh": True,
            "stops": {pos.symbol: round(pos.effective_stop, 4)
                      for pos in self.book.positions if pos is not None},
            "ranked": ranked, "carry_exits": carry,
            "prev_close": {s: c for s, c in closes.items() if c is not None},
            "qualified": len(ranked), "frozen_at": now_ist().isoformat(),
            "excluded_missing": excluded or []}

        # -- 4. shadow (daily-close rule) -----------------------------------------
        sp = self.shadow_plan
        if sp and not sp.get("filled") and sp["signal_date"] < date_iso:
            closes_prev = await self._latest_closes(symbols, before=date_iso)
            res = engine.fill_open(self.shadow_book, sp, opens_T, date_iso,
                                   closes_prev, p)
            for t in res["trades"]:
                t["book"] = "shadow"
                t["meta"] = {**(t.get("meta") or {}), "book": "shadow",
                             "exit_basis": "daily_close"}
                await self.store.add_trade(t)
            sp["held"] = res["held_exits"]
        held_carry = list((sp or {}).get("held") or [])
        new_sp = engine.evaluate_close(self.shadow_book, self.scored, date_iso,
                                       eligible, p)
        for e in held_carry:
            if e["symbol"] in self.shadow_book.held_symbols() and \
                    e["symbol"] not in {x["symbol"] for x in new_sp["exits"]}:
                new_sp["exits"].insert(0, e)
        self.shadow_plan = new_sp

        # -- 5. curve ----------------------------------------------------------------
        bench_close = (day_bars.get(BENCH) or {}).get("c")
        eq = self.book.equity(closes)
        seq = self.shadow_book.equity(closes)
        await self.store.upsert_equity_point(
            date_iso, round(eq, 2), round(self.book.cash, 2),
            len(self.book.held_symbols()), bench_close, round(seq, 2))
        if not self.status["go_live"]:
            self.status["go_live"] = date_iso
            await self.store.set_meta("go_live", date_iso)
            # the first session the dashboard can WATCH from its 09:15 open
            now = now_ist()
            d = now.date()
            if not (d.weekday() < 5 and _sec_of_day(now) < intraday.SESSION_START):
                d += timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            fs = max(d.isoformat(), (datetime.fromisoformat(date_iso)
                                     + timedelta(days=1)).date().isoformat())
            self.status["first_session"] = fs
            await self.store.set_meta("first_session", fs)
        await self.store.set_meta("last_eod", date_iso)
        self.status["last_eod"] = date_iso
        self.force_freeze.pop(date_iso, None)
        await self._persist()
        await self._start_feed()                 # subscribe any new names
        log.info("FREEZE %s: live ₹%.0f (%d held) | shadow ₹%.0f (%d held) | "
                 "%d qualified", date_iso, eq, len(self.book.held_symbols()),
                 seq, len(self.shadow_book.held_symbols()), len(ranked))

    async def force_freeze_now(self, date_iso: str) -> dict:
        """Operator: freeze this session WITHOUT the symbols still missing
        (they are excluded from ranking that day and recorded on the plan)."""
        missing, _ = await self._missing_for(date_iso)
        self.force_freeze[date_iso] = set(missing) | {"__history__"}
        await self.run_daily(fetch=False)
        return {"excluded": missing, "last_eod": self.status["last_eod"]}

    # ------------------------------------------------------------- bar import

    async def import_bars_csv(self, text: str) -> dict:
        """One-time history import. CSV header: symbol,date,o,h,l,c,volume,
        volume_adj (scripts/export_bars.py). Upserts, then runs the daily
        job on stored data — which GOES LIVE at the latest session; nothing
        earlier is simulated."""
        import csv as _csv
        import io as _io
        reader = _csv.DictReader(_io.StringIO(text))
        need = {"symbol", "date", "o", "h", "l", "c"}
        if not reader.fieldnames or not need.issubset(set(reader.fieldnames)):
            raise ValueError("CSV header must be: symbol,date,o,h,l,c,volume,"
                             "volume_adj (use scripts/export_bars.py)")
        by_symbol: dict[str, list[dict]] = {}
        bad = 0
        for row in reader:
            try:
                sym = row["symbol"].strip().upper()
                vol = float(row["volume"]) if row.get("volume") else None
                vadj = float(row["volume_adj"]) if row.get("volume_adj") else vol
                by_symbol.setdefault(sym, []).append({
                    "date": row["date"].strip(), "o": float(row["o"]),
                    "h": float(row["h"]), "l": float(row["l"]),
                    "c": float(row["c"]), "volume": vol, "volume_adj": vadj})
            except (TypeError, ValueError, KeyError):
                bad += 1
        if not by_symbol:
            raise ValueError("no valid rows found")
        total, latest = 0, "1970-01-01"
        for sym, bars in by_symbol.items():
            await self.store.upsert_bars(sym, bars)
            total += len(bars)
            latest = max(latest, max(b["date"] for b in bars))
        log.info("Imported %d bars for %d symbols (latest %s, %d bad rows)",
                 total, len(by_symbol), latest, bad)
        await self.run_daily(fetch=False)
        return {"bars": total, "symbols": len(by_symbol), "latest": latest,
                "bad_rows": bad, "benchmark_included": BENCH in by_symbol,
                "last_freeze": self.status["last_eod"]}

    # ------------------------------------------------------- corporate actions

    @staticmethod
    def _adjust_book(book: Book, sym: str, f: float, last_close: float | None):
        for pos in book.positions:
            if pos is not None and pos.symbol == sym:
                archived = pos.as_dict()
                ref = last_close if last_close else pos.entry_price
                new_shares = int(pos.shares * f)
                leftover = pos.shares * f - new_shares
                cash_credit = leftover * (ref / f)
                pos.shares = float(new_shares)
                pos.entry_price /= f
                pos.entry_atr /= f
                pos.initial_stop /= f
                if pos.trail_stop is not None:
                    pos.trail_stop /= f
                if pos.highest_close is not None:
                    pos.highest_close /= f
                book.cash += cash_credit
                return archived, cash_credit
        return None, 0.0

    async def apply_corp_action(self, action_id: int) -> dict:
        acts = {a["id"]: a for a in await self.store.corp_actions()}
        a = acts.get(action_id)
        if not a or a["status"] != "pending":
            raise ValueError("unknown or already-resolved action")
        f = float(a["factor"] or 0)
        if f <= 0:
            raise ValueError("a positive factor is required to apply")
        sym, ex = a["symbol"], a["ex_date"]
        last = await self._bars_asof(sym, now_ist().date().isoformat(), 1)
        last_close = last[-1]["c"] if last else None
        pre = {}
        for name, book in (("live", self.book), ("shadow", self.shadow_book)):
            arch, credit = self._adjust_book(book, sym, f, last_close)
            if arch:
                pre[name] = {"position": arch, "cash_credit": round(credit, 2)}
        # frozen plans carry prices too: rescale them so the session that
        # trades post-ex prices compares against post-ex levels
        if self.plan:
            pre["live_plan"] = {k: self.plan.get(k) for k in ("stops", "prev_close")}
            if sym in (self.plan.get("stops") or {}):
                self.plan["stops"][sym] /= f
            if sym in (self.plan.get("prev_close") or {}):
                self.plan["prev_close"][sym] /= f
            for r in self.plan.get("ranked") or []:
                if r["symbol"] == sym:
                    r["atr"] /= f
                    r["close"] = r["close"] / f if r.get("close") else r.get("close")
            for c in self.plan.get("carry_exits") or []:
                if c["symbol"] == sym:
                    c["stop"] /= f
        if self.shadow_plan:
            for e in (self.shadow_plan.get("entries") or []):
                if e["symbol"] == sym:
                    e["atr"] /= f
        if pre:
            await self.store.set_json(f"corp_action_prestate:{action_id}", pre)
        if not a["skip_volume"]:
            await self.store.scale_volume_adj_before(sym, ex, f)
        await self.store.update_corp_action(
            action_id, status="applied", applied_at=now_ist().isoformat())
        # Dhan back-adjusts history at the ex-date: the stored series is stale.
        await self.store.delete_bars(sym)
        await self._backfill_missing(force=[sym])
        await self._persist()
        return {"adjusted_position": bool(pre.get("live") or pre.get("shadow"))}

    # --------------------------------------------------------------- snapshot

    def _alerts(self, now: datetime) -> list[dict]:
        out = []
        d = self.status.get("daily") or {}
        sec = _sec_of_day(now)
        alert_sec = intraday._sec(config.FREEZE_ALERT)
        if d.get("target") and now.weekday() < 5 and sec >= alert_sec \
                and d["target"] < now.date().isoformat():
            out.append({"level": "critical",
                        "msg": f"NOT LIVE YET — {d['message']}" if d.get("message")
                               and self.plan is None else
                               f"DAILY FREEZE NOT DONE for {d['target']} — "
                               f"{len(d.get('missing') or [])} symbol(s) still "
                               f"missing their daily bar. Stops are held at the "
                               f"last frozen levels; NO entries or refills until "
                               f"it lands (Diagnostics → Freeze anyway)."})
        for e in self.status.get("errors") or []:
            out.append({"level": "critical", "msg": e})
        in_session = now.weekday() < 5 and \
            intraday.SESSION_START <= sec < intraday.SESSION_END
        if in_session and self.plan is not None:
            fst = None
            if self.feed is not None:
                fst = self.feed.connected
            age = self.recorder.status().get("last_tick_age_s")
            if fst is False or age is None or age > config.FEED_STALE_SECONDS:
                out.append({"level": "critical",
                            "msg": "LIVE FEED DOWN — "
                                   + ("not connected" if fst is False
                                      else f"no tick for {age}s" if age
                                      else "no tick received yet")
                                   + ". Bars without prints are checked on the "
                                     "last known price (stale)."})
        return out

    async def snapshot(self) -> dict:
        now = now_ist()
        base = {"build": config.BUILD, "time_ist": now.isoformat(),
                "status": self.status, "credentials": creds.current.status(),
                "params": self.params,
                "params_default": dict(config.STRATEGY_PARAMS),
                "param_choices": config.PARAM_CHOICES,
                "benchmark_label": config.BENCHMARK_LABEL,
                "known_differences": config.KNOWN_DIFFERENCES,
                "rates_verified": config.RATES_VERIFIED,
                "slots": list(intraday.LABELS)}
        if not self.store.available:
            return {**base, "book": self.book.to_record(), "equity": None,
                    "plan": None, "live": None, "shadow": None,
                    "candidates": [], "trades": [], "curve": [],
                    "unmapped": [], "corp_actions": [], "constituents": [],
                    "stats": {}, "alerts": [{"level": "critical",
                                             "msg": self.status["boot"]}]}
        curve = await self.store.equity_curve()
        live_trades = await self.store.trades(300, book="live")
        shadow_trades = await self.store.trades(300, book="shadow")
        live = self.live_chain[-1] if self.live_chain else None
        marks = dict(live["marks"]) if live else \
            {s: m["close"] for s, m in self.scored.items() if m.get("close")}
        view_book = Book.from_record(live["book"]) if live else self.book
        positions = []
        for pos in view_book.positions:
            if pos is None:
                continue
            px = marks.get(pos.symbol)
            pend = [e for e in (live or {}).get("pending_exits", [])
                    if e["symbol"] == pos.symbol]
            stop = (live or {}).get("stops", {}).get(pos.symbol, pos.effective_stop)
            positions.append({**pos.as_dict(), "mark": px, "stop": stop,
                              "upnl": round((px - pos.entry_price) * pos.shares, 2)
                              if px else None,
                              "pending_exit": pend[0] if pend else None})
        candidates = sorted(
            (dict(symbol=s, score=m["score"], mom121=m.get("mom121"),
                  c1=m["c1"], c2=m["c2"], c3=m["c3"], c4=m["c4"],
                  close=m.get("close"), rankable=m["rankable"])
             for s, m in self.scored.items()),
            key=lambda d: (-(d["score"]), -(d["mom121"] or -9)))[:40]
        comparison = []
        for t in live_trades:
            m = t.get("meta") or {}
            comparison.append({"symbol": t["symbol"], "exit_date": t["exit_date"],
                               "exit_price": t["exit_price"],
                               "net_pnl": t["net_pnl"],
                               "trigger": m.get("trigger"),
                               "trigger_timestamp": m.get("trigger_timestamp"),
                               "fill_timestamp": m.get("fill_timestamp"),
                               "trigger_price": m.get("trigger_price"),
                               "stop": m.get("stop"),
                               "stale_price": m.get("stale_price"),
                               "closing_window_only": m.get("closing_window_only"),
                               "halt_delay_bars": m.get("halt_delay_bars"),
                               "daily_rule": m.get("daily_rule")})
        dr = [c["daily_rule"] for c in comparison if c.get("daily_rule")]
        return {
            **base,
            "book": view_book.to_record(),
            "committed_book": self.book.to_record(),
            "equity": view_book.equity(marks) if marks else None,
            "positions": positions,
            "plan": self.plan,
            "live": None if live is None else {
                k: live[k] for k in ("date", "final", "closed", "cur", "events",
                                     "alerts", "notes", "waiting",
                                     "pending_exits", "carry_exits", "plan_fresh")},
            "live_chain_dates": [r["date"] for r in self.live_chain],
            "recorder": self.recorder.status(),
            "feed": None if self.feed is None else {
                "connected": self.feed.connected,
                "reconnects": self.feed.reconnects,
                "last_error": self.feed.last_error,
                "instruments": self.feed.tracked_count()},
            "shadow": {"book": self.shadow_book.to_record(),
                       "equity": self.shadow_book.equity(marks) if marks else None,
                       "plan": self.shadow_plan},
            "candidates": candidates,
            "trades": live_trades,
            "shadow_trades": shadow_trades,
            "comparison": comparison,
            "comparison_summary": {
                "live_exits": len(comparison),
                "daily_would_also_exit": sum(1 for d in dr if d.get("would_exit")),
                "daily_would_hold": sum(1 for d in dr if d.get("would_exit") is False),
                "closing_window_only": sum(1 for c in comparison
                                           if c.get("closing_window_only")),
                "stale_price": sum(1 for c in comparison if c.get("stale_price"))},
            "curve": curve,
            "unmapped": self.unmapped,
            "corp_actions": await self.store.corp_actions(),
            "constituents": await self.store.constituents(),
            "stats": {"live": self._stats([c["equity"] for c in curve], live_trades),
                      "shadow": self._stats([c.get("shadow_equity") for c in curve
                                             if c.get("shadow_equity") is not None],
                                            shadow_trades)},
            "alerts": self._alerts(now),
        }

    @staticmethod
    def _stats(eq: list[float], trades: list[dict]) -> dict:
        closed = [t for t in trades if t.get("net_pnl") is not None]
        out: dict = {"trades": len(closed)}
        wins = [t for t in closed if t["net_pnl"] > 0]
        out["win_rate"] = round(100 * len(wins) / len(closed), 1) if closed else None
        if eq:
            peak, dd = eq[0], 0.0
            for v in eq:
                peak = max(peak, v)
                dd = min(dd, v / peak - 1)
            out["max_drawdown_pct"] = round(dd * 100, 2)
            out["total_return_pct"] = round((eq[-1] / config.STARTING_CAPITAL - 1)
                                            * 100, 2)
        return out


hub = EquityHub()
