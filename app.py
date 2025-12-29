# app.py
import streamlit as st
import pandas as pd
import numpy as np
import io
import math
from typing import List, Tuple, Optional

st.set_page_config(page_title="Auto-Accept Prep Rules — p75 exporter", layout="wide")

# ---------------- utils ----------------
def make_unique_columns(cols: List[str]) -> List[str]:
    """Make duplicate column names unique by appending .1, .2, ..."""
    out = []
    seen = {}
    for c in cols:
        if c not in seen:
            seen[c] = 0
            out.append(c)
        else:
            seen[c] += 1
            new = f"{c}.{seen[c]}"
            # ensure new is unique too
            while new in seen:
                seen[c] += 1
                new = f"{c}.{seen[c]}"
            seen[new] = 0
            out.append(new)
    return out

def normalize_colname(s: str) -> str:
    """lower and strip"""
    return str(s).strip().lower()

def detect_columns_auto(df: pd.DataFrame) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Try to detect columns: venue, purchase id, prep time, day name, hour, units"""
    cols = list(df.columns)
    lower = {normalize_colname(c): c for c in cols}
    # candidates for each role (patterns)
    venue_keys = ["venue", "venue name", "branch", "store", "merchant", "שם סניף"]
    pid_keys = ["purchase id", "order id", "purchaseid", "orderid", "מזהה הזמנה"]
    prep_keys = ["time to prepare", "time to prepare the goods", "time to prepare the order", "time to prepare goods", "time to prepare", "prep", "prep time", "time to prepare the goods (hh:mm:ss)", "3) time to prepare the goods", "זמן להכין", "זמן להכנת"]
    day_keys = ["day of week", "time delivered day of week", "day", "יום בשבוע"]
    hour_keys = ["hour", "time received hour of the day", "hour of the day", "time received hour", "שעה"]
    units_keys = ["units", "units sold total", "units sold", "quantity", "qty", "כמות יחידות", "כמות יחידות בהזמנה"]

    def find(keys):
        for k in keys:
            lk = normalize_colname(k)
            if lk in lower:
                return lower[lk]
        # try substring matching
        for col in cols:
            lc = normalize_colname(col)
            for k in keys:
                if normalize_colname(k) in lc:
                    return col
        return None

    venue = find(venue_keys)
    pid = find(pid_keys)
    prep = find(prep_keys)
    day = find(day_keys)
    hour = find(hour_keys)
    units = find(units_keys)

    return venue, pid, prep, day, hour, units

def to_seconds_generic(x) -> Optional[int]:
    """Convert value to seconds. Accepts timedeltas, '0 days 00:41:21', '00:41:21', numeric seconds, or floats."""
    if pd.isna(x):
        return None
    # pandas Timedelta
    if isinstance(x, pd.Timedelta):
        return int(x.total_seconds())
    # numpy timedelta64
    if isinstance(x, np.timedelta64):
        try:
            return int(pd.to_timedelta(x).total_seconds())
        except Exception:
            pass
    # numeric
    if isinstance(x, (int, np.integer)) and not isinstance(x, bool):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return int(x)
    s = str(x).strip()
    if s == "":
        return None
    # some excel exports include "0 days 00:41:20.976000"
    if "days" in s and "0 days" in s:
        try:
            # pandas can parse it
            td = pd.to_timedelta(s)
            return int(td.total_seconds())
        except Exception:
            pass
    # colon separated hh:mm:ss or mm:ss
    if ":" in s:
        parts = [p for p in s.split(":") if p != ""]
        try:
            parts = [int(float(p)) for p in parts]
            if len(parts) == 3:
                return parts[0]*3600 + parts[1]*60 + parts[2]
            if len(parts) == 2:
                return parts[0]*60 + parts[1]
            return parts[0]
        except Exception:
            pass
    # fallback try float/int
    try:
        return int(float(s))
    except Exception:
        return None

def hour_bucket_from_hour(h):
    try:
        h = int(h)
    except:
        return "other"
    if 6 <= h <= 11:
        return "06-12"
    if 12 <= h <= 16:
        return "12-17"
    if 17 <= h <= 22:
        return "17-23"
    return "other"

def round_half_up(x):
    return int(math.floor(x + 0.5))

# ---------------- UI ----------------
st.title("Auto-Accept Prep Rules — p75 exporter")
st.caption("העלה Excel, בדוק זיהוי עמודות ולחץ **Generate CSV**")

uploaded = st.file_uploader("Upload Excel (.xlsx)", type=["xlsx", "xls"], accept_multiple_files=False)

if uploaded is None:
    st.info("העלה כאן את קובץ ה-Excel שלך (העמודות שציינת: Time Delivered Day of Week, Time Received Hour of the Day, Units Sold Total, 3) Time to prepare the goods (hh:mm:ss) ).")
    st.stop()

# read file into DataFrame
try:
    df = pd.read_excel(uploaded, engine="openpyxl")
except Exception as e:
    st.error(f"Error reading Excel: {e}")
    st.stop()

# make column names unique to avoid duplicate-label errors
orig_cols = list(df.columns)
unique_cols = make_unique_columns([str(c) for c in orig_cols])
df.columns = unique_cols

st.success(f"Loaded uploaded file: {getattr(uploaded, 'name', 'uploaded file')}")
st.write(f"Rows total: **{len(df)}**")

# auto-detect columns and allow override
venue_col_auto, pid_col_auto, prep_col_auto, day_col_auto, hour_col_auto, units_col_auto = detect_columns_auto(df)

with st.expander("Detected / choose columns (אם לא נכון בחר ידנית)"):
    col_options = list(df.columns)
    st.write("Detected (auto):")
    st.write({
        "venue": venue_col_auto,
        "purchase_id": pid_col_auto,
        "prep": prep_col_auto,
        "day": day_col_auto,
        "hour": hour_col_auto,
        "units": units_col_auto
    })
    venue_col = st.selectbox("Venue column (שם סניף)", col_options, index=col_options.index(venue_col_auto) if venue_col_auto in col_options else 0)
    pid_col = st.selectbox("Purchase ID column (מזהה הזמנה) — optional", [None] + col_options, index=0 if pid_col_auto not in col_options else col_options.index(pid_col_auto)+1)
    prep_col = st.selectbox("Prep time column (זמן להכין את ההזמנה)", col_options, index=col_options.index(prep_col_auto) if prep_col_auto in col_options else 0)
    day_col = st.selectbox("Day of week column (יום בשבוע)", col_options, index=col_options.index(day_col_auto) if day_col_auto in col_options else 0)
    hour_col = st.selectbox("Hour of day column (שעה ביום)", col_options, index=col_options.index(hour_col_auto) if hour_col_auto in col_options else 0)
    units_col = st.selectbox("Units column (כמות יחידות)", col_options, index=col_options.index(units_col_auto) if units_col_auto in col_options else 0)

lookback_months = st.number_input("Lookback months (0 = no filtering)", min_value=0, max_value=120, value=6, step=1)

st.write(f"Selected columns: prep = **{prep_col}**, day = **{day_col}**, hour = **{hour_col}**, units = **{units_col}**")

# units bins & labels requested by you
UNIT_BINS = [0, 5, 10, 20, 30, 40, 50, 80, 99999]
UNIT_LABELS = [
    "1-5",
    "6-10",
    "11-20",
    "21-30",
    "31-40",
    "41-50",
    "51-80",
    "81-999",
]

# Generate button
if st.button("Generate CSV"):
    try:
        working = df.copy()
        # compute prep_seconds safely
        prep_list = [to_seconds_generic(x) for x in working[prep_col].tolist()]  # use .tolist on Series is ok
        working["prep_seconds"] = prep_list

        non_empty = working["prep_seconds"].notna().sum()
        st.write(f"Non-empty prep values: **{non_empty}**")

        # timestamp lookback — we only filter if day or hour column is not explicitly datetime,
        # but since user provides day string and hour numeric, the lookback will be skipped
        # unless there is a datetime-like column (we attempt to detect).
        # We won't fail on missing dates; just proceed.

        # day_of_week: take from user-selected column (string like Tuesday etc.)
        working["day_of_week"] = working[day_col].astype(str).fillna("Unknown")

        # hour: use hour_col value (should be numeric hour)
        # if hour_col contains datetime values, extract hour
        if np.issubdtype(working[hour_col].dtype, np.datetime64):
            working["hour"] = pd.to_datetime(working[hour_col]).dt.hour
        else:
            # attempt numeric conversion
            working["hour"] = pd.to_numeric(working[hour_col], errors="coerce").fillna(0).astype(int)

        working["hour_bucket"] = working["hour"].apply(hour_bucket_from_hour)

        # units numeric
        working["units"] = pd.to_numeric(working[units_col], errors="coerce").fillna(0).astype(int)

        # make categorical ordered units_bucket
        working["units_bucket"] = pd.cut(working["units"], bins=UNIT_BINS, labels=UNIT_LABELS, right=True)
        working["units_bucket"] = working["units_bucket"].astype(pd.CategoricalDtype(categories=UNIT_LABELS, ordered=True))

        # remove rows without prep_seconds
        df_valid = working[working["prep_seconds"].notna()].copy()
        if df_valid.empty:
            st.error("אין תוצאות — לא נמצאו שורות עם ערכי זמן תקינים בעמודת ה-prep (prep seconds). בדוק את העמודה שנבחרה כ־prep.")
            st.stop()

        # aggregate p75
        agg_rows = []
        group_cols = ["day_of_week", "hour_bucket", "units_bucket"]
        for (day, hb, ub), g in df_valid.groupby(group_cols):
            vals = g["prep_seconds"].dropna().astype(float).values
            if len(vals) == 0:
                continue
            p75 = float(np.percentile(vals, 75))
            p75_rounded = round_half_up(p75 / 60.0) * 60
            agg_rows.append({
                "day_of_week": day,
                "hour_bucket": hb,
                "units_bucket": str(ub),
                "p75_seconds": int(p75_rounded),
                "orders_count": int(len(vals)),
                "base_p75_minutes": int(p75_rounded // 60)
            })

        out_df = pd.DataFrame(agg_rows)
        if out_df.empty:
            st.warning("לא נוצרו שורות אחרי האגרגציה — כנראה שאין שורות תואמות לקבוצות שנבחרו.")
            st.stop()

        # ensure columns exist for sorting
        # make day_of_week categorical in a reasonable order (Mon..Sun) if values match english names
        day_order = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday","Unknown"]
        if set(out_df["day_of_week"].unique()).issubset(set(day_order)):
            out_df["day_of_week"] = pd.Categorical(out_df["day_of_week"], categories=day_order, ordered=True)

        # ensure units_bucket categorical preserves requested order
        out_df["units_bucket"] = pd.Categorical(out_df["units_bucket"], categories=UNIT_LABELS, ordered=True)

        # sort
        sort_cols = ["day_of_week", "hour_bucket", "units_bucket"]
        existing_sort_cols = [c for c in sort_cols if c in out_df.columns]
        out_df = out_df.sort_values(existing_sort_cols).reset_index(drop=True)

        st.success("הפקה בוצעה — להורדה למטה")
        st.dataframe(out_df.head(200))

        # CSV download
        csv_bytes = out_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download CSV", data=csv_bytes, file_name="prep_p75_output.csv", mime="text/csv")

    except Exception as e:
        st.exception(f"Error during computation: {e}")
