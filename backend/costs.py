"""
Delivery cash-equity costs (Section 8). The DP charge is a FLAT Rs 20 per
scrip per sell — never a percentage — because on a Rs 4,000 position that
flat fee is 50bps on one side of one trade, and a percentage model would
flatter small accounts. Not modelled (per the document): market impact,
bid-ask spread, slippage against the opening print, income tax.
"""
from __future__ import annotations

from . import config


def buy_cost(price: float, shares: int) -> float:
    turnover = price * shares
    brokerage = turnover * config.COST_BROKERAGE_PCT
    stt = turnover * config.COST_STT_DELIVERY_PCT
    stamp = turnover * config.COST_STAMP_BUY_PCT
    exch = turnover * config.COST_EXCHANGE_PCT
    sebi = turnover * config.COST_SEBI_PCT
    gst = (brokerage + exch + sebi) * config.COST_GST_PCT
    return brokerage + stt + stamp + exch + sebi + gst


def sell_cost(price: float, shares: int) -> float:
    turnover = price * shares
    brokerage = turnover * config.COST_BROKERAGE_PCT
    stt = turnover * config.COST_STT_DELIVERY_PCT
    exch = turnover * config.COST_EXCHANGE_PCT
    sebi = turnover * config.COST_SEBI_PCT
    gst = (brokerage + exch + sebi) * config.COST_GST_PCT
    dp = config.COST_DP_PER_SELL          # flat, per scrip per sell
    return brokerage + stt + exch + sebi + gst + dp
