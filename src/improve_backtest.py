"""
=============================================================================
 IMPROVEMENT A/B/C TEST  (research-only)
=============================================================================
Tests two proposed improvements to the multi-asset daily Donchian portfolio,
judged OUT-OF-SAMPLE, against the current equal-weight baseline:

  A. BASELINE            - equal-weight Donchian per coin (current live logic).
  B. + BTC REGIME FILTER - a coin may only be long when BTC is above its
                           regime MA (alts are ~0.8 correlated to BTC; this is
                           meant to cut failed-breakout whipsaw in BTC downtrends).
  C. + VOL-TARGET SIZING - on top of B, weight each coin by 1/volatility so
                           high-vol coins (DOGE/SOL) get smaller allocations.

Everything else identical (entry/ATR-trail from config, fees, slippage). The
benchmark is an equal-weight Buy & Hold of the same coins.

    python src/improve_backtest.py --split 2024-06-01

Research only - never trades.
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
from src.regime_backtester import Run, metrics, BACKTEST_DIR  # noqa: E402
from src.strategy_search import simulate, expo_donchian  # noqa: E402
from src.backtester import _daily  # noqa: E402


def _idx(df: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(df["timestamp"]).tz_convert("UTC").tz_localize(None)


def asset_exposure(df: pd.DataFrame, entry: int, atr_mult: float,
                   regime_on: pd.Series | None) -> np.ndarray:
    d = {"close": df["close"].to_numpy(), "high_s": df["high"], "low_s": df["low"],
         "close_s": df["close"],
         "atr": ta.volatility.average_true_range(df["high"], df["low"], df["close"], 14).to_numpy()}
    expo = expo_donchian(d, {"entry": entry, "exit": 999, "atr_mult": atr_mult})
    if regime_on is not None:
        aligned = regime_on.reindex(_idx(df), method="ffill").fillna(0).to_numpy()
        expo = expo * aligned
    return expo


def run_config(frames: dict[str, pd.DataFrame], entry: int, atr_mult: float,
               capital: float, fee: float, slip: float, split: np.datetime64,
               regime_ma: int = 0, vol_target: bool = False,
               risk_parity: bool = False) -> dict:
    bases = list(frames.keys())
    n = len(bases)

    regime_on = None
    if regime_ma > 0:
        btc = frames["BTC"]
        ro = (btc["close"] > btc["close"].rolling(regime_ma).mean())
        regime_on = pd.Series(ro.to_numpy(), index=_idx(btc)).astype(float)

    # Sizing weights:
    #   risk_parity -> inverse mean ATR% (the ATR-stop risk budget used live: lower-
    #                  vol coins get more capital so each carries similar stop risk),
    #   vol_target  -> inverse daily-return stdev,
    #   else        -> equal weight.
    if risk_parity:
        inv = {}
        for b in bases:
            atr = ta.volatility.average_true_range(
                frames[b]["high"], frames[b]["low"], frames[b]["close"], 14)
            m = (atr / frames[b]["close"]).dropna().mean()
            inv[b] = 1.0 / (m if m and m > 0 else 1.0)
        tot = sum(inv.values())
        weights = {b: inv[b] / tot for b in bases}
    elif vol_target:
        inv = {}
        for b in bases:
            r = frames[b]["close"].pct_change().dropna()
            inv[b] = 1.0 / (r.std() if r.std() > 0 else 1.0)
        tot = sum(inv.values())
        weights = {b: inv[b] / tot for b in bases}
    else:
        weights = {b: 1.0 / n for b in bases}

    eq_series, bh_series, expo_series, sw_series = {}, {}, {}, {}
    for b in bases:
        df = frames[b]
        expo = asset_exposure(df, entry, atr_mult, regime_on)
        close = df["close"].to_numpy()
        run = simulate(b, expo, close, capital / n, fee, slip)       # equal slice; scaled below
        bh = simulate(b, np.ones(len(close)), close, capital / n, fee, slip)
        idx = _idx(df)
        f = weights[b] * n                                           # scale to target weight
        eq_series[b] = pd.Series(run.equity * f, index=idx)
        bh_series[b] = pd.Series(bh.equity, index=idx)               # B&H stays equal-weight
        expo_series[b] = pd.Series(run.exposure, index=idx)
        sw_series[b] = pd.Series(run.switch, index=idx)

    cstart = max(s.index.min() for s in eq_series.values())
    cend = min(s.index.max() for s in eq_series.values())
    cal = pd.date_range(cstart, cend, freq="D")
    port = np.sum([eq_series[b].reindex(cal, method="ffill").to_numpy() for b in bases], axis=0)
    bh = np.sum([bh_series[b].reindex(cal, method="ffill").to_numpy() for b in bases], axis=0)
    expo = np.mean([expo_series[b].reindex(cal, method="ffill").fillna(0).to_numpy() for b in bases], axis=0)
    sw = np.sum([sw_series[b].reindex(cal).fillna(0).to_numpy() for b in bases], axis=0)
    agg = Run("port", port, expo, sw, np.zeros(len(cal)))
    cts = cal.to_numpy()
    oos, full = cts > split, np.ones(len(cts), bool)
    return {"oos": metrics(agg, oos, bh), "full": metrics(agg, full, bh),
            "bh_oos": metrics(Run("bh", bh, np.ones(len(cal)), np.zeros(len(cal)), np.zeros(len(cal))), oos, bh),
            "bh_full": metrics(Run("bh", bh, np.ones(len(cal)), np.zeros(len(cal)), np.zeros(len(cal))), full, bh)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", type=str, default="2024-06-01")
    ap.add_argument("--years", type=float, default=8.0)
    ap.add_argument("--regime-ma", type=int, default=100)
    ap.add_argument("--exchange", type=str, default="auto")
    args = ap.parse_args()
    logger.remove()
    logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

    cfg = load_config()
    fee, slip = cfg["execution"]["taker_fee_pct"], cfg["execution"]["paper_slippage_pct"]
    capital = cfg["risk"]["default_capital_usd"]
    dn = cfg["strategy"]["donchian"]
    entry, atr_mult = dn["entry_period"], dn["atr_trail_mult"]
    bases = cfg["universe"]["bases"]
    if "BTC" not in bases:
        bases = ["BTC"] + bases

    frames = {}
    for b in bases:
        try:
            frames[b] = _daily(b, args.years, args.exchange)
        except Exception as exc:
            logger.warning("skip {} ({})", b, str(exc).splitlines()[0][:60])
    bases = list(frames.keys())
    split = np.datetime64(pd.Timestamp(args.split))
    logger.info("A/B/C on {} | entry {} | trail {}x | regime MA {} | split {}",
                bases, entry, atr_mult, args.regime_ma, args.split)

    configs = [
        ("A baseline (equal-weight)", dict(regime_ma=0, vol_target=False)),
        ("B + BTC regime filter", dict(regime_ma=args.regime_ma, vol_target=False)),
        ("C + regime + vol-target", dict(regime_ma=args.regime_ma, vol_target=True)),
        ("D + regime + risk-parity(ATR)", dict(regime_ma=args.regime_ma, risk_parity=True)),
    ]
    cols = ["total_return_pct", "cagr_pct", "max_dd_pct", "mar", "sharpe", "pct_in_market", "switches"]
    hdr = "".join(f"{h:>9}" for h in ["Ret%", "CAGR%", "MaxDD%", "MAR", "Sharpe", "InMkt%", "Sw"])

    def row(label, m):
        return f"  {label:<28}" + "".join(f"{str(m.get(c,'-')):>9}" for c in cols)

    out = []
    base_res = None
    for win, key in [("OUT-OF-SAMPLE (judge here)", "oos"), ("FULL PERIOD", "full")]:
        out.append(""); out.append("=" * 100); out.append(f"  {win}"); out.append("=" * 100)
        out.append(f"  {'Config':<28}{hdr}")
        for name, kw in configs:
            res = run_config(frames, entry, atr_mult, capital, fee, slip, split, **kw)
            out.append(row(name, res[key]))
            if name.startswith("A"):
                out.append(row("   Buy & Hold (equal-wt)", res["bh_" + key]))
        out.append("=" * 100)
    report = "\n".join(out)
    print(report)
    os.makedirs(BACKTEST_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(BACKTEST_DIR, f"improve_{stamp}.txt"), "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    logger.info("Saved improve_{}.txt", stamp)


if __name__ == "__main__":
    main()
