# Equity 100 Momentum — paper-live monitor

Paper-trades the confidence-scored NIFTY 100 momentum strategy: signals as
the backtest methodology document (v1.0, 21 Oct 2026) specifies, exits as the
**Hourly Exit Specification v1.0** (21 Oct 2026) specifies. Every known
difference from the backtest is listed on the Diagnostics page. LONG-ONLY
PAPER TRADING — nothing here places real orders.

## Two books, same signals
- **Live** — stops checked on HOURLY closes (at or below the stop frozen at
  the prior daily close), filled at the next hourly bar's open.
- **Shadow** — the backtest's daily-close rule on the same signals, so the
  effect of the exit convention is visible on its own (spec §9). Every live
  exit also records what the daily rule would have done.

## The day
- **Overnight / pre-open — daily freeze.** Dhan's daily historical API is
  T-1, so session T's completed bar appears overnight. Every 10 minutes the
  job checks; once EVERY symbol has the bar it commits session T, ratchets
  trailing stops from T's close, scores/ranks, and freezes the plan for the
  next session. It never freezes on partial data by itself (Diagnostics →
  "Freeze anyway" is the operator's call). Not frozen by 09:05 → red banner;
  the session still checks stops at the last frozen levels but takes no
  entries or refills until the freeze lands, and a late freeze then replays
  the session exactly as an on-time one would have.
- **09:15–15:30 — live session.** The Dhan feed builds hourly bars
  (09:15 … 14:15 + the 15:15 stub). Fresh entries fill at the 09:15 official
  open; a position opening at/below its stop exits at that open (slot
  refills 10:15); a bar close at/below the stop exits at the next bar's open
  and the slot refills one bar later from the frozen ranked list; a breach on
  the 15:15 stub fills at the next session's open and is flagged.
- The session is a deterministic replay of (committed book, frozen plan,
  recorded bars), saved every few seconds — a restart loses nothing recorded.

## Data needed
- Live feed: any Dhan plan.
- Daily bars: Dhan's **Data API** subscription on the same client ID
  (₹499 + tax / month). Without it the daily freeze cannot run (DH-902).
- Hourly history is NOT needed — the dashboard records its own hourly bars.

## Go-live
The first freeze is go-live: both books start with the starting capital and
no positions. Nothing earlier is ever simulated.

---

# Strategies 2 and 3 (page: /strategies)

Momentum Barbell 20 and Sector Momentum 12 from the "Three Systematic Equity
Strategies" specification (29 Oct 2026), paper trading, Rs 2,00,000 each.
Separate code (`backend/s23/`), separate database (`strategies.db` on the
same Render disk) — Strategy 1 above is untouched.

- Daily bars from Dhan (T-1): each session is evaluated once its completed
  bar is in (or at 08:45 before the next open with whatever arrived, the
  missing names recorded), and orders fill at the next session's official
  open. More than one missed session halts both until you approve on the page.
- Day 1 fills every seat at once (initial build, flagged).
- NSE calendar on the page (2026 holidays + Muhurat 8 Nov); a reminder from
  15 Dec to enter next year's list. Monthly NIFTY 500 upload (NSE
  ind_nifty500list) with a review-then-apply step.
- Rules follow the backtest code (`barbell20/factor_test.py` config 2 and
  `sector_rotation.py` + `d1_exit4.py`); where the spec differs, the code
  wins and the difference is listed on the Data & Audit tab. On synthetic
  markets the dashboard engine reproduces those engines fill for fill.
