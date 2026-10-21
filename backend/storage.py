"""
SQLite persistence. Same philosophy as the options dashboards: nothing that
was ever written is deleted by code; corrections are archived next to the
original. All writes go through asyncio.to_thread so the event loop never
blocks on disk.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL, date TEXT NOT NULL,
    o REAL, h REAL, l REAL, c REAL,
    volume REAL, volume_adj REAL,
    PRIMARY KEY (symbol, date));
CREATE TABLE IF NOT EXISTS constituents (
    symbol TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1,
    security_id TEXT,
    added_date TEXT, removed_date TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS corp_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, ex_date TEXT NOT NULL, kind TEXT,
    factor REAL, skip_volume INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending/applied/rejected
    source TEXT NOT NULL DEFAULT 'manual',    -- manual/auto
    note TEXT, created_at TEXT, applied_at TEXT);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, slot INTEGER,
    entry_date TEXT, entry_price REAL, shares REAL,
    exit_date TEXT, exit_price REAL,
    entry_cost REAL, exit_cost REAL,
    gross_pnl REAL, net_pnl REAL,
    exit_reason TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS equity_curve (
    date TEXT PRIMARY KEY,
    equity REAL, cash REAL, holdings INTEGER, benchmark REAL);
"""


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.error: str | None = None
        self._lock = threading.Lock()
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self.available = True
        except Exception as exc:                 # storage down ≠ dashboard down
            self.available = False
            self.error = str(exc)
            self._conn = None

    # ---- sync core (always under the lock) ----------------------------------

    def _exec(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall()
            self._conn.commit()
            return rows

    def _many(self, sql: str, rows: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    # ---- meta ----------------------------------------------------------------

    async def get_meta(self, key: str) -> str | None:
        rows = await asyncio.to_thread(
            self._exec, "SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    async def set_meta(self, key: str, value: str) -> None:
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))

    async def get_json(self, key: str, default: Any = None) -> Any:
        raw = await self.get_meta(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    async def set_json(self, key: str, value: Any) -> None:
        await self.set_meta(key, json.dumps(value))

    # ---- bars ----------------------------------------------------------------

    async def upsert_bars(self, symbol: str, bars: list[dict]) -> None:
        rows = [(symbol, b["date"], b["o"], b["h"], b["l"], b["c"],
                 b.get("volume"), b.get("volume_adj", b.get("volume")))
                for b in bars]
        await asyncio.to_thread(
            self._many,
            "INSERT INTO bars(symbol,date,o,h,l,c,volume,volume_adj) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET "
            "o=excluded.o,h=excluded.h,l=excluded.l,c=excluded.c,"
            "volume=excluded.volume,volume_adj=excluded.volume_adj", rows)

    async def bars_for(self, symbol: str, limit: int = 400) -> list[dict]:
        rows = await asyncio.to_thread(
            self._exec,
            "SELECT * FROM bars WHERE symbol=? ORDER BY date DESC LIMIT ?",
            (symbol, limit))
        return [dict(r) for r in reversed(rows)]

    async def delete_bars(self, symbol: str) -> None:
        """Only used when a corporate action forces a full refetch — the
        refetched, back-adjusted series REPLACES the stale one."""
        await asyncio.to_thread(
            self._exec, "DELETE FROM bars WHERE symbol=?", (symbol,))

    async def scale_volume_adj_before(self, symbol: str, ex_date: str,
                                      factor: float) -> None:
        await asyncio.to_thread(
            self._exec,
            "UPDATE bars SET volume_adj = volume_adj * ? "
            "WHERE symbol=? AND date < ? AND volume_adj IS NOT NULL",
            (factor, symbol, ex_date))

    async def dates_with_bars(self, symbol: str) -> set[str]:
        rows = await asyncio.to_thread(
            self._exec, "SELECT date FROM bars WHERE symbol=?", (symbol,))
        return {r["date"] for r in rows}

    # ---- constituents ---------------------------------------------------------

    async def constituents(self) -> list[dict]:
        rows = await asyncio.to_thread(
            self._exec, "SELECT * FROM constituents ORDER BY symbol", ())
        return [dict(r) for r in rows]

    async def upsert_constituent(self, symbol: str, **fields) -> None:
        cur = await asyncio.to_thread(
            self._exec, "SELECT symbol FROM constituents WHERE symbol=?", (symbol,))
        if cur:
            sets = ", ".join(f"{k}=?" for k in fields)
            await asyncio.to_thread(
                self._exec,
                f"UPDATE constituents SET {sets} WHERE symbol=?",
                (*fields.values(), symbol))
        else:
            cols = ["symbol", *fields.keys()]
            q = ", ".join("?" for _ in cols)
            await asyncio.to_thread(
                self._exec,
                f"INSERT INTO constituents({', '.join(cols)}) VALUES({q})",
                (symbol, *fields.values()))

    # ---- corp actions ---------------------------------------------------------

    async def add_corp_action(self, **f) -> int:
        rows = await asyncio.to_thread(
            self._exec,
            "INSERT INTO corp_actions(symbol,ex_date,kind,factor,skip_volume,"
            "status,source,note,created_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "RETURNING id",
            (f["symbol"], f["ex_date"], f.get("kind"), f.get("factor"),
             int(f.get("skip_volume", 0)), f.get("status", "pending"),
             f.get("source", "manual"), f.get("note"), f.get("created_at")))
        return rows[0]["id"]

    async def corp_actions(self, status: str | None = None) -> list[dict]:
        if status:
            rows = await asyncio.to_thread(
                self._exec,
                "SELECT * FROM corp_actions WHERE status=? ORDER BY id DESC",
                (status,))
        else:
            rows = await asyncio.to_thread(
                self._exec, "SELECT * FROM corp_actions ORDER BY id DESC", ())
        return [dict(r) for r in rows]

    async def update_corp_action(self, action_id: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        await asyncio.to_thread(
            self._exec, f"UPDATE corp_actions SET {sets} WHERE id=?",
            (*fields.values(), action_id))

    # ---- trades / curve -------------------------------------------------------

    async def add_trade(self, t: dict) -> None:
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO trades(symbol,slot,entry_date,entry_price,shares,"
            "exit_date,exit_price,entry_cost,exit_cost,gross_pnl,net_pnl,"
            "exit_reason,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["symbol"], t.get("slot"), t["entry_date"], t["entry_price"],
             t["shares"], t["exit_date"], t["exit_price"], t["entry_cost"],
             t["exit_cost"], t["gross_pnl"], t["net_pnl"], t["exit_reason"],
             json.dumps(t.get("meta") or {})))

    async def trades(self, limit: int = 500) -> list[dict]:
        rows = await asyncio.to_thread(
            self._exec, "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    async def upsert_equity_point(self, date: str, equity: float, cash: float,
                                  holdings: int, benchmark: float | None) -> None:
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO equity_curve(date,equity,cash,holdings,benchmark) "
            "VALUES(?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET "
            "equity=excluded.equity,cash=excluded.cash,"
            "holdings=excluded.holdings,benchmark=excluded.benchmark",
            (date, equity, cash, holdings, benchmark))

    async def equity_curve(self) -> list[dict]:
        rows = await asyncio.to_thread(
            self._exec, "SELECT * FROM equity_curve ORDER BY date", ())
        return [dict(r) for r in rows]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
