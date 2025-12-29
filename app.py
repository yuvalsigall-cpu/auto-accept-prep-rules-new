#!/usr/bin/env python3
"""
process_prep.py
Read an order-level Excel and compute p75 preparation time per:
 - day_of_week (Monday..Sunday)  <- supports existing day column names (Heb/Eng/numbers)
 - hour_bucket (06-12,12-17,17-23) <- supports existing hour column
 - units_bucket (1-5, 6-10, 11-15, ...)
Outputs CSV with columns:
day_of_week, hour_bucket, units_bucket, p75_seconds, orders_count, base_p75_minutes
"""
import argparse
import math
import time
import importlib
import os

# -------- lazy loaders (retries/backoff) --------
def _try_import(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        return None

def get_numpy(retries=10, delay=0.5):
    backoff = delay
    for _ in range(retries):
        m = _try_import("numpy")
        if m is not None:
            return m
        time.sleep(backoff)
        backoff = min(backoff * 1.2, 3.0)
    raise ModuleNotFoundError("numpy not available")

def get_pandas(retries=10, delay=0.5):
    backoff = delay
    for _ in range(retries):
        m = _try_import("pandas")
        if m is not None:
            return m
        time.sleep(backoff)
        backoff = min(backoff * 1.2, 3.0)
    raise ModuleNotFoundError("pandas not available")


# -------- constants --------
UNIT_BINS = [0, 5, 10, 15, 20, 25, 55, 99999]
HOUR_BUCKETS = [("06-12", 6, 11), ("12-17", 12, 16), ("17-23", 17, 22)]
MERGE_THRESHOLD_SEC = 4 * 60  # איחוד מדרגות אם p75 קרובים פחות מ-4 דק

# השמות שאתה ציינת:
DAY_COL_CANDIDATE = "Time Delivered Day of Week"
HOUR_COL_CANDIDATE = "Time Received Hour of the Day"

# -------- helpers --------
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--input", "-i", required=True, help="input Excel (.xlsx)")
    p.add_argument("--out", "-o", required=True, help="output CSV")
    p.add_argument("--lookback_months", type=int, default=6, help="how many months back to keep (requires timestamp column)")
    return p.parse_args()

def normalize_day_col(pd, val):
    if pd.isna(val):
        return None
    s = str(val).strip()
    if s == "":
        return None
    lower = s.lower()
    mapping = {
        # numbers
        "0":"Sunday","1":"Monday","2":"Tuesday","3":"Wednesday","4":"Thursday","5":"Friday","6":"Saturday",
        # english short
        "sun":"Sunday","mon":"Monday","tue":"Tuesday","wed":"Wednesday","thu":"Thursday","fri":"Friday","sat":"Saturday",
        # english full
        "sunday":"Sunday","monday":"Monday","tuesday":"Tuesday","wednesday":"Wednesday","thursday":"Thursday","friday":"Friday","saturday":"Saturday",
        # hebrew common forms
        "ראשון":"Sunday","שני":"Monday","שלישי":"Tuesday","רביעי":"Wednesday","חמישי":"Thursday","שישי":"Friday","שבת":"Saturday",
        "יום ראשון":"Sunday","יום שני":"Monday","יום שלישי":"Tuesday","יום רביעי":"Wednesday","יום חמישי":"Thursday","יום שישי":"Friday","יום שבת":"Saturday",
        "שני":"Monday", "ששי":"Friday"
    }
    return mapping.get(lower, s)  # אם לא מזוהה נחזיר את המלל המקורי (יכול להיות כבר באנגלית תקינה)

def to_seconds_safe(pd, x):
    # המרת פורמטים שונים לשניות (int/float/'mm:ss'/'hh:mm:ss'/Timedelta)
    import numpy as _np
    if isinstance(x, (pd.Series, pd.DataFrame)):
        try:
            x = x.iloc[0]
        except Exception:
            return _np.nan
    if pd.isna(x):
        return _np.nan
    # pandas Timedelta
    import datetime
    if isinstance(x, datetime.timedelta):
        return x.total_seconds()
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if ":" in s:
        parts = [p for p in s.split(":") if p != ""]
        try:
            parts = [int(p) for p in parts]
            if len(parts) == 3:
                return parts[0]*3600 + parts[1]*60 + parts[2]
            if len(parts) == 2:
                return parts[0]*60 + parts[1]
            return float(parts[0])
        except:
            try:
                return float(s)
            except:
                return _np.nan
    try:
        return float(s)
    except:
        return _np.nan

def hour_bucket_from_hour(h):
    try:
        h = int(h)
    except:
        return "other"
    for name, start, end in HOUR_BUCKETS:
        if start <= h <= end:
            return name
    return "other"

def round_half_up_seconds(seconds):
    minutes = seconds / 60.0
    rounded_min = int(math.floor(minutes + 0.5))
    return rounded_min * 60

# -------- main logic --------
def main():
    pd = get_pandas()
    np = get_numpy()
    args = parse_args()

    # read input
    df = pd.read_excel(args.input, engine="openpyxl")

    # handle duplicate column names by renaming duplicates (so assignment won't error)
    if df.columns.duplicated().any():
        cols = list(df.columns)
        counts = {}
        newcols = []
        for c in cols:
            counts.setdefault(c, 0)
            if counts[c] == 0:
                newcols.append(c)
            else:
                newcols.append(f"{c}__dup{counts[c]}")
            counts[c] += 1
        df.columns = newcols

    # detect units & prep columns heuristically (if names differ)
    cols_lower = {str(c).lower(): c for c in df.columns}
    units_col = None
    prep_col = None
    ts_col = None
    for k, v in cols_lower.items():
        if units_col is None and any(x in k for x in ("units", "items", "qty", "quantity")):
            units_col = v
        if prep_col is None and any(x in k for x in ("prep", "prepare", "preparation", "time to prepare", "prep_seconds", "prep_time", "time")):
            prep_col = v
        if ts_col is None and any(x in k for x in ("timestamp", "time", "date", "created", "created_at", "delivered", "order_time")):
            ts_col = v

    if units_col is None or prep_col is None:
        raise SystemExit("Couldn't detect units or prep time columns. Found: " + ", ".join(df.columns.astype(str)))

    # Keep only relevant columns + optional day/hour columns you provided
    extra_cols = []
    if DAY_COL_CANDIDATE in df.columns:
        extra_cols.append(DAY_COL_CANDIDATE)
    if HOUR_COL_CANDIDATE in df.columns:
        extra_cols.append(HOUR_COL_CANDIDATE)
    if ts_col and ts_col in df.columns and ts_col not in extra_cols:
        extra_cols.append(ts_col)

    df = df[[units_col, prep_col] + extra_cols].copy()

    # numeric units
    df["units"] = pd.to_numeric(df[units_col], errors="coerce").fillna(0).astype(int)

    # prepare seconds safely (vectorized apply is ok here because we handle scalars robustly)
    df["prep_seconds"] = df[prep_col].apply(lambda x: to_seconds_safe(pd, x))

    # day_of_week: prefer the explicit column you provided
    if DAY_COL_CANDIDATE in df.columns:
        df["day_of_week"] = df[DAY_COL_CANDIDATE].apply(lambda x: normalize_day_col(pd, x) or "Unknown")
    elif ts_col and ts_col in df.columns:
        df["ts"] = pd.to_datetime(df[ts_col], errors="coerce")
        if df["ts"].notna().any():
            df["day_of_week"] = df["ts"].dt.day_name()
        else:
            df["day_of_week"] = "Unknown"
    else:
        df["day_of_week"] = "Unknown"

    # hour: prefer the explicit hour column you provided
    if HOUR_COL_CANDIDATE in df.columns:
        df["hour"] = pd.to_numeric(df[HOUR_COL_CANDIDATE], errors="coerce").fillna(0).astype(int)
    elif "ts" in df.columns:
        df["hour"] = df["ts"].dt.hour.fillna(0).astype(int)
    else:
        df["hour"] = 0

    df["hour_bucket"] = df["hour"].apply(hour_bucket_from_hour)

    # units buckets
    labels = []
    for i in range(len(UNIT_BINS) - 1):
        hi = UNIT_BINS[i+1]
        labels.append(f"{UNIT_BINS[i]+1}-{hi if hi<99999 else '999'}")
    df["units_bucket"] = pd.cut(df["units"], bins=UNIT_BINS, labels=labels, right=True)

    # aggregate per day_of_week, hour_bucket, units_bucket
    rows = []
    group_cols = ["day_of_week", "hour_bucket", "units_bucket"]
    grouped = df.groupby(group_cols, dropna=False)

    for (day, hour_b, units_b), g in grouped:
        vals = g["prep_seconds"].dropna().astype(float).tolist()
        if len(vals) == 0:
            continue
        # compute p75
        p75 = float(np.percentile(vals, 75))
        p75_rounded = round_half_up_seconds(p75)
        rows.append({
            "day_of_week": day,
            "hour_bucket": hour_b,
            "units_bucket": str(units_b),
            "p75_seconds": int(p75_rounded),
            "orders_count": int(len(vals)),
            "base_p75_minutes": int(p75_rounded // 60)
        })

    out = pd.DataFrame(rows)
    if not out.empty:
        # apply merging / monotonic post-processing per day/hour (optional: keep as-is if you prefer)
        # כאן נשמור את המיזוג/מונוטוני כמחלקה נפרדת אם תרצה בהמשך; כרגע שמרנו את p75 ישירות.
        out = out.sort_values(["day_of_week", "hour_bucket", "units_bucket"])
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    out.to_csv(args.out, index=False)
    print("Wrote", args.out)
    print("Rows:", len(out))

if __name__ == "__main__":
    main()
