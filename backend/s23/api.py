"""HTTP surface for Strategies 2 and 3. Every write needs ADMIN_PASSWORD
(refused while it is unset), exactly like Strategy 1's endpoints."""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .. import creds
from .hub import STRATS, s23hub

router = APIRouter()
FRONTEND = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")


def _admin(payload: dict) -> None:
    pw = creds.admin_password()
    if not pw:
        raise HTTPException(403, "ADMIN_PASSWORD is not set on the server — "
                                 "writes are refused until it is")
    if str(payload.get("password") or "") != pw:
        raise HTTPException(403, "Wrong password.")


@router.get("/strategies")
async def page():
    return FileResponse(os.path.join(FRONTEND, "strategies.html"))


@router.get("/api/s23/overview")
async def overview():
    return JSONResponse(await s23hub.overview())


@router.get("/api/s23/snapshot/{sid}")
async def snapshot(sid: str):
    if sid not in STRATS:
        raise HTTPException(404, "unknown strategy")
    return JSONResponse(await s23hub.snapshot(sid))


@router.post("/api/s23/run")
async def run(payload: dict):
    _admin(payload)
    await s23hub.run_daily()
    return {"ok": True}


@router.post("/api/s23/approve-catchup")
async def approve(payload: dict):
    _admin(payload)
    return {"ok": True, **(await s23hub.approve_catchup())}


@router.post("/api/s23/resume")
async def resume(payload: dict):
    _admin(payload)
    sid = str(payload.get("strategy") or "")
    if sid not in STRATS:
        raise HTTPException(400, "strategy must be barbell or sector")
    await s23hub.resume(sid)
    return {"ok": True}


@router.post("/api/s23/calendar")
async def calendar(payload: dict):
    """{password, holidays: {date: desc}, special: {date: desc}, covered_through}"""
    _admin(payload)
    try:
        await s23hub.save_calendar(dict(payload.get("holidays") or {}),
                                   dict(payload.get("special") or {}),
                                   str(payload.get("covered_through") or ""))
    except ValueError as e:
        raise HTTPException(400, f"bad date: {e}")
    return {"ok": True}


@router.post("/api/s23/universe/upload")
async def universe_upload(payload: dict):
    _admin(payload)
    text = payload.get("csv") or ""
    if not text.strip():
        raise HTTPException(400, "csv is empty")
    try:
        return {"ok": True, **(await s23hub.upload_universe(
            str(payload.get("filename") or "upload.csv"), text))}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/s23/universe/apply")
async def universe_apply(payload: dict):
    _admin(payload)
    try:
        return {"ok": True, **(await s23hub.apply_universe(
            int(payload.get("upload_id") or 0), payload.get("assignments")))}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/s23/index-map")
async def index_map(payload: dict):
    _admin(payload)
    try:
        await s23hub.set_index_id(str(payload.get("name") or ""),
                                  str(payload.get("security_id") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/api/s23/corp-action")
async def corp(payload: dict):
    _admin(payload)
    op = str(payload.get("op") or "")
    try:
        if op == "add":
            aid = await s23hub.corp_add(str(payload.get("symbol") or ""),
                                        str(payload.get("ex_date") or ""),
                                        payload.get("kind"),
                                        float(payload.get("factor")),
                                        payload.get("note"))
            return {"ok": True, "id": aid}
        if op == "apply":
            return {"ok": True, **(await s23hub.corp_apply(int(payload.get("id"))))}
        if op == "reject":
            await s23hub.corp_reject(int(payload.get("id")))
            return {"ok": True}
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e))
    raise HTTPException(400, "op must be add/apply/reject")
