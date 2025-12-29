#!/usr/bin/env python3
"""
app.py

Dual-mode tool:
 - CLI: python app.py --input in.xlsx --out out.csv
 - Streamlit: streamlit run app.py  (provides file uploader + download)

Reads order-level Excel and computes p75 preparation time per:
 - day_of_week (Monday..Sunday)
 - hour_bucket (06-12,12-17,17-23)
 - units_bucket (1-5, 6-10, 11-15, ...)
Outputs CSV with columns:
 day_of_week, hour_bucket, units_bucket, p75_seconds, orders_count, base_p75_minutes
"""
from __future__ import annotations
import argparse
import math
import time
import importlib
import sys
import os
from typing import Optional

# optional streamlit UI
try:
    import streamlit as st  # type: ignore
except Exception:
    st = None  # not running under streamlit

# -------- lazy imports (robust to environments that install deps at runtime) --------
def _try_import(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        return None

def get_pandas(retries: int = 8, delay: float = 0.8):
    backoff = delay
    for _ in range(retries):
        mod = _try_import("pandas")
        if mod is not None:
            return mod
        time.sleep(backoff)
        backoff = min(backoff * 1.2, 3.0)
    # last try (let exception propagate)
    return importlib.import_module("pandas")

def get_numpy(retries: int = 8, delay: float = 0.8):
    backoff = delay
    for _ in range(retries):
        mod = _try_import("numpy")
        if mod is not None:
            return mod
        time.sleep(backoff)
        backoff = min(backoff * 1.2, 3.0)
    return importlib.import_module("numpy")

# -------- constants (can be adjusted) --------
UNIT_BINS = [0, 5, 10, 15, 20, 25, 55, 99999]
HOUR_BUCKETS = [
    ("06-12", 6, 11),
    ("12-17", 12, 16),
    ("17-23", 17, 22),
]

# default parameters
DEFAULT_LOOKBACK_MONTHS = 6

# -------- helpers --------
def make_columns_unique(cols):
    """Return list of unique column names preserving order, appending suffix for duplicates."""
    seen = {}
    out = []
    for c in cols:
        key = str(c)
        if key in seen:
            seen[key] += 1
            out.append(f"{key}__dup{seen[key]}")
        else:
            seen[key] = 0
            out.append(key)
    return out

def detect_columns(df):
    """
    Heuristic detection for:
     - units column (units/items/qty)
     - prep time column (prep/prepare/preparation/time to prepare/time)
     - timestamp column (timestamp/time/date/created)
    Returns tuple (units_col_name, prep_col_name, ts_col_name_or_None)
    """
    cols_map = {str(c).lower(): c for c in df.columns}
    units_col = None
    prep_col = None
    ts_col = None
    for lower, orig in cols_map.items():
        if units_col is None and any(k in lower for k in ("units","items","qty","unit_count","quantity","units_sold","unitssold")):
            units_col = orig
        if prep_col is None and any(k in lower for k in ("prep","prepare","preparation","preptime","time to prepare","time_to_prepare","time_to_pick","time")):
            # be conservative: skip "time received" if we can detect better, but keep heuristic simple
            prep_col = orig
        if ts_col is None and any(k in lower for k in ("timestamp","time","date","created","time received","time_received","time_of_order","time delivered","time_delivered","time_received")):
            ts_col = orig
    return units_col, prep_col, ts_col

def to_seconds(x, pd):
    """
    Convert value to integer seconds. Accepts: numeric (assumed seconds), strings like 'MM:SS' or 'HH:MM:SS',
    or numeric in string form.
    Returns None if cannot parse.
    """
    # guard against being passed a Series accidentally
    import numpy as _np  # small local import
    if isinstance(x, (pd.Series, _np.ndarray)):
        return None
    if pd.isna(x):
        return None
    if isinstance(x, (int, float)):
        try:
            return int(x)
        except Exception:
            return None
    s = str(x).strip()
    if s == "":
        return None
    if ":" in s:
        parts = [p for p in s.split(":") if p != ""]
        try:
            parts = [int(p) for p in parts]
        except Exception:
            return None
        if len(parts) == 3:
            return parts[0]*3600 + parts[1]*60 + parts[2]
        if len(parts) == 2:
            return parts[0]*60 + parts[1]
        return parts[0]
    # try float/int
    try:
        return int(float(s))
    except Exception:
        return None

def hour_bucket_from_hour(h):
    try:
        h = int(h)
    except Exception:
        return "other"
    for name, start, end in HOUR_BUCKETS:
        if start <= h <= end:
            return name
    return "other"

def round_half_up(x):
    return int(math.floor(x + 0.5))

# -------- core computation --------
def compute_p75_from_dataframe(df, lookback_months: int = DEFAULT_LOOKBACK_MONTHS, venue: Optional[str] = None):
    """
    Input: a pandas DataFrame (raw orders). The function:
     - makes columns unique if needed
     - detects units/prep/timestamp columns (heuristic)
     - filters by lookback_months if timestamp exists
     - buckets by day_of_week, hour_bucket, units_bucket
     - computes p75 (75th percentile) of prep_seconds per group
     - rounds p75 to nearest minute (round half up) and outputs seconds
    Returns: pandas.DataFrame with columns:
     day_of_week, hour_bucket, units_bucket, p75_seconds, orders_count, base_p75_minutes
    """
    pd = get_pandas()
    np = get_numpy()

    # protect against duplicate column names by making them unique
    if df.columns.duplicated().any():
        df = df.copy()
        df.columns = make_columns_unique(list(df.columns))

    units_col, prep_col, ts_col = detect_columns(df)

    if units_col is None or prep_col is None:
        raise ValueError(
            "Couldn't detect units or preparation-time column. Columns found: " + ", ".join(map(str, df.columns))
        )

    # optionally filter venue if provided (if there's a 'venue' column)
    if venue:
        possible_cols = [c for c in df.columns if 'venue' in str(c).lower() or 'store' in str(c).lower()]
        if possible_cols:
            vcol = possible_cols[0]
            df = df[df[vcol].astype(str).str.contains(venue, case=False, na=False)]

    # make a working copy with only relevant columns
    keep_cols = [units_col, prep_col]
    if ts_col:
        keep_cols.append(ts_col)
    dfw = df[keep_cols].copy()

    # normalize units
    dfw["units"] = pd.to_numeric(dfw[units_col], errors="coerce").fillna(0).astype(int)

    # compute prep_seconds robustly
    dfw["prep_seconds"] = dfw[prep_col].apply(lambda x: to_seconds(x, pd))

    # timestamp handling + lookback
    if ts_col and ts_col in dfw.columns:
        dfw["ts"] = pd.to_datetime(dfw[ts_col], errors="coerce")
        if lookback_months and lookback_months > 0:
            cutoff = pd.Timestamp.now() - pd.DateOffset(months=int(lookback_months))
            dfw = dfw[dfw["ts"].isna() | (dfw["ts"] >= cutoff)]

    # day_of_week and hour
    if "ts" in dfw.columns and dfw["ts"].notna().any():
        dfw["day_of_week"] = dfw["ts"].dt.day_name()
        dfw["hour"] = dfw["ts"].dt.hour.fillna(0).astype(int)
    else:
        dfw["day_of_week"] = "Unknown"
        dfw["hour"] = 0

    dfw["hour_bucket"] = dfw["hour"].apply(hour_bucket_from_hour)

    # units buckets
    bins = UNIT_BINS
    labels = []
    for i in range(len(bins)-1):
        labels.append(f"{bins[i]+1}-{bins[i+1] if bins[i+1]<99999 else '999'}")
    dfw["units_bucket"] = pd.cut(dfw["units"], bins=bins, labels=labels, right=True)

    # group and compute p75
    rows = []
    group_cols = ["day_of_week", "hour_bucket", "units_bucket"]

    grouped = dfw.groupby(group_cols, dropna=False)
    for name, g in grouped:
        day, hour_b, units_b = name
        values = g["prep_seconds"].dropna().astype(float)
        count = int(values.shape[0])
        if count == 0:
            continue
        p75 = float(np.percentile(values, 75))
        # round to nearest minute (half-up) then convert to seconds
        p75_rounded_seconds = round_half_up(p75 / 60.0) * 60
        rows.append({
            "day_of_week": day,
            "hour_bucket": hour_b,
            "units_bucket": str(units_b),
            "p75_seconds": int(p75_rounded_seconds),
            "orders_count": count,
            "base_p75_minutes": int(p75_rounded_seconds // 60)
        })

    out = get_pandas().DataFrame(rows)
    if out.empty:
        # create empty frame with canonical columns
        out = get_pandas().DataFrame(columns=["day_of_week","hour_bucket","units_bucket","p75_seconds","orders_count","base_p75_minutes"])
    out = out.sort_values(["day_of_week","hour_bucket","units_bucket"])
    return out

# -------- CLI and Streamlit glue --------
def cli_main():
    parser = argparse.ArgumentParser(prog="app.py", description="Compute p75 prep times from order-level Excel")
    parser.add_argument("--input", "-i", required=True, help="input Excel (.xlsx)")
    parser.add_argument("--out", "-o", required=True, help="output CSV")
    parser.add_argument("--lookback_months", "-l", type=int, default=DEFAULT_LOOKBACK_MONTHS, help="lookback months filter")
    parser.add_argument("--venue", "-v", default=None, help="optional venue filter")
    args = parser.parse_args()

    pd = get_pandas()
    # read file
    print("Reading", args.input)
    df = pd.read_excel(args.input, engine="openpyxl")
    print("Computing p75 ...")
    out = compute_p75_from_dataframe(df, lookback_months=args.lookback_months, venue=args.venue)
    out.to_csv(args.out, index=False)
    print("Wrote", args.out)
    return 0

def streamlit_main():
    pd = get_pandas()
    st.set_page_config(page_title="Prep p75 tool", layout="wide")
    st.title("Prep p75 generator")
    st.markdown("Upload an order-level Excel file (columns: units, prep time, timestamp). The tool computes p75 preparation time per day/hour/units bucket.")

    uploaded = st.file_uploader("Upload Excel (.xlsx)", type=["xlsx","xls"])
    default_input_path = None
    # show list of files in /mnt/data for convenience
    try:
        files = os.listdir("/mnt/data")
        if files:
            st.sidebar.markdown("Files in /mnt/data:")
            for f in files:
                st.sidebar.write(f)
            default_input_path = os.path.join("/mnt/data", files[0])
            st.sidebar.caption("You can place files in /mnt/data and they will appear here.")
    except Exception:
        pass

    lookback = st.number_input("Lookback months (0 = no limit)", min_value=0, max_value=60, value=DEFAULT_LOOKBACK_MONTHS, step=1)
    venue = st.text_input("Optional venue filter (substring)")

    run_button = st.button("Run" if uploaded else "Run on default /mnt/data file")
    df = None
    if uploaded:
        try:
            df = pd.read_excel(uploaded, engine="openpyxl")
            st.success(f"Loaded uploaded file: {uploaded.name}")
        except Exception as e:
            st.error(f"Error reading uploaded file: {e}")
    elif default_input_path and run_button:
        try:
            df = pd.read_excel(default_input_path, engine="openpyxl")
            st.success(f"Loaded default file: {default_input_path}")
        except Exception as e:
            st.error(f"Error reading default file: {e}")

    if df is not None:
        with st.spinner("Computing p75 ..."):
            try:
                out = compute_p75_from_dataframe(df, lookback_months=int(lookback), venue=venue if venue.strip() else None)
                st.write("Results (first rows):")
                st.dataframe(out.head(200))
                csv_bytes = out.to_csv(index=False).encode("utf-8")
                st.download_button("Download CSV", csv_bytes, file_name="prep_p75_output.csv", mime="text/csv")
            except Exception as e:
                st.error("Error during computation: " + str(e))

# -------- entrypoint --------
def main_entry():
    # If running under streamlit (module imported by streamlit), prefer streamlit UI
    if st is not None:
        # streamlit will import this module and execute top-level code; provide UI
        streamlit_main()
    else:
        # CLI mode
        return cli_main()

if __name__ == "__main__":
    # when executed directly, detect whether CLI args supplied; if none and streamlit available, run UI
    # prefer CLI when args provided
    if len(sys.argv) > 1 and not (st is not None and any(a.startswith("--") for a in sys.argv[1:]) == False):
        # likely CLI use (has args)
        cli_main()
    else:
        # fallback: if Streamlit available, run UI; otherwise, require CLI args
        if st is not None:
            streamlit_main()
        else:
            print("No Streamlit detected and no CLI args were provided. Run with:")
            print("  python app.py --input path/to/in.xlsx --out path/to/out.csv")
            sys.exit(1)
