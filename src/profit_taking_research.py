"""
=============================================================================
 PROFIT-TAKING / SIGNAL RESEARCH  (research-only; live code untouched)
=============================================================================
Tests two proposed ways to "see" more profit, judged OUT-OF-SAMPLE against the
current live logic, on the multi-asset daily Donchian portfolio:

  A. BASELINE            - current live: per-coin Donchian breakout + ATR
                           chandelier trail + BTC regime filter, equal-weight.
                           (This is what main_loop.py trades today.)

  B. + SCALE-OUT/RATCHET - same entries, but ACTIVELY TAKE PROFIT:
                             * sell tranches as a winner extends (default 1/3 at
                               +2*ATR, 1/3 at +4*ATR from entry), and
                             * tighten the chandelier multiple as profit grows
                               (3.0 -> 2.5 -> 2.0), so a parabolic move gives
                               back less at the top.
                           The remainder still rides the trail (winners run).

  C. MOMENTUM TOP-K      - same per-coin Donchian/trail signals, but instead of
                           first-come equal-weight, each day hold only the K
                           STRONGEST coins (by N-day momentum) among those with
                           an active signal, daily-rebalanced. Exploits the
                           cross-sectional momentum your universe already has.

Everything is fee+slippage aware and uses the SAME entry_period / atr_trail_mult
/ regime settings as config/trading_config.yaml, so A reproduces live. OOS is the
column that matters; a good backtest is a candidate to validate further, not a
guarantee.

HOW TO RUN
    python src/profit_taking_research.py
    python src/profit_taking_research.py --symbols BTC,ETH,ADA,DOGE --split 2024-06-01
    python src/profit_taking_research.py --topk 3 --mom-lookback 90 \
        --scale-pcts 0.33,0.33 --scale-atr 2,4 --ratchet 3.0,2.5,2.0

RESEARCH ONLY: never trades, never touches live state or any account.
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


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _idx(df: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(df["timestamp"]).tz_convert("UTC").tz_localize(None)


def _atr(df: pd.DataFrame, window: int = 14) -> np.ndarray:
    return ta.volatility.average_true_range(df["high"], df["low"], df["close"], window).to_numpy()


def build_regime(frames: dict[str, pd.DataFrame], ma_period: int) -> pd.Series | None:
    """1/0 daily series: is BTC above its regime MA? (None disables the gate)."""
    if ma_period <= 0 or "BTC" not in frames:
        return None
    btc = frames["BTC"]
    on = (btc["close"] > btc["close"].rolling(ma_period).mean()).astype(float)
    return pd.Series(on.to_numpy(), index=_idx(btc))


def _regime_array(regime_on: pd.Series | None, df: pd.DataFrame) -> np.ndarray:
    if regime_on is None:
        return np.ones(len(df))
    return regime_on.reindex(_idx(df), method="ffill").fillna(0).to_numpy()


# --------------------------------------------------------------------------- #
# A. Baseline per-coin run (reuses the EXACT validated exposure logic)         #
# --------------------------------------------------------------------------- #
def baseline_asset(df: pd.DataFrame, entry: int, atr_mult: float,
                   regime_on: pd.Series | None, capital: float, fee: float, slip: float) -> Run:
    d = {"close": df["close"].to_numpy(), "high_s": df["high"], "low_s": df["low"],
         "close_s": df["close"], "atr": _atr(df)}
    expo = expo_donchian(d, {"entry": entry, "exit": 999, "atr_mult": atr_mult})
    expo = expo * _regime_array(regime_on, df)
    return simulate("baseline", expo, df["close"].to_numpy(), capital, fee, slip)


# --------------------------------------------------------------------------- #
# B. Per-coin run WITH scale-out tranches + ratcheting chandelier              #
# --------------------------------------------------------------------------- #
def scaleout_asset(df: pd.DataFrame, entry: int, regime_on: pd.Series | None,
                   capital: float, fee: float, slip: float,
                   scale_pcts: list[float], scale_atr: list[float],
                   ratchet: list[float],
                   breakeven_after_tier: int = 0,
                   breakeven_buffer_atr: float = 0.0) -> Run:
    """
    Faithful long-only state machine (no lookahead; no constant-fraction churn):

      ENTER full on a fresh `entry`-day high (regime permitting).
      While long, each day:
        * peak  = highest close since entry,
        * profit_atr = (close - entry_price) / ATR_at_entry,
        * at each profit level scale_atr[k] sell scale_pcts[k] of the ORIGINAL
          position ONCE (tranche profit-taking); the remainder rides on,
        * chandelier multiple tightens by profit tier (ratchet[]): base ratchet[0],
          and after crossing scale_atr[k] it uses ratchet[k+1] (tighter),
        * once `breakeven_after_tier` (>0) tranches have fired, the stop is also
          floored at entry + `breakeven_buffer_atr`*ATR_at_entry - this MIRRORS the
          live main_loop._manage breakeven lock (risk_manager.profit_taking_plan).
          With breakeven_after_tier=0 the floor is OFF (the original validated B),
          so callers can compare the validated logic to the TRUE live logic.
        * EXIT the remainder when close < max(peak - mult*ATR, breakeven_floor),
          or on regime-off.
    """
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    atr = _atr(df)
    prior_high = pd.Series(high).rolling(entry).max().shift(1).to_numpy()
    reg = _regime_array(regime_on, df)

    n = len(close)
    eq = np.empty(n); expo = np.zeros(n); sw = np.zeros(n); fees = np.zeros(n)
    cash = capital; units = 0.0
    invested = False
    entry_price = 0.0; entry_atr = 0.0; orig_units = 0.0; peak = 0.0; tranche = 0

    def chandelier_mult(profit_atr: float) -> float:
        mult = ratchet[0]
        for k, thr in enumerate(scale_atr):
            if profit_atr >= thr and k + 1 < len(ratchet):
                mult = ratchet[k + 1]
        return mult

    def sell(qty: float, price: float, i: int) -> None:
        nonlocal cash, units
        qty = min(qty, units)
        if qty <= 0:
            return
        proceeds = qty * price * (1 - slip)
        f = proceeds * fee
        cash += proceeds - f
        units -= qty
        fees[i] += f; sw[i] = 1

    for i in range(n):
        price = close[i]
        if not invested:
            breakout = prior_high[i] == prior_high[i] and price > prior_high[i]
            if breakout and reg[i] > 0 and cash > 0 and atr[i] == atr[i] and atr[i] > 0:
                f = cash * fee
                units = (cash - f) / (price * (1 + slip))
                cash = 0.0; fees[i] += f; sw[i] = 1
                invested = True
                entry_price = price; entry_atr = atr[i]; orig_units = units
                peak = price; tranche = 0
        else:
            peak = max(peak, price)
            profit_atr = (price - entry_price) / entry_atr if entry_atr > 0 else 0.0

            # 1) tranche profit-taking (each level fires at most once)
            while tranche < len(scale_pcts) and profit_atr >= scale_atr[tranche]:
                sell(orig_units * scale_pcts[tranche], price, i)
                tranche += 1

            # 2) ratcheting chandelier exit on the remainder, plus the live
            #    breakeven floor once enough tiers have fired (mirrors the live
            #    current_stop = max(chandelier, entry + buffer*ATR) lock).
            if units > 0:
                mult = chandelier_mult(profit_atr)
                stop = peak - mult * atr[i] if atr[i] == atr[i] else -np.inf
                if breakeven_after_tier > 0 and tranche >= breakeven_after_tier:
                    stop = max(stop, entry_price + breakeven_buffer_atr * entry_atr)
                if reg[i] <= 0 or price < stop:
                    sell(units, price, i)
                    invested = False

        eq[i] = cash + units * price
        expo[i] = (units * price) / eq[i] if eq[i] > 0 else 0.0

    return Run("scaleout", eq, expo, sw, fees)


# --------------------------------------------------------------------------- #
# Equal-weight portfolio aggregate from per-coin Runs (same pattern as         #
# backtester.py: split capital evenly, forward-fill onto a common calendar).   #
# --------------------------------------------------------------------------- #
def equalweight_portfolio(runs: dict[str, Run], frames: dict[str, pd.DataFrame],
                          name: str) -> tuple[Run, Run, np.ndarray]:
    bases = list(runs.keys())
    eq_s, bh_s, ex_s, sw_s = {}, {}, {}, {}
    for b in bases:
        idx = _idx(frames[b])
        eq_s[b] = pd.Series(runs[b].equity, index=idx)
        ex_s[b] = pd.Series(runs[b].exposure, index=idx)
        sw_s[b] = pd.Series(runs[b].switch, index=idx)
        bh_close = frames[b]["close"].to_numpy()
        bh_eq = bh_close / bh_close[0] * (runs[b].equity[0])  # B&H of this slice
        bh_s[b] = pd.Series(bh_eq, index=idx)
    cstart = max(s.index.min() for s in eq_s.values())
    cend = min(s.index.max() for s in eq_s.values())
    cal = pd.date_range(cstart, cend, freq="D")
    port = np.sum([eq_s[b].reindex(cal, method="ffill").to_numpy() for b in bases], axis=0)
    bh = np.sum([bh_s[b].reindex(cal, method="ffill").to_numpy() for b in bases], axis=0)
    ex = np.mean([ex_s[b].reindex(cal, method="ffill").fillna(0).to_numpy() for b in bases], axis=0)
    sw = np.sum([sw_s[b].reindex(cal).fillna(0).to_numpy() for b in bases], axis=0)
    agg = Run(name, port, ex, sw, np.zeros(len(cal)))
    agg_bh = Run("Buy & Hold", bh, np.ones(len(cal)), np.zeros(len(cal)), np.zeros(len(cal)))
    return agg, agg_bh, cal.to_numpy()


# --------------------------------------------------------------------------- #
# C. Cross-sectional momentum: hold the top-K active coins, daily-rebalanced.  #
# --------------------------------------------------------------------------- #
def _zscore_at(rows: list[np.ndarray], cand: list[int], i: int) -> dict[int, float]:
    """Cross-sectional z-score of each component (rows) over candidate columns at
    day i, summed is left to the caller. Returns {component_index_in_rows-agnostic}
    ... actually returns {j: [z_per_component]} via a dict keyed by candidate j."""
    out: dict[int, list[float]] = {j: [] for j in cand}
    for arr in rows:
        vals = [arr[i, j] for j in cand]
        finite = [v for v in vals if v == v]
        if len(finite) >= 2:
            mean = sum(finite) / len(finite)
            std = (sum((v - mean) ** 2 for v in finite) / len(finite)) ** 0.5
        else:
            mean, std = 0.0, 0.0
        for j in cand:
            v = arr[i, j]
            out[j].append(((v - mean) / std) if (std > 0 and v == v) else 0.0)
    return out


def momentum_topk(frames: dict[str, pd.DataFrame], entry: int, atr_mult: float,
                  regime_on: pd.Series | None, capital: float, fee: float, slip: float,
                  topk: int, mom_lookback: int, name: str,
                  rebalance_every: int = 1, keep_band: int = 0,
                  rank_mode: str = "mom", roc_short: int = 20,
                  comp_weights: dict[str, float] | None = None,
                  btc_base: str = "BTC",
                  reweight: bool = True, cap_mult: float | None = None) -> tuple[Run, Run, np.ndarray]:
    """
    Cross-sectional momentum, daily-rebalanced by default but with two turnover
    controls so it isn't a churn mirage:

      rebalance_every : only ROTATE/re-weight every N days (1 = original daily).
                        Between rebalances, winners drift (run) untouched.
      keep_band       : hysteresis - keep a held coin until its momentum rank
                        slips below topk+keep_band (avoids swapping on tiny
                        rank flips). 0 = no hysteresis.

    rank_mode : "mom"  -> rank by N-day ROC (the validated control).
                "weak" -> weakest first (falsification control).
                "none" -> fixed index order (no momentum info).
                "composite" -> rank by a z-scored blend of breakout strength, long
                               ROC, short ROC, relative strength vs BTC, and inverse
                               ATR (mirrors MomentumRotation's composite scoring).

    Risk is never deferred: a held coin whose Donchian/trail signal turns OFF
    (or regime-off) is exited the SAME day, regardless of the rebalance clock.
    """
    bases = list(frames.keys())
    close_s, active_s, mom_s = {}, {}, {}
    # Extra component series only needed for the composite ranker.
    brk_s, invv_s, rocS_s = {}, {}, {}
    for b in bases:
        df = frames[b]
        idx = _idx(df)
        atr = _atr(df)
        d = {"close": df["close"].to_numpy(), "high_s": df["high"], "low_s": df["low"],
             "close_s": df["close"], "atr": atr}
        active = expo_donchian(d, {"entry": entry, "exit": 999, "atr_mult": atr_mult})
        active = active * _regime_array(regime_on, df)
        mom = (df["close"] / df["close"].shift(mom_lookback) - 1.0).to_numpy()
        close_s[b] = pd.Series(df["close"].to_numpy(), index=idx)
        active_s[b] = pd.Series(active, index=idx)
        mom_s[b] = pd.Series(mom, index=idx)
        if rank_mode == "composite":
            prior_high = df["high"].rolling(entry).max().shift(1).to_numpy()
            with np.errstate(divide="ignore", invalid="ignore"):
                brk = np.where(atr > 0, (df["close"].to_numpy() - prior_high) / atr, np.nan)
                invv = np.where(atr > 0, df["close"].to_numpy() / atr, np.nan)
            rocS = (df["close"] / df["close"].shift(roc_short) - 1.0).to_numpy()
            brk_s[b] = pd.Series(brk, index=idx)
            invv_s[b] = pd.Series(invv, index=idx)
            rocS_s[b] = pd.Series(rocS, index=idx)

    cstart = max(s.index.min() for s in close_s.values())
    cend = min(s.index.max() for s in close_s.values())
    cal = pd.date_range(cstart, cend, freq="D")
    closes = np.column_stack([close_s[b].reindex(cal, method="ffill").to_numpy() for b in bases])
    active = np.column_stack([active_s[b].reindex(cal, method="ffill").fillna(0).to_numpy() for b in bases])
    mom = np.column_stack([mom_s[b].reindex(cal, method="ffill").to_numpy() for b in bases])

    # Composite component matrices (T x J), aligned to the same calendar.
    comp_rows = None
    comp_w = None
    if rank_mode == "composite":
        w = comp_weights or {"breakout": 0.30, "roc_long": 0.30, "roc_short": 0.15,
                             "rel_btc": 0.15, "inv_vol": 0.10}
        brk = np.column_stack([brk_s[b].reindex(cal, method="ffill").to_numpy() for b in bases])
        invv = np.column_stack([invv_s[b].reindex(cal, method="ffill").to_numpy() for b in bases])
        rocS = np.column_stack([rocS_s[b].reindex(cal, method="ffill").to_numpy() for b in bases])
        btc_col = bases.index(btc_base) if btc_base in bases else None
        rel = mom - (mom[:, [btc_col]] if btc_col is not None else 0.0)
        comp_rows = {"breakout": brk, "roc_long": mom, "roc_short": rocS,
                     "rel_btc": rel, "inv_vol": invv}
        comp_w = w

    T, J = closes.shape
    cash = capital; units = np.zeros(J); held: set[int] = set()
    eq = np.empty(T); expo = np.empty(T); sw = np.zeros(T); fees = np.zeros(T)
    bh_units = (capital / J) / closes[0]
    bh_curve = closes @ bh_units

    def sell_all(j: int, price: np.ndarray, i: int) -> None:
        nonlocal cash
        if units[j] > 0 and price[j] > 0:
            proceeds = units[j] * price[j] * (1 - slip); f = proceeds * fee
            cash += proceeds - f; fees[i] += f; sw[i] += 1; units[j] = 0.0
        held.discard(j)

    for i in range(T):
        price = closes[i]
        # 1) RISK (any day): exit holdings whose signal/regime went off.
        for j in list(held):
            if active[i, j] < 1 or price[j] <= 0:
                sell_all(j, price, i)

        # 2) ROTATION + re-weight: only on the rebalance clock.
        if i % rebalance_every == 0:
            tot = cash + float(np.sum(units * price))
            # Same candidate pool for every rank_mode (fair control): active,
            # priced, and momentum defined. Only the ORDERING differs.
            cand = [j for j in range(J) if active[i, j] >= 1 and price[j] > 0 and mom[i, j] == mom[i, j]]
            if rank_mode == "mom":       # strongest momentum first (the thesis)
                cand.sort(key=lambda j: mom[i, j], reverse=True)
            elif rank_mode == "weak":    # weakest first (falsification control)
                cand.sort(key=lambda j: mom[i, j])
            elif rank_mode == "composite" and comp_rows is not None:
                zs = _zscore_at(list(comp_rows.values()), cand, i)
                wts = list(comp_w.values())
                score = {j: sum(wt * z for wt, z in zip(wts, zs[j])) for j in cand}
                cand.sort(key=lambda j: score[j], reverse=True)
            else:                        # "none": fixed index order = no momentum info
                cand.sort()
            rank = {j: r for r, j in enumerate(cand)}
            keep = [j for j in held if rank.get(j, 10**9) < topk + keep_band]
            target = list(keep)
            for j in cand:                       # fill empty slots from strongest
                if len(target) >= topk:
                    break
                if j not in target:
                    target.append(j)
            target_set = set(target[:topk])

            for j in list(held):                 # drop coins rotated out
                if j not in target_set:
                    sell_all(j, price, i)
            if target_set:
                slot = tot / topk                # equal-weight; cash left if < K names
                if reweight:
                    # REBALANCED (the validated backtest): trim/top-up EVERY target
                    # name to the equal slot each rebalance.
                    for j in target_set:             # trim first to free cash
                        d = slot - units[j] * price[j]
                        if d < -tot * 1e-6:
                            qty = min((-d) / price[j], units[j])
                            proceeds = qty * price[j] * (1 - slip); f = proceeds * fee
                            cash += proceeds - f; units[j] -= qty; fees[i] += f; sw[i] += 1
                    for j in target_set:             # then top up
                        d = slot - units[j] * price[j]
                        if d > tot * 1e-6 and cash > 0:
                            spend = min(d, cash); f = spend * fee
                            units[j] += (spend - f) / (price[j] * (1 + slip))
                            cash -= spend; fees[i] += f; sw[i] += 1
                    held = set(target_set)
                else:
                    # WHOLE-POSITION (what live main_loop._rotate actually does): held
                    # winners RUN untouched; only NEW target names are funded, from
                    # cash, strongest-first (so a low-cash book under-fills exactly as
                    # live does). Optional cap_mult trims any name above cap_mult*slot
                    # to bound single-name concentration while still letting it run.
                    if cap_mult is not None:
                        cap_val = cap_mult * slot
                        for j in list(held):
                            over = units[j] * price[j] - cap_val
                            if over > tot * 1e-6 and price[j] > 0:
                                qty = min(over / price[j], units[j])
                                proceeds = qty * price[j] * (1 - slip); f = proceeds * fee
                                cash += proceeds - f; units[j] -= qty; fees[i] += f; sw[i] += 1
                    for j in cand:                   # strongest-first new entries
                        if j in target_set and j not in held and price[j] > 0 and cash > tot * 1e-6:
                            spend = min(slot, cash); f = spend * fee
                            units[j] += (spend - f) / (price[j] * (1 + slip))
                            cash -= spend; fees[i] += f; sw[i] += 1
                            held.add(j)
                    held = {j for j in range(J) if units[j] > 0}

        eq[i] = cash + float(np.sum(units * price))
        expo[i] = float(np.sum(units * price)) / eq[i] if eq[i] > 0 else 0.0

    agg = Run(name, eq, expo, sw, fees)
    agg_bh = Run("Buy & Hold", bh_curve, np.ones(T), np.zeros(T), np.zeros(T))
    return agg, agg_bh, cal.to_numpy()


# --------------------------------------------------------------------------- #
def _parse_floats(s: str) -> list[float]:
    return [float(x) for x in s.split(",") if x.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Profit-taking / signal research (OOS).")
    ap.add_argument("--symbols", type=str, default="BTC,ETH,SOL,XRP,DOGE,ADA,BNB,VET",
                    help="Comma bases. Default = full universe.")
    ap.add_argument("--years", type=float, default=8.0)
    ap.add_argument("--split", type=str, default="2024-06-01")
    ap.add_argument("--exchange", type=str, default="auto")
    ap.add_argument("--topk", type=int, default=3)
    ap.add_argument("--mom-lookback", type=int, default=90)
    ap.add_argument("--rebalance-every", type=int, default=7, help="rotate every N days (C2)")
    ap.add_argument("--keep-band", type=int, default=2, help="hysteresis ranks for C2")
    ap.add_argument("--cap-mult", type=float, default=1.5,
                    help="whole-position concentration cap (trim a name above cap_mult/K)")
    # Stressed cost regime (small-account taker on alts: wider spreads, more slip).
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

    # B (scale-out) defaults come from the live `strategy.profit_taking` block so
    # the research mirrors what main_loop.py would actually do once enabled.
    pt = cfg["strategy"].get("profit_taking", {}) or {}
    tiers = pt.get("tiers") or [{"profit_atr": 1.5, "scale_pct": 0.33},
                                {"profit_atr": 3.0, "scale_pct": 0.33}]
    scale_atr = [float(t["profit_atr"]) for t in tiers]
    scale_pcts = [float(t["scale_pct"]) for t in tiers]
    ratchet = [float(m) for m in (pt.get("ratchet_trail_mults") or [atr_mult, 2.5, 2.0])]
    # D (composite ranking) weights/horizon come from the allocator config.
    mr = cfg["strategy"].get("allocation", {}).get("momentum_rotation", {}) or {}
    comp = mr.get("composite", {}) or {}
    comp_weights = comp.get("weights")
    roc_short = int(comp.get("roc_short_days", 20))

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
    if not bases:
        logger.error("No data for any requested asset.")
        return

    regime_on = build_regime(frames, regime_ma)
    split = np.datetime64(pd.Timestamp(args.split))
    cols = ["total_return_pct", "cagr_pct", "max_dd_pct", "mar", "sharpe", "pct_in_market", "switches"]
    hdr = "".join(f"{h:>9}" for h in ["Ret%", "CAGR%", "MaxDD%", "MAR", "Sharpe", "InMkt%", "Sw"])

    logger.info("Coins {} ({}) | entry {} | trail {}x | regimeMA {} | split {}",
                bases, len(bases), entry, atr_mult, regime_ma, args.split)
    logger.info("Momentum top-{} ({}d). C2 rotates every {}d, keep-band {}.",
                args.topk, args.mom_lookback, args.rebalance_every, args.keep_band)
    logger.info("Costs: nominal fee {:.2%}/slip {:.2%}  |  STRESS fee {:.2%}/slip {:.2%}",
                fee0, slip0, args.stress_fee, args.stress_slip)

    _eq_ts = pd.date_range(max(_idx(frames[b]).min() for b in bases),
                           min(_idx(frames[b]).max() for b in bases), freq="D").to_numpy()

    def _row(label: str, m: dict) -> str:
        return f"  {label:<26}" + "".join(f"{str(m.get(c, '-')):>9}" for c in cols)

    def build(fee: float, slip: float):
        base_runs = {b: baseline_asset(frames[b], entry, atr_mult, regime_on,
                                       capital / len(bases), fee, slip) for b in bases}
        A, A_bh, _ = equalweight_portfolio(base_runs, frames, "A baseline (live logic)")
        # B: staged scale-out + ratcheting chandelier, NO breakeven floor.
        #    This is the originally-validated logic (the +15.2% OOS number).
        scale_runs = {b: scaleout_asset(frames[b], entry, regime_on, capital / len(bases),
                                        fee, slip, scale_pcts, scale_atr, ratchet) for b in bases}
        B, _, _ = equalweight_portfolio(scale_runs, frames, "B scale-out (no BE floor)")
        # B2: SAME, but WITH the live breakeven floor from strategy.profit_taking
        #     (breakeven_after_tier / breakeven_buffer_atr). This is the TRUE logic
        #     main_loop.py runs once profit_taking.enabled=true - judge live on THIS.
        be_after = int(pt.get("breakeven_after_tier", 1))
        be_buf = float(pt.get("breakeven_buffer_atr", 0.5))
        runs_b2 = {b: scaleout_asset(frames[b], entry, regime_on, capital / len(bases),
                                     fee, slip, scale_pcts, scale_atr, ratchet,
                                     breakeven_after_tier=be_after, breakeven_buffer_atr=be_buf)
                   for b in bases}
        B2, _, _ = equalweight_portfolio(runs_b2, frames, f"B2 +BE floor t{be_after} (LIVE)")
        # B3: looser breakeven (arm only after BOTH tranches) - a candidate config
        #     that protects the runner less aggressively, preserving more upside.
        runs_b3 = {b: scaleout_asset(frames[b], entry, regime_on, capital / len(bases),
                                     fee, slip, scale_pcts, scale_atr, ratchet,
                                     breakeven_after_tier=len(scale_pcts), breakeven_buffer_atr=be_buf)
                   for b in bases}
        B3, _, _ = equalweight_portfolio(runs_b3, frames, f"B3 +BE floor t{len(scale_pcts)}")
        C1, C1_bh, cts = momentum_topk(frames, entry, atr_mult, regime_on, capital, fee, slip,
                                       args.topk, args.mom_lookback, "C1 momentum (daily)",
                                       rebalance_every=1, keep_band=0)
        C2, _, _ = momentum_topk(frames, entry, atr_mult, regime_on, capital, fee, slip,
                                 args.topk, args.mom_lookback,
                                 f"C2 rebal ({args.rebalance_every}d+band)",
                                 rebalance_every=args.rebalance_every, keep_band=args.keep_band)
        # C2 whole-position = what live main_loop._rotate ACTUALLY does (held winners
        # run, never trimmed) - judge the LIVE momentum behaviour on THIS row.
        C2WP, _, _ = momentum_topk(frames, entry, atr_mult, regime_on, capital, fee, slip,
                                   args.topk, args.mom_lookback,
                                   f"C2 whole-pos (LIVE)",
                                   rebalance_every=args.rebalance_every, keep_band=args.keep_band,
                                   reweight=False)
        # C2 whole-position + concentration cap (let winners run up to cap_mult/K).
        C2WPC, _, _ = momentum_topk(frames, entry, atr_mult, regime_on, capital, fee, slip,
                                    args.topk, args.mom_lookback,
                                    f"C2 wp+cap{args.cap_mult:g}",
                                    rebalance_every=args.rebalance_every, keep_band=args.keep_band,
                                    reweight=False, cap_mult=args.cap_mult)
        # D: composite ranker (breakout+ROC+rel-strength+inv-vol) vs plain momentum.
        C3, _, _ = momentum_topk(frames, entry, atr_mult, regime_on, capital, fee, slip,
                                 args.topk, args.mom_lookback,
                                 f"C3 composite ({args.rebalance_every}d+band)",
                                 rebalance_every=args.rebalance_every, keep_band=args.keep_band,
                                 rank_mode="composite", roc_short=roc_short, comp_weights=comp_weights)
        return A, A_bh, B, B2, B3, C1, C2, C2WP, C2WPC, C3, cts

    def cost_section(title: str, fee: float, slip: float) -> list[str]:
        A, A_bh, B, B2, B3, C1, C2, C2WP, C2WPC, C3, cts = build(fee, slip)
        out = ["", "#" * 100, f"  {title}  (fee {fee:.2%}/side, slip {slip:.2%})", "#" * 100]
        for wname, mask_of in (("OUT-OF-SAMPLE (judge here)", lambda ts: ts > split),
                               ("FULL PERIOD", lambda ts: np.ones(len(ts), bool))):
            out += ["", "=" * 100, f"  {wname}", "=" * 100, f"  {'Config':<26}{hdr}"]
            out.append(_row(A.name, metrics(A, mask_of(_eq_ts), A_bh.equity)))
            out.append(_row("   Buy & Hold (eq-wt)", metrics(A_bh, mask_of(_eq_ts), A_bh.equity)))
            out.append(_row(B.name, metrics(B, mask_of(_eq_ts), A_bh.equity)))
            out.append(_row(B2.name, metrics(B2, mask_of(_eq_ts), A_bh.equity)))
            out.append(_row(B3.name, metrics(B3, mask_of(_eq_ts), A_bh.equity)))
            out.append(_row(C1.name, metrics(C1, mask_of(cts), A_bh.equity)))
            out.append(_row(C2.name, metrics(C2, mask_of(cts), A_bh.equity)))
            out.append(_row(C2WP.name, metrics(C2WP, mask_of(cts), A_bh.equity)))
            out.append(_row(C2WPC.name, metrics(C2WPC, mask_of(cts), A_bh.equity)))
            out.append(_row(C3.name, metrics(C3, mask_of(cts), A_bh.equity)))
            out.append("=" * 100)
        return out

    lines = cost_section("NOMINAL COSTS", fee0, slip0)
    lines += cost_section("STRESSED COSTS", args.stress_fee, args.stress_slip)
    report = "\n".join(lines)
    print(report)

    os.makedirs(BACKTEST_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(BACKTEST_DIR, f"profit_taking_{stamp}.txt"), "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    logger.info("Saved profit_taking_{}.txt to {}. OOS is the column that matters.", stamp, BACKTEST_DIR)


if __name__ == "__main__":
    main()