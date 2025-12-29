#!/usr/bin/env python3
"""
app.py - compute p75 prep times from order-level Excel

Usage:
 - CLI: python app.py --input path/in.xlsx --out path/out.csv
 - Streamlit UI: streamlit run app.py   (upload file, Run)
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
    st = None

# -------- lazy imports (robust) --------
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

# -------- constants --------
UNIT_BINS = [0, 5, 10, 15, 20, 25, 55, 99999]
HOUR_BUCKETS = [
    ("06-12", 6, 11),
    ("12-17", 12, 16),
    ("17-23", 17, 22),
]
DEFAULT_LOOKBACK_MONTHS = 6

# -------- helpers --------
def make_columns_unique(cols):
    """Make duplicate column names unique by adding suffix __dupN"""
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
    """Return (units_col, prep_col, ts_col_or_None). Works on exact df.columns."""
    cols_map = {str(c).lower(): c for c in df.columns}
    units_col = None
    prep_col = None
    ts_col = None
    for lower, orig in cols_map.items():
        if units_col is None and any(k in lower for k in ("units","items","qty","quantity")):
            units_col = orig
        if prep_col is None and any(k in lower for k in ("prep","prepare","preparation","prep_time","preptime","time to prepare","time_to_prepare","preparation_time")):
            prep_col = orig
        if ts_col is None and any(k in lower for k in ("timestamp","time","date","created","delivered","received","time_received","time_delivered")):
            ts_col = orig
    return units_col, prep_col, ts_col

def to_seconds(x, pd):
    """Convert single value to seconds. Returns None if cannot parse."""
    import numpy as _np
    # avoid accepting Series/array here
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
    pd = get_pandas()
    np = get_numpy()

    # make copy and ensure unique column names if duplicates exist (this fixes the reindex error)
    dfc = df.copy()
    if dfc.columns.duplicated().any():
        dfc.columns = make_columns_unique(list(dfc.columns))

    # detect columns
    units_col, prep_col, ts_col = detect_columns(dfc)
    # helpful error message listing columns
    if units_col is None or prep_col is None:
        raise ValueError("Couldn't detect required columns. Available columns: " + ", ".join(map(str, dfc.columns)))

    # optional venue filter
    if venue:
        venue_cols = [c for c in dfc.columns if 'venue' in str(c).lower() or 'store' in str(c).lower()]
        if venue_cols:
            vcol = venue_cols[0]
            dfc = dfc[dfc[vcol].astype(str).str.contains(venue, case=False, na=False)]

    # keep relevant columns
    keep = [units_col, prep_col] + ([ts_col] if ts_col else [])
    dfw = dfc[keep].copy()

    # normalize units
    dfw["units"] = pd.to_numeric(dfw[units_col], errors="coerce").fillna(0).astype(int)

    # ensure prep series is a Series (not DataFrame) even if name collision existed
    s_prep = dfw[prep_col]
    if isinstance(s_prep, pd.DataFrame):
        # choose first column if duplicated labels produced a DataFrame
        s_prep = s_prep.iloc[:, 0]

    # compute prep_seconds safely using list comprehension to avoid pandas weirdness
    prep_list = []
    for v in s_prep.tolist():
        prep_list.append(to_seconds(v, pd))
    dfw["prep_seconds"] = pd.Series(prep_list, index=dfw.index)

    # timestamp handling and lookback
    if ts_col and ts_col in dfw.columns:
        # make sure ts column also single series
        s_ts = dfw[ts_col]
        if isinstance(s_ts, pd.DataFrame):
            s_ts = s_ts.iloc[:,0]
        dfw["ts"] = pd.to_datetime(s_ts, errors="coerce")
        if lookback_months and lookback_months > 0:
            cutoff = pd.Timestamp.now() - pd.DateOffset(months=int(lookback_months))
            dfw = dfw[dfw["ts"].isna() | (dfw["ts"] >= cutoff)]

    # day/hour
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
        p75 = float(get_numpy().percentile(values, 75))
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
        out = get_pandas().DataFrame(columns=["day_of_week","hour_bucket","units_bucket","p75_seconds","orders_count","base_p75_minutes"])
    out = out.sort_values(["day_of_week","hour_bucket","units_bucket"])
    return out

# -------- CLI & UI --------
def cli_main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", "-i", required=True, help="input Excel (.xlsx)")
    parser.add_argument("--out", "-o", required=True, help="output CSV")
    parser.add_argument("--lookback_months", "-l", type=int, default=DEFAULT_LOOKBACK_MONTHS)
    parser.add_argument("--venue", "-v", default=None)
    args = parser.parse_args()

    pd = get_pandas()
    print("Reading", args.input)
    df = pd.read_excel(args.input, engine="openpyxl")
    print("Detected columns:", ", ".join(map(str, df.columns)))
    out = compute_p75_from_dataframe(df, lookback_months=args.lookback_months, venue=args.venue)
    out.to_csv(args.out, index=False)
    print("Wrote", args.out)

def streamlit_main():
    pd = get_pandas()
    st.set_page_config(page_title="Prep p75 tool", layout="wide")
    st.title("Prep p75 generator")
    st.markdown("Upload order-level Excel. The tool detects columns heuristically (units, prep-time, timestamp).")

    uploaded = st.file_uploader("Upload Excel (.xlsx)", type=["xlsx","xls"])
    default_input_path = None
    try:
        files = os.listdir("/mnt/data")
        if files:
            st.sidebar.markdown("Files in /mnt/data:")
            for f in files:
                st.sidebar.write(f)
            default_input_path = os.path.join("/mnt/data", files[0])
    except Exception:
        pass

    lookback = st.number_input("Lookback months (0 = no limit)", min_value=0, max_value=60, value=DEFAULT_LOOKBACK_MONTHS)
    venue = st.text_input("Optional venue substring")

    run_button = st.button("Run" if uploaded else "Run on default /mnt/data file")
    df = None
    if uploaded:
        try:
            df = pd.read_excel(uploaded, engine="openpyxl")
            st.success(f"Loaded uploaded file: {uploaded.name}")
            st.write("Columns detected:", list(df.columns))
        except Exception as e:
            st.error(f"Error reading uploaded file: {e}")
    elif default_input_path and run_button:
        try:
            df = pd.read_excel(default_input_path, engine="openpyxl")
            st.success(f"Loaded default file: {default_input_path}")
            st.write("Columns detected:", list(df.columns))
        except Exception as e:
            st.error(f"Error reading default file: {e}")

    if df is not None:
        with st.spinner("Computing p75 ..."):
            try:
                out = compute_p75_from_dataframe(df, lookback_months=int(lookback), venue=venue if venue.strip() else None)
                st.write("Results:")
                st.dataframe(out)
                csv_bytes = out.to_csv(index=False).encode("utf-8")
                st.download_button("Download CSV", csv_bytes, file_name="prep_p75_output.csv", mime="text/csv")
            except Exception as e:
                st.error("Error during computation: " + str(e))

if __name__ == "__main__":
    # If streamlit is running this module, streamlit will import it and execute; we prefer UI
    if st is not None and ("streamlit" in sys.argv[0] or any("streamlit" in a for a in sys.argv)):
        streamlit_main()
    else:
        # CLI mode
        if len(sys.argv) == 1:
            # no args -> if streamlit available, run UI, else show usage
            if st is not None:
                streamlit_main()
            else:
                print("Run with: python app.py --input in.xlsx --out out.csv")
                sys.exit(1)
        else:
            cli_main()
