# app.py
import streamlit as st
import pandas as pd
import numpy as np
import math
from io import BytesIO

st.set_page_config(page_title="Auto-Accept Prep Rules", layout="wide")
st.title("Auto-Accept Prep Rules")
st.write("העלה קובץ Excel ותקבל CSV עם זמני הכנה (P75)")

# ----------------- Config -----------------
UNIT_BINS = [0, 5, 10, 15, 20, 25, 55, 99999]
HOUR_BUCKETS = [
    ("06-12", 6, 11),
    ("12-17", 12, 16),
    ("17-23", 17, 22),
]

# ----------------- Helpers -----------------
def detect_columns(df):
    cols = {str(c).lower(): c for c in df.columns}
    units_col = None
    prep_col = None
    ts_col = None

    for k, v in cols.items():
        if units_col is None and any(x in k for x in ("units", "items", "qty")):
            units_col = v
        if prep_col is None and any(x in k for x in ("prep", "prepare", "time")):
            prep_col = v
        if ts_col is None and any(x in k for x in ("date", "time", "timestamp", "created")):
            ts_col = v

    return units_col, prep_col, ts_col

def _is_non_scalar(obj):
    # treat strings/bytes as scalar
    if isinstance(obj, (str, bytes)):
        return False
    # pandas objects and numpy arrays etc.
    return hasattr(obj, "__len__") and not isinstance(obj, (str, bytes))

def to_seconds(x):
    """
    Robust conversion of many formats to integer seconds.
    If x is non-scalar (Series/ndarray), try to extract scalar via .item().
    If cannot extract, return None.
    """
    # handle non-scalar containers gracefully
    try:
        if _is_non_scalar(x):
            # try to extract scalar (e.g., numpy scalar inside an array or single-element Series)
            if hasattr(x, "item"):
                try:
                    x = x.item()
                except Exception:
                    # item() failed — give up
                    return None
            else:
                # list/tuple/Index etc. — if length 1, use first element
                try:
                    if len(x) == 1:
                        x = x[0]
                    else:
                        return None
                except Exception:
                    return None
    except Exception:
        return None

    # now x should be scalar (or string)
    if pd.isna(x):
        return None

    if isinstance(x, (int, float, np.integer, np.floating)):
        try:
            return int(x)
        except Exception:
            return None

    s = str(x).strip()
    if s == "":
        return None

    # hh:mm:ss or mm:ss
    if ":" in s:
        try:
            parts = [int(p) for p in s.split(":")]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            return parts[0]
        except Exception:
            # fall through to numeric parse
            pass

    # numeric string
    try:
        return int(float(s))
    except Exception:
        return None

def hour_bucket(h):
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

# ----------------- UI -----------------
uploaded = st.file_uploader("Upload Excel (.xlsx)", type=["xlsx"])
venue = st.text_input("Venue (אופציונלי)")
generate = st.button("Generate CSV")

if generate:
    if uploaded is None:
        st.error("לא הועלה קובץ")
    else:
        try:
            df = pd.read_excel(BytesIO(uploaded.read()), engine="openpyxl")
        except Exception as e:
            st.error("לא הצלחנו לקרוא את הקובץ (בדוק שהוא XLSX תקין)")
            st.exception(e)
            st.stop()

        units_col, prep_col, ts_col = detect_columns(df)
        if units_col is None or prep_col is None:
            st.error("לא נמצאו עמודות 'units' או 'prep time' בקובץ (בדוק שמות העמודות).")
            st.write("עמודות שבקובץ:", list(df.columns))
            st.stop()

        # keep relevant cols
        cols_keep = [units_col, prep_col]
        if ts_col:
            cols_keep.append(ts_col)
        df = df[cols_keep].copy()

        # normalize units
        df["units"] = pd.to_numeric(df[units_col], errors="coerce").fillna(0).astype(int)

        # compute prep_seconds robustly
        # We'll apply to each element; function handles Series/arrays too.
        try:
            df["prep_seconds"] = df[prep_col].apply(to_seconds)
        except Exception as e:
            st.error("שגיאה בחישוב prep_seconds — פרטי השגיאה למטה")
            st.exception(e)
            st.stop()

        # optional timestamp handling
        if ts_col:
            df["ts"] = pd.to_datetime(df[ts_col], errors="coerce")
            df["day_of_week"] = df["ts"].dt.day_name().fillna("Unknown")
            df["hour"] = df["ts"].dt.hour.fillna(0).astype(int)
        else:
            df["day_of_week"] = "Unknown"
            df["hour"] = 0

        df["hour_bucket"] = df["hour"].apply(hour_bucket)

        labels = [
            f"{UNIT_BINS[i]+1}-{UNIT_BINS[i+1] if UNIT_BINS[i+1] < 99999 else '999'}"
            for i in range(len(UNIT_BINS)-1)
        ]
        df["units_bucket"] = pd.cut(df["units"], bins=UNIT_BINS, labels=labels)

        # aggregate groups
        rows = []
        group_cols = ["day_of_week", "hour_bucket", "units_bucket"]

        for (day, hour_b, units_b), g in df.groupby(group_cols):
            vals = g["prep_seconds"].dropna().astype(float)
            if len(vals) == 0:
                continue
            p75 = float(np.percentile(vals, 75))
            p75_rounded = round_half_up(p75 / 60.0) * 60
            rows.append({
                "day_of_week": day,
                "hour_bucket": hour_b,
                "units_bucket": str(units_b),
                "p75_seconds": int(p75_rounded),
                "orders_count": int(len(vals)),
                "base_p75_minutes": int(p75_rounded // 60),
            })

        out = pd.DataFrame(rows)
        if out.empty:
            st.warning("לא נמצאו ערכים תקפים אחרי המרה (בדוק את עמודת ה-prep/time).")
        else:
            out = out.sort_values(["day_of_week", "hour_bucket", "units_bucket"])
            st.success("החישוב הושלם")
            st.dataframe(out)

            csv = out.to_csv(index=False).encode("utf-8")
            st.download_button("Download CSV", csv, file_name="prep_p75.csv", mime="text/csv")
