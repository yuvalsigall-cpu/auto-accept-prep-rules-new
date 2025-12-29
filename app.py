# app.py
import streamlit as st
import pandas as pd
import numpy as np
import math
from io import BytesIO

st.set_page_config(layout="wide", page_title="Prep p75 exporter")

UNIT_BINS = [0, 5, 10, 15, 20, 25, 55, 99999]
HOUR_BUCKETS = [
    ("06-12", 6, 11),
    ("12-17", 12, 16),
    ("17-23", 17, 22),
]

def make_unique_columns(cols):
    """Make duplicate column names unique by adding suffixes."""
    seen = {}
    new = []
    for c in cols:
        if c not in seen:
            seen[c] = 0
            new.append(c)
        else:
            seen[c] += 1
            new_name = f"{c}__dup{seen[c]}"
            # ensure not colliding with existing
            while new_name in seen:
                seen[c] += 1
                new_name = f"{c}__dup{seen[c]}"
            seen[new_name] = 0
            new.append(new_name)
    return new

def detect_columns(df):
    """Return (units_col, prep_col, ts_col, optional_hour_col, optional_day_col) - names or None"""
    cols_lower = {c.lower(): c for c in df.columns}
    # exact helpful names
    exact_day = ["time delivered day of week", "time delivered day", "day of week", "day"]
    exact_hour = ["time received hour of the day", "time received hour", "hour of day", "hour"]
    # search for units
    units = None
    prep = None
    ts = None
    hourcol = None
    daycol = None

    # try exacts
    for e in exact_day:
        if e in cols_lower and daycol is None:
            daycol = cols_lower[e]
    for e in exact_hour:
        if e in cols_lower and hourcol is None:
            hourcol = cols_lower[e]

    # generic heuristics
    for low, orig in cols_lower.items():
        if units is None and any(k in low for k in ("units", "items", "qty", "quantity")):
            units = orig
        if prep is None and any(k in low for k in ("prep", "prepare", "preparation", "time to prepare", "prep_seconds", "prep time")):
            prep = orig
        if ts is None and any(k in low for k in ("timestamp", "time", "date", "delivered", "received")):
            # prefer full timestamp-like columns; we'll refine later
            ts = orig

    # if there is a column explicitly named like "time delivered..." prefer it as ts or day
    # already handled some above

    return units, prep, ts, hourcol, daycol

def to_seconds_scalar(x):
    """Convert a single scalar to integer seconds or None. Robust parsing."""
    try:
        if pd.isna(x):
            return None
    except Exception:
        # if pd.isna fails on weird input, fallback
        pass
    # numeric
    if isinstance(x, (int, float, np.integer, np.floating)) and not (isinstance(x, float) and math.isnan(x)):
        try:
            return int(x)
        except Exception:
            return None
    s = str(x).strip()
    if s == "" or s.lower() in ("nan", "none", "null"):
        return None
    # patterns like H:M:S or M:S
    if ":" in s:
        parts = [p for p in s.split(":") if p != ""]
        try:
            parts = [int(p) for p in parts]
        except Exception:
            # maybe "0:02:30.0" etc -> try floats
            try:
                parts = [int(float(p)) for p in parts]
            except Exception:
                return None
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return parts[0]
    # maybe "2m30s", "2h", "150s"
    s_low = s.lower()
    # handle simple patterns: number with unit
    import re
    m = re.match(r"^(\d+\.?\d*)\s*(s|sec|secs|seconds)$", s_low)
    if m:
        return int(float(m.group(1)))
    m = re.match(r"^(\d+\.?\d*)\s*(m|min|mins|minutes)$", s_low)
    if m:
        return int(float(m.group(1)) * 60)
    m = re.match(r"^(\d+\.?\d*)\s*(h|hr|hrs|hours)$", s_low)
    if m:
        return int(float(m.group(1)) * 3600)
    # fallback numeric parse
    try:
        val = float(s)
        return int(val)
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

