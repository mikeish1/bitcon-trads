"""
=============================================================================
 UNIVERSE-EXPANSION RESEARCH  (research-only; live config untouched)
=============================================================================
Runs the strict liquidity + correlation + portfolio-benefit gates (src/universe.py)
on each PROPOSED candidate coin and prints an APPROVE / REJECT report with the
underlying metrics. Nothing is added to the live universe automatically: a human
reviews this report and, only for approved names, copies them into
`universe.expansion.approved_expanded_universe` (then into `universe.bases`).

  python -m src.universe_expansion_research
  python -m src.universe_expansion_research --candidates AVAX,LINK,LTC,DOT
  python -m src.universe_expansion_research --members BTC,ETH,SOL,XRP,DOGE,ADA \
        --candidates AVAX,LINK --years 4

Existing-member + candidate daily candles are cached under backtests/ (downloaded
on first use, like the other backtesters). RESEARCH ONLY: never trades.
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

import pandas as pd  # noqa: E402
from loguru import logger  # noqa: E402

from src.config import load_config  # noqa: E402
from src.backtester import _daily  # noqa: E402
from src.regime_backtester import BACKTEST_DIR  # noqa: E402
from src.universe import validate_universe_addition  # noqa: E402


def _load(bases: list[str], years: float, exchange: str) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for b in bases:
        try:
            frames[b] = _daily(b, years, exchange)
        except Exception as exc:
            logger.warning("skip {} ({})", b, str(exc).splitlines()[0][:70])
    return frames


def main() -> None:
    ap = argparse.ArgumentParser(description="Strict universe-expansion validation.")
    ap.add_argument("--members", type=str, default=None, help="Existing universe (default: config universe.bases).")
    ap.add_argument("--candidates", type=str, default=None,
                    help="Comma list (default: config universe.expansion.candidates).")
    ap.add_argument("--years", type=float, default=4.0)
    ap.add_argument("--exchange", type=str, default="auto")
    # Optional threshold overrides (defaults come from config.liquidity_filters).
    # Useful to recalibrate to the live venue's volume scale without editing YAML.
    ap.add_argument("--min-volume", type=float, default=None, help="Absolute ADV floor (USDT).")
    ap.add_argument("--rel-pct", type=float, default=None, help="ADV floor as a fraction of median member ADV.")
    ap.add_argument("--max-corr", type=float, default=None, help="Max pairwise return correlation.")
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

    cfg = load_config()
    cfg.setdefault("liquidity_filters", {})
    if args.min_volume is not None:
        cfg["liquidity_filters"]["min_avg_daily_volume_usdt"] = args.min_volume
    if args.rel_pct is not None:
        cfg["liquidity_filters"]["min_relative_to_median_pct"] = args.rel_pct
    if args.max_corr is not None:
        cfg["liquidity_filters"]["max_pairwise_correlation"] = args.max_corr
    exp = (cfg.get("universe", {}) or {}).get("expansion", {}) or {}
    members = ([m.strip().upper() for m in args.members.split(",")] if args.members
               else [str(b).upper() for b in cfg["universe"]["bases"]])
    candidates = ([c.strip().upper() for c in args.candidates.split(",")] if args.candidates
                  else [str(c).upper() for c in exp.get("candidates", [])])
    # Always include BTC among members (regime/correlation anchor).
    if "BTC" not in members:
        members = ["BTC"] + members
    candidates = [c for c in candidates if c not in members]
    if not candidates:
        logger.error("No candidates to evaluate (set universe.expansion.candidates or --candidates).")
        return

    lf = cfg.get("liquidity_filters", {})
    logger.info("Members: {} | Candidates: {}", members, candidates)
    logger.info("Gates: ADV >= ${:,.0f}/{}d | corr <= {:.2f}/{}d | vol_red >= {:.0%} OR calmar >= {:+.2f}"
                " | turnover <= {:.0%}",
                float(lf.get("min_avg_daily_volume_usdt", 30e6)), lf.get("volume_window", 30),
                float(lf.get("max_pairwise_correlation", 0.90)), lf.get("correlation_lookback", 180),
                float(lf.get("min_vol_reduction_pct", 0.0)), float(lf.get("min_calmar_improvement", 0.0)),
                float(lf.get("max_turnover_increase_pct", 0.50)))

    member_frames = _load(members, args.years, args.exchange)
    if len(member_frames) < 2:
        logger.error("Need >= 2 existing members with data.")
        return

    lines: list[str] = ["", "=" * 92, "  UNIVERSE-EXPANSION VALIDATION REPORT", "=" * 92]
    approved: list[str] = []
    for cand in candidates:
        try:
            cand_df = _daily(cand, args.years, args.exchange)
        except Exception as exc:
            lines.append(f"  {cand:<6} DATA UNAVAILABLE - {str(exc).splitlines()[0][:60]}")
            continue
        verdict = validate_universe_addition(cand, cand_df, member_frames, cfg)
        flag = "APPROVED" if verdict["approved"] else "REJECTED"
        lines.append("")
        lines.append(f"  {cand:<6} {flag}   gates: " +
                     ", ".join(f"{k}={'ok' if v else 'no'}" for k, v in verdict["gates"].items()))
        for r in verdict["reasons"]:
            lines.append(f"         - {r}")
        if verdict["approved"]:
            approved.append(cand)
    lines.append("")
    lines.append("=" * 92)
    lines.append(f"  APPROVED for expansion: {approved or '(none)'}")
    lines.append("  -> copy approved names into universe.expansion.approved_expanded_universe,")
    lines.append("     then into universe.bases, to trade them live.")
    lines.append("=" * 92)

    report = "\n".join(lines)
    print(report)
    os.makedirs(BACKTEST_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(BACKTEST_DIR, f"universe_expansion_{stamp}.txt"), "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    logger.info("Saved universe_expansion_{}.txt to {}.", stamp, BACKTEST_DIR)


if __name__ == "__main__":
    main()
