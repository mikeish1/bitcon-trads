"""
=============================================================================
 SLEEVE-ALLOCATOR RESEARCH  (research-only; demonstrates the overlay)
=============================================================================
Shows the thin SleeveAllocator improving risk-adjusted diversification versus a
STATIC equal-weight blend of the three sleeves. Because only the Donchian sleeve
has a price-history backtest in this repo, the three sleeve curves here are:

  * donchian : REAL equal-weight Donchian crypto portfolio (cached daily candles).
  * etf      : REAL defensive proxy - a 200-day MA long/flat curve on BTC (a lower-
               turnover trend sleeve standing in for the ETF-momentum bot).
  * carry    : a calibrated MODEL of delta-neutral funding carry (steady ~APR drift,
               tiny vol) - the live carry bot produces the real curve.

The allocator only ever sees equity curves, so the SOURCE of each curve is
irrelevant to what it does; this script just needs three differently-shaped curves
to exercise it. We then blend the sleeves three ways - static equal weight, and the
allocator's risk_parity / momentum_of_strategies (periodically rebalanced) - and
report combined vol, CAGR, max drawdown and Calmar.

  python -m src.portfolio_sleeve_research
  python -m src.portfolio_sleeve_research --rebalance-every 21 --carry-apr 0.08

RESEARCH ONLY: never trades, never touches live state.
=============================================================================
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import ta  # noqa: E402
from loguru import logger  # noqa: E402

from src.config import load_config  # noqa: E402
from src.backtester import _daily  # noqa: E402
from src.regime_backtester import BACKTEST_DIR  # noqa: E402
from src.strategy_search import simulate, expo_donchian, expo_ma_filter  # noqa: E402
from src.portfolio_sleeve_allocator import SleeveAllocator  # noqa: E402
from src.universe import portfolio_stats, _date_index  # noqa: E402


def _donchian_curve(frames: dict[str, pd.DataFrame], entry: int, atr_mult: float,
                    fee: float, slip: float) -> pd.Series:
    bases, series = list(frames.keys()), {}
    for b in bases:
        df = frames[b]
        d = {"close": df["close"].to_numpy(), "high_s": df["high"], "low_s": df["low"],
             "close_s": df["close"],
             "atr": ta.volatility.average_true_range(df["high"], df["low"], df["close"], 14).to_numpy()}
        expo = expo_donchian(d, {"entry": entry, "exit": 999, "atr_mult": atr_mult})
        run = simulate(b, expo, df["close"].to_numpy(), 1000.0 / len(bases), fee, slip)
        series[b] = pd.Series(run.equity, index=_date_index(df))
    cstart = max(s.index.min() for s in series.values())
    cend = min(s.index.max() for s in series.values())
    cal = pd.date_range(cstart, cend, freq="D")
    return pd.Series(np.sum([series[b].reindex(cal, method="ffill").to_numpy() for b in bases], axis=0),
                     index=cal)


def _ma_filter_curve(df: pd.DataFrame, fee: float, slip: float, period: int = 200) -> pd.Series:
    d = {"close": df["close"].to_numpy(), "close_s": df["close"]}
    expo = expo_ma_filter(d, {"period": period, "buffer": 0.0})
    run = simulate("etf-proxy", expo, df["close"].to_numpy(), 1000.0, fee, slip)
    return pd.Series(run.equity, index=_date_index(df))


def _carry_curve(index: pd.DatetimeIndex, apr: float, daily_vol: float, seed: int = 7) -> pd.Series:
    """Calibrated model of a delta-neutral funding sleeve: steady APR drift + tiny
    noise (no price exposure). Illustrative only - the live carry bot is the source
    of truth."""
    rng = np.random.default_rng(seed)
    daily = apr / 365.0 + rng.normal(0.0, daily_vol, size=len(index))
    return pd.Series(1000.0 * np.cumprod(1.0 + daily), index=index)


def _combined(returns: pd.DataFrame, alloc: SleeveAllocator, mode: str,
              rebalance_every: int, lookback: int) -> tuple[pd.Series, dict]:
    """Daily-compounded blend of the sleeve returns. `mode='equal'` holds 1/3 each;
    allocator modes recompute target weights every `rebalance_every` days from the
    trailing `lookback` window (with the allocator's own rebalance-threshold band)."""
    cols = list(returns.columns)
    eq = (1.0 + returns).cumprod()
    dates = returns.index
    weights, comb = None, np.empty(len(dates))
    for i in range(len(dates)):
        if mode == "equal":
            w = {c: 1.0 / len(cols) for c in cols}
        else:
            if i >= 5 and (weights is None or i % rebalance_every == 0):
                window = {c: {"equity": eq[c].iloc[max(0, i - lookback):i + 1]} for c in cols}
                weights = alloc.compute_weights(window, mode=mode, prev_weights=weights)
            w = weights or {c: 1.0 / len(cols) for c in cols}
        comb[i] = sum(w.get(c, 0.0) * returns[c].iloc[i] for c in cols)
    curve = pd.Series(1000.0 * np.cumprod(1.0 + comb), index=dates)
    return curve, (weights or {c: 1.0 / len(cols) for c in cols})


def main() -> None:
    ap = argparse.ArgumentParser(description="Sleeve-allocator diversification demo.")
    ap.add_argument("--symbols", type=str, default=None, help="Donchian sleeve universe (default: config).")
    ap.add_argument("--years", type=float, default=6.0)
    ap.add_argument("--exchange", type=str, default="auto")
    ap.add_argument("--rebalance-every", type=int, default=21)
    ap.add_argument("--carry-apr", type=float, default=0.08)
    ap.add_argument("--carry-vol", type=float, default=0.0015)
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="WARNING", format="<green>{time:HH:mm:ss}</green> | {message}")

    cfg = load_config()
    fee, slip = cfg["execution"]["taker_fee_pct"], cfg["execution"]["paper_slippage_pct"]
    dn = cfg["strategy"]["donchian"]
    entry, atr_mult = dn["entry_period"], dn["atr_trail_mult"]
    lookback = int(cfg.get("portfolio", {}).get("sleeves", {}).get("lookback_days", 60))
    bases = ([b.strip().upper() for b in args.symbols.split(",")] if args.symbols
             else [str(b).upper() for b in cfg["universe"]["bases"]])

    frames = {}
    for b in bases:
        try:
            frames[b] = _daily(b, args.years, args.exchange)
        except Exception as exc:
            print(f"skip {b} ({str(exc).splitlines()[0][:60]})")
    btc = frames["BTC"] if "BTC" in frames else _daily("BTC", args.years, args.exchange)

    # Build the three sleeve equity curves and align on a common calendar.
    don = _donchian_curve(frames, entry, atr_mult, fee, slip)
    etf = _ma_filter_curve(btc, fee, slip)
    cal = pd.date_range(max(don.index.min(), etf.index.min()),
                        min(don.index.max(), etf.index.max()), freq="D")
    don = don.reindex(cal, method="ffill")
    etf = etf.reindex(cal, method="ffill")
    carry = _carry_curve(cal, args.carry_apr, args.carry_vol)
    eqdf = pd.DataFrame({"donchian": don, "etf": etf, "carry": carry}).dropna()
    returns = eqdf.pct_change().dropna()

    alloc = SleeveAllocator(cfg)
    cols = ["total_return_pct", "cagr_pct", "vol_pct", "max_dd_pct", "calmar"]
    hdr = "".join(f"{h:>12}" for h in ["Return%", "CAGR%", "Vol%", "MaxDD%", "Calmar"])

    def _stats_row(label: str, curve: pd.Series, w: dict) -> str:
        s = portfolio_stats(curve.to_numpy())
        tot = (curve.iloc[-1] / curve.iloc[0] - 1.0) * 100
        wtxt = " | " + ", ".join(f"{k}:{v:.0%}" for k, v in w.items())
        return (f"  {label:<26}" + "".join(f"{v:>12}" for v in [
            f"{tot:.1f}", f"{s['cagr']*100:.1f}", f"{s['vol']*100:.1f}",
            f"{s['max_dd']*100:.1f}",
            f"{s['calmar']:.2f}" if np.isfinite(s['calmar']) else "inf"]) + wtxt)

    eq_curve, eq_w = _combined(returns, alloc, "equal", args.rebalance_every, lookback)
    rp_curve, rp_w = _combined(returns, alloc, "risk_parity", args.rebalance_every, lookback)
    mo_curve, mo_w = _combined(returns, alloc, "momentum_of_strategies", args.rebalance_every, lookback)

    lines = ["", "=" * 100,
             f"  SLEEVE ALLOCATOR vs STATIC EQUAL WEIGHT  ({eqdf.index[0]:%Y-%m-%d} -> {eqdf.index[-1]:%Y-%m-%d}, "
             f"rebalance {args.rebalance_every}d, lookback {lookback}d)", "=" * 100,
             "  Per-sleeve (standalone):", "  " + "-" * 96, f"  {'Sleeve':<26}{hdr}"]
    for name in ("donchian", "etf", "carry"):
        lines.append(_stats_row(name, eqdf[name], {name: 1.0}))
    lines += ["", "  Combined book:", "  " + "-" * 96, f"  {'Blend':<26}{hdr}    final weights"]
    lines.append(_stats_row("static equal-weight", eq_curve, eq_w))
    lines.append(_stats_row("allocator risk_parity", rp_curve, rp_w))
    lines.append(_stats_row("allocator momentum", mo_curve, mo_w))
    lines.append("=" * 100)
    report = "\n".join(lines)
    print(report)

    os.makedirs(BACKTEST_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(BACKTEST_DIR, f"sleeve_alloc_{stamp}.txt"), "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    print(f"\nSaved sleeve_alloc_{stamp}.txt to {BACKTEST_DIR}.")


if __name__ == "__main__":
    main()
