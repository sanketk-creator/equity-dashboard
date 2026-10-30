"""
NIFTY 500 universe and Strategy 3 sector map (spec §4.1, §5.1, §5.9.2).

Seed: the operator's MW-NIFTY-500-30-Oct-2026.csv (500 symbols) and the
spec's §5.9.2 sector lists (208 stocks in 8 sectors). Updated monthly from
NSE's ind_nifty500list file (Company Name, Industry, Symbol, Series, ISIN
Code): the dashboard shows who joined, who left and any industry change, and
applies only after the operator confirms.

Operator decisions (30 Oct 2026):
  * A HELD stock that leaves the NIFTY 500 is held to its own exit (stop or
    rank); it simply cannot be bought again. Same for a stock whose industry
    moves it out of / between sectors: its current seat is unaffected; the
    new mapping applies to future entries only.
"""
from __future__ import annotations

import csv
import io
import os

# Spec §4.1 (Strategy 2) — 8 of the listed 9 apply (DUMMYHEG is not in the
# list). Spec §5.1.1 (Strategy 3) — PATANJALI, HEXT, EMBASSY, BIRET,
# BAGMANE: all five are inside this set.
EXCLUDED = {"HEXT", "PATANJALI", "EMBASSY", "BIRET", "BAGMANE",
            "STLTECH", "HFCL", "MTARTECH", "DUMMYHEG"}

SECTORS = ("Auto", "Pharma", "Energy", "FMCG", "IT", "Metal", "Realty", "Media")

# NSE 'Industry' label(s) -> sector (spec §5.1.1)
INDUSTRY_TO_SECTOR = {
    "AUTOMOBILE AND AUTO COMPONENTS": "Auto",
    "HEALTHCARE": "Pharma",
    "OIL GAS & CONSUMABLE FUELS": "Energy",
    "POWER": "Energy",
    "FAST MOVING CONSUMER GOODS": "FMCG",
    "INFORMATION TECHNOLOGY": "IT",
    "METALS & MINING": "Metal",
    "REALTY": "Realty",
    "MEDIA ENTERTAINMENT & PUBLICATION": "Media",
}

# Dhan index names (normalised: upper-case, alphanumerics only) for the
# sector indices and the benchmark (spec §5.1).
SECTOR_INDEX = {
    "Auto": "NIFTY AUTO", "Pharma": "NIFTY PHARMA", "Energy": "NIFTY ENERGY",
    "FMCG": "NIFTY FMCG", "IT": "NIFTY IT", "Metal": "NIFTY METAL",
    "Realty": "NIFTY REALTY", "Media": "NIFTY MEDIA",
}
BENCH_INDEX = "NIFTY 500"

SECTOR_SEED = {
    "Auto": "APOLLOTYRE ARE&M ASAHIINDIA ATHERENERG BAJAJ-AUTO BALKRISIND BELRISE BHARATFORG BOSCHLTD CEATLTD CIEINDIA CRAFTSMAN EICHERMOT ENDURANCE EXIDEIND FORCEMOT GABRIEL HEROMOTOCO HYUNDAI JBMA JKTYRE M&M MARUTI MINDACORP MOTHERSON MRF MSUMI OLAELEC OLECTRA RKFORGE SANSERA SCHAEFFLER SHRIPISTON SONACOMS TENNIND TIINDIA TMPV TVSMOTOR UNOMINDA ZFCVINDIA",
    "Pharma": "ABBOTINDIA ACUTAAS AJANTPHARM ALKEM ANTHEM APOLLOHOSP ASTERDM AUROPHARMA BIOCON CAPLIPOINT CIPLA COHANCE CONCORDBIO DIVISLAB DRREDDY EMCURE FORTIS GLAND GLAXO GLENMARK GRANULES IPCALAB KIMS LALPATHLAB LAURUSLABS LUPIN MANKIND MAXHEALTH MEDANTA NATCOPHARM NEULANDLAB NH ONESOURCE POLYMED PPLPHARMA RAINBOW RUBICON SAILIFE SUNPHARMA SYNGENE TORNTPHARM VIJAYA WOCKPHARMA ZYDUSLIFE",
    "Energy": "ACMESOLAR ADANIENSOL ADANIGREEN ADANIPOWER AEGISLOG AEGISVOPAK ATGL BHARATCOAL BPCL CASTROLIND CESC CHENNPETRO CLEANMAX COALINDIA GAIL HINDPETRO IGL IOC JPPOWER JSWENERGY MGL MRPL NAVA NHPC NLCINDIA NTPC NTPCGREEN OIL ONGC PETRONET POWERGRID RELIANCE RPOWER SJVN TATAPOWER TORNTPOWER VEDPOWER VOGL",
    "FMCG": "ABDL AVANTIFEED AWL BALRAMCHIN BRITANNIA CCL COLPAL CUPID DABUR DOMS EIDPARRY EMAMILTD GILLETTE GODFRYPHLP GODREJCP HINDUNILVR HONASA ITC LTFOODS MARICO NESTLEIND RADICO TATACONSUM UBL UNITDSPR VBL ZYDUSWELL",
    "IT": "AFFLE BBOX BSOFT COFORGE CYIENT FRACTAL HCLTECH IKS INFY INTELLECT KPITTECH LTM LTTS MPHASIS NETWEB OFSS PERSISTENT SAGILITY TATAELXSI TATATECH TCS TECHM WIPRO ZENSARTECH",
    "Metal": "ADANIENT GMDCLTD GRAVITA HINDALCO HINDCOPPER HINDZINC JAINREC JINDALSTEL JSL JSWSTEEL LLOYDSME NATIONALUM NMDC NSLNISP SAIL SARDAEN TATASTEEL VAML VEDL VISL",
    "Realty": "ABREL ANANTRAJ BRIGADE DLF GODREJPROP LODHA OBEROIRLTY PHOENIXLTD PRESTIGE SIGNATURE SOBHA",
    "Media": "PFOCUS PVRINOX SUNTV ZEEL",
}


