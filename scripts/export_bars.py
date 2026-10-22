#!/usr/bin/env python3
"""
Run this on YOUR machine, next to your backtest data, to produce one CSV the
dashboard can import.

    python3 export_bars.py /path/to/parquet_folder bars_export.csv
    python3 export_bars.py /path/to/parquet_folder bars_export.csv --since 2024-01-01

It reads every per-symbol file in the folder (.parquet or .csv; the symbol is
the filename), keeps date/open/high/low/close/volume (and volume_adj when the
column exists — otherwise volume is used, matching the data-layer convention
that unadjusted equals raw), and writes one combined CSV:

    symbol,date,o,h,l,c,volume,volume_adj

If the folder contains a NIFTY 50 index series, name the file NIFTY50.parquet
(or NIFTY50.csv) — it becomes the dashboard's session calendar and benchmark
line. Without it the dashboard falls back to a majority-vote calendar and
draws no benchmark.

Needs pandas (pip install pandas pyarrow).
"""
import sys
from pathlib import Path

import pandas as pd

ALIASES = {"date": ["date", "datetime", "ts", "timestamp"],
           "o": ["o", "open"], "h": ["h", "high"], "l": ["l", "low"],
           "c": ["c", "close"], "volume": ["volume", "vol", "v"],
           "volume_adj": ["volume_adj", "vol_adj", "adj_volume"]}


def pick(df, names):
    for n in names:
        if n in df.columns:
            return df[n]
    return None


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    folder, out = Path(sys.argv[1]), Path(sys.argv[2])
    since = None
    if "--since" in sys.argv:
        since = sys.argv[sys.argv.index("--since") + 1]
    rows, files = 0, 0
    with out.open("w") as f:
        f.write("symbol,date,o,h,l,c,volume,volume_adj\n")
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() not in (".parquet", ".csv"):
                continue
            sym = path.stem.upper().replace("NIFTY50", "__NIFTY50__")
            df = (pd.read_parquet(path) if path.suffix.lower() == ".parquet"
                  else pd.read_csv(path))
            df.columns = [str(c).strip().lower() for c in df.columns]
            if pick(df, ALIASES["date"]) is None and df.index.name:
                df = df.reset_index()
                df.columns = [str(c).strip().lower() for c in df.columns]
            d = pick(df, ALIASES["date"])
            if d is None:
                print(f"skip {path.name}: no date column")
                continue
            dates = pd.to_datetime(d).dt.strftime("%Y-%m-%d")
            cols = {k: pick(df, v) for k, v in ALIASES.items() if k != "date"}
            if any(cols[k] is None for k in ("o", "h", "l", "c")):
                print(f"skip {path.name}: missing OHLC")
                continue
            vol = cols["volume"]
            vadj = cols["volume_adj"] if cols["volume_adj"] is not None else vol
            for i in range(len(df)):
                dt = dates.iloc[i]
                if since and dt < since:
                    continue
                v = "" if vol is None else vol.iloc[i]
                va = "" if vadj is None else vadj.iloc[i]
                f.write(f"{sym},{dt},{cols['o'].iloc[i]},{cols['h'].iloc[i]},"
                        f"{cols['l'].iloc[i]},{cols['c'].iloc[i]},{v},{va}\n")
                rows += 1
            files += 1
    print(f"wrote {rows} bars from {files} files -> {out}")


if __name__ == "__main__":
    main()
