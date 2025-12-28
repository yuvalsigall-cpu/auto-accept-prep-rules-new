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


def to_seconds(x):
    if pd.isna(x):
        return None

    if isinstance(x, (int, float)):
        return int(x)

    s = str(x)
    if ":" in s:
        parts = s.split(":")
        parts = [int(p) for p in parts]
        if len(parts) == 3:
            return parts[0]*3600 + parts[1]*60 + parts[2]
        if len(parts) == 2:
            return parts[0]*60 + parts[1]

    try:
        return int(float(s))
    except:
        return None


def hour_bucket(h):
    try:
        h = int(h)
    except:
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

            units_col, prep_col, ts_col = detect_columns(df)
            if units_col is None or prep_col is None:
                st.error("לא נמצאו עמודות units / prep time בקובץ")
                st.stop()

            df = df[[units_col, prep_col] + ([ts_col] if ts_col else [])].copy()
            df["units"] = pd.to_numeric(df[units_col], errors="coerce").fillna(0).astype(int)
            df["prep_seconds"] = df[prep_col].apply(to_seconds)

            if ts_col:
                df["ts"] = pd.to_datetime(df[ts_col], errors="coerce")
                df["day_of_week"] = df["ts"].dt.day_name()
                df["hour"] = df["ts"].dt.hour
            else:
                df["day_of_week"] = "Unknown"
                df["hour"] = 0

            df["hour_bucket"] = df["hour"].apply(hour_bucket)

            labels = [
                f"{UNIT_BINS[i]+1}-{UNIT_BINS[i+1] if UNIT_BINS[i+1] < 99999 else '999'}"
                for i in range(len(UNIT_BINS)-1)
            ]
            df["units_bucket"] = pd.cut(df["units"], bins=UNIT_BINS, labels=labels)

            rows = []
            for (day, hour_b, units_b), g in df.groupby(
                ["day_of_week", "hour_bucket", "units_bucket"]
            ):
                vals = g["prep_seconds"].dropna()
                if len(vals) == 0:
                    continue

                p75 = np.percentile(vals, 75)
                p75_sec = round_half_up(p75 / 60) * 60

                rows.append({
                    "day_of_week": day,
                    "hour_bucket": hour_b,
                    "units_bucket": str(units_b),
                    "p75_seconds": int(p75_sec),
                    "orders_count": len(vals),
                    "base_p75_minutes": int(p75_sec / 60),
                })

            out = pd.DataFrame(rows)
            st.success("חישוב הושלם")
            st.dataframe(out)

            csv = out.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download CSV",
                csv,
                file_name="prep_p75.csv",
                mime="text/csv"
            )

        except Exception as e:
            st.error("שגיאה בהרצה")
            st.exception(e)