def norm_label(s: str) -> str:
    return " ".join((s or "").upper().replace("&AMP;", "&").split())


def norm_index(s: str) -> str:
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def seed_symbols() -> list[str]:
    p = os.path.join(os.path.dirname(__file__), "data",
                     "nifty500_seed_2026-10-30.txt")
    with open(p, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def seed_sector_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for sec, names in SECTOR_SEED.items():
        for s in names.split():
            if s in out:
                raise ValueError(f"{s} is in two sectors ({out[s]}, {sec}) — "
                                 f"spec §5.1 requires the run to halt")
            out[s] = sec
    return out


def parse_upload(text: str) -> dict:
    """Accepts NSE's ind_nifty500list (Company Name, Industry, Symbol,
    Series, ISIN Code) or a MW-NIFTY-500 market-watch export (SYMBOL, ...).
    Returns {format, rows: {symbol: {industry, company, isin}}}."""
    text = text.lstrip("﻿")
    rd = csv.DictReader(io.StringIO(text))
    cols = {(c or "").strip().upper(): c for c in (rd.fieldnames or [])}
    rows: dict[str, dict] = {}
    if "SYMBOL" in cols and "INDUSTRY" in cols:
        fmt = "ind_nifty500list"
        for r in rd:
            s = (r.get(cols["SYMBOL"]) or "").strip().upper()
            if not s:
                continue
            rows[s] = {"industry": (r.get(cols["INDUSTRY"]) or "").strip(),
                       "company": (r.get(cols.get("COMPANY NAME", ""), "") or "").strip(),
                       "isin": (r.get(cols.get("ISIN CODE", ""), "") or "").strip(),
                       "series": (r.get(cols.get("SERIES", ""), "") or "").strip()}
    elif "SYMBOL" in cols:
        fmt = "mw_nifty500"
        for r in rd:
            s = (r.get(cols["SYMBOL"]) or "").strip().upper()
            if not s or s.startswith("NIFTY "):
                continue
            rows[s] = {"industry": None, "company": None, "isin": None,
                       "series": None}
    else:
        raise ValueError("Unrecognised file: expected NSE's ind_nifty500list "
                         "(Company Name, Industry, Symbol, Series, ISIN Code) "
                         "or a MW-NIFTY-500 export with a SYMBOL column")
    if len(rows) < 450 or len(rows) > 520:
        raise ValueError(f"{len(rows)} symbols — a NIFTY 500 list should have "
                         f"about 500. Wrong file?")
    return {"format": fmt, "rows": rows}


def diff_upload(parsed: dict, current: dict[str, dict]) -> dict:
    """current: {symbol: {active, sector, industry}}. Returns the changes the
    operator must confirm."""
    new = parsed["rows"]
    active = {s for s, r in current.items() if r.get("active")}
    joined = sorted(set(new) - active)
    left = sorted(active - set(new))
    industry_changes, unknown_industries, sector_changes = [], set(), []
    joined_detail = []
    for s in sorted(new):
        ind = new[s]["industry"]
        if ind is None:
            sec = current.get(s, {}).get("sector")
        else:
            sec = INDUSTRY_TO_SECTOR.get(norm_label(ind))
            if ind and norm_label(ind) not in INDUSTRY_TO_SECTOR:
                unknown_industries.add(ind)
        if s in joined:
            joined_detail.append({"symbol": s, "industry": ind,
                                  "sector": sec,
                                  "excluded": s in EXCLUDED,
                                  "needs_sector": ind is None})
            continue
        old = current.get(s, {})
        if ind is not None and old.get("industry") and \
                norm_label(old["industry"]) != norm_label(ind):
            industry_changes.append({"symbol": s, "from": old["industry"],
                                     "to": ind})
        if ind is not None and (old.get("sector") or None) != (sec or None):
            sector_changes.append({"symbol": s, "from": old.get("sector"),
                                   "to": sec})
    return {"format": parsed["format"], "count": len(new),
            "joined": joined_detail, "left": left,
            "industry_changes": industry_changes,
            "sector_changes": sector_changes,
            "unrecognised_industry_labels": sorted(unknown_industries)}
