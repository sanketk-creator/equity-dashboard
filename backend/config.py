"""
Equity 100 Momentum — paper-live monitor. Configuration.

Every convention here replicates the backtest methodology document
(v1.0, 21 Oct 2026) or records an explicit, user-chosen deviation.
KNOWN DIFFERENCES from the backtest (Section 12 requires these be recorded,
never silently absorbed) are listed in KNOWN_DIFFERENCES below and shown on
the Diagnostics page.
"""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

BUILD = "2026-10-22-equity-v6-hourly"

# --- Dhan credentials (same pattern as the options dashboards) --------------
DHAN_CLIENT_ID = os.getenv("DHAN_CLIENT_ID", "").strip()
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "").strip()

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

DB_PATH = os.getenv("DB_PATH", "./data/equity.db")

# --- The strategy, per the methodology document ------------------------------
STARTING_CAPITAL = float(os.getenv("STARTING_CAPITAL", "100000"))

# Defaults below mirror the document exactly; sizing_mode is the one
# user-chosen deviation (carry_forward; the backtest used re_level).
STRATEGY_PARAMS: dict = {
    "max_holdings": 10,
    "score_gate": 4,              # 4 of 4 criteria
    "percentile_pass": 0.60,      # criteria 2 & 3 pass at percentile >= this (inclusive)
    "vol_ratio_threshold": 1.2,   # criterion 4 passes STRICTLY above this
    "dma_window": 200,
    "ret6m_window": 126,
    "vol_window": 126,            # std of 126 LOG returns, ddof=1, x sqrt(252)
    "vol_avg_short": 5,
    "vol_avg_long": 20,
    "mom_skip": 21,               # 12-1 momentum: shift(21)/shift(252) - 1
    "mom_window": 252,
    "atr_window": 14,
    "atr_mult_initial": 2.0,      # entry - 2 x ATR(14) at signal day
    "atr_mult_trail": 3.0,        # highest close - 3 x current ATR
    "trail_activation_mult": 1.0, # activates at entry + 1 x entry ATR (close)
    "sizing_mode": "carry_forward",   # "carry_forward" | "re_level"  (BACKTEST = re_level)
    "target_weight_cap": 0.20,    # min(20%, 1/N)
    "min_bars_to_rank": 252,
}
PARAM_CHOICES = {"sizing_mode": ["carry_forward", "re_level"]}

# --- Costs (Section 8) --------------------------------------------------------
# Delivery cash-equity charges. Every rate is env-overridable; RATES_VERIFIED
# stays 0 until checked against a real contract note.
COST_BROKERAGE_PCT = float(os.getenv("COST_BROKERAGE_PCT", "0.0003"))      # 0.03%/side
COST_STT_DELIVERY_PCT = float(os.getenv("COST_STT_DELIVERY_PCT", "0.001"))  # 0.1% buy & sell
COST_STAMP_BUY_PCT = float(os.getenv("COST_STAMP_BUY_PCT", "0.00015"))      # buy side
COST_EXCHANGE_PCT = float(os.getenv("COST_EXCHANGE_PCT", "0.0000297"))      # NSE
COST_SEBI_PCT = float(os.getenv("COST_SEBI_PCT", "0.000001"))
COST_GST_PCT = float(os.getenv("COST_GST_PCT", "0.18"))                     # on brokerage+exch+sebi
COST_DP_PER_SELL = float(os.getenv("COST_DP_PER_SELL", "20.0"))             # flat Rs/scrip/sell
RATES_VERIFIED = os.getenv("RATES_VERIFIED", "0") == "1"

# --- Scheduler ---------------------------------------------------------------
# Dhan's DAILY historical API is T-1 (support article: "provides data only up
# to the previous trading day"), so day T's completed bar appears overnight.
# The daily job polls until every symbol has it, then freezes the plan for
# the next session — normally well before 09:15. Unfrozen at FREEZE_ALERT the
# dashboard goes red: the session still runs (stops held at the last frozen
# levels) but takes NO entries or refills until the freeze lands, and a late
# freeze replays the session exactly as an on-time one would have.
DAILY_POLL_MINUTES = int(os.getenv("DAILY_POLL_MINUTES", "10"))
FREEZE_ALERT = os.getenv("FREEZE_ALERT", "09:05")
BACKFILL_SESSIONS = int(os.getenv("BACKFILL_SESSIONS", "330"))

# Hourly bars: late packets for a bar get this long after the bar's end
# before the bar is treated as complete (and checked).
BAR_GRACE_SECONDS = float(os.getenv("BAR_GRACE_SECONDS", "10"))
# The feed is "down" when nothing has arrived for this long in the session.
FEED_STALE_SECONDS = float(os.getenv("FEED_STALE_SECONDS", "120"))

# --- Dhan live feed (verbatim from the Nifty dashboard's PROVEN client) -------
DHAN_FEED_URL = "wss://api-feed.dhan.co"
SEGMENT_NAME_BY_CODE = {
    0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY",
    4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CURRENCY", 8: "BSE_FNO",
}
REQ_SUBSCRIBE_TICKER = 15
REQ_SUBSCRIBE_QUOTE = 17
REQ_SUBSCRIBE_FULL = 21
RESP_TICKER = 2
RESP_QUOTE = 4
RESP_OI = 5
RESP_PREV_CLOSE = 6
RESP_FULL = 8
RESP_DISCONNECT = 50
MAX_INSTRUMENTS_PER_MESSAGE = 100


