# app.py
import streamlit as st
import pandas as pd
import numpy as np
import io
import math
from typing import Optional, Tuple, List

st.set_page_config(page_title="Auto-Accept Prep Rules — p75 exporter", layout="wide")


UNIT_BINS = [0, 5, 10, 15, 20, 25, 55, 99999]
HOUR_BUCKETS = [
    ("06-12", 6, 11),
    ("12-17", 12, 16),
    ("17-23", 17, 22),
]


# ---- utilities ----
def make_unique_columns(cols: List[str]) -> List[str]:
    """Ensure column names are unique by appending suffixes if duplicated."""
    counts = {}
    out = []
    for c in cols:
        base = c.strip()
        if base in counts:
            counts[base] += 1
            out.append(f"{base}__dup{counts[base]}")
        else:
            counts[base] = 0
            out.append(base)
    return out


def detect_columns(df: pd.DataFrame) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Try to detect: venue_col, prep_col, day_col, hour_col, units_col
    Return names (or None).
    """
    cols_lower = {c.lower(): c for c in df.columns}
    # candidate name lists (extend if needed)
    CANDIDATES = {
        "venue": ["venue", "venue name", "שם סניף", "merchant", "merchant name"],
        "prep": ["time to prepare", "time to prepare the goods", "3) time to prepare the goods (hh:mm:ss)", "time to prepare", "prep", "preparation", "זמן להכין"],
        "day": ["time delivered day of week", "day of week", "time delivered", "יום בשבוע", "day"],
        "hour": ["time received hour of the day", "hour of the day", "time received", "שעה", "hour"],
        "units": ["units sold total", "units", "כמות", "quantity", "qty", "units sold"],
    }

    def find_one(cands):
        for cand in cands:
            k = cand.lower()
            if k in cols_lower:
                return cols_lower[k]
        # try substring match
        for k_lower, orig in cols_lower.items():
            for cand in cands:
                if cand.lower() in k_lower:
                    return orig
        return None

    venue_col = find_one(CANDIDATES["venue"])
    prep_col = find_one(CANDIDATES["prep"])
    day_col = find_one(CANDIDATES["day"])
    hour_col = find_one(CANDIDATES["hour"])
    units_col = find_one(CANDIDATES["units"])

    return venue_col, prep_col, day_col, hour_col, units_col


def to_seconds_scalar(x) -> Optional[int]:
    """Convert scalar/pandas value to integer seconds or None."""
    # handle pandas Timedelta
    if isinstance(x, pd.Timedelta):
        return int(x.total_seconds())

    # handle pandas Timestamp durations (rare) or numpy types
    try:
        if pd.isna(x):
            return None
    except Exception:
        pass

    if isinstance(x, (int, np.integer, float, np.floating)):
        try:
            if math.isnan(x):
                return None
            return int(float(x))
        except Exception:
            return None

    s = str(x).strip()
    if s == "" or s.lower() in ("nan", "none", "null"):
        return None

    # if string like '0 days 00:41:20.976000' — try extract hh:mm:ss
    if "days" in s and ":" in s:
        # common pandas Timedelta string
        try:
            # split on space and take the time part
            parts = s.split()
            for p in parts:
                if ":" in p:
                    s = p
                    break
        except Exception:
            pass

    # formats like HH:MM:SS or MM:SS
    if ":" in s:
        parts = [p for p in s.split(":") if p != ""]
        try:
            parts_i = [int(float(p)) for p in parts]
        except Exception:
            return None
        if len(parts_i) == 3:
            return parts_i[0] * 3600 + parts_i[1] * 60 + parts_i[2]
        if len(parts_i) == 2:
            return parts_i[0] * 60 + parts_i[1]
        return parts_i[0]

    # numeric string
    try:
        return int(float(s))
    except Exception:
        return None


def hour_bucket_from_hour(h: int) -> str:
    for name, start, end in HOUR_BUCKETS:
        try:
            if start <= int(h) <= end:
                return name
        except Exception:
            continue
    return "other"


def round_half_up_minutes_to_seconds(x_seconds: float) -> int:
    minutes = x_seconds / 60.0
    return int(math.floor(minutes + 0.5)) * 60


# ---- UI ----
st.title("Auto-Accept Prep Rules — p75 exporter")
st.caption("העלה Excel, בדוק זיהוי עמודות ולחץ Generate CSV")

uploaded = st.file_uploader("Upload Excel (.xlsx)", type=["xlsx", "xls"], accept_multiple_files=False)
lookback_months = st.number_input("Lookback months (0 = no filtering)", min_value=0, max_value=120, value=6, step=1)

if uploaded is None:
    st.info("העלה קובץ Excel כדי להתחיל")
    st.stop()

# read file
try:
    df_raw = pd.read_excel(uploaded, engine="openpyxl")
except Exception as e:
    st.error(f"Error reading Excel: {e}")
    st.stop()

# clean column names and ensure uniqueness
orig_cols = [str(c) for c in df_raw.columns]
unique_cols = make_unique_columns(orig_cols)
df_raw.columns = unique_cols

st.success(f"Loaded uploaded file: {getattr(uploaded, 'name', 'uploaded file')}")
st.write(f"Rows total: {len(df_raw)}")
# detect columns
venue_col, prep_col, day_col, hour_col, units_col = detect_columns(df_raw)

with st.expander("Detected / choose columns (אם לא נכון בחר ידנית)"):
    st.write("Detected (may be None):")
    st.write(f"Venue: `{venue_col}`")
    st.write(f"Prep column: `{prep_col}`")
    st.write(f"Day column: `{day_col}`")
    st.write(f"Hour column: `{hour_col}`")
    st.write(f"Units column: `{units_col}`")

    # allow manual override
    venue_col = st.selectbox("Venue column", options=[None] + list(df_raw.columns), index=0 if venue_col is None else list(df_raw.columns).index(venue_col)+1)
    prep_col = st.selectbox("Prep column", options=[None] + list(df_raw.columns), index=0 if prep_col is None else list(df_raw.columns).index(prep_col)+1)
    day_col = st.selectbox("Day-of-week column", options=[None] + list(df_raw.columns), index=0 if day_col is None else list(df_raw.columns).index(day_col)+1)
    hour_col = st.selectbox("Hour column", options=[None] + list(df_raw.columns), index=0 if hour_col is None else list(df_raw.columns).index(hour_col)+1)
    units_col = st.selectbox("Units column", options=[None] + list(df_raw.columns), index=0 if units_col is None else list(df_raw.columns).index(units_col)+1)

# basic checks
if prep_col is None:
    st.error("חסר עמודת prep (time to prepare). בחר אותה בהרחבה למעלה.")
    st.stop()
if units_col is None:
    st.warning("לא זוהתה עמודת units — נחזור לערכים ברירת מחדל (0) אם לא תבחר.")
# test non-empty prep values
non_empty_prep = df_raw[prep_col].notna().sum()
st.write(f"Non-empty prep values: {non_empty_prep}")

# show sample of prep values
with st.expander("דוגמא ערכי prep (עד 50):"):
    st.write(df_raw[prep_col].head(50).tolist())

# ---- compute ----
if st.button("Generate CSV"):
    df = df_raw.copy()

    # ensure the selected columns exist
    for c in [prep_col, units_col, day_col, hour_col]:
        if c is not None and c not in df.columns:
            st.error(f"Column `{c}` not found in dataframe.")
            st.stop()

    # compute prep_seconds robustly
    try:
        prep_seconds_list = [to_seconds_scalar(x) for x in df[prep_col].tolist()]
    except Exception as e:
        st.error(f"Error converting prep values: {e}")
        st.stop()

    df["prep_seconds"] = prep_seconds_list
    non_empty_after = df["prep_seconds"].dropna().shape[0]
    st.write(f"Non-empty converted prep_seconds: {non_empty_after}")

    if non_empty_after == 0:
        st.error("אין תוצאות — בדוק את עמודת ה-prep (לא נמצאו ערכי זמן תקינים לעיבוד).")
        st.stop()

    # units
    if units_col and units_col in df.columns:
        df["units"] = pd.to_numeric(df[units_col], errors="coerce").fillna(0).astype(int)
    else:
        df["units"] = 0

    # day of week
    if day_col and day_col in df.columns:
        # use as string day names if present
        df["day_of_week"] = df[day_col].astype(str).fillna("Unknown")
    else:
        # try infer from timestamp columns (none in this UI) -> fallback
        df["day_of_week"] = "Unknown"

    # hour
    if hour_col and hour_col in df.columns:
        # if numeric
        def parse_hour(val):
            try:
                return int(float(val))
            except Exception:
                # if string like '17:23' extract part before colon
                s = str(val)
                if ":" in s:
                    try:
                        return int(s.split(":")[0])
                    except:
                        return 0
                return 0
        df["hour"] = df[hour_col].apply(parse_hour)
    else:
        df["hour"] = 0

    df["hour_bucket"] = df["hour"].apply(hour_bucket_from_hour)

    # units buckets
    labels = []
    for i in range(len(UNIT_BINS) - 1):
        labels.append(f"{UNIT_BINS[i] + 1}-{UNIT_BINS[i + 1] if UNIT_BINS[i + 1] < 99999 else '999'}")
    df["units_bucket"] = pd.cut(df["units"], bins=UNIT_BINS, labels=labels, right=True)

    # lookback filtering on provided date column — not available here; if user has 'ts' column, add logic
    if lookback_months > 0:
        st.info("Lookback months is set, but no timestamp column is used for filtering in this simple UI. (If you have a timestamp column, we can add filtering.)")

    # group and compute p75
    rows = []
    group_cols = ["day_of_week", "hour_bucket", "units_bucket"]
    grouped = df.groupby(group_cols, dropna=False)

    for name, g in grouped:
        vals = g["prep_seconds"].dropna().astype(float).values
        if vals.size == 0:
            continue
        p75 = float(np.percentile(vals, 75))
        p75_rounded = round_half_up_minutes_to_seconds(p75)
        rows.append({
            "day_of_week": name[0],
            "hour_bucket": name[1],
            "units_bucket": str(name[2]),
            "p75_seconds": int(p75_rounded),
            "orders_count": int(vals.size),
            "base_p75_minutes": int(p75_rounded // 60)
        })

    out_df = pd.DataFrame(rows)
    if out_df.empty:
        st.error("לא נוצרו תוצאות אחרי האגגרגציה.")
        st.stop()

    out_df = out_df.sort_values(["day_of_week", "hour_bucket", "units_bucket"])
    st.success(f"Generated {len(out_df)} aggregated rows. Preview:")
    st.dataframe(out_df.head(200))

    # file download
    csv_bytes = out_df.to_csv(index=False).encode("utf-8")
    st.download_button("Download CSV", data=csv_bytes, file_name="prep_p75_output.csv", mime="text/csv")
