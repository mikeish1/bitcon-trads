"""
=============================================================================
 MOMENTUM CONTROLS & ROBUSTNESS SWEEP  (research-only; live code untouched)
=============================================================================
Decides whether the daily cross-sectional momentum result (C1) is a real edge
or an artifact, with three honest tests on the full universe:

  1. CONTROL (selection vs deployment): pooled top-K, daily, ranked by
       * momentum   (the thesis)
       * none        (fixed order = same pooling/concentration, NO momentum info)
       * weakest     (falsification: rank by WORST momentum)
     If momentum is real: momentum >> none >> weakest. If "none" ~ "momentum",
     the win was just deploying more capital into fewer names, not selection.

  2. CADENCE SWEEP: how fast must it rotate? rebalance_every in {1,2,3,5,7}.

  3. K & LOOKBACK SWEEPS: robustness of top-K and the momentum window.

Every cell is shown at NOMINAL and STRESSED costs, judged OUT-OF-SAMPLE. Capital
is just a scale factor here ($250 default is only the config default; the % cost
model is capital-agnostic, so these results apply at any size the user funds).

    python src/momentum_controls.py
    python src/momentum_controls.py --split 2024-06-01 --topk 3

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
from loguru import logger  # noqa: E402

from src.config import load_config  # noqa: E402
from src.regime_backtester import metrics, BACKTEST_DIR  # noqa: E402
from src.backtester import _daily  # noqa: E402
from src.profit_taking_research import (  # noqa: E402
    momentum_topk, baseline_asset, equalweight_portfolio, build_regime, _idx,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Momentum controls + robustness sweep (OOS).")
    ap.add_argument("--symbols", type=str, default="BTC,ETH,SOL,XRP,DOGE,ADA,BNB,VET")
    ap.add_argument("--years", type=float, default=8.0)
    ap.add_argument("--split", type=str, default="2024-06-01")
    ap.add_argument("--exchange", type=str, default="auto")
    ap.add_argument("--topk", type=int, default=3)
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
    split = np.datetime64(pd.Timestamp(args.split))
    fee_s, slip_s = args.stress_fee, args.stress_slip

    logger.info("Coins {} ({}) | entry {} trail {}x regimeMA {} | split {}",
                bases, len(bases), entry, atr_mult, regime_ma, args.split)
    logger.info("Nominal fee {:.2%}/slip {:.2%} | stress fee {:.2%}/slip {:.2%}",
                fee0, slip0, fee_s, slip_s)

    def mom_run(fee, slip, topk, lookback, rebal, rank_mode, name):
        return momentum_topk(frames, entry, atr_mult, regime_on, capital, fee, slip,
                             topk, lookback, name, rebalance_every=rebal,
                             keep_band=0, rank_mode=rank_mode)

    lines: list[str] = []

    # --- baseline (reference), reproduces the existing equal-weight backtest ---
    def baseline(fee, slip):
        runs = {b: baseline_asset(frames[b], entry, atr_mult, regime_on,
                                  capital / len(bases), fee, slip) for b in bases}
        return equalweight_portfolio(runs, frames, "A baseline (live model)")

    full_cols = ["total_return_pct", "cagr_pct", "max_dd_pct", "mar", "sharpe", "pct_in_market", "switches"]
    full_hdr = "".join(f"{h:>9}" for h in ["Ret%", "CAGR%", "MaxDD%", "MAR", "Sharpe", "InMkt%", "Sw"])

    def row(label, m, cols):
        return f"  {label:<28}" + "".join(f"{str(m.get(c, '-')):>9}" for c in cols)

    # ====================================================================== #
    # 1. CONTROLS: momentum vs none vs weakest (daily top-K)                  #
    # ====================================================================== #
    for cost_name, fee, slip in (("NOMINAL", fee0, slip0), ("STRESSED", fee_s, slip_s)):
        lines += ["", "#" * 100,
                  f"  1. CONTROLS - daily top-{args.topk}, ranked by ... ({cost_name} costs)",
                  "#" * 100, f"  {'Config':<28}{full_hdr}"]
        A, A_bh, _ = baseline(fee, slip)
        a_ts = pd.date_range(max(_idx(frames[b]).min() for b in bases),
                             min(_idx(frames[b]).max() for b in bases), freq="D").to_numpy()
        lines.append(row(A.name, metrics(A, a_ts > split, A_bh.equity), full_cols))
        lines.append(row("   Buy & Hold (eq-wt)", metrics(A_bh, a_ts > split, A_bh.equity), full_cols))
        for mode, label in (("mom", "momentum (thesis)"), ("none", "none (deploy-only ctrl)"),
                            ("weak", "weakest (falsification)")):
            run, bh, cts = mom_run(fee, slip, args.topk, args.mom_lookback, 1, mode, label)
            lines.append(row(label, metrics(run, cts > split, bh.equity), full_cols))
        lines.append("#" * 100)

    # ====================================================================== #
    # Sweeps: compact OOS rows at nominal + stress side by side              #
    # ====================================================================== #
    sweep_cols = ["total_return_pct", "cagr_pct", "max_dd_pct", "mar", "sharpe", "switches"]
    sweep_hdr = ("  {:<14}".format("param")
                 + "".join(f"{h:>8}" for h in ["NomRet", "NomMAR", "NomShp"])
                 + "   |"
                 + "".join(f"{h:>8}" for h in ["StrRet", "StrMAR", "StrShp"])
                 + f"{'Sw':>8}")

    def sweep_row(label, run_n, bh_n, cts_n, run_s, bh_s, cts_s):
        mn = metrics(run_n, cts_n > split, bh_n.equity)
        ms = metrics(run_s, cts_s > split, bh_s.equity)
        return ("  {:<14}".format(label)
                + f"{mn['total_return_pct']:>8}{mn['mar']:>8}{mn['sharpe']:>8}   |"
                + f"{ms['total_return_pct']:>8}{ms['mar']:>8}{ms['sharpe']:>8}{mn['switches']:>8}")

    def sweep(title, variants):
        out = ["", "=" * 100, f"  {title}", "=" * 100, sweep_hdr, "  " + "-" * 96]
        for label, (topk, lookback, rebal) in variants:
            rn = mom_run(fee0, slip0, topk, lookback, rebal, "mom", label)
            rs = mom_run(fee_s, slip_s, topk, lookback, rebal, "mom", label)
            out.append(sweep_row(label, rn[0], rn[1], rn[2], rs[0], rs[1], rs[2]))
        out.append("=" * 100)
        return out

    lines += sweep(f"2. CADENCE SWEEP (top-{args.topk}, {args.mom_lookback}d momentum)",
                   [(f"every {n}d", (args.topk, args.mom_lookback, n)) for n in (1, 2, 3, 5, 7)])
    lines += sweep(f"3. TOP-K SWEEP (daily, {args.mom_lookback}d momentum)",
                   [(f"K={k}", (k, args.mom_lookback, 1)) for k in (2, 3, 4, 5)])
    lines += sweep(f"4. LOOKBACK SWEEP (daily, top-{args.topk})",
                   [(f"{lb}d", (args.topk, lb, 1)) for lb in (30, 60, 90, 120, 150)])

    report = "\n".join(lines)
    print(report)
    os.makedirs(BACKTEST_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(BACKTEST_DIR, f"momentum_controls_{stamp}.txt"), "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    logger.info("Saved momentum_controls_{}.txt. OOS is the column that matters.", stamp)


if __name__ == "__main__":
    main()
