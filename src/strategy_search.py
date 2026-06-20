"""
=============================================================================
 STRATEGY SEARCH  (research-only; live code untouched)
=============================================================================
Hunts for a long-only auto-trading algorithm that actually performs on BTC.

It tests a LIBRARY of strategy families, each tuned ONLY on in-sample data,
then judged OUT-OF-SAMPLE against Buy & Hold. Every long-only strategy is
expressed as a daily exposure signal (1 = hold BTC, 0 = cash) and run through
one shared, fee-aware simulator, so they're compared apples-to-apples.

Families tested:
  - Buy & Hold (benchmark)
  - Dual moving-average crossover
  - MA trend filter (price vs MA, with hysteresis)
  - Donchian breakout (+ optional ATR chandelier trail)
  - Time-series momentum (absolute momentum)
  - RSI(2) mean-reversion dip-buying inside an uptrend
  - MACD trend

Anti-overfit: each family is tuned on IN-SAMPLE by MAR, then that single config
is scored OUT-OF-SAMPLE. The full leaderboard is printed so you see everything,
not just the winner. Selecting the best family on OOS is mild selection bias —
treat a winner as a candidate to validate further, not gospel.

HOW TO RUN
    python src/strategy_search.py
    python src/strategy_search.py --split 2024-06-01

Uses the cached daily data from the regime backtester (downloads if missing).
RESEARCH ONLY: never trades, never touches live state.
=============================================================================
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Callable

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import ta  # noqa: E402
from loguru import logger  # noqa: E402

from src.config import load_config  # noqa: E402
from src.regime_backtester import Run, metrics, DEFAULT_CSV, download_daily, BACKTEST_DIR  # noqa: E402


# --------------------------------------------------------------------------- #
# Shared fee-aware simulator: exposure (0/1 per day) -> equity                 #
# --------------------------------------------------------------------------- #
def simulate(name: str, exposure: np.ndarray, close: np.ndarray,
             capital: float, fee: float, slip: float) -> Run:
    n = len(close)
    eq = np.empty(n); cash = capital; btc = 0.0
    sw = np.zeros(n); fees = np.zeros(n)
    state = 0
    for i in range(n):
        want = int(exposure[i]) if exposure[i] == exposure[i] else 0  # NaN -> cash
        if want != state:
            if want == 1:  # buy
                f = cash * fee
                btc = (cash - f) / (close[i] * (1 + slip)); cash = 0.0
                fees[i] += f; sw[i] = 1; state = 1
            else:          # sell
                f = btc * close[i] * (1 - slip) * fee
                cash = btc * close[i] * (1 - slip) - f; btc = 0.0
                fees[i] += f; sw[i] = 1; state = 0
        eq[i] = cash + btc * close[i]
    return Run(name, eq, exposure.astype(float), sw, fees)


# --------------------------------------------------------------------------- #
# Strategy families -> each returns a 0/1 exposure array (no lookahead)        #
# --------------------------------------------------------------------------- #
def expo_buy_hold(d: dict, _p) -> np.ndarray:
    return np.ones(len(d["close"]))


def expo_ma_cross(d: dict, p) -> np.ndarray:
    fast = d["close_s"].rolling(p["fast"]).mean()
    slow = d["close_s"].rolling(p["slow"]).mean()
    return (fast > slow).to_numpy().astype(float)


def expo_ma_filter(d: dict, p) -> np.ndarray:
    close = d["close"]; ma = d["close_s"].rolling(p["period"]).mean().to_numpy()
    buf = p["buffer"]; n = len(close); exp = np.zeros(n); inpos = False
    for i in range(n):
        m = ma[i]
        if m == m:
            if not inpos and close[i] > m:
                inpos = True
            elif inpos and close[i] < m * (1 - buf):
                inpos = False
        exp[i] = 1.0 if inpos else 0.0
    return exp


def expo_donchian(d: dict, p) -> np.ndarray:
    high_s, low_s, close = d["high_s"], d["low_s"], d["close"]
    up = high_s.rolling(p["entry"]).max().shift(1).to_numpy()   # prior N-day high
    dn = low_s.rolling(p["exit"]).min().shift(1).to_numpy()     # prior M-day low
    atr = d["atr"]; mult = p.get("atr_mult", 0.0)
    n = len(close); exp = np.zeros(n); inpos = False; peak = 0.0
    for i in range(n):
        if not inpos:
            if up[i] == up[i] and close[i] > up[i]:
                inpos = True; peak = close[i]
        else:
            peak = max(peak, close[i])
            hit_dn = dn[i] == dn[i] and close[i] < dn[i]
            hit_ch = mult > 0 and atr[i] == atr[i] and close[i] < peak - mult * atr[i]
            if hit_dn or hit_ch:
                inpos = False
        exp[i] = 1.0 if inpos else 0.0
    return exp


def expo_tsmom(d: dict, p) -> np.ndarray:
    close = d["close_s"]; lb = p["lookback"]
    mom = close / close.shift(lb) - 1.0
    return (mom > 0).to_numpy().astype(float)


def expo_rsi2(d: dict, p) -> np.ndarray:
    close = d["close"]; rsi = d["rsi2"]; trend = d["close_s"].rolling(p["trend_ma"]).mean().to_numpy()
    n = len(close); exp = np.zeros(n); inpos = False
    for i in range(n):
        up_trend = trend[i] == trend[i] and close[i] > trend[i]
        if not inpos:
            if up_trend and rsi[i] == rsi[i] and rsi[i] < p["buy"]:
                inpos = True
        else:
            if (rsi[i] == rsi[i] and rsi[i] > p["exit"]) or not up_trend:
                inpos = False
        exp[i] = 1.0 if inpos else 0.0
    return exp


def expo_macd(d: dict, _p) -> np.ndarray:
    return (d["macd"] > d["macd_sig"]).to_numpy().astype(float)


# (family_name, exposure_fn, list-of-param-dicts) ; [] params means single config
FAMILIES: list[tuple[str, Callable, list[dict]]] = [
    ("Dual MA cross", expo_ma_cross,
     [{"fast": f, "slow": s} for f in (10, 20, 50) for s in (50, 100, 200) if f < s]),
    ("MA filter", expo_ma_filter,
     [{"period": pr, "buffer": b} for pr in (100, 150, 200) for b in (0.0, 0.03, 0.05)]),
    ("Donchian breakout", expo_donchian,
     [{"entry": e, "exit": x, "atr_mult": 0.0} for e in (20, 40, 55) for x in (10, 20)]),
    ("Donchian + ATR trail", expo_donchian,
     [{"entry": e, "exit": 999, "atr_mult": m}
      for e in (20, 40, 55, 70) for m in (2.5, 3.0, 3.5, 4.0)]),
    ("Time-series momentum", expo_tsmom,
     [{"lookback": lb} for lb in (30, 60, 90, 120)]),
    ("RSI2 dip-buy in uptrend", expo_rsi2,
     [{"buy": b, "exit": x, "trend_ma": t} for b in (5, 10) for x in (50, 70) for t in (100, 200)]),
    ("MACD trend", expo_macd, [{}]),
]


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Search long-only BTC strategies.")
    ap.add_argument("--split", type=str, default="2024-06-01")
    ap.add_argument("--years", type=float, default=8.0)
    ap.add_argument("--exchange", type=str, default="auto")
    ap.add_argument("--symbol", type=str, default=None)
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

    cfg = load_config()
    fee = cfg["execution"]["taker_fee_pct"]; slip = cfg["execution"]["paper_slippage_pct"]
    capital = cfg["risk"]["default_capital_usd"]

    if os.path.exists(DEFAULT_CSV):
        df = pd.read_csv(DEFAULT_CSV); df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    else:
        df = download_daily(args.symbol or "BTC/USDT", args.years, args.exchange)
        df.to_csv(DEFAULT_CSV, index=False)

    close = df["close"].to_numpy()
    d = {
        "close": close, "close_s": df["close"], "high_s": df["high"], "low_s": df["low"],
        "atr": ta.volatility.average_true_range(df["high"], df["low"], df["close"], 14).to_numpy(),
        "rsi2": ta.momentum.rsi(df["close"], window=2).to_numpy(),
        "macd": ta.trend.MACD(df["close"]).macd(),
        "macd_sig": ta.trend.MACD(df["close"]).macd_signal(),
    }
    ts = pd.DatetimeIndex(df["timestamp"]).tz_convert("UTC").tz_localize(None).to_numpy()
    split = np.datetime64(pd.Timestamp(args.split))
    is_mask, oos_mask, full = ts <= split, ts > split, np.ones(len(ts), bool)
    logger.info("Data {:%Y-%m-%d}->{:%Y-%m-%d} ({} days). Split {}. Fee {:.2%}, slip {:.2%}.",
                df.iloc[0]["timestamp"], df.iloc[-1]["timestamp"], len(df), args.split, fee, slip)

    bh = simulate("Buy & Hold", expo_buy_hold(d, None), close, capital, fee, slip)

    # Tune each family on IN-SAMPLE by MAR; keep that single config for OOS.
    results: list[tuple[str, Run, dict]] = [("Buy & Hold", bh, {})]
    for fam, fn, grid in FAMILIES:
        best = None
        for p in grid:
            run = simulate(fam, fn(d, p), close, capital, fee, slip)
            m = metrics(run, is_mask, close)
            score = m.get("mar", -999)
            score = -999 if score == "inf" else score
            if best is None or score > best[0]:
                best = (score, run, p)
        results.append((fam, best[1], best[2]))

    # Leaderboard, ranked by OUT-OF-SAMPLE MAR.
    def oos_mar(run: Run) -> float:
        v = metrics(run, oos_mask, close).get("mar", -999)
        return -999 if v == "inf" else v
    ranked = sorted(results, key=lambda r: oos_mar(r[1]), reverse=True)

    cols = ["total_return_pct", "cagr_pct", "max_dd_pct", "mar", "sharpe", "pct_in_market", "switches"]
    hdr = ["Ret%", "CAGR%", "MaxDD%", "MAR", "Shrp", "InMkt%", "Sw"]
    lines = []
    for title, mask in [("IN-SAMPLE (tuning)", is_mask),
                        ("OUT-OF-SAMPLE (judge here)", oos_mask),
                        ("FULL PERIOD", full)]:
        lines.append("")
        lines.append("=" * 104)
        lines.append(f"  {title}   (ranked by OOS MAR)")
        lines.append("=" * 104)
        lines.append(f"  {'Strategy':<26}{'Params':<22}" + "".join(f"{h:>8}" for h in hdr))
        for fam, run, p in ranked:
            m = metrics(run, mask, close)
            if not m:
                continue
            ps = ",".join(f"{k}={v}" for k, v in p.items())[:20]
            lines.append(f"  {fam:<26}{ps:<22}" + "".join(f"{str(m[c]):>8}" for c in cols))
        lines.append("=" * 104)
    report = "\n".join(lines)
    print(report)

    os.makedirs(BACKTEST_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(BACKTEST_DIR, f"strategy_search_{stamp}.txt"), "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    logger.info("Saved strategy_search_{}.txt to {}.", stamp, BACKTEST_DIR)

    win = ranked[0]
    logger.info("Top by OOS MAR: {} {} (OOS MAR {:.2f}). B&H OOS MAR {:.2f}.",
                win[0], win[2], oos_mar(win[1]), oos_mar(bh))


if __name__ == "__main__":
    main()
