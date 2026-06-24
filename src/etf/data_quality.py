"""
ETF bar data-quality validation (PURE, offline-testable).

Equity feeds occasionally return bad ticks, duplicate timestamps, or missing
sessions. Feeding those straight into the Donchian/momentum signal corrupts the
ATR, the breakout high, and the rank. This module sanitises a raw OHLCV frame and
reports what it found, so the data layer can clean + log before computing
indicators. It is deliberately conservative: it drops only *structurally
impossible* rows (NaN/non-positive prices, high < low, duplicate timestamps) and
merely *flags* softer anomalies (calendar gaps, OHLC inconsistencies) rather than
silently discarding real data.

No network, no SDK types — operates on the plain frame the broker returns
(columns: timestamp, open, high, low, close, volume).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

_OHLC = ("open", "high", "low", "close")
_REQUIRED = ("timestamp", "open", "high", "low", "close", "volume")


@dataclass
class BarQualityReport:
    clean: pd.DataFrame                 # sanitised, timestamp-sorted frame
    ok: bool                            # enough usable rows to trade/backtest
    issues: list[str] = field(default_factory=list)
    dropped: int = 0                    # rows removed as structurally invalid


def validate_bars(df: pd.DataFrame | None, *, symbol: str = "",
                  min_rows: int = 2, max_gap_days: int = 5) -> BarQualityReport:
    """Sanitise an OHLCV frame and report quality issues.

    Drops (structurally invalid): rows with NaN/inf or non-positive O/H/L/C, rows
    with high < low, and duplicate timestamps (keeping the last). Flags only
    (kept): OHLC-consistency quirks (high below open/close, low above open/close)
    and calendar gaps larger than `max_gap_days` (a missing week+ of sessions).
    Returns the cleaned, ascending-by-timestamp frame; `ok` is False when fewer
    than `min_rows` usable rows remain.
    """
    issues: list[str] = []
    if df is None or len(df) == 0:
        return BarQualityReport(clean=_empty(), ok=False, issues=["no bars returned"])

    missing = [c for c in _REQUIRED if c not in df.columns]
    if missing:
        return BarQualityReport(clean=_empty(), ok=False,
                                issues=[f"missing columns: {', '.join(missing)}"])

    out = df[list(_REQUIRED)].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    for col in (*_OHLC, "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    n0 = len(out)

    # --- structurally invalid rows (dropped) --------------------------------
    bad_ts = out["timestamp"].isna()
    if bad_ts.any():
        issues.append(f"{int(bad_ts.sum())} unparseable timestamp(s) dropped")
        out = out[~bad_ts]

    nan_price = out[list(_OHLC)].isna().any(axis=1)
    if nan_price.any():
        issues.append(f"{int(nan_price.sum())} row(s) with NaN price dropped")
        out = out[~nan_price]

    nonpos = (out[list(_OHLC)] <= 0).any(axis=1)
    if nonpos.any():
        issues.append(f"{int(nonpos.sum())} row(s) with non-positive price dropped")
        out = out[~nonpos]

    impossible = out["high"] < out["low"]
    if impossible.any():
        issues.append(f"{int(impossible.sum())} row(s) with high < low dropped")
        out = out[~impossible]

    out = out.sort_values("timestamp")
    dup = out["timestamp"].duplicated(keep="last")
    if dup.any():
        issues.append(f"{int(dup.sum())} duplicate timestamp(s) collapsed")
        out = out[~dup]

    out = out.reset_index(drop=True)
    dropped = n0 - len(out)

    # --- soft anomalies (flagged, not dropped) ------------------------------
    if len(out):
        hi_ok = out["high"] >= out[["open", "close"]].max(axis=1)
        lo_ok = out["low"] <= out[["open", "close"]].min(axis=1)
        inconsistent = int((~(hi_ok & lo_ok)).sum())
        if inconsistent:
            issues.append(f"{inconsistent} row(s) with OHLC inconsistency (kept)")

    if len(out) > 1:
        gaps = out["timestamp"].diff().dt.days.fillna(0)
        big = gaps > max_gap_days
        if big.any():
            issues.append(f"{int(big.sum())} calendar gap(s) > {max_gap_days}d "
                          f"(max {int(gaps.max())}d) (kept)")

    ok = len(out) >= min_rows
    if not ok:
        issues.append(f"only {len(out)} usable row(s) (< {min_rows})")
    return BarQualityReport(clean=out, ok=ok, issues=issues, dropped=dropped)


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_REQUIRED))
