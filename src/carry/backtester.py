"""
Carry backtester (research only - never trades).

Two layers:
  * run_series(...)  - PURE: feed a list of per-interval funding rates and it
    replays the exact entry/hold/unwind rule, accruing funding minus round-trip
    fees, and reports APR / drawdown / deployment. Unit-tested offline.
  * CLI              - pulls REAL history via ccxt fetch_funding_rate_history and
    runs run_series on it:  python -m src.carry.backtester --assets BTC,ETH

Funding carry is genuinely backtestable because historical funding is published;
this is far more trustworthy than any Polymarket replay. It still ignores fill
competition and assumes you held the modelled notional - treat it as a sanity
check, with live `sim` mode as the real validation.
"""
from __future__ import annotations

import argparse
from typing import Any, Optional

from .signal import fee_drag_apr


def run_series(rates: list[float], *, min_entry_apr: float, min_hold_apr: float,
               flip_confirm_reads: int, expected_hold_days: float, taker: float,
               slip: float, periods_per_year: float) -> dict[str, Any]:
    """Replay the carry rule over a per-interval funding-rate series.

    Returns per-$1-of-notional PnL plus summary stats. Fees: one open and one
    close each cost 2 legs => open_fee = close_fee = 2*(taker+slip) of notional.
    """
    leg_cost = 2.0 * (taker + slip)                       # one side (2 legs)
    roundtrip = 2.0 * leg_cost                            # open + close
    fee_drag = fee_drag_apr(roundtrip, expected_hold_days)

    held = False
    low_reads = 0
    pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    intervals_deployed = 0
    n_trades = 0
    n_flips = 0

    for rate in rates:
        gross_apr = rate * periods_per_year
        net_apr = gross_apr - fee_drag
        if not held:
            if net_apr >= min_entry_apr:
                held, low_reads = True, 0
                pnl -= leg_cost                          # pay to open
                n_trades += 1
        else:
            pnl += rate                                  # this interval's funding (signed)
            intervals_deployed += 1
            if gross_apr < min_hold_apr:
                low_reads += 1
                if rate < 0:
                    n_flips += 1
                if low_reads >= flip_confirm_reads:
                    held = False
                    pnl -= leg_cost                      # pay to close
            else:
                low_reads = 0
        peak = max(peak, pnl)
        max_dd = max(max_dd, peak - pnl)

    if held:                                             # mark a final close for fairness
        pnl -= leg_cost
    n = len(rates)
    years = n / periods_per_year if periods_per_year else 0.0
    return {
        "n_intervals": n,
        "years": round(years, 3),
        "pnl_per_notional": round(pnl, 5),
        "apr": round(pnl / years, 4) if years else 0.0,
        "max_drawdown_per_notional": round(max_dd, 5),
        "pct_deployed": round(intervals_deployed / n, 3) if n else 0.0,
        "n_trades": n_trades,
        "n_negative_intervals": n_flips,
        "fee_drag_apr": round(fee_drag, 4),
    }


def _fetch_rates(exchange_id: str, symbol: str, limit: int) -> list[float]:  # pragma: no cover
    import ccxt
    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    ex.load_markets()
    hist = ex.fetch_funding_rate_history(symbol, limit=limit)
    return [float(h["fundingRate"]) for h in hist if h.get("fundingRate") is not None]


def _resolve_perp(exchange_id: str, asset: str) -> Optional[str]:  # pragma: no cover
    import ccxt
    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    markets = ex.load_markets()
    for q in ("USD", "USDT", "USDC"):
        for m in markets.values():
            if m.get("base") == asset and m.get("swap") and m.get("linear") and m.get("quote") == q:
                return m["symbol"]
    return None


def main() -> None:  # pragma: no cover - network CLI
    p = argparse.ArgumentParser(description="Funding-carry backtest on real history.")
    p.add_argument("--assets", default="BTC,ETH,SOL")
    p.add_argument("--perp-venue", default="krakenfutures")
    p.add_argument("--interval-hours", type=float, default=8.0)
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--min-entry-apr", type=float, default=0.08)
    p.add_argument("--min-hold-apr", type=float, default=0.02)
    p.add_argument("--flip-confirm-reads", type=int, default=3)
    p.add_argument("--expected-hold-days", type=float, default=30.0)
    p.add_argument("--taker", type=float, default=0.0005)
    p.add_argument("--slip", type=float, default=0.0005)
    args = p.parse_args()

    ppy = 8760.0 / args.interval_hours
    for asset in [a.strip().upper() for a in args.assets.split(",") if a.strip()]:
        sym = _resolve_perp(args.perp_venue, asset)
        if not sym:
            print(f"{asset}: no linear perp on {args.perp_venue}")
            continue
        rates = _fetch_rates(args.perp_venue, sym, args.limit)
        if not rates:
            print(f"{asset}: no funding history")
            continue
        stats = run_series(
            rates, min_entry_apr=args.min_entry_apr, min_hold_apr=args.min_hold_apr,
            flip_confirm_reads=args.flip_confirm_reads, expected_hold_days=args.expected_hold_days,
            taker=args.taker, slip=args.slip, periods_per_year=ppy)
        print(f"\n{asset} ({sym}, {len(rates)} intervals):")
        for k, v in stats.items():
            print(f"  {k:28s} {v}")


if __name__ == "__main__":
    main()
