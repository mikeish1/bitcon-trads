"""
Unit tests for the ETF bar data-quality validator (pure, offline).

Covers: clean passthrough, dropped structural corruption (NaN / non-positive /
high<low / duplicate timestamps / bad timestamps), flagged-but-kept soft anomalies
(calendar gaps, OHLC inconsistency), and the empty / missing-column / too-short
fatal cases.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.etf.data_quality import validate_bars


def _bars(closes, start="2024-01-01", freq="D"):
    ts = pd.date_range(start=start, periods=len(closes), freq=freq, tz="UTC")
    return pd.DataFrame({
        "timestamp": ts, "open": closes,
        "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
        "close": closes, "volume": [1_000.0] * len(closes),
    })


def test_clean_frame_passes_with_no_issues():
    df = _bars([100.0, 101.0, 102.0, 103.0])
    r = validate_bars(df)
    assert r.ok and r.issues == [] and r.dropped == 0
    assert len(r.clean) == 4
    assert list(r.clean["timestamp"]) == list(df["timestamp"])


def test_empty_and_none_are_fatal():
    for bad in (None, pd.DataFrame()):
        r = validate_bars(bad)
        assert not r.ok and r.issues and len(r.clean) == 0


def test_missing_columns_fatal():
    df = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01"], utc=True), "close": [1.0]})
    r = validate_bars(df)
    assert not r.ok and any("missing columns" in i for i in r.issues)


def test_nan_and_nonpositive_prices_dropped():
    df = _bars([100.0, 101.0, 102.0, 103.0, 104.0])
    df.loc[1, "close"] = np.nan          # NaN row -> dropped
    df.loc[3, "open"] = 0.0              # non-positive -> dropped
    r = validate_bars(df)
    assert r.dropped == 2 and len(r.clean) == 3
    assert any("NaN price" in i for i in r.issues)
    assert any("non-positive" in i for i in r.issues)


def test_high_below_low_dropped():
    df = _bars([100.0, 101.0, 102.0])
    df.loc[1, "high"] = 50.0             # high < low -> structurally impossible
    r = validate_bars(df)
    assert r.dropped == 1 and len(r.clean) == 2
    assert any("high < low" in i for i in r.issues)


def test_duplicate_timestamps_collapsed_keep_last():
    df = _bars([100.0, 101.0, 102.0])
    df.loc[2, "timestamp"] = df.loc[1, "timestamp"]   # dup of row 1
    df.loc[2, "close"] = 999.0
    r = validate_bars(df)
    assert any("duplicate timestamp" in i for i in r.issues)
    # the kept duplicate is the LAST one (close 999 at that timestamp)
    dup_ts = df.loc[1, "timestamp"]
    kept = r.clean[r.clean["timestamp"] == dup_ts]
    assert len(kept) == 1 and kept.iloc[0]["close"] == 999.0


def test_calendar_gap_flagged_but_kept():
    a = _bars([100.0, 101.0, 102.0], start="2024-01-01")
    b = _bars([110.0, 111.0], start="2024-03-01")     # ~2-month gap
    df = pd.concat([a, b], ignore_index=True)
    r = validate_bars(df, max_gap_days=5)
    assert r.ok and r.dropped == 0 and len(r.clean) == 5
    assert any("calendar gap" in i for i in r.issues)


def test_ohlc_inconsistency_flagged_but_kept():
    df = _bars([100.0, 101.0, 102.0])
    df.loc[1, "high"] = df.loc[1, "close"] - 1.0      # high below close, but >= low
    df.loc[1, "low"] = df.loc[1, "high"] - 0.5
    r = validate_bars(df)
    assert r.dropped == 0 and len(r.clean) == 3
    assert any("OHLC inconsistency" in i for i in r.issues)


def test_too_few_rows_not_ok():
    df = _bars([100.0])
    r = validate_bars(df, min_rows=2)
    assert not r.ok and any("usable row" in i for i in r.issues)
