1. Create a new GitHub repo `equity-dashboard`, upload this folder's contents.
2. Render: New Web Service from the repo. Starter (always-on) plan + the 1GB
   disk from render.yaml. Set env vars: DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN,
   ADMIN_PASSWORD.
3. First boot maps the 115 seeded symbols against Dhan's scrip master and
   backfills ~330 sessions of daily bars (several minutes). Watch Diagnostics:
   "Unmapped symbols" must end up empty — anything listed there needs its NSE
   trading symbol corrected on the Universe page.
4. Prune the Universe page to the actual current NIFTY 100 membership.
5. The first EOD run (18:30 IST, or "Run EOD now") produces the first frozen
   plan; fills begin the following session at 09:20.
