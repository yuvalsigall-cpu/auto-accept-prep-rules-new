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
def make_columns_unique(cols):
    """
    Ensure column names are unique.
    If a name repeats, append _1, _2, ...
    cols: iterable of column names
    returns: list of unique column names in same order
    """
    out = []
    counts = {}
    for c in cols:
        base = str(c)
        if base not in counts:
            counts[base] = 0
            out.append(base)
        else:
            counts[base] += 1
            new = f"{base}_{counts[base]}"
            # ensure new isn't already used (rare)
            i = 1
            while new in counts:
                i += 1
                new = f"{base}_{counts[base]}_{i}"
            counts[new] = 0
            out.append(new)
    return out


def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Make column names unique and strip whitespace.
    """
    df = df.copy()
    cols = [str(c).strip() for c in df.columns]
    unique = make_columns_unique(cols)
    df.columns = unique
    return df


def detect_columns(df: pd.DataFrame):
    """
    Heuristic: find units and prep columns.
    Returns (units_col, prep_col, ts_col_or_None)
    """
    units_col = None
    prep_col = None
    ts_col = None
    for c in df.columns:
        cl = c.lower()
        if units_col is None and any(x in cl for x in ["units", "items", "qty", "quantity"]):
            units_col = c
        if prep_col is None and any(x in cl for x in ["prep", "prepare", "preparation", "time to prepare", "time"]):
            # avoid matching 'order time' as prep accidentally: prefer exact tokens
            prep_col = c
        if ts_col is None and any(x in cl for x in ["timestamp", "time", "date", "created", "order_time", "order date"]):
            ts_col = c
    return units_col, prep_col, ts_col


def to_seconds_safe(x):
    """
    Convert value to seconds safely. Returns np.nan on failure.
    Handles numbers, strings 'hh:mm:ss' or 'mm:ss', pandas Timedelta.
    """
    if x is None:
        return np.nan
    # pandas NA checks
    if pd.isna(x):
        return np.nan

    # pandas Timedelta or similar
    try:
        if hasattr(x, "total_seconds"):
            return float(x.total_seconds())
    except Exception:
        pass

    # numeric
    if isinstance(x, (int, float, np.integer, np.floating)):
        return float(x)

    s = str(x).strip()
    if s == "":
        return np.nan

    # hh:mm:ss or mm:ss
    if ":" in s:
        parts = s.split(":")
        try:
            parts = [int(p) for p in parts]
            if len(parts) == 3:
                return parts[0]*3600 + parts[1]*60 + parts[2]
            if len(parts) == 2:
                return parts[0]*60 + parts[1]
            return float(parts[0])
        except Exception:
            return np.nan

    # plain number string
    try:
        return float(s)
    except Exception:
        return np.nan


def hour_bucket(h):
    try:
        h = int(float(h))
    except Exception:
        return "other"
    for name, start, end in HOUR_BUCKETS:
        if start <= h <= end:
            return name
    return "other"


def round_half_up_minutes(seconds):
    # seconds -> rounded minutes (half up)
    if pd.isna(seconds):
        return 0
    return int(math.floor(seconds / 60.0 + 0.5))


# ---------------- UI ----------------
uploaded = st.file_uploader("Upload Excel (.xlsx)", type=["xlsx"])
generate = st.button("Generate CSV")

if generate:
    if uploaded is None:
        st.error("Please upload an Excel file")
        st.stop()

    try:
        df_raw = pd.read_excel(BytesIO(uploaded.read()), engine="openpyxl")
    except Exception as e:
        st.exception(e)
        st.stop()

    # Fix duplicate headers and whitespace
    df = clean_columns(df_raw)

    # Detect columns
    units_col, prep_col, ts_col = detect_columns(df)

    if units_col is None or prep_col is None:
        st.error("Could not detect 'units' or 'prep time' columns automatically.")
        st.write("Columns found:", list(df.columns)[:50])
        st.stop()

    # Convert units
    try:
        df["units"] = pd.to_numeric(df[units_col], errors="coerce").fillna(0).astype(int)
    except Exception:
        # fallback: coerce to int in a safer way
        df["units"] = df[units_col].apply(lambda x: int(float(x)) if pd.notna(x) else 0)

    # Build prep_seconds using a stable list comprehension (avoids reindex issues)
    prep_values = df[prep_col].tolist()
    prep_seconds = [to_seconds_safe(x) for x in prep_values]
    df["prep_seconds"] = prep_seconds  # length matches because .tolist()

    # Timestamp -> hour if present
    if ts_col in df.columns:
        try:
            df["ts"] = pd.to_datetime(df[ts_col], errors="coerce")
            df["hour"] = df["ts"].dt.hour.fillna(0).astype(int)
        except Exception:
            df["hour"] = 0
    else:
        df["hour"] = 0

    df["hour_bucket"] = df["hour"].apply(hour_bucket)

    # Units bucket labels and cut
    labels = []
    for i in range(len(UNIT_BINS)-1):
        labels.append(f"{UNIT_BINS[i]+1}-{UNIT_BINS[i+1] if UNIT_BINS[i+1] < 10**8 else '999'}")
    try:
        df["units_bucket"] = pd.cut(df["units"], bins=UNIT_BINS, labels=labels)
    except Exception:
        # fallback: simple mapping by integer
        df["units_bucket"] = df["units"].apply(lambda u: next((lab for i, lab in enumerate(labels) if UNIT_BINS[i] < u <= UNIT_BINS[i+1]), "unknown"))

    # Aggregate: group by hour_bucket + units_bucket (and optionally day if desired)
    rows = []
    group_cols = ["hour_bucket", "units_bucket"]

    grouped = df.groupby(group_cols, dropna=False)
    for name, g in grouped:
        vals = pd.to_numeric(g["prep_seconds"], errors="coerce").dropna().values
        if len(vals) == 0:
            continue
        p75 = float(np.percentile(vals, 75))
        minutes = round_half_up_minutes(p75)
        rows.append({
            "hour_bucket": name[0],
            "units_bucket": str(name[1]),
            "p75_seconds": int(minutes * 60),
            "orders_count": int(len(vals)),
            "base_p75_minutes": int(minutes)
        })

    out = pd.DataFrame(rows)
    if out.empty:
        st.warning("No valid prep_seconds values found after parsing. Check your prep column format.")
        st.write("Preview (first rows):")
        st.dataframe(df.head(10))
        st.stop()

    out = out.sort_values(["hour_bucket", "units_bucket"]).reset_index(drop=True)

    st.success("Done — results below")
    st.dataframe(out)

    csv = out.to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", csv, file_name="prep_p75.csv", mime="text/csv")
