#!/usr/bin/env python3
"""
process_prep.py
Compute p75 preparation times per (day_of_week, hour_bucket, units_bucket)
Implements:
 - merge adjacent unit-buckets whose p75 differ by < MERGE_THRESHOLD_SEC (default 4 minutes)
 - enforce monotonic non-decreasing p75 across increasing unit buckets
Outputs CSV columns:
 day_of_week, hour_bucket, units_bucket, p75_seconds, orders_count, base_p75_minutes
"""
import argparse
import math
import numpy as np
import pandas as pd
from typing import List, Tuple

# ------------ CONFIG ------------
UNIT_BINS = [0, 5, 10, 15, 20, 25, 55, 99999]
HOUR_BUCKETS = [
    ("06-12", 6, 11),
    ("12-17", 12, 16),
    ("17-23", 17, 22),
]
MERGE_THRESHOLD_SEC = 4 * 60  # 4 minutes => if diff < this, merge buckets
# ---------------------------------

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--input", "-i", required=True, help="input Excel (.xlsx)")
    p.add_argument("--out", "-o", required=True, help="output CSV")
    p.add_argument("--venue", default=None, help="optional venue filter (not used currently)")
    p.add_argument("--lookback_months", type=int, default=6)
    return p.parse_args()

def clean_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Make duplicate column names unique by appending suffixes (so no duplicate labels)."""
    cols = list(df.columns)
    counts = {}
    newcols = []
    for c in cols:
        if c not in counts:
            counts[c] = 0
            newcols.append(c)
        else:
            counts[c] += 1
            newcols.append(f"{c}__dup{counts[c]}")
    df = df.copy()
    df.columns = newcols
    return df

def detect_columns(df: pd.DataFrame) -> Tuple[str, str, str]:
    cols = {str(c).lower(): c for c in df.columns}
    units_col = None
    prep_col = None
    ts_col = None
    for k_orig, v in cols.items():
        k = k_orig
        if units_col is None and any(x in k for x in ("units","items","qty","quantity")):
            units_col = v
        if prep_col is None and any(x in k for x in ("prep","prepare","preparation","time to prepare","time","prep_seconds")):
            prep_col = v
        if ts_col is None and any(x in k for x in ("timestamp","time","date","created","created_at")):
            ts_col = v
    return units_col, prep_col, ts_col

def to_seconds(x) -> float:
    """Convert many formats to seconds. Return np.nan when cannot parse."""
    if pd.isna(x):
        return np.nan
    # pandas Timedelta
    if isinstance(x, pd.Timedelta):
        return x.total_seconds()
    # datetime (not expected) -> NaN
    if isinstance(x, (list, dict, tuple, pd.Series, pd.DataFrame)):
        return np.nan
    # numbers
    try:
        if isinstance(x, (int, float, np.integer, np.floating)):
            return float(x)
    except Exception:
        pass
    s = str(x).strip()
    # hh:mm:ss or mm:ss or hh:mm
    if ":" in s:
        parts = [p for p in s.split(":") if p != ""]
        try:
            parts = [int(p) for p in parts]
            if len(parts) == 3:
                return parts[0]*3600 + parts[1]*60 + parts[2]
            if len(parts) == 2:
                return parts[0]*60 + parts[1]
            return float(parts[0])
        except Exception:
            return np.nan
    # numeric in string
    try:
        return float(s)
    except Exception:
        return np.nan

def hour_bucket_from_hour(h: int) -> str:
    for name, start, end in HOUR_BUCKETS:
        if start <= h <= end:
            return name
    return "other"

def round_half_up_seconds(seconds: float) -> int:
    # rounds minutes half-up, returns seconds (multiple of 60)
    minutes = seconds / 60.0
    rounded_min = int(math.floor(minutes + 0.5))
    return rounded_min * 60

def aggregate_and_process(df: pd.DataFrame, lookback_months: int=6) -> pd.DataFrame:
    # 1) clean duplicate cols
    df = clean_duplicate_columns(df)

    # 2) detect columns
    units_col, prep_col, ts_col = detect_columns(df)
    if units_col is None or prep_col is None:
        raise SystemExit(f"Couldn't detect units or prep columns. Available columns: {list(df.columns)}")

    # keep relevant columns (if ts exists)
    keep_cols = [units_col, prep_col]
    if ts_col is not None:
        keep_cols.append(ts_col)
    df = df[keep_cols].copy()

    # units numeric
    df["units"] = pd.to_numeric(df[units_col], errors="coerce").fillna(0).astype(int)

    # convert prep -> seconds
    df["prep_seconds"] = df[prep_col].apply(to_seconds)

    # optional lookback filter if timestamp present
    if ts_col in df.columns:
        df["ts"] = pd.to_datetime(df[ts_col], errors="coerce")
        cutoff = pd.Timestamp.now() - pd.DateOffset(months=lookback_months)
        df = df[df["ts"].isna() | (df["ts"] >= cutoff)]

    # day/hour
    if "ts" in df.columns and df["ts"].notna().any():
        df["day_of_week"] = df["ts"].dt.day_name()
        df["hour"] = df["ts"].dt.hour
    else:
        df["day_of_week"] = "Unknown"
        df["hour"] = 0

    df["hour_bucket"] = df["hour"].apply(hour_bucket_from_hour)

    # units bucket labels
    labels = []
    for i in range(len(UNIT_BINS)-1):
        hi = UNIT_BINS[i+1]
        labels.append(f"{UNIT_BINS[i]+1}-{hi if hi<99999 else '999'}")
    df["units_bucket"] = pd.cut(df["units"], bins=UNIT_BINS, labels=labels, right=True)

    # Now aggregate per (day_of_week, hour_bucket) and process buckets inside each group
    rows = []
    group_cols = ["day_of_week", "hour_bucket"]

    for (day, hour_b), g in df.groupby(group_cols):
        # create ordered list of buckets
        buckets = []
        for lb in labels:
            vals = g.loc[g["units_bucket"] == lb, "prep_seconds"].dropna().astype(float).tolist()
            buckets.append({"label": lb, "vals": vals})

        # merge adjacent buckets when difference < MERGE_THRESHOLD_SEC
        changed = True
        while True:
            merged_any = False
            new_buckets = []
            i = 0
            while i < len(buckets):
                if i == len(buckets)-1:
                    new_buckets.append(buckets[i])
                    i += 1
                    continue
                a = buckets[i]
                b = buckets[i+1]
                # compute p75 only if both have values
                if len(a["vals"])>0 and len(b["vals"])>0:
                    p75_a = float(np.percentile(a["vals"], 75))
                    p75_b = float(np.percentile(b["vals"], 75))
                    if abs(p75_a - p75_b) < MERGE_THRESHOLD_SEC:
                        # merge into single bucket (concat vals), label combine range
                        merged_vals = a["vals"] + b["vals"]
                        merged_label = f"{str(a['label']).split('-')[0]}-{str(b['label']).split('-')[-1]}"
                        new_buckets.append({"label": merged_label, "vals": merged_vals})
                        i += 2
                        merged_any = True
                        continue
                # otherwise keep a
                new_buckets.append(a)
                i += 1
            buckets = new_buckets
            if not merged_any:
                break

        # create p75 per bucket (after merges), enforce monotonic non-decreasing
        p75_list = []
        counts = []
        for b in buckets:
            if len(b["vals"])==0:
                p75_list.append(np.nan)
                counts.append(0)
            else:
                p75 = float(np.percentile(b["vals"], 75))
                p75_list.append(p75)
                counts.append(len(b["vals"]))

        # enforce monotonic non-decreasing: if current < previous -> set current = previous
        for i in range(1, len(p75_list)):
            if np.isnan(p75_list[i]):
                continue
            # find previous non-nan
            j = i-1
            while j>=0 and np.isnan(p75_list[j]):
                j -= 1
            if j>=0 and p75_list[i] < p75_list[j]:
                p75_list[i] = p75_list[j]

        # now append rows for buckets that have counts>0
        for idx, b in enumerate(buckets):
            cnt = counts[idx]
            if cnt == 0:
                continue
            p75 = p75_list[idx]
            if np.isnan(p75):
                continue
            p75_rounded = round_half_up_seconds(p75)
            rows.append({
                "day_of_week": day,
                "hour_bucket": hour_b,
                "units_bucket": b["label"],
                "p75_seconds": int(p75_rounded),
                "orders_count": int(cnt),
                "base_p75_minutes": int(p75_rounded // 60)
            })

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["day_of_week", "hour_bucket", "units_bucket"])
    return out

def main():
    args = parse_args()
    df = pd.read_excel(args.input, engine="openpyxl")
    out_df = aggregate_and_process(df, lookback_months=args.lookback_months)
    out_df.to_csv(args.out, index=False)
    print("Wrote", args.out)

if __name__ == "__main__":
    main()
