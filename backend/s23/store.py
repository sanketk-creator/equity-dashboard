"""
Persistence for Strategies 2 and 3 — spec §12. Its OWN SQLite file
(strategies.db next to the Strategy 1 database), so nothing here can touch
Strategy 1's tables. Historical signals are immutable (spec §14.3): a
correction is a new row, never an UPDATE.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT NOT NULL, date TEXT NOT NULL,
  o REAL, h REAL, l REAL, c REAL, volume REAL,
  PRIMARY KEY (symbol, date));
CREATE TABLE IF NOT EXISTS universe (
  symbol TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1,
  sector TEXT, industry TEXT, company TEXT, isin TEXT,
  added_date TEXT, removed_date TEXT, security_id TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS index_map (
  name TEXT PRIMARY KEY, security_id TEXT, dhan_symbol TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS uploads (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, filename TEXT,
  format TEXT, diff_json TEXT, rows_json TEXT, status TEXT, applied_at TEXT);
CREATE TABLE IF NOT EXISTS signals (
  signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_id TEXT NOT NULL, strategy_version TEXT, symbol TEXT,
  signal_type TEXT, signal_date TEXT, signal_timestamp TEXT,
  bar_status TEXT NOT NULL CHECK (bar_status = 'COMPLETE'),
  signal_reference_price REAL, reason TEXT, also_triggered TEXT,
  indicator_values_json TEXT, rank_at_signal INTEGER, data_timestamp TEXT,
  flags_json TEXT, corrects_signal_id INTEGER);
CREATE TABLE IF NOT EXISTS orders (
  order_id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER,
  strategy_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
  signal_date TEXT NOT NULL, intended_fill_date TEXT, status TEXT,
  reason TEXT, also_triggered TEXT, target_shares INTEGER,
  target_weight REAL, fill_price REAL, fill_timestamp TEXT,
  filled_shares INTEGER, costs_json TEXT, slippage_assumed REAL,
  attempts INTEGER DEFAULT 0, initial_build INTEGER DEFAULT 0,
  UNIQUE (strategy_id, symbol, signal_date, side),
  CHECK (intended_fill_date IS NULL OR signal_date < intended_fill_date));
CREATE TABLE IF NOT EXISTS positions (
  strategy_id TEXT NOT NULL, symbol TEXT NOT NULL, json TEXT,
  PRIMARY KEY (strategy_id, symbol));
CREATE TABLE IF NOT EXISTS trades (
  trade_id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id TEXT,
  symbol TEXT, sleeve_or_sector TEXT, entry_signal_date TEXT,
  entry_fill_timestamp TEXT, entry_price REAL, exit_signal_date TEXT,
  exit_fill_timestamp TEXT, exit_price REAL, quantity INTEGER,
  holding_days_calendar INTEGER, holding_days_sessions INTEGER,
  exit_reason TEXT, also_triggered TEXT, gross_pnl REAL, total_costs REAL,
  dp_charge REAL, net_pnl REAL, return_pct REAL, initial_build INTEGER,
  json TEXT);
CREATE TABLE IF NOT EXISTS portfolio_daily (
  date TEXT NOT NULL, strategy_id TEXT NOT NULL, nav REAL, cash REAL,
  invested_capital REAL, n_positions INTEGER, n_sectors INTEGER,
  realised_pnl_cumulative REAL, unrealised_pnl REAL,
  drawdown_from_peak REAL, data_feed_status TEXT, strategy_status TEXT,
  benchmark REAL, nav_after_slippage REAL,
  PRIMARY KEY (date, strategy_id));
CREATE TABLE IF NOT EXISTS sector_scores (
  date TEXT NOT NULL, sector TEXT NOT NULL, m1 REAL, m2 REAL, m3 REAL,
  m4 REAL, z1 REAL, z2 REAL, z3 REAL, z4 REAL, score REAL, rank INTEGER,
  close_vs_sma200 REAL, above_sma200 INTEGER, seated INTEGER,
  weight_order INTEGER, PRIMARY KEY (date, sector));
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, strategy_id TEXT,
  bar_date TEXT, kind TEXT, detail_json TEXT);
CREATE TABLE IF NOT EXISTS corp_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
  ex_date TEXT NOT NULL, kind TEXT, factor REAL,
  status TEXT NOT NULL DEFAULT 'pending', note TEXT, created_at TEXT,
  applied_at TEXT, prestate_json TEXT);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self.error: str | None = None
        self._lock = threading.Lock()
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False,
                                         timeout=30)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self.available = True
        except Exception as exc:
            self._conn = None
            self.available = False
            self.error = str(exc)

    # ---- core ---------------------------------------------------------------

    def _run(self, fn):
        if self._conn is None:
            raise RuntimeError(f"strategies database unavailable: {self.error}")
        with self._lock:
            try:
                out = fn(self._conn)
                self._conn.commit()
                return out
            except Exception:
                self._conn.rollback()
                raise

    async def tx(self, fn):
        """Run fn(conn) atomically in a worker thread."""
        return await asyncio.to_thread(self._run, fn)

    async def q(self, sql: str, args: tuple = ()) -> list[dict]:
        return await self.tx(lambda c: [dict(r) for r in c.execute(sql, args).fetchall()])

    async def x(self, sql: str, args: tuple = ()) -> None:
        await self.tx(lambda c: c.execute(sql, args))

    async def get_json(self, key: str, default: Any = None) -> Any:
        rows = await self.q("SELECT value FROM meta WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (TypeError, ValueError):
            return default

    async def set_json(self, key: str, value: Any) -> None:
        await self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) "
                     "DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    # ---- bars ---------------------------------------------------------------

    async def upsert_bars(self, symbol: str, bars: list[dict]) -> None:
        rows = [(symbol, b["date"], b["o"], b["h"], b["l"], b["c"], b.get("volume"))
                for b in bars]
        await self.tx(lambda c: c.executemany(
            "INSERT INTO bars(symbol,date,o,h,l,c,volume) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol,date) DO UPDATE SET o=excluded.o,h=excluded.h,"
            "l=excluded.l,c=excluded.c,volume=excluded.volume", rows))

    async def bars_upto(self, symbol: str, date: str, limit: int = 420) -> list[dict]:
        rows = await self.q("SELECT date,o,h,l,c,volume FROM bars WHERE symbol=? "
                            "AND date<=? ORDER BY date DESC LIMIT ?",
                            (symbol, date, limit))
        return rows[::-1]

    async def bar_counts(self) -> dict[str, int]:
        rows = await self.q("SELECT symbol, COUNT(*) n FROM bars GROUP BY symbol")
        return {r["symbol"]: r["n"] for r in rows}

    async def symbols_with_bar(self, date: str) -> set[str]:
        rows = await self.q("SELECT symbol FROM bars WHERE date=?", (date,))
        return {r["symbol"] for r in rows}

    async def delete_bars(self, symbol: str) -> None:
        await self.x("DELETE FROM bars WHERE symbol=?", (symbol,))

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
