# app.py
import streamlit as st
import importlib
import time
import math
import traceback
from io import BytesIO

# -------- Lazy imports with retries --------
def _try_import(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        return None

def get_pandas(retries=40, delay=1.0):
    backoff = delay
    for i in range(retries):
        m = _try_import("pandas")
        if m is not None:
            return m
        time.sleep(backoff)
        backoff = min(backoff * 1.2, 5.0)
    raise ModuleNotFoundError("pandas not available after retries")

def get_numpy(retries=40, delay=1.0):
    backoff = delay
    for i in range(retries):
        m = _try_import("numpy")
        if m is not None:
            return m
        time.sleep(backoff)
        backoff = min(backoff * 1.2, 5.0)
    raise ModuleNotFoundError("numpy not available after retries")

# -------- Constants --------
UNIT_BINS = [0, 5, 10, 15, 20, 25, 55, 99999]
HOUR_BUCKETS = [
    ("06-12", 6, 11),
    ("12-17", 12, 16),
    ("17-23", 17, 22),
]

# -------- Helpers --------
def detect_columns(df):
    # returns (units_col, prep_col, ts_col)
    cols = {str(c).lower(): c for c in df.columns}
    units_col = None
    prep_col = None
    ts_col = None
    for k, v in cols.items():
        if units_col is None and any(x in k for x in ("units", "items", "qty", "quantity")):
            units_col = v
        if prep_col is None and any(x in k for x in ("prep", "prepare", "preparation", "time to prepare", "time")):
            prep_col = v
        if ts_col is None and any(x in k for x in ("timestamp", "time", "date", "created", "order_at")):
            ts_col = v
    return units_col, prep_col, ts_col

def to_seconds_factory(get_pandas_fn):
    # returns a to_seconds function that uses pandas checks dynamically
    pd = get_pandas_fn()
    def to_seconds(x):
        if pd.isna(x):
            return None
        if isinstance(x, (int, float)):
            return int(x)
        s = str(x).strip()
        if ":" in s:
            parts = [p for p in s.split(":") if p != ""]
            try:
                parts = [int(p) for p in parts]
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
    return to_seconds

def hour_bucket_from_hour(h):
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

# -------- Core computation --------
def compute_p75_from_dataframe(df_input, lookback_months=6, venue=None, get_pandas_fn=get_pandas, get_numpy_fn=get_numpy):
    """
    df_input: pandas.DataFrame (raw uploaded excel)
    returns: pandas.DataFrame aggregated with columns:
      day_of_week, hour_bucket, units_bucket, p75_seconds, orders_count, base_p75_minutes
    """
    pd = get_pandas_fn()
    np = get_numpy_fn()

    units_col, prep_col, ts_col = detect_columns(df_input)
    if units_col is None or prep_col is None:
        raise ValueError(f"Couldn't detect units or prep columns. Columns: {list(df_input.columns)}")

    # keep relevant columns (if ts_col is None we avoid adding it)
    keep_cols = [units_col, prep_col] + ([ts_col] if ts_col is not None else [])
    df = df_input.loc[:, keep_cols].copy()

    # numeric units
    df["units"] = pd.to_numeric(df[units_col], errors="coerce").fillna(0).astype(int)

    # prep_seconds
    to_seconds = to_seconds_factory(get_pandas_fn)
    df["prep_seconds"] = df[prep_col].apply(to_seconds)

    # optional timestamp handling
    if ts_col is not None and ts_col in df.columns:
        df["ts"] = pd.to_datetime(df[ts_col], errors="coerce")
        cutoff = pd.Timestamp.now() - pd.DateOffset(months=lookback_months)
        # keep rows with missing ts OR ts within lookback
        df = df[df["ts"].isna() | (df["ts"] >= cutoff)]

    # optional venue filter (explicit check)
    if venue is not None and str(venue).strip() != "":
        # check if there is a column that might be venue; if not, assume user filtered pre-upload
        # (if you have a specific column name you can change this logic to use that column)
        possible_cols = [c for c in df.columns if "venue" in str(c).lower() or "merchant" in str(c).lower() or "store" in str(c).lower()]
        if possible_cols:
            col = possible_cols[0]
            df = df[df[col].astype(str).str.contains(str(venue), case=False, na=False)]

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
        labels.append(f"{UNIT_BINS[i]+1}-{UNIT_BINS[i+1] if UNIT_BINS[i+1] < 99999 else '999'}")
    df["units_bucket"] = pd.cut(df["units"], bins=UNIT_BINS, labels=labels, right=True)

    # aggregate groups (explicit groupby columns)
    group_cols = ["day_of_week", "hour_bucket", "units_bucket"]
    rows = []
    # use dropna=False to keep groups that may include NaN labels if any
    grouped = df.groupby(group_cols, dropna=False)
    for name, g in grouped:
        # name is (day, hour_bucket, units_bucket)
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

    out = pd.DataFrame(rows) if len(rows) > 0 else pd.DataFrame(columns=["day_of_week","hour_bucket","units_bucket","p75_seconds","orders_count","base_p75_minutes"])
    out = out.sort_values(["day_of_week","hour_bucket","units_bucket"])
    return out

# -------- Streamlit UI --------
st.set_page_config(page_title="Auto-Accept Prep Rules", layout="wide")
st.title("Auto-Accept Prep Rules")
st.write("מוכן לעלאת Excel (xlsx).")

# upload
uploaded = st.file_uploader("Upload Excel (xlsx)", type=["xlsx"])
venue_input = st.text_input("Venue (אופציונלי)")

# action
if uploaded is not None:
    st.info("קובץ נקלט. הקש Generate כדי להתחיל.")
    if st.button("Generate CSV"):
        # show loading spinner and robust exception output
        try:
            with st.spinner("טוען ספריות ובודק נתונים..."):
                # Try to obtain pandas/numpy (will wait if the environment still installs)
                pd = get_pandas()
                np = get_numpy()
            # read excel into dataframe
            try:
                # read bytes with pandas
                df_input = pd.read_excel(BytesIO(uploaded.read()), engine="openpyxl")
            except Exception as e_read:
                st.error("שגיאה בקריאת הקובץ כ־Excel — נסה לשמור כ־XLSX תקין.")
                st.exception(e_read)
                raise

            with st.spinner("מחשב p75..."):
                out_df = compute_p75_from_dataframe(df_input, lookback_months=6, venue=venue_input, get_pandas_fn=get_pandas, get_numpy_fn=get_numpy)

            if out_df.empty:
                st.warning("לא נמצאו נתונים לתוצאה (לא נמצאו קבוצות עם prep time תקף). בדוק את העמודות בקובץ.")
            else:
                st.success("הושלם! הורד CSV למטה.")
                csv_bytes = out_df.to_csv(index=False).encode("utf-8")
                st.download_button("הורד CSV של p75", csv_bytes, file_name="prep_p75_output.csv", mime="text/csv")
                st.dataframe(out_df)
        except Exception as e:
            tb = traceback.format_exc()
            st.error("שגיאה בחישוב — פירוט מלא למטה (העתק ושלח לי את הטקסט הזה אם צריך עזרה).")
            st.code(tb)
            # also log to server console
            print(tb)
else:
    st.info("העלאת קובץ מוצגת כאן — גרור ושחרר XLSX או לחץ Browse files.")
