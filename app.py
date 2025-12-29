# app.py
import streamlit as st
import pandas as pd
import numpy as np
import math
from io import BytesIO

st.set_page_config(page_title="Prep Time P75", layout="wide")
st.title("Prep Time P75 Generator")

# ---------------- CONFIG ----------------
UNIT_BINS = [0, 5, 10, 15, 20, 25, 55, 10**9]
HOUR_BUCKETS = [
    ("06-12", 6, 11),
    ("12-17", 12, 16),
    ("17-23", 17, 22),
]

# ---------------- HELPERS ----------------
def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Make column names unique (fix duplicate headers from Excel)
    """
    df = df.copy()
    df.columns = pd.io.parsers.ParserBase(
        {"names": df.columns}
    )._maybe_dedup_names(df.columns)
    return df


def detect_columns(df: pd.DataFrame):
    units_col = None
    prep_col = None
    hour_col = None

    for c in df.columns:
        cl = c.lower()
        if units_col is None and any(x in cl for x in ["units", "items", "qty"]):
            units_col = c
        if prep_col is None and any(x in cl for x in ["prep", "prepare", "time"]):
            prep_col = c
        if hour_col is None and "hour" in cl:
            hour_col = c

    return units_col, prep_col, hour_col


def to_seconds_safe(x):
    """
    Convert value to seconds safely.
    Never raises.
    """
    if pd.isna(x):
        return np.nan

    # pandas Timedelta
    if hasattr(x, "total_seconds"):
        return x.total_seconds()

    # numeric
    if isinstance(x, (int, float, np.integer, np.floating)):
        return float(x)

    s = str(x).strip()
    if ":" in s:
        parts = s.split(":")
        try:
            parts = [int(p) for p in parts]
            if len(parts) == 3:
                return parts[0]*3600 + parts[1]*60 + parts[2]
            if len(parts) == 2:
                return parts[0]*60 + parts[1]
        except:
            return np.nan

    try:
        return float(s)
    except:
        return np.nan


def hour_bucket(h):
    try:
        h = int(h)
    except:
        return "other"

    for name, start, end in HOUR_BUCKETS:
        if start <= h <= end:
            return name
    return "other"


def round_half_up_minutes(seconds):
    return int(math.floor(seconds / 60 + 0.5))


# ---------------- UI ----------------
uploaded = st.file_uploader("Upload Excel (.xlsx)", type=["xlsx"])
generate = st.button("Generate CSV")

if generate:
    if uploaded is None:
        st.error("Please upload an Excel file")
        st.stop()

    # Read file
    df = pd.read_excel(BytesIO(uploaded.read()), engine="openpyxl")

    # Fix duplicate headers
    df = clean_columns(df)

    # Detect columns
    units_col, prep_col, hour_col = detect_columns(df)

    if units_col is None or prep_col is None:
        st.error("Could not detect units or prep time columns")
        st.write("Columns found:", list(df.columns))
        st.stop()

    # Units
    df["units"] = pd.to_numeric(df[units_col], errors="coerce").fillna(0).astype(int)

    # Prep seconds (NO apply, NO reindex)
    prep_series = df[prep_col].values
    df["prep_seconds"] = np.array([to_seconds_safe(x) for x in prep_series])

    # Hour
    if hour_col is not None:
        df["hour"] = pd.to_numeric(df[hour_col], errors="coerce").fillna(0).astype(int)
    else:
        df["hour"] = 0

    df["hour_bucket"] = df["hour"].apply(hour_bucket)

    # Units bucket
    labels = [
        f"{UNIT_BINS[i]+1}-{UNIT_BINS[i+1] if UNIT_BINS[i+1] < 10**8 else '999'}"
        for i in range(len(UNIT_BINS)-1)
    ]
    df["units_bucket"] = pd.cut(df["units"], bins=UNIT_BINS, labels=labels)

    # Aggregate
    rows = []
    for (hour_b, units_b), g in df.groupby(["hour_bucket", "units_bucket"]):
        vals = g["prep_seconds"].dropna()
        if len(vals) == 0:
            continue

        p75 = np.percentile(vals, 75)
        minutes = round_half_up_minutes(p75)

        rows.append({
            "hour_bucket": hour_b,
            "units_bucket": str(units_b),
            "p75_seconds": int(minutes * 60),
            "orders_count": len(vals),
            "base_p75_minutes": minutes,
        })

    out = pd.DataFrame(rows).sort_values(["hour_bucket", "units_bucket"])

    st.success("Done")
    st.dataframe(out)

    csv = out.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        csv,
        file_name="prep_p75.csv",
        mime="text/csv"
    )
