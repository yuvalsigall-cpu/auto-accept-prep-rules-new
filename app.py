# app.py
import streamlit as st
import pandas as pd
import numpy as np
import math
from datetime import datetime
from io import StringIO, BytesIO

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
    # handle NaN
    if pd.isna(x):
        return None
    # numbers -> seconds assumed
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


def compute_p75_dataframe(df: pd.DataFrame, lookback_months=6, venue=None):
    # detect
    units_col, prep_col, ts_col = detect_columns(df)
    if units_col is None or prep_col is None:
        raise ValueError(
            "לא ניתן למצוא עמודת 'units' או עמודת 'prep time' בקובץ. עמודות שנמצאו: "
            + ", ".join(df.columns.astype(str))
        )

    # keep relevant
    cols = [units_col, prep_col]
    if ts_col:
        cols.append(ts_col)
    df = df[cols].copy()

    # units -> int
    df["units"] = pd.to_numeric(df[units_col], errors="coerce").fillna(0).astype(int)
    df["prep_seconds"] = df[prep_col].apply(to_seconds)

    # optional venue filter if the dataset has a column named 'venue' or similar
    if venue:
        # try to find a column that looks like venue
        possible = [c for c in df.columns if "venue" in c.lower() or "merchant" in c.lower() or "store" in c.lower()]
        if possible:
            col = possible[0]
            df = df[df[col].astype(str).str.contains(str(venue), case=False, na=False)]

    # timestamps and lookback
    if ts_col and ts_col in df.columns:
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
    for i in range(len(UNIT_BINS) - 1):
        labels.append(f"{UNIT_BINS[i] + 1}-{UNIT_BINS[i + 1] if UNIT_BINS[i + 1] < 99999 else '999'}")

    df["units_bucket"] = pd.cut(df["units"], bins=UNIT_BINS, labels=labels, right=True)

    # group and compute
    rows = []
    group_cols = ["day_of_week", "hour_bucket", "units_bucket"]
    grouped = df.groupby(group_cols, dropna=False)

    for name, g in grouped:
        day, hour_b, units_b = name
        values = g["prep_seconds"].dropna().astype(float)
        if len(values) == 0:
            continue

        p75 = float(np.percentile(values, 75))
        p75_rounded = round_half_up(p75 / 60.0) * 60

        rows.append({
            "day_of_week": day,
            "hour_bucket": hour_b,
            "units_bucket": str(units_b),
            "p75_seconds": int(p75_rounded),
            "orders_count": int(len(values)),
            "base_p75_minutes": int(p75_rounded // 60)
        })

    out = pd.DataFrame(rows)
    out = out.sort_values(["day_of_week", "hour_bucket", "units_bucket"])
    return out


# ---- Streamlit UI ----
st.title("Auto-Accept Prep Rules")
st.markdown("מחשב CSV להעלאה לקבוצת אקפט — העלה קובץ Excel (order level) והורד CSV עם p75 לפי קבוצות.")

uploaded = st.file_uploader("Upload Excel (.xlsx)", type=["xlsx", "xls"], accept_multiple_files=False)
col1, col2 = st.columns([1, 1])
with col1:
    venue = st.text_input("Venue (אופציונלי)")
with col2:
    lookback_months = st.number_input("Lookback months", min_value=0, max_value=24, value=6, step=1)

generate = st.button("Generate CSV")

if uploaded is not None:
    st.info(f"Uploaded: {uploaded.name} — size {uploaded.size} bytes")
else:
    st.info("אין קובץ מעומסת")

if generate:
    if uploaded is None:
        st.error("יש לבחור קובץ Excel לפני לחיצה על Generate CSV")
    else:
        try:
            # read file into pandas (openpyxl used automatically for .xlsx)
            df_input = pd.read_excel(uploaded, engine="openpyxl")
        except Exception as e:
            st.exception(f"שגיאה בקריאת הקובץ: {e}")
        else:
            try:
                with st.spinner("מחשב..."):
                    out_df = compute_p75_dataframe(df_input, lookback_months=lookback_months, venue=venue if venue else None)
                if out_df.empty:
                    st.warning("לא נמצאו ריצות/ערכים מתאימים בתוצאות — בדוק את נתוני הקובץ והעמודות.")
                else:
                    st.success("חישוב הושלם.")
                    st.dataframe(out_df.head(200))

                    # prepare CSV for download
                    csv_bytes = out_df.to_csv(index=False).encode("utf-8")
                    st.download_button(
                        label="הורד CSV",
                        data=csv_bytes,
                        file_name="prep_p75.csv",
                        mime="text/csv"
                    )
            except Exception as e:
                st.exception(f"שגיאה בחישוב: {e}")

st.markdown("---")
st.markdown("הסברים וטיפים:")
st.markdown("- ודא שהקובץ כולל עמודה למספר פריטים ('units', 'items', 'qty') ועמודת זמן/זמן הכנה ('prep','prepare','time').")
st.markdown("- אם יש לך עמודת venue/merchant בשם שונה, כתוב את הטקסט בשדה 'Venue' כדי לסנן.")
st.markdown("- אם יש בעיה נוספת, שלח לי את ה־Traceback (לשונית Manage app → Logs).")
