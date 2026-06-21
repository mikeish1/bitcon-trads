"""
=============================================================================
 MOMENTUM - FINAL VALIDATION  (research-only; live code untouched)
=============================================================================
Two checks on the candidate config that the controls+sweeps pointed to:

    hold TOP-4 coins, rotate every 2 days, 90-day momentum, BTC regime ON.

  (a) HEAD-TO-HEAD: best momentum config vs the current live baseline (and B&H),
      OUT-OF-SAMPLE and FULL, at NOMINAL and STRESSED costs. The combined config
      is tested as ONE thing (the sweeps only varied one knob at a time, so the
      joint optimum was never actually run until here).

  (b) WALK-FORWARD ACROSS REGIMES: the single 2024-06 OOS split is one market
      regime. Here the SAME fixed config is scored
        * per calendar period (2020Q4..2026YTD) - bull, bear, chop separately,
        * and after several rolling split dates,
      so we can see whether the edge is regime-dependent or persistent.

Capital is a pure scale factor (% cost model), so results are size-agnostic; the
$250 in config is only a default. RESEARCH ONLY: never trades, never touches
live state.

    python src/momentum_final.py
    python src/momentum_final.py --topk 4 --rebalance-every 2 --mom-lookback 90
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
from loguru import logger  # noqa: E402

from src.config import load_config  # noqa: E402
from src.regime_backtester import metrics, BACKTEST_DIR  # noqa: E402
from src.backtester import _daily  # noqa: E402
from src.profit_taking_research import (  # noqa: E402
    momentum_topk, baseline_asset, equalweight_portfolio, build_regime, _idx,
)

COLS = ["total_return_pct", "cagr_pct", "max_dd_pct", "mar", "sharpe", "pct_in_market", "switches"]
HDR = "".join(f"{h:>9}" for h in ["Ret%", "CAGR%", "MaxDD%", "MAR", "Sharpe", "InMkt%", "Sw"])


def _row(label: str, m: dict) -> str:
    if not m:
        return f"  {label:<26}{'(window too short)':>9}"
    return f"  {label:<26}" + "".join(f"{str(m.get(c, '-')):>9}" for c in COLS)


def main() -> None:
    ap = argparse.ArgumentParser(description="Momentum final validation (OOS + walk-forward).")
    ap.add_argument("--symbols", type=str, default="BTC,ETH,SOL,XRP,DOGE,ADA,BNB,VET")
    ap.add_argument("--years", type=float, default=8.0)
    ap.add_argument("--split", type=str, default="2024-06-01")
    ap.add_argument("--exchange", type=str, default="auto")
    ap.add_argument("--topk", type=int, default=4)
    ap.add_argument("--rebalance-every", type=int, default=2)
    ap.add_argument("--mom-lookback", type=int, default=90)
    ap.add_argument("--stress-fee", type=float, default=0.003)
    ap.add_argument("--stress-slip", type=float, default=0.004)
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

    cfg = load_config()
    fee0, slip0 = cfg["execution"]["taker_fee_pct"], cfg["execution"]["paper_slippage_pct"]
    capital = cfg["risk"]["default_capital_usd"]
    dn = cfg["strategy"]["donchian"]
    entry, atr_mult = dn["entry_period"], dn["atr_trail_mult"]
    regime_ma = cfg["strategy"].get("btc_regime", {}).get("ma_period", 100) \
        if cfg["strategy"].get("btc_regime", {}).get("enabled", False) else 0

    bases = [b.strip().upper() for b in args.symbols.split(",")]
    if "BTC" not in bases:
        bases = ["BTC"] + bases
    frames: dict[str, pd.DataFrame] = {}
    for b in bases:
        try:
            frames[b] = _daily(b, args.years, args.exchange)
        except Exception as exc:
            logger.warning("skip {} ({})", b, str(exc).splitlines()[0][:60])
    bases = list(frames.keys())
    regime_on = build_regime(frames, regime_ma)
    fee_s, slip_s = args.stress_fee, args.stress_slip
    k, rebal, lb = args.topk, args.rebalance_every, args.mom_lookback
    name = f"BEST momentum (top-{k}/{rebal}d/{lb}d)"

    logger.info("Coins {} ({}) | BEST = top-{} / every {}d / {}d momentum | regimeMA {}",
                bases, len(bases), k, rebal, lb, regime_ma)
    logger.info("Nominal fee {:.2%}/slip {:.2%} | stress fee {:.2%}/slip {:.2%}",
                fee0, slip0, fee_s, slip_s)

    def best(fee, slip):
        return momentum_topk(frames, entry, atr_mult, regime_on, capital, fee, slip,
                             k, lb, name, rebalance_every=rebal, keep_band=0, rank_mode="mom")

    def base(fee, slip):
        runs = {b: baseline_asset(frames[b], entry, atr_mult, regime_on,
                                  capital / len(bases), fee, slip) for b in bases}
        return equalweight_portfolio(runs, frames, "A baseline (live model)")

    best_n, bh_n, cts = best(fee0, slip0)
    best_s, _, _ = best(fee_s, slip_s)
    A_n, A_bh, _ = base(fee0, slip0)
    A_s, _, _ = base(fee_s, slip_s)
    split = np.datetime64(pd.Timestamp(args.split))

    lines: list[str] = []

    # ---------------- (a) HEAD-TO-HEAD ---------------- #
    for cost_name, BST, AA in (("NOMINAL", best_n, A_n), ("STRESSED", best_s, A_s)):
        for wname, mask in (("OUT-OF-SAMPLE (after %s)" % args.split, cts > split),
                            ("FULL PERIOD", np.ones(len(cts), bool))):
            lines += ["", "=" * 100, f"  (a) HEAD-TO-HEAD - {wname}  [{cost_name} costs]",
                      "=" * 100, f"  {'Config':<26}{HDR}"]
            lines.append(_row(BST.name, metrics(BST, mask, bh_n.equity)))
            lines.append(_row(AA.name, metrics(AA, mask, bh_n.equity)))
            lines.append(_row("   Buy & Hold (eq-wt)", metrics(bh_n, mask, bh_n.equity)))
            lines.append("=" * 100)

    # ---------------- (b) WALK-FORWARD: per calendar period ---------------- #
    cts_ts = pd.DatetimeIndex(cts)
    years = sorted(set(cts_ts.year))
    periods = []
    for y in years:
        start = np.datetime64(pd.Timestamp(year=y, month=1, day=1))
        end = np.datetime64(pd.Timestamp(year=y + 1, month=1, day=1))
        periods.append((str(y), start, end))
    lines += ["", "#" * 100,
              "  (b) WALK-FORWARD - per calendar year (same fixed config, each regime isolated)",
              "#" * 100,
              "  {:<8}{:>9}{:>9}{:>8}{:>8}   |{:>9}{:>9}{:>8}   |{:>9}".format(
                  "Year", "BestRet", "BestDD", "BMAR", "BShp", "BaseRet", "BaseDD", "BaseMAR", "BH_Ret")]
    lines.append("  " + "-" * 96)
    for label, start, end in periods:
        mask = (cts >= start) & (cts < end)
        mb = metrics(best_n, mask, bh_n.equity)
        ma = metrics(A_n, mask, bh_n.equity)
        mh = metrics(bh_n, mask, bh_n.equity)
        if not mb:
            continue
        lines.append(
            "  {:<8}{:>9}{:>9}{:>8}{:>8}   |{:>9}{:>9}{:>8}   |{:>9}".format(
                label, mb["total_return_pct"], mb["max_dd_pct"], mb["mar"], mb["sharpe"],
                ma.get("total_return_pct", "-"), ma.get("max_dd_pct", "-"), ma.get("mar", "-"),
                mh.get("total_return_pct", "-")))
    lines.append("#" * 100)

    # ---------------- (b) WALK-FORWARD: rolling split dates ---------------- #
    lines += ["", "#" * 100,
              "  (b) WALK-FORWARD - OOS after each split date (Best nominal/stress vs Baseline)",
              "#" * 100,
              "  {:<12}{:>9}{:>8}   |{:>9}{:>8}   |{:>9}{:>8}".format(
                  "Split", "BestRetN", "MARn", "BestRetS", "MARs", "BaseRet", "MAR")]
    lines.append("  " + "-" * 80)
    for sp in ("2021-06-01", "2022-01-01", "2022-06-01", "2023-01-01",
               "2023-06-01", "2024-01-01", "2024-06-01", "2025-01-01"):
        s = np.datetime64(pd.Timestamp(sp))
        mask = cts > s
        mbn = metrics(best_n, mask, bh_n.equity)
        mbs = metrics(best_s, mask, bh_n.equity)
        ma = metrics(A_n, mask, bh_n.equity)
        if not mbn:
            continue
        lines.append("  {:<12}{:>9}{:>8}   |{:>9}{:>8}   |{:>9}{:>8}".format(
            sp, mbn["total_return_pct"], mbn["mar"], mbs["total_return_pct"], mbs["mar"],
            ma.get("total_return_pct", "-"), ma.get("mar", "-")))
    lines.append("#" * 100)

    report = "\n".join(lines)
    print(report)
    os.makedirs(BACKTEST_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(BACKTEST_DIR, f"momentum_final_{stamp}.txt"), "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    logger.info("Saved momentum_final_{}.txt. OOS / per-regime are the columns that matter.", stamp)


if __name__ == "__main__":
    main()
