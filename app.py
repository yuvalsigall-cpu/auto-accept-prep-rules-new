# app.py
import streamlit as st
import pandas as pd
import numpy as np
import math

st.set_page_config(page_title="Auto-Accept Prep Rules — p75 exporter", layout="wide")

# ---------------- helpers ----------------
def make_unique_columns(cols):
    out = []
    seen = {}
    for c in cols:
        if c not in seen:
            seen[c] = 0
            out.append(c)
        else:
            seen[c] += 1
            new = f"{c}.{seen[c]}"
            while new in seen:
                seen[c] += 1
                new = f"{c}.{seen[c]}"
            seen[new] = 0
            out.append(new)
    return out

def normalize_colname(s: str) -> str:
    return str(s).strip().lower()

def detect_columns_auto(df: pd.DataFrame):
    cols = list(df.columns)
    lower = {normalize_colname(c): c for c in cols}
    venue_keys = ["venue", "venue name", "branch", "store", "merchant", "שם סניף"]
    pid_keys = ["purchase id", "order id", "purchaseid", "orderid", "מזהה הזמנה"]
    prep_keys = ["time to prepare", "time to prepare the goods", "prep", "prep time",
                 "time to prepare the goods (hh:mm:ss)", "3) time to prepare the goods", "זמן להכין"]
    day_keys = ["day of week", "time delivered day of week", "day", "יום בשבוע"]
    hour_keys = ["hour", "time received hour of the day", "hour of the day", "time received hour", "שעה"]
    units_keys = ["units", "units sold total", "units sold", "quantity", "qty", "כמות יחידות", "כמות יחידות בהזמנה"]

    def find(keys):
        for k in keys:
            lk = normalize_colname(k)
            if lk in lower:
                return lower[lk]
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

def to_seconds_generic(x):
    if pd.isna(x):
        return None
    if isinstance(x, pd.Timedelta):
        return int(x.total_seconds())
    if isinstance(x, np.timedelta64):
        try:
            return int(pd.to_timedelta(x).total_seconds())
        except:
            pass
    if isinstance(x, (int, np.integer)) and not isinstance(x, bool):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return int(x)
    s = str(x).strip()
    if s == "":
        return None
    # handle "0 days 00:41:21.976000"
    try:
        if "days" in s:
            td = pd.to_timedelta(s)
            return int(td.total_seconds())
    except:
        pass
    if ":" in s:
        parts = [p for p in s.split(":") if p != ""]
        try:
            parts = [int(float(p)) for p in parts]
            if len(parts) == 3:
                return parts[0]*3600 + parts[1]*60 + parts[2]
            if len(parts) == 2:
                return parts[0]*60 + parts[1]
            return parts[0]
        except:
            pass
    try:
        return int(float(s))
    except:
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

def parse_bucket_label(label: str):
    try:
        a,b = str(label).split("-")
        return int(a), int(b)
    except:
        return (0, 99999)

def make_label(low:int, high:int)->str:
    return f"{low}-{high}"

def merge_adjacent_list(rows, minute_threshold=4):
    """
    rows: list of dicts sorted by units_bucket ascending.
    Merge adjacent entries while abs(diff_in_minutes) <= minute_threshold.
    After merge: units_bucket label is combined range, p75_seconds = max of p75_seconds,
    orders_count = sum.
    """
    cur = [r.copy() for r in rows]
    while True:
        merged_any = False
        new_list = []
        i = 0
        while i < len(cur):
            if i < len(cur)-1:
                left = cur[i]
                right = cur[i+1]
                left_min = left["p75_seconds"]//60
                right_min = right["p75_seconds"]//60
                if abs(right_min - left_min) <= minute_threshold:
                    l_low, l_high = parse_bucket_label(str(left["units_bucket"]))
                    r_low, r_high = parse_bucket_label(str(right["units_bucket"]))
                    new_label = make_label(l_low, r_high)
                    merged = {
                        "units_bucket": new_label,
                        "p75_seconds": int(max(left["p75_seconds"], right["p75_seconds"])),
                        "orders_count": int(left.get("orders_count",0) + right.get("orders_count",0)),
                    }
                    # copy other keys if present (day/hour)
                    for k in left:
                        if k not in merged:
                            merged[k] = left[k]
                    for k in right:
                        if k not in merged:
                            merged[k] = right[k]
                    new_list.append(merged)
                    i += 2
                    merged_any = True
                    continue
            new_list.append(cur[i])
            i += 1
        cur = new_list
        if not merged_any:
            break
    return cur

# ---------------- UI ----------------
st.title("Auto-Accept Prep Rules — p75 exporter")
st.caption("העלה Excel, בחר/וודא עמודות ולחץ Generate CSV")

uploaded = st.file_uploader("Upload Excel (.xlsx)", type=["xlsx","xls"])
if uploaded is None:
    st.info("אנא העלה קובץ Excel. דוגמאות כותרות: 'Time Delivered Day of Week', 'Time Received Hour of the Day', 'Units Sold Total', '3) Time to prepare the goods (hh:mm:ss)'.")
    st.stop()

try:
    df = pd.read_excel(uploaded, engine="openpyxl")
except Exception as e:
    st.error(f"Error reading Excel: {e}")
    st.stop()

orig_cols = list(df.columns)
unique_cols = make_unique_columns([str(c) for c in orig_cols])
df.columns = unique_cols

st.success(f"Loaded uploaded file: {getattr(uploaded,'name','uploaded file')}")
st.write(f"Rows total: **{len(df)}**")

