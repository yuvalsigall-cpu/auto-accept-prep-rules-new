# app.py (תעתיק/הדבק את כל הקובץ הזה)
import streamlit as st
import pandas as pd
import numpy as np
import math
from datetime import datetime
from io import BytesIO

st.set_page_config(page_title="Auto-Accept Prep Rules", layout="centered")

UNIT_BINS = [0, 5, 10, 15, 20, 25, 55, 99999]
HOUR_BUCKETS = [
    ("06-12", 6, 11),
    ("12-17", 12, 16),
    ("17-23", 17, 22),
]

def detect_columns(df: pd.DataFrame):
    cols = {c.lower(): c for c in df.columns}
    units_col = None
    prep_col = None
    ts_col = None
    for k, v in cols.items():
        if any(x in k for x in ("units", "items", "qty")) and units_col is None:
            units_col = v
        if any(x in k for x in ("prep", "prepare", "preparation", "time to prepare", "time")) and prep_col is None:
            prep_col = v
        if any(x in k for x in ("timestamp", "time", "date", "created")) and ts_col is None:
            ts_col = v
    return units_col, prep_col, ts_col

def to_seconds(x):
    if pd.isna(x):
        return None
    if isinstance(x, (int, float)):
        return int(x)
    s = str(x).strip()
    if ":" in s:
        try:
            parts = [int(p) for p in s.split(":") if p != ""]
        except:
            return None
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

def compute_p75_dataframe(df: pd.DataFrame, lookback_months=6, venue: str | None = None):
    # detect columns
    units_col, prep_col, ts_col = detect_columns(df)
    if units_col is None or prep_col is None:
        raise ValueError(
            "לא נמצא units או prep column. כותרות בקובץ: " + ", ".join(df.columns.astype(str))
        )

    # keep only relevant columns safely
    keep_cols = [units_col, prep_col]
    if ts_col is not None and ts_col in df.columns:
        keep_cols.append(ts_col)
    df = df[keep_cols].copy()

    # units -> int
    df["units"] = pd.to_numeric(df[units_col], errors="coerce").fillna(0).astype(int)
    df["prep_seconds"] = df[prep_col].apply(to_seconds)

    # optional venue filtering: only if user provided non-empty string
    if venue is not None and str(venue).strip() != "":
        # find candidate column name for venue
        possible = [c for c in df.columns if "venue" in c.lower() or "merchant" in c.lower() or "store" in c.lower()]
        if len(possible) > 0:
            col = possible[0]
            # use str.contains safely (na=False)
            df = df[df[col].astype(str).str.contains(str(venue), case=False, na=False)]
        else:
            # if user asked to filter but there is no venue-like column, return empty result
            return pd.DataFrame([])

    # timestamps & lookback
    if ts_col is not None and ts_col in df.columns:
        df["ts"] = pd.to_datetime(df[ts_col], errors="coerce")
        cutoff = pd.Timestamp.now() - pd.DateOffset(months=lookback_months)
        df = df[df["ts"].isna() | (df["ts"] >= cutoff)]

    # derive day / hour
    if "ts" in df.columns and df["ts"].notna().any():
        df["day_of_week"] = df["ts"].dt.day_name()
        df["hour"] = df["ts"].dt.hour
    else:
        df["day_of_week"] = "Unknown"
        df["hour"] = 0

    df["hour_bucket"] = df["hour"].apply(hour_bucket_from_hour)

    # units buckets
    labels = []
    for i in range(len(UNIT_BINS) - 1):
        labels.append(f"{UNIT_BINS[i] + 1}-{UNIT_BINS[i + 1] if UNIT_BINS[i + 1] < 99999 else '999'}")
    df["units_bucket"] = pd.cut(df["units"], bins=UNIT_BINS, labels=labels, right=True)

    # group and compute p75
    rows = []
    group_cols = ["day_of_week", "hour_bucket", "units_bucket"]
    grouped = df.groupby(group_cols, dropna=False)

    for name, g in grouped:
        # name is a tuple (day, hour_bucket, units_bucket)
        values = g["prep_seconds"].dropna().astype(float)
        if values.size == 0:
            continue
        p75 = float(np.percentile(values, 75))
        p75_rounded = round_half_up(p75 / 60.0) * 60
        rows.append({
            "day_of_week": name[0],
            "hour_bucket": name[1],
            "units_bucket": str(name[2]),
            "p75_seconds": int(p75_rounded),
            "orders_count": int(values.size),
            "base_p75_minutes": int(p75_rounded // 60)
        })

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["day_of_week", "hour_bucket", "units_bucket"])
    return out

# Streamlit UI
st.title("Auto-Accept Prep Rules")
st.markdown("מחשב CSV עם p75 preparation time. העלה Excel ואז לחץ Generate.")

uploaded = st.file_uploader("Upload Excel (.xlsx)", type=["xlsx", "xls"])
venue = st.text_input("Venue (אופציונלי)")
lookback_months = st.number_input("Lookback months", min_value=0, max_value=24, value=6, step=1)
generate = st.button("Generate CSV")

if uploaded is not None:
    st.info(f"Uploaded {uploaded.name} ({uploaded.size} bytes)")

if generate:
    if uploaded is None:
        st.error("בחר קובץ לפני הפעלת הייצור")
    else:
        try:
            df_in = pd.read_excel(uploaded, engine="openpyxl")
        except Exception as e:
            st.exception(f"שגיאה בקריאת הקובץ: {e}")
        else:
            try:
                with st.spinner("מחשב p75..."):
                    out_df = compute_p75_dataframe(df_in, lookback_months=lookback_months, venue=venue)
                if out_df.empty:
                    st.warning("לא נוצרו שורות — בדוק פילטר Venue/כותרות/טווח זמן.")
                else:
                    st.success("חישוב הושלם.")
                    st.dataframe(out_df.head(200))
                    csv_bytes = out_df.to_csv(index=False).encode("utf-8")
                    st.download_button("הורד CSV", data=csv_bytes, file_name="prep_p75.csv", mime="text/csv")
            except Exception as e:
                st.exception(f"שגיאה בחישוב: {e}")

st.markdown("---")
st.markdown("טיפים: ודא שיש עמודת units (או items/qty) ועמודת prep/time. אם השמות שונים שלח לי את כותרות העמודות.")
