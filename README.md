# Equity 100 Momentum — paper-live monitor

Paper-trades the confidence-scored NIFTY 100 momentum strategy exactly as the
backtest methodology document (v1.0, 21 Oct 2026) specifies, and records every
known difference on the Diagnostics page. LONG-ONLY PAPER TRADING — nothing
here places real orders.

## The daily cycle
- **18:30 IST (EOD):** ingest the day's completed bars (Dhan), compute the
  4-criteria scores, 12-1 ranks and stop checks ONCE, freeze the plan.
- **09:20 IST (next session):** fill the frozen plan at the live open —
  exits first, then refills in rank order.
- Stops are judged on the daily close only. No take-profit, no rank exit,
  no open-gap exit, no regime filter.
- If the service was down (or the token stale), the next EOD catches up every
  missed session from stored daily bars; late fills use that day's recorded
  open — the same price a live run would have used.

## Deploy
Upload this repo to GitHub; Render auto-deploys per render.yaml. Needs an
ALWAYS-ON instance (the scheduler must be awake at 09:20 and 18:30 IST) and a
persistent disk for the database. Set DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN and
ADMIN_PASSWORD in Render's Environment tab. The Dhan token expires daily —
renew it from the Diagnostics page.

## Conventions
See backend/config.py KNOWN_DIFFERENCES and the methodology document. The
sizing default is carry_forward (user decision, 21 Oct 2026); the backtest
used re_level — flip it on the Parameters card to make curves comparable.
