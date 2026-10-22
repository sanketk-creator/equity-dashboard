"""FastAPI surface. Every write is gated by ADMIN_PASSWORD, which refuses
when unset — an unset password is not a blank one."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import config, creds
from .hub import hub

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s")
log = logging.getLogger("equity.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await hub.start()
    yield
    await hub.stop()


app = FastAPI(title="Equity 100 Momentum — paper-live", lifespan=lifespan)
FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")


def _check_admin(payload: dict) -> None:
    pw = creds.admin_password()
    if not pw:
        raise HTTPException(403, "ADMIN_PASSWORD is not set on the server — "
                                 "writes are refused until it is")
    if str(payload.get("password") or "") != pw:
        raise HTTPException(403, "Wrong password.")


@app.get("/")
async def index():
    return FileResponse(os.path.join(FRONTEND, "index.html"))


@app.get("/api/health")
async def health():
    return {"ok": True, "build": config.BUILD,
            "boot": hub.status.get("boot"),
            "credentials": creds.current.ok}


@app.get("/api/snapshot")
async def snapshot():
    return JSONResponse(await hub.snapshot())


@app.post("/api/token/update")
async def token_update(payload: dict):
    """Paste a token in by hand — persisted, survives restarts."""
    _check_admin(payload)
    tok = str(payload.get("access_token") or "").strip()
    if not tok:
        raise HTTPException(400, "access_token is empty")
    status = await hub.update_credentials(
        tok, str(payload.get("client_id") or "").strip() or None)
    return {"ok": True, "status": status}


@app.post("/api/token/refresh")
async def token_refresh(payload: dict):
    """Generate a fresh token from the stored PIN + TOTP secret (the same
    auto-login the Nifty dashboard uses)."""
    _check_admin(payload)
    from .auth import TokenRefreshError
    try:
        status = await hub.refresh_token_now()
    except TokenRefreshError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "status": status}


@app.post("/api/params")
async def set_params(payload: dict):
    _check_admin(payload)
    if payload.get("clear"):
        await hub.set_params({})
    else:
        try:
            await hub.set_params(payload.get("overrides") or {})
        except ValueError as e:
            raise HTTPException(400, str(e))
    return {"ok": True, "params": hub.params}


@app.post("/api/universe")
async def universe(payload: dict):
    """{password, symbol, action: add|remove|restore, note?}. Removal never
    touches an open position — the holding is managed to its own exit; the
    symbol just stops being eligible for NEW entries (point-in-time)."""
    _check_admin(payload)
    sym = str(payload.get("symbol") or "").strip().upper()
    action = str(payload.get("action") or "")
    if not sym or action not in ("add", "remove", "restore"):
        raise HTTPException(400, "symbol and action (add/remove/restore) required")
    from .hub import now_ist
    today = now_ist().date().isoformat()
    if action == "add":
        await hub.store.upsert_constituent(sym, active=1, added_date=today,
                                           note=payload.get("note"))
        hub.status["boot"] = "mapping symbols"
        await hub._map_symbols()
        await hub._backfill_missing(force=[sym] if sym in hub.symbol_ids else [])
        hub.status["boot"] = "ready"
    elif action == "remove":
        await hub.store.upsert_constituent(sym, active=0, removed_date=today,
                                           note=payload.get("note"))
    else:
        await hub.store.upsert_constituent(sym, active=1, removed_date=None,
                                           note=payload.get("note"))
    return {"ok": True}


@app.post("/api/corp-action")
async def corp_action(payload: dict):
    """{password, op: add|apply|reject, ...}. 'add' takes symbol, ex_date,
    kind, factor (bonus a:b -> (a+b)/b; split X->Y -> X/Y), skip_volume.
    'apply' adjusts any open position (pre-state archived), scales pre-ex
    volume_adj unless skip_volume, and forces a full refetch of the symbol's
    back-adjusted history."""
    _check_admin(payload)
    op = str(payload.get("op") or "")
    from .hub import now_ist
    if op == "add":
        sym = str(payload.get("symbol") or "").strip().upper()
        ex = str(payload.get("ex_date") or "").strip()
        if not sym or not ex:
            raise HTTPException(400, "symbol and ex_date required")
        try:
            factor = float(payload.get("factor"))
        except (TypeError, ValueError):
            raise HTTPException(400, "factor must be a number "
                                     "(bonus a:b → (a+b)/b; split X→Y → X/Y)")
        if factor <= 0:
            raise HTTPException(400, "factor must be positive")
        aid = await hub.store.add_corp_action(
            symbol=sym, ex_date=ex, kind=payload.get("kind"),
            factor=factor, skip_volume=bool(payload.get("skip_volume")),
            source="manual", note=payload.get("note"),
            created_at=now_ist().isoformat())
        return {"ok": True, "id": aid}
    if op in ("apply", "reject"):
        aid = int(payload.get("id") or 0)
        if op == "reject":
            await hub.store.update_corp_action(aid, status="rejected")
            return {"ok": True}
        try:
            out = await hub.apply_corp_action(aid)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, **out}
    raise HTTPException(400, "op must be add/apply/reject")


@app.post("/api/import/bars")
async def import_bars(payload: dict):
    """Import daily bars from a CSV produced by scripts/export_bars.py on
    your machine: {password, csv: "<file contents>"}. Upserts everything and
    processes any sessions the new bars complete — the data path that needs
    NO Dhan historical subscription."""
    _check_admin(payload)
    text = payload.get("csv") or ""
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(400, "csv is empty")
    if len(text) > 80_000_000:
        raise HTTPException(400, "csv too large — split it (e.g. --since)")
    try:
        out = await hub.import_bars_csv(text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **out}


@app.post("/api/run/daily")
async def run_daily(payload: dict):
    """Run the daily job now: fetch any missing daily bars, freeze every
    complete session (normally automatic every few minutes)."""
    _check_admin(payload)
    await hub.run_daily()
    return {"ok": True, "last_eod": hub.status.get("last_eod"),
            "daily": hub.status.get("daily"),
            "errors": hub.status.get("errors")}


@app.post("/api/run/eod")                      # old name, same job
async def run_eod(payload: dict):
    return await run_daily(payload)


@app.post("/api/freeze/force")
async def freeze_force(payload: dict):
    """{password, date}: freeze that session WITHOUT the symbols still
    missing their daily bar (excluded from that day's ranking, recorded on
    the plan). Operator decision — never automatic."""
    _check_admin(payload)
    d = str(payload.get("date") or "").strip()
    if not d:
        raise HTTPException(400, "date required")
    out = await hub.force_freeze_now(d)
    return {"ok": True, **out, "daily": hub.status.get("daily")}
