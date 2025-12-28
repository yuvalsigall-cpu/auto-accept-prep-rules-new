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

def to_seconds_factory(get_pandas_fn, get_numpy_fn=None):
    pd = get_pandas_fn()
    np = None
    if get_numpy_fn is not None:
        try:
            np = get_numpy_fn()
        except Exception:
            np = None

    def to_seconds(x):
        # Handle Series / ndarray / list-like by taking first non-null element if possible
        try:
            if hasattr(x, "dtype") or isinstance(x, (list, tuple)):
                try:
                    ser = pd.Series(x)
                except Exception:
                    ser = None
                if ser is not None:
                    if ser.isna().all():
                        return None
                    try:
                        first = ser[~ser.isna()].iloc[0]
                        x = first
                    except Exception:
                        pass
        except Exception:
            pass

        # scalar handling
        try:
            if pd.isna(x):
                return
