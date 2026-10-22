"""
Dhan REST, equity edition.

  daily_history(security_id)   /v2/charts/historical   NSE_EQ EQUITY daily bars
  index_history(security_id)   same endpoint            IDX_I INDEX (benchmark)
  ohlc(ids)                    /v2/marketfeed/ohlc      today's open/last per id
  scrip_master()               api-scrip-master.csv     symbol -> security id

Paced: the historical endpoint is called one symbol at a time with a small
sleep — the whole universe backfill is a boot-time job, not a hot path.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import date, datetime, timedelta

import httpx

from . import config, creds

log = logging.getLogger("equity.rest")

BASE = "https://api.dhan.co/v2"
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"


class DhanRest:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=30.0)

    async def aclose(self):
        await self._client.aclose()

    def _headers(self) -> dict:
        return {"access-token": creds.current.access_token,
                "client-id": creds.current.client_id,
                "Content-Type": "application/json"}

    # ------------------------------------------------------------- historical

    async def _historical(self, security_id: str, segment: str, instrument: str,
                          days_back: int) -> list[dict]:
        to_d = date.today()
        from_d = to_d - timedelta(days=int(days_back * 1.7) + 30)
        # Payload shape matches the Nifty dashboard's PROVEN daily_candles call
        # exactly — no extra fields.
        body = {"securityId": str(security_id), "exchangeSegment": segment,
                "instrument": instrument,
                "fromDate": from_d.isoformat(), "toDate": to_d.isoformat()}
        r = await self._client.post(f"{BASE}/charts/historical",
                                    json=body, headers=self._headers())
        if r.status_code >= 400:
            # Dhan's error body says WHY (stale token vs endpoint not enabled
            # on the subscription). Surface it verbatim instead of guessing.
            raise RuntimeError(
                f"Dhan {r.status_code} on charts/historical: {r.text[:300]}")
        d = r.json()
        if isinstance(d.get("data"), dict):
            d = d["data"]
        out = []
        ts = d.get("timestamp") or []
        for i in range(len(ts)):
            out.append({
                "date": datetime.fromtimestamp(ts[i], config.IST).date().isoformat(),
                "o": float(d["open"][i]), "h": float(d["high"][i]),
                "l": float(d["low"][i]), "c": float(d["close"][i]),
                "volume": float((d.get("volume") or [0] * len(ts))[i]),
            })
        return out

    async def daily_history(self, security_id: str,
                            days_back: int = 330) -> list[dict]:
        return await self._historical(security_id, "NSE_EQ", "EQUITY", days_back)

    async def index_history(self, security_id: int,
                            days_back: int = 330) -> list[dict]:
        return await self._historical(str(security_id), "IDX_I", "INDEX", days_back)

    # ------------------------------------------------------------------ quote

    async def ohlc(self, security_ids: list[str],
                   segment: str = "NSE_EQ") -> dict[str, dict]:
        """Today's OHLC per security id (the OPEN is what fills use)."""
        out: dict[str, dict] = {}
        for i in range(0, len(security_ids), 800):
            chunk = [int(s) for s in security_ids[i:i + 800]]
            r = await self._client.post(f"{BASE}/marketfeed/ohlc",
                                        json={segment: chunk},
                                        headers=self._headers())
            r.raise_for_status()
            data = (r.json().get("data") or {}).get(segment) or {}
            for sid, q in data.items():
                o = (q.get("ohlc") or {})
                out[str(sid)] = {"open": o.get("open"), "high": o.get("high"),
                                 "low": o.get("low"), "close": o.get("close"),
                                 "last": q.get("last_price")}
            await asyncio.sleep(0.4)
        return out

    # ----------------------------------------------------------- scrip master

    async def scrip_master_nse_eq(self) -> dict[str, str]:
        """TRADING SYMBOL -> security id for NSE equities (EQ/BE series)."""
        r = await self._client.get(SCRIP_MASTER_URL, timeout=120.0)
        r.raise_for_status()
        out: dict[str, str] = {}
        reader = csv.DictReader(io.StringIO(r.text))
        for row in reader:
            try:
                if (row.get("SEM_EXM_EXCH_ID") or "").strip() != "NSE":
                    continue
                if (row.get("SEM_INSTRUMENT_NAME") or "").strip() != "EQUITY":
                    continue
                series = (row.get("SEM_SERIES") or "").strip()
                if series not in ("EQ", "BE", ""):
                    continue
                sym = (row.get("SEM_TRADING_SYMBOL") or "").strip().upper()
                sid = (row.get("SEM_SMST_SECURITY_ID") or "").strip()
                if sym and sid and sym not in out:
                    out[sym] = sid
            except Exception:                    # one bad row never kills the map
                continue
        return out
