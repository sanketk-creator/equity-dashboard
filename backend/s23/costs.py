"""
Transaction costs, spec §9.5 — identical for Strategies 2 and 3.

  Brokerage   0.03%    both sides
  STT         0.10%    both sides
  Stamp duty  0.015%   buy only
  Exchange    0.00325% before 2024-09-01, 0.00297% from that date, both sides
  GST         18% of (brokerage + exchange charges)
  DP          flat Rs 20 per scrip per SELL fill, including partial trims

No SEBI fee: the spec's worked examples (buy Rs 10,000 = 15.39, sell
Rs 10,000 = 33.89) do not include one, and these books must reconcile to
them. (Strategy 1's own cost model is separate and unchanged.)
"""
from __future__ import annotations

BROKERAGE = 0.0003
STT = 0.001
STAMP_BUY = 0.00015
EXCH_OLD = 0.0000325
EXCH_NEW = 0.0000297
EXCH_SWITCH = "2024-09-01"
GST = 0.18
DP_PER_SELL = 20.0


def _exch(date_iso: str) -> float:
    return EXCH_OLD if date_iso < EXCH_SWITCH else EXCH_NEW


def itemise(side: str, price: float, shares: int, date_iso: str) -> dict:
    value = price * shares
    if shares <= 0:
        return {"value": 0.0, "brokerage": 0.0, "stt": 0.0, "stamp": 0.0,
                "exchange": 0.0, "gst": 0.0, "dp": 0.0, "total": 0.0}
    brk = value * BROKERAGE
    stt = value * STT
    exch = value * _exch(date_iso)
    stamp = value * STAMP_BUY if side == "BUY" else 0.0
    gst = (brk + exch) * GST
    dp = DP_PER_SELL if side == "SELL" else 0.0
    total = brk + stt + stamp + exch + gst + dp
    return {"value": value, "brokerage": brk, "stt": stt, "stamp": stamp,
            "exchange": exch, "gst": gst, "dp": dp, "total": total}


def buy_cost(price: float, shares: int, date_iso: str) -> float:
    return itemise("BUY", price, shares, date_iso)["total"]


def sell_cost(price: float, shares: int, date_iso: str) -> float:
    return itemise("SELL", price, shares, date_iso)["total"]
