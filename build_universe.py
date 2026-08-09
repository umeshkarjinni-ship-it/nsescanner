"""
Build a full NSE stock universe (1500+ symbols) with Large/Mid/Small cap
categorization, sourced directly from NSE's own published lists — instead
of a hand-typed list (which is small and error-prone, as we found out with
ZOMATO/ALKYL).

WHAT IT DOWNLOADS
------------------
1. The complete list of NSE-listed equities (all ~2000 symbols):
   https://archives.nseindia.com/content/equities/EQUITY_L.csv
2. Category lists to tag each symbol as Large / Mid / Small cap:
   - Nifty 100        -> Large
   - Nifty Midcap 150 -> Mid
   - Nifty Smallcap 250 -> Small
   Anything in EQUITY_L.csv but not in any of the above is tagged "Other"
   (micro-caps, recently listed, illiquid, etc. — you may want to exclude
   these from daily scanning).

IMPORTANT — NSE BLOCKS BOTS
-----------------------------
NSE's website actively blocks non-browser traffic. This script tries to
work around that with browser-like headers and a session warm-up, but if
NSE still blocks it (you'll see a clear error), use the MANUAL FALLBACK:

  1. Open each URL below directly in Chrome/Edge (a real browser bypasses
     the block), and use Ctrl+S to save each as a .csv file in this same
     folder, with EXACTLY these filenames:

     https://archives.nseindia.com/content/equities/EQUITY_L.csv
        -> save as equity_l.csv
     https://archives.nseindia.com/content/indices/ind_nifty100list.csv
        -> save as nifty100.csv
     https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv
        -> save as niftymidcap150.csv
     https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv
        -> save as niftysmallcap250.csv

  2. Run this script again — it automatically uses the local files if
     they're already present instead of re-downloading.

USAGE
-----
    python build_universe.py

Produces stocks_universe_full.csv (Symbol, Name, Category) ready to plug
into nse_scanner.py by setting UNIVERSE_CSV = "stocks_universe_full.csv"
"""

import io
import os
import sys
import pandas as pd
import requests

OUTPUT_CSV = "stocks_universe_full.csv"

SOURCES = {
    "equity_l.csv": "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
    "nifty100.csv": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "niftymidcap150.csv": "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "niftysmallcap250.csv": "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def get_csv_text(session: requests.Session, url: str) -> str:
    r = session.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


def fetch_all_remote() -> dict:
    """Try downloading all 4 NSE source files. Returns dict[filename] -> text, or {} on failure."""
    session = requests.Session()
    try:
        # Warm up session/cookies by hitting the main site first — NSE
        # frequently rejects requests that skip this step.
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=20)
    except Exception as e:
        print(f"Warning: could not warm up NSE session ({e}); attempting direct download anyway.")

    out = {}
    for fname, url in SOURCES.items():
        try:
            print(f"Downloading {url} ...")
            out[fname] = get_csv_text(session, url)
            print(f"  OK ({len(out[fname])} bytes)")
        except Exception as e:
            print(f"  FAILED: {e}")
    return out


def load_local_or_remote() -> dict:
    """Prefer local files (from the manual fallback) if present; else download."""
    texts = {}
    missing = []
    for fname in SOURCES:
        if os.path.exists(fname):
            print(f"Using local file: {fname}")
            with open(fname, "r", encoding="utf-8", errors="ignore") as f:
                texts[fname] = f.read()
        else:
            missing.append(fname)

    if missing:
        print(f"\n{len(missing)} file(s) not found locally — attempting automated download...")
        remote = fetch_all_remote()
        texts.update(remote)

    return texts


def build_universe():
    texts = load_local_or_remote()

    if "equity_l.csv" not in texts:
        print("\n" + "=" * 70)
        print("Could not obtain the full equity list (EQUITY_L.csv), automated")
        print("or local. Follow the MANUAL FALLBACK instructions in the header")
        print("comment of this script, then run it again.")
        print("=" * 70)
        sys.exit(1)

    full = pd.read_csv(io.StringIO(texts["equity_l.csv"]))
    full.columns = [c.strip() for c in full.columns]
    # NSE's EQUITY_L.csv columns: SYMBOL, NAME OF COMPANY, SERIES, ...
    full = full[full["SERIES"].str.strip() == "EQ"].copy()
    full["SYMBOL"] = full["SYMBOL"].str.strip()
    full["NAME OF COMPANY"] = full["NAME OF COMPANY"].str.strip()

    def load_category_set(fname):
        if fname not in texts:
            print(f"  Note: {fname} unavailable — that category won't be tagged.")
            return set()
        df = pd.read_csv(io.StringIO(texts[fname]))
        df.columns = [c.strip() for c in df.columns]
        return set(df["Symbol"].str.strip())

    large_set = load_category_set("nifty100.csv")
    mid_set = load_category_set("niftymidcap150.csv")
    small_set = load_category_set("niftysmallcap250.csv")

    def categorize(sym):
        if sym in large_set:
            return "Large"
        if sym in mid_set:
            return "Mid"
        if sym in small_set:
            return "Small"
        return "Other"

    full["Category"] = full["SYMBOL"].apply(categorize)

    result = full[["SYMBOL", "NAME OF COMPANY", "Category"]].rename(
        columns={"SYMBOL": "Symbol", "NAME OF COMPANY": "Name"})
    result = result.drop_duplicates(subset="Symbol").sort_values(["Category", "Symbol"])

    result.to_csv(OUTPUT_CSV, index=False)

    print(f"\n{'=' * 70}")
    print(f"Universe saved to {OUTPUT_CSV} — {len(result)} symbols total")
    print(result["Category"].value_counts().to_string())
    print("=" * 70)
    print(f"\nTo use it: open nse_scanner.py and set")
    print(f'  UNIVERSE_CSV = "{OUTPUT_CSV}"')
    print("Note: 'Other' includes micro-caps, recent listings, and thinly")
    print("traded stocks — consider filtering these out or scanning them")
    print("separately, since sparse volume can make signals unreliable.")


if __name__ == "__main__":
    build_universe()