def feed_url() -> str:
    from .creds import current
    return (f"{DHAN_FEED_URL}?version=2&token={current.access_token}"
            f"&clientId={current.client_id}&authType=2")

# --- Benchmark ---------------------------------------------------------------
# NIFTY 50 PRICE index (Dhan id 13, IDX_I). NOT the TRI the document compares
# against: the price index understates the benchmark by roughly 1.2-1.5%/yr of
# dividends. Labelled as such everywhere it is drawn.
BENCHMARK_SECURITY_ID = int(os.getenv("BENCHMARK_SECURITY_ID", "13"))
BENCHMARK_LABEL = "NIFTY 50 (price index, not TRI)"

# --- Universe seed (the user's 21 Oct 2026 list: 115 symbols) -----------------
UNIVERSE_SEED = [s for s in """
ABB ADANIENSOL ADANIENT ADANIGREEN ADANIPORTS ADANIPOWER AMBUJACEM APOLLOHOSP
ASIANPAINT AUBANK AXISBANK BAJAJ-AUTO BAJAJFINSV BAJAJHFL BAJAJHLDNG BAJFINANCE
BANKBARODA BEL BHARTIARTL BOSCHLTD BPCL BRITANNIA BSE CANBK CGPOWER CHOLAFIN
COALINDIA DABUR DIVISLAB DLF DMART DRREDDY EICHERMOT ENRIN ETERNAL GAIL
GODREJCP GRASIM HAL HAVELLS HCLTECH HDFCAMC HDFCBANK HDFCLIFE HEROMOTOCO
HINDALCO HINDUNILVR HYUNDAI ICICIBANK ICICIGI ICICIPRULI IDEA INDHOTEL INDIGO
INDUSINDBK INFY IOC IRFC ITC JINDALSTEL JIOFIN JSWENERGY JSWSTEEL KOTAKBANK
LICI LODHA LT LTIM LTF M&M MARUTI MAXHEALTH MAZDOCK MOTHERSON MUTHOOTFIN
NAUKRI NESTLEIND NTPC ONGC PFC PIDILITIND PNB POLYCAB POWERGRID POWERINDIA
RECLTD RELIANCE SBICARD SBILIFE SBIN SHREECEM SHRIRAMFIN SIEMENS SOLARINDS
SUNPHARMA SWIGGY TATACAP TATACONSUM TATAMOTORS TATAPOWER TATASTEEL TCS TECHM
TITAN TMCV TORNTPHARM TRENT TVSMOTOR ULTRACEMCO UNITDSPR VAML VBL VEDL WIPRO
ZYDUSLIFE
""".split() if s]

# Section 3.5: IDEA is excluded throughout. Seeded inactive.
SEED_EXCLUDED = {"IDEA": "Excluded per methodology §3.5 (unadjusted 2019 rights issue)"}

# --- Known differences from the backtest (Section 12 compliance panel) --------
KNOWN_DIFFERENCES = [
    ("Exit convention (Hourly Exit Spec §9)", "The LIVE book exits on HOURLY "
     "closes (at-or-below the stop frozen at the prior daily close) and fills "
     "at the next hourly bar's open; the backtest exits on the DAILY close. "
     "Expected, not a bug: hourly exits fire more often and earlier. Every "
     "live exit is tagged exit_basis=hourly with what the daily rule would "
     "have done, and a SHADOW book runs the daily rule on the same signals "
     "so the two equity curves sit side by side."),
    ("Hourly bars source", "Built by the dashboard from the live Dhan feed: "
     "open = first trade in the hour (09:15 = the official session open), "
     "close = last trade before the hour ends. Not Dhan's historical 60-min "
     "candles; small differences from a downloaded hourly series are "
     "possible."),
    ("15:15 closing window", "Live data has the 15:15-15:30 stub; the "
     "historical feed lacks it from 2026-08-03. Every exit triggered only in "
     "that window is flagged closing_window_only."),
    ("Gap-exit refill", "A position that opens at/below its stop exits at "
     "that 09:15 open; its slot refills at the 10:15 open (operator "
     "decision, 22 Oct 2026, literal spec §6)."),
    ("Daily-data timing", "Dhan's daily historical API is T-1, so the "
     "freeze for session T runs overnight / pre-open rather than at T's "
     "close. Same bar, same numbers; only the wall-clock time differs."),
    ("Sizing convention", "carry_forward (user decision, 21 Oct 2026). The "
     "backtest and every number in §10 used re_level — the paper and backtest "
     "curves are NOT directly comparable while this is set. Editable on the "
     "Parameters card (sizing_mode)."),
    ("Benchmark", "NIFTY 50 PRICE index from Dhan; the document benchmarks "
     "against the TRI. The price line understates the benchmark by the "
     "dividend yield (~1.2-1.5%/yr)."),
    ("Universe seed", "115 symbols supplied on 21 Oct 2026 — more than a "
     "100-name membership; prune on the Universe page. Point-in-time behaviour "
     "holds only from go-live forward."),
    ("Corporate-action volume rule", "The backtest resolved apply-vs-skip per "
     "event against NSE traded quantity; forward, the factor is APPLIED to "
     "pre-ex-date volumes by default with a per-event skip switch on the "
     "approval form."),
    ("Cost rates", "Statutory rates are coded from published schedules and "
     "env-overridable; RATES_VERIFIED=0 until checked against a contract note. "
     "The Rs 20 DP charge per scrip per sell is applied exactly as specified."),
]