venue_col_auto, pid_col_auto, prep_col_auto, day_col_auto, hour_col_auto, units_col_auto = detect_columns_auto(df)

with st.expander("Detected / choose columns (אם לא נכון - בחר ידנית)"):
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

# create 5-unit bins up to a large number; ensure labels count = bins-1
MAX_UNITS = 1000
step = 5
# edges: 0,5,10,...,1000, 99999  (last edge large)
edges = list(range(0, MAX_UNITS+step, step))
if edges[-1] < 99999:
    edges.append(99999)
# labels must be len(edges)-1
labels = []
for i in range(len(edges)-1):
    low = edges[i] + 1
    high = edges[i+1]
    if high >= 99999:
        labels.append(f"{low}-999")
    else:
        labels.append(f"{low}-{high}")

# sanity check
if len(labels) != len(edges)-1:
    st.error("Internal error building bins/labels (length mismatch).")
    st.stop()

if st.button("Generate CSV"):
    try:
        working = df.copy()

        # compute prep_seconds safely
        working["prep_seconds"] = [ to_seconds_generic(x) for x in working[prep_col].tolist() ]
        non_empty = int(working["prep_seconds"].notna().sum())
        st.write(f"Non-empty prep values: **{non_empty}**")

        if non_empty == 0:
            st.error("אין תוצאות — לא נמצאו ערכי prep תקינים בעמודה שנבחרה.")
            st.stop()

        # optional lookback filter
        if lookback_months > 0 and day_col in working.columns:
            # if there is a timestamp column (we used day_col as day name only) skip unless a timestamp exists
            # We don't have a real timestamp column here; skip lookback if not provided
            pass

        # day_of_week
        working["day_of_week"] = working[day_col].astype(str).fillna("Unknown")

        # hour
        if np.issubdtype(working[hour_col].dtype, np.datetime64):
            working["hour"] = pd.to_datetime(working[hour_col]).dt.hour
        else:
            working["hour"] = pd.to_numeric(working[hour_col], errors="coerce").fillna(0).astype(int)
        working["hour_bucket"] = working["hour"].apply(hour_bucket_from_hour)

        # units numeric
        working["units"] = pd.to_numeric(working[units_col], errors="coerce").fillna(0).astype(int)

        # units buckets categorical
        # NOTE: use the edges list (which ends with 99999)
        working["units_bucket"] = pd.cut(working["units"], bins=edges, labels=labels, right=True)
        # ensure ordered categorical
        working["units_bucket"] = working["units_bucket"].astype(pd.CategoricalDtype(categories=labels, ordered=True))

        df_valid = working[working["prep_seconds"].notna()].copy()
        if df_valid.empty:
            st.error("אין שורות עם prep תקין לאחר סינון.")
            st.stop()

        # aggregate base p75 per group
        agg_rows = []
        group_cols = ["day_of_week", "hour_bucket", "units_bucket"]
        grouped = df_valid.groupby(group_cols)
        for (day, hb, ub), g in grouped:
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

        if len(agg_rows) == 0:
            st.warning("לא נוצרו שורות אגggregציה.")
            st.stop()

        base_df = pd.DataFrame(agg_rows)
        base_df["units_bucket"] = pd.Categorical(base_df["units_bucket"], categories=labels, ordered=True)
        base_df = base_df.sort_values(["day_of_week", "hour_bucket", "units_bucket"]).reset_index(drop=True)

        # merge adjacent buckets per (day,hour) by threshold <=4 minutes
        merged_rows_all = []
        groups = base_df.groupby(["day_of_week", "hour_bucket"])
        for (day, hb), sub in groups:
            sub_sorted = sub.sort_values("units_bucket").reset_index(drop=True)
            simple = []
            for _, r in sub_sorted.iterrows():
                simple.append({
                    "day_of_week": day,
                    "hour_bucket": hb,
                    "units_bucket": str(r["units_bucket"]),
                    "p75_seconds": int(r["p75_seconds"]),
                    "orders_count": int(r["orders_count"])
                })
            merged_simple = merge_adjacent_list(simple, minute_threshold=4)
            for item in merged_simple:
                merged_rows_all.append({
                    "day_of_week": item["day_of_week"],
                    "hour_bucket": item["hour_bucket"],
                    "units_bucket": item["units_bucket"],
                    "p75_seconds": int(item["p75_seconds"]),
                    "orders_count": int(item.get("orders_count",0)),
                    "base_p75_minutes": int(item["p75_seconds"]//60)
                })

        out_df = pd.DataFrame(merged_rows_all)
        if out_df.empty:
            st.warning("לא נוצרו תוצאות לאחר מיזוגים.")
            st.stop()

        # sort days in natural order if english names present
        day_order = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday","Unknown"]
        if set(out_df["day_of_week"].unique()).issubset(set(day_order)):
            out_df["day_of_week"] = pd.Categorical(out_df["day_of_week"], categories=day_order, ordered=True)

        # order by units lower bound
        def lower_bound(label):
            try:
                return int(str(label).split("-")[0])
            except:
                return 999999
        out_df["units_lower"] = out_df["units_bucket"].apply(lambda x: lower_bound(str(x)))
        out_df = out_df.sort_values(["day_of_week","hour_bucket","units_lower"]).drop(columns=["units_lower"]).reset_index(drop=True)

        st.success("הפקה בוצעה — להורדה למטה")
        st.dataframe(out_df.head(300))

        csv_bytes = out_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download CSV", data=csv_bytes, file_name="prep_p75_output_merged.csv", mime="text/csv")

    except Exception as e:
        st.exception(f"str: Error during computation: {e}")
