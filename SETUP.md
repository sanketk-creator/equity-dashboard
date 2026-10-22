1. Subscribe to Dhan's Data API on the client ID the dashboard uses
   (Dhan web → DhanHQ APIs → Data APIs; ₹499 + tax a month, renews every 30
   days from your Dhan balance).
2. Upload this folder's contents to the `equity-dashboard` GitHub repo
   (uploading never deletes old files — none need deleting this time).
3. Render already has the service, the 1 GB disk at /var/data and the env
   vars (DB_PATH, DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET, ADMIN_PASSWORD).
   No new variables. Deploy OUTSIDE market hours.
4. First boot: maps the symbols, starts the live feed, backfills ~330
   sessions of daily bars (several minutes), then the first freeze = go-live.
   Diagnostics → "Unmapped symbols" must be empty; prune the Universe page to
   the real NIFTY 100 membership.
5. Next session: fresh entries at 09:15, hourly checks all day. Diagnostics
   shows the feed, the hourly bars and the daily-freeze state.