def compute_p75(df, units_col, prep_col, ts_col=None, hour_col=None, day_col=None, lookback_months=6):
    # make sure columns unique
    df = df.copy()
    df.columns = make_unique_columns(df.columns)

    # if units column missing, set default units=1
    if units_col is None or units_col not in df.columns:
        df["units"] = 1
    else:
        df["units"] = pd.to_numeric(df[units_col], errors="coerce").fillna(0).astype(int)

    # prep_seconds: vectorize parsing
    if prep_col is None or prep_col not in df.columns:
        raise ValueError("Couldn't find a preparation-time column (prep).")
    # apply scalar function safely
    df["prep_seconds"] = df[prep_col].apply(lambda x: to_seconds_scalar(x))

    # timestamps / day / hour
    if day_col and day_col in df.columns:
        df["day_of_week"] = df[day_col].astype(str)
    elif hour_col and hour_col in df.columns:
        # if we have only hour column, set day unknown but take hour
        df["day_of_week"] = "Unknown"
        df["hour"] = pd.to_numeric(df[hour_col], errors="coerce").fillna(0).astype(int)
    elif ts_col and ts_col in df.columns:
        df["ts"] = pd.to_datetime(df[ts_col], errors="coerce")
        # lookback filter
        if lookback_months is not None and lookback_months > 0:
            cutoff = pd.Timestamp.now() - pd.DateOffset(months=lookback_months)
            df = df[df["ts"].isna() | (df["ts"] >= cutoff)]
        # extract day and hour
        if df["ts"].notna().any():
            df["day_of_week"] = df["ts"].dt.day_name()
            df["hour"] = df["ts"].dt.hour
        else:
            df["day_of_week"] = "Unknown"
            df["hour"] = 0
    else:
        # no timestamp info at all
        df["day_of_week"] = "Unknown"
        df["hour"] = 0

    # if hour column exists but not set above
    if "hour" not in df.columns and hour_col and hour_col in df.columns:
        df["hour"] = pd.to_numeric(df[hour_col], errors="coerce").fillna(0).astype(int)

    df["hour_bucket"] = df["hour"].apply(hour_bucket_from_hour)

    # units buckets (labels)
    labels = []
    for i in range(len(UNIT_BINS) - 1):
        labels.append(f"{UNIT_BINS[i]+1}-{UNIT_BINS[i+1] if UNIT_BINS[i+1] < 99999 else '999'}")
    df["units_bucket"] = pd.cut(df["units"], bins=UNIT_BINS, labels=labels, right=True)

    # filter rows with valid prep_seconds
    df_valid = df[df["prep_seconds"].notna()].copy()
    if df_valid.empty:
        return pd.DataFrame([])  # empty result

    # group and compute p75
    rows = []
    group_cols = ["day_of_week", "hour_bucket", "units_bucket"]
    grouped = df_valid.groupby(group_cols, dropna=False)
    for name, g in grouped:
        day, hour_b, units_b = name
        values = g["prep_seconds"].astype(float).values
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
    if out.empty:
        return out
    out = out.sort_values(["day_of_week", "hour_bucket", "units_bucket"])
    return out

# ---------- Streamlit UI ----------
st.title("Auto-Accept Prep Rules — p75 exporter")
st.markdown("העלה קובץ Excel, בדוק זיהוי עמודות ולחץ **Generate CSV**")

uploaded = st.file_uploader("Upload Excel (.xlsx)", type=["xlsx","xls"])
if uploaded is None:
    st.info("העלה כאן את הקובץ ואנחנו נמשיך.")
    st.stop()

# read excel (first sheet)
try:
    df = pd.read_excel(uploaded, engine="openpyxl")
except Exception as e:
    st.error(f"Error reading Excel: {e}")
    st.stop()

# fix duplicate column names
if df.columns.duplicated().any():
    st.warning("נמצאו שמות עמודות כפולים — נעשה אותם ייחודיים אוטומטית.")
    df.columns = make_unique_columns(df.columns)

st.success(f"Loaded uploaded file: {uploaded.name}")

# detect candidates
units_col, prep_col, ts_col, hour_col, day_col = detect_columns(df)

with st.expander("Detected / choose columns (אם לא נכון בחר ידנית)"):
    st.write("Columns in file:", list(df.columns))
    units_col = st.selectbox("Units column (כמות פריטים)", options=[None] + list(df.columns), index=0 if units_col is None else list(df.columns).index(units_col)+1)
    prep_col = st.selectbox("Prep/time column (שעת הכנה)", options=[None] + list(df.columns), index=0 if prep_col is None else list(df.columns).index(prep_col)+1)
    ts_col = st.selectbox("Timestamp column (תאריך/זמן) — optional", options=[None] + list(df.columns), index=0 if ts_col is None else list(df.columns).index(ts_col)+1)
    hour_col = st.selectbox("Hour-only column (שעה) — optional", options=[None] + list(df.columns), index=0 if hour_col is None else (list(df.columns).index(hour_col)+1 if hour_col in df.columns else 0))
    day_col = st.selectbox("Day-of-week column (יום בשבוע) — optional", options=[None] + list(df.columns), index=0 if day_col is None else (list(df.columns).index(day_col)+1 if day_col in df.columns else 0))

lookback = st.number_input("Lookback months (0 = no filtering)", min_value=0, max_value=120, value=6, step=1)
generate = st.button("Generate CSV")

# small preview / quick stats
st.write("Rows total:", len(df))
if prep_col and prep_col in df.columns:
    st.write("Non-empty prep values:", int(df[prep_col].notna().sum()))
else:
    st.write("Prep column not set.")

if generate:
    try:
        res = compute_p75(df, units_col, prep_col, ts_col=ts_col, hour_col=hour_col, day_col=day_col, lookback_months=(lookback if lookback>0 else None))
        if res.empty:
            st.error("אין תוצאות — לא נמצאו שורות עם ערכי זמן תקינים לעיבוד (prep seconds). בדוק את עמודת ה־prep וה־lookback.")
            # show sample of prep column
            if prep_col and prep_col in df.columns:
                st.write("דוגמה לערכי prep (עד 30 לא-ריקים):")
                st.write(df[prep_col].dropna().astype(str).head(30).tolist())
            st.stop()
        st.success(f"נוצרו {len(res)} שורות פלט.")
        st.dataframe(res.head(200))

        # prepare CSV for download
        csv_bytes = res.to_csv(index=False).encode("utf-8")
        st.download_button("Download CSV", data=csv_bytes, file_name="prep_p75_output.csv", mime="text/csv")
    except Exception as e:
        st.exception(f"Error during computation: {e}")
