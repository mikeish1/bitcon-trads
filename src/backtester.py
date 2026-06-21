"""
=============================================================================
 MULTI-ASSET BACKTESTER  (daily Donchian trend-follower)
=============================================================================
Backtests the VALIDATED daily Donchian breakout + ATR chandelier-trail strategy
across the whole configured universe, with the SAME standards for every coin.

  * Single-asset:   python src/backtester.py --symbols BTC
  * Multi-asset:    python src/backtester.py --symbols BTC,ETH,SOL,XRP,DOGE,ADA
  * Whole config:   python src/backtester.py            (uses universe in config)

For each coin it downloads daily candles, runs the exact strategy used live
(entry_period + atr_trail_mult from config/trading_config.yaml), and reports
per-asset metrics. It also builds an EQUAL-WEIGHT PORTFOLIO (capital split evenly
across the coins, over their common date range) and reports aggregate metrics +
an aggregate Buy & Hold benchmark.

Metrics (in-sample / out-of-sample / full): total return, CAGR, max drawdown,
MAR, Sharpe, % time in market, # switches - per asset AND in aggregate.

Honest reminder: a backtest is encouraging, not a guarantee. Out-of-sample is
the column that matters. Research only - never trades, never touches live state.
=============================================================================
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import ta  # noqa: E402
from loguru import logger  # noqa: E402

from src.config import load_config  # noqa: E402
from src.regime_backtester import Run, metrics, download_daily, BACKTEST_DIR  # noqa: E402
from src.strategy_search import simulate, expo_donchian  # noqa: E402


def _daily(base: str, years: float, exchange: str) -> pd.DataFrame:
    """Download (and cache) daily candles for one base asset."""
    cache = os.path.join(BACKTEST_DIR, f"{base}_1d.csv")
    if os.path.exists(cache):
        df = pd.read_csv(cache)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df
    df = download_daily(f"{base}/USDT", years, exchange)
    os.makedirs(BACKTEST_DIR, exist_ok=True)
    df.to_csv(cache, index=False)
    return df


def _run_asset(df: pd.DataFrame, entry: int, atr_mult: float, capital: float, fee: float,
               slip: float, regime_on: pd.Series | None = None) -> tuple[Run, Run, np.ndarray, np.ndarray]:
    """Return (strategy_run, buyhold_run, close, timestamps) for one asset."""
    close = df["close"].to_numpy()
    d = {"close": close, "high_s": df["high"], "low_s": df["low"], "close_s": df["close"],
         "atr": ta.volatility.average_true_range(df["high"], df["low"], df["close"], 14).to_numpy()}
    expo = expo_donchian(d, {"entry": entry, "exit": 999, "atr_mult": atr_mult})
    if regime_on is not None:  # gate: only long while BTC is in an uptrend
        idx = pd.DatetimeIndex(df["timestamp"]).tz_convert("UTC").tz_localize(None)
        expo = expo * regime_on.reindex(idx, method="ffill").fillna(0).to_numpy()
    strat = simulate("Donchian", expo, close, capital, fee, slip)
    bh = simulate("Buy & Hold", np.ones(len(close)), close, capital, fee, slip)
    ts = pd.DatetimeIndex(df["timestamp"]).tz_convert("UTC").tz_localize(None).to_numpy()
    return strat, bh, close, ts


def _fmt(m: dict[str, Any]) -> str:
    cols = ["total_return_pct", "cagr_pct", "max_dd_pct", "mar", "sharpe", "pct_in_market", "switches"]
    return "".join(f"{str(m.get(c, '-')):>9}" for c in cols)


HDR = "".join(f"{h:>9}" for h in ["Ret%", "CAGR%", "MaxDD%", "MAR", "Sharpe", "InMkt%", "Sw"])


def main() -> None:
    ap = argparse.ArgumentParser(description="Multi-asset daily Donchian backtester.")
    ap.add_argument("--symbols", type=str, default=None, help="Comma list of bases, e.g. BTC,ETH,SOL.")
    ap.add_argument("--years", type=float, default=8.0)
    ap.add_argument("--split", type=str, default="2024-06-01", help="In-sample/OOS boundary (UTC).")
    ap.add_argument("--capital", type=float, default=None)
    ap.add_argument("--exchange", type=str, default="auto")
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

    cfg = load_config()
    fee, slip = cfg["execution"]["taker_fee_pct"], cfg["execution"]["paper_slippage_pct"]
    capital = args.capital if args.capital else cfg["risk"]["default_capital_usd"]
    dn = cfg["strategy"]["donchian"]
    entry, atr_mult = dn["entry_period"], dn["atr_trail_mult"]

    bases = ([b.strip().upper() for b in args.symbols.split(",")] if args.symbols
             else cfg["universe"]["bases"])
    logger.info("Backtesting {} on daily Donchian ({}-day breakout, {}x ATR trail). Split {}.",
                bases, entry, atr_mult, args.split)

    # Download + build per-asset frames.
    frames: dict[str, pd.DataFrame] = {}
    for b in bases:
        try:
            frames[b] = _daily(b, args.years, args.exchange)
        except Exception as exc:
            logger.warning("Skipping {} (no data): {}", b, str(exc).splitlines()[0][:70])
    bases = list(frames.keys())
    if not bases:
        logger.error("No data for any requested asset.")
        return

    # Optional BTC regime filter (matches live config).
    rg = cfg["strategy"].get("btc_regime", {})
    regime_on = None
    if rg.get("enabled"):
        btc = frames.get("BTC")
        if btc is None:
            try:
                btc = _daily("BTC", args.years, args.exchange)
            except Exception:
                btc = None
        if btc is not None:
            ro = btc["close"] > btc["close"].rolling(rg.get("ma_period", 100)).mean()
            regime_on = pd.Series(ro.to_numpy(),
                                  index=pd.DatetimeIndex(btc["timestamp"]).tz_convert("UTC").tz_localize(None)).astype(float)
            logger.info("BTC regime filter ON (MA {}).", rg.get("ma_period", 100))

    split = np.datetime64(pd.Timestamp(args.split))
    lines: list[str] = []

    # ---- Per-asset (full-period + OOS) ----
    per_capital = capital / len(bases)
    aligned_equity: dict[str, pd.Series] = {}
    aligned_bh: dict[str, pd.Series] = {}
    aligned_expo: dict[str, pd.Series] = {}
    aligned_sw: dict[str, pd.Series] = {}
    lines.append("")
    lines.append("=" * 92)
    lines.append(f"  PER-ASSET  (OOS = after {args.split})")
    lines.append("=" * 92)
    lines.append(f"  {'Asset':<8}{'Window':<10}{HDR}")
    for b in bases:
        strat, bh, close, ts = _run_asset(frames[b], entry, atr_mult, per_capital, fee, slip, regime_on)
        oos = ts > split
        full = np.ones(len(ts), bool)
        m_oos = metrics(strat, oos, close)
        m_full = metrics(strat, full, close)
        if m_full:
            lines.append(f"  {b:<8}{'full':<10}{_fmt(m_full)}")
        if m_oos:
            lines.append(f"  {'':<8}{'OOS':<10}{_fmt(m_oos)}")
        # Store equity indexed by timestamp for the portfolio aggregate.
        idx = pd.DatetimeIndex(frames[b]["timestamp"]).tz_convert("UTC").tz_localize(None)
        aligned_equity[b] = pd.Series(strat.equity, index=idx)
        aligned_bh[b] = pd.Series(bh.equity, index=idx)
        aligned_expo[b] = pd.Series(strat.exposure, index=idx)
        aligned_sw[b] = pd.Series(strat.switch, index=idx)
    lines.append("=" * 92)

    # ---- Equal-weight portfolio on a CONTINUOUS daily calendar ----
    # (Forward-fill through delisting gaps like XRP's, so the aggregate equity
    #  curve is chronological and CAGR annualizes by real calendar time.)
    cstart = max(s.index.min() for s in aligned_equity.values())
    cend = min(s.index.max() for s in aligned_equity.values())
    cal = pd.date_range(cstart, cend, freq="D")
    if len(cal) > 60:
        eqs = [aligned_equity[b].reindex(cal, method="ffill").to_numpy() for b in bases]
        bhs = [aligned_bh[b].reindex(cal, method="ffill").to_numpy() for b in bases]
        expos = [aligned_expo[b].reindex(cal, method="ffill").fillna(0).to_numpy() for b in bases]
        sws = [aligned_sw[b].reindex(cal).fillna(0).to_numpy() for b in bases]
        port_eq = np.sum(eqs, axis=0)
        port_bh = np.sum(bhs, axis=0)
        agg = Run("Portfolio", port_eq, np.mean(expos, axis=0), np.sum(sws, axis=0), np.zeros(len(cal)))
        agg_bh = Run("Agg Buy&Hold", port_bh, np.ones(len(cal)), np.zeros(len(cal)), np.zeros(len(cal)))
        common = cal
        cts = cal.to_numpy()
        masks = [("IN-SAMPLE", cts <= split), ("OUT-OF-SAMPLE", cts > split), ("FULL", np.ones(len(cts), bool))]
        lines.append("")
        lines.append("=" * 92)
        lines.append(f"  EQUAL-WEIGHT PORTFOLIO ({len(bases)} assets, common range "
                     f"{pd.Timestamp(common[0]):%Y-%m-%d} -> {pd.Timestamp(common[-1]):%Y-%m-%d})")
        lines.append("=" * 92)
        lines.append(f"  {'Strategy':<16}{'Window':<14}{HDR}")
        for title, mask in masks:
            ms = metrics(agg, mask, port_bh)
            mb = metrics(agg_bh, mask, port_bh)
            if ms:
                lines.append(f"  {'Portfolio':<16}{title:<14}{_fmt(ms)}")
            if mb:
                lines.append(f"  {'Buy & Hold':<16}{title:<14}{_fmt(mb)}")
            lines.append("  " + "-" * 88)
        lines.append("=" * 92)
    else:
        lines.append("  (Not enough overlapping history for a portfolio aggregate.)")

    report = "\n".join(lines)
    print(report)
    os.makedirs(BACKTEST_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(BACKTEST_DIR, f"multi_backtest_{stamp}.txt"), "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    logger.info("Saved multi_backtest_{}.txt to {}.", stamp, BACKTEST_DIR)
    logger.info("Done. OOS is the column that matters; a good backtest is not a guarantee.")


if __name__ == "__main__":
    main()
