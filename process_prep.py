#!/usr/bin/env python3
"""
process_prep.py
Read an order-level Excel and compute p75 preparation time per:
 - day_of_week (Monday..Sunday)
 - hour_bucket (06-12,12-17,17-23)
 - units_bucket (1-5, 6-10, 11-15, ...)
Outputs CSV with columns:
day_of_week, hour_bucket, units_bucket, p75_seconds, orders_count, base_p75_minutes
"""

import argparse
import math
import time
import importlib
import pandas as pd


# -------- numpy lazy loader (חשוב!) --------
def get_numpy(retries=5, delay=1.0):
    for _ in range(retries):
        try:
            return importlib.import_module("numpy")
        except ModuleNotFoundError:
            time.sleep(delay)
    raise ModuleNotFoundError("numpy not available")


# -------- constants --------
UNIT_BINS = [0, 5, 10, 15, 20, 25, 55, 99999]

HOUR_BUCKETS = [
    ("06-12", 6, 11),
    ("12-17", 12, 16),
    ("17-23", 17, 22),
]


# -------- helpers --------
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--input", "-i", required=True, help="input Excel (.xlsx)")
    p.add_argument("--out", "-o", required=True, help="output CSV")
    p.add_argument("--venue", default=None, help="optional venue filter")
    p.add_argument("--lookback_months", type=int, default=6)
    return p.parse_args()


def detect_columns(df):
    cols = {c.lower(): c for c in df.columns}
    units_col = None
    prep_col = None
    ts_col = None

    for k, v in cols.items():
        if any(x in k for x in ("units", "items", "qty")) and units_col is None:
            units_col = v
        if any(x in k for x in ("prep", "prepare", "preparation", "time")) and prep_col is None:
            prep_col = v
        if any(x in k for x in ("timestamp", "date", "created")) and ts_col is None:
            ts_col = v

    return units_col, prep_col, ts_col


def to_seconds(x):
    if pd.isna(x):
        return None
    if isinstance(x, (int, float)):
        return int(x)

    s = str(x)
    if ":" in s:
        parts = [int(p) for p in s.split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return parts[0]

    try:
        return int(float(s))
    except:
        return None


def hour_bucket_from_hour(h):
    for name, start, end in HOUR_BUCKETS:
        if start <= h <= end:
            return name
    return "other"


def round_half_up(x):
    return int(math.floor(x + 0.5))


# -------- main logic --------
def main():
    args = parse_args()

    df = pd.read_excel(args.input, engine="openpyxl")

    units_col, prep_col, ts_col = detect_columns(df)
    if units_col is None or prep_col is None:
        raise SystemExit(
            "Couldn't detect units or prep time columns. Columns found: "
            + ", ".join(df.columns.astype(str))
        )

    df = df[[units_col, prep_col] + ([ts_col] if ts_col else [])].copy()

    df["units"] = pd.to_numeric(df[units_col], errors="coerce").fillna(0).astype(int)
    df["prep_seconds"] = df[prep_col].apply(to_seconds)

    if ts_col and ts_col in df.columns:
        df["ts"] = pd.to_datetime(df[ts_col], errors="coerce")
        cutoff = pd.Timestamp.now() - pd.DateOffset(months=args.lookback_months)
        df = df[df["ts"].isna() | (df["ts"] >= cutoff)]

    if "ts" in df.columns and df["ts"].notna().any():
        df["day_of_week"] = df["ts"].dt.day_name()
        df["hour"] = df["ts"].dt.hour
    else:
        df["day_of_week"] = "Unknown"
        df["hour"] = 0

    df["hour_bucket"] = df["hour"].apply(hour_bucket_from_hour)

    labels = []
    for i in range(len(UNIT_BINS) - 1):
        labels.append(
            f"{UNIT_BINS[i] + 1}-{UNIT_BINS[i + 1] if UNIT_BINS[i + 1] < 99999 else '999'}"
        )

    df["units_bucket"] = pd.cut(
        df["units"], bins=UNIT_BINS, labels=labels, right=True
    )

    rows = []
    group_cols = ["day_of_week", "hour_bucket", "units_bucket"]

    for name, g in df.groupby(group_cols):
        np = get_numpy()  # ← זה התיקון הקריטי
        day, hour_b, units_b = name

        values = g["prep_seconds"].dropna().astype(float)
        if len(values) == 0:
            continue

        p75 = float(np.percentile(values, 75))
        p75_rounded = round_half_up(p75 / 60.0) * 60

        rows.append(
            {
                "day_of_week": day,
                "hour_bucket": hour_b,
                "units_bucket": str(units_b),
                "p75_seconds": int(p75_rounded),
                "orders_count": int(len(values)),
                "base_p75_minutes": int(p75_rounded // 60),
            }
        )

    out = pd.DataFrame(rows)
    out = out.sort_values(["day_of_week", "hour_bucket", "units_bucket"])
    out.to_csv(args.out, index=False)

    print("Wrote", args.out)


if __name__ == "__main__":
    main()
