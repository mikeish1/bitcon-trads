"""
=============================================================================
 REGIME / DEFENSIVE RESEARCH BACKTESTER  (read-only; live code untouched)
=============================================================================
Validates the "default-invested, step-aside-on-regime-breakdown" thesis on
DAILY BTC, against three honest baselines, with an in-sample / out-of-sample
split and risk-adjusted metrics.

Strategies compared:
  1. Buy & Hold (lump sum)
  2. DCA (ease-in over N weeks, then hold)  - fairer for a small account
  3. 200-day MA filter  (hold BTC when close > MA, cash when < MA*(1-buffer))
  4. Regime allocator   (MA filter + optional rising-slope gate + chandelier exit)

Metrics (in-sample / out-of-sample / full): total return, CAGR, max drawdown,
MAR (CAGR/|maxDD|), Sharpe (daily, annualized), % time in market, # switches,
fee+slippage drag, and up/down capture vs buy & hold.

HOW TO RUN
    python src/regime_backtester.py
    python src/regime_backtester.py --years 8 --split 2024-06-01

First run downloads daily BTC (public, no keys) and caches to
backtests/BTC_1d.csv. This is RESEARCH ONLY - it never trades and never touches
the live trading_state.db or any account.
=============================================================================
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import ccxt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import ta  # noqa: E402
from loguru import logger  # noqa: E402

from src.config import load_config  # noqa: E402
from src.regime import get_regime_state  # noqa: E402

BACKTEST_DIR = os.path.join(_PROJECT_ROOT, "backtests")
DEFAULT_CSV = os.path.join(BACKTEST_DIR, "BTC_1d.csv")


# --------------------------------------------------------------------------- #
# Data                                                                         #
# --------------------------------------------------------------------------- #
def _sources(symbol: str) -> list[tuple[str, str]]:
    btc_usd = "BTC/USD" if symbol.upper().startswith("BTC") else symbol
    # Prefer venues whose ccxt daily pagination is well-behaved (verified earlier).
    return [("binanceus", symbol), ("okx", symbol), ("kraken", symbol),
            ("coinbase", btc_usd), ("binance", symbol)]


def _paginate_daily(ex, symbol: str, years: float) -> pd.DataFrame:
    tf_ms = ex.parse_timeframe("1d") * 1000
    now = ex.milliseconds()
    since = now - int(years * 365 * 24 * 60 * 60 * 1000)
    rows: list[list] = []
    for _ in range(120):
        batch = ex.fetch_ohlcv(symbol, "1d", since=since, limit=300)
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1][0] + tf_ms
        if nxt <= since or batch[-1][0] >= now:
            break
        since = nxt
        time.sleep(ex.rateLimit / 1000)
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df


def download_daily(symbol: str, years: float, exchange_id: str) -> pd.DataFrame:
    src = _sources(symbol) if exchange_id == "auto" else [(exchange_id, symbol)]
    last_err = None
    for ex_id, sym in src:
        try:
            ex = getattr(ccxt, ex_id)({"enableRateLimit": True})
            ex.fetch_ohlcv(sym, "1d", limit=5)
        except Exception as exc:
            last_err = exc
            logger.warning("{} unavailable ({}); next.", ex_id, str(exc).splitlines()[0][:60])
            continue
        logger.info("Downloading ~{}y daily {} from {}...", years, sym, ex_id)
        df = _paginate_daily(ex, sym, years)
        if len(df) > 400:
            logger.info("Got {} daily candles from {} (since {:%Y-%m-%d}).",
                        len(df), ex_id, df.iloc[0]["timestamp"])
            return df
    raise RuntimeError(f"Could not download daily data. Last error: {last_err}")


def get_data(args) -> pd.DataFrame:
    os.makedirs(BACKTEST_DIR, exist_ok=True)
    if args.csv:
        df = pd.read_csv(args.csv)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = df[c].astype(float)
        return df.sort_values("timestamp").reset_index(drop=True)
    if os.path.exists(DEFAULT_CSV):
        logger.info("Using cached daily data {} (delete to refresh).", DEFAULT_CSV)
        df = pd.read_csv(DEFAULT_CSV)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df
    df = download_daily(args.symbol or "BTC/USDT", args.years, args.exchange)
    df.to_csv(DEFAULT_CSV, index=False)
    return df


# --------------------------------------------------------------------------- #
# Strategy simulators - each returns aligned per-day arrays                    #
# --------------------------------------------------------------------------- #
@dataclass
class Run:
    name: str
    equity: np.ndarray      # $ value per day (mark-to-market)
    exposure: np.ndarray    # 1.0 if holding BTC at day close, else 0.0
    switch: np.ndarray      # 1.0 on days a buy/sell happened
    fees: np.ndarray        # $ fees paid that day


def buy_hold(close: np.ndarray, capital: float, fee: float, slip: float) -> Run:
    eq = np.empty(len(close))
    entry = close[0] * (1 + slip)
    f0 = capital * fee
    btc = (capital - f0) / entry
    eq[:] = btc * close
    exp = np.ones(len(close))
    sw = np.zeros(len(close)); sw[0] = 1
    fees = np.zeros(len(close)); fees[0] = f0
    return Run("Buy & Hold", eq, exp, sw, fees)


def dca(close: np.ndarray, ts: np.ndarray, capital: float, fee: float, slip: float,
        weeks: int = 12) -> Run:
    """Ease in: invest capital/weeks once per 7 days for `weeks` buys, then hold."""
    eq = np.empty(len(close)); cash = capital; btc = 0.0
    sw = np.zeros(len(close)); fees = np.zeros(len(close)); exp = np.zeros(len(close))
    tranche = capital / weeks
    done = 0
    last_buy_day = -7
    for i in range(len(close)):
        if done < weeks and (i - last_buy_day) >= 7 and cash > 0:
            spend = min(tranche, cash)
            f = spend * fee
            btc += (spend - f) / (close[i] * (1 + slip))
            cash -= spend
            fees[i] += f; sw[i] = 1; done += 1; last_buy_day = i
        eq[i] = cash + btc * close[i]
        exp[i] = 1.0 if btc * close[i] > cash else (btc * close[i]) / max(eq[i], 1e-9)
    return Run(f"DCA ({weeks}-wk ease-in)", eq, exp, sw, fees)


def regime(close: np.ndarray, high: np.ndarray, ma: np.ndarray, atr: np.ndarray,
           slope_ok: np.ndarray, capital: float, fee: float, slip: float,
           buffer: float, use_slope: bool, chandelier_mult: float, name: str) -> Run:
    """
    State machine on daily closes:
      ENTER (go fully invested) when close > MA  (and rising-slope if enabled).
      EXIT to cash when close < MA*(1-buffer), OR (chandelier) close has fallen
      chandelier_mult*ATR below the highest close since entry.
    chandelier_mult <= 0 disables the chandelier (-> plain MA filter).
    """
    eq = np.empty(len(close)); cash = capital; btc = 0.0; invested = False
    peak = 0.0
    sw = np.zeros(len(close)); fees = np.zeros(len(close)); exp = np.zeros(len(close))
    for i in range(len(close)):
        m = ma[i]
        if not np.isnan(m):
            if not invested:
                want = close[i] > m and (slope_ok[i] if use_slope else True)
                if want and cash > 0:
                    f = cash * fee
                    btc = (cash - f) / (close[i] * (1 + slip))
                    cash = 0.0; invested = True; peak = close[i]
                    fees[i] += f; sw[i] = 1
            else:
                peak = max(peak, close[i])
                hard = close[i] < m * (1 - buffer)
                chand = chandelier_mult > 0 and atr[i] == atr[i] and \
                    close[i] < peak - chandelier_mult * atr[i]
                if hard or chand:
                    f = btc * close[i] * (1 - slip) * fee
                    cash = btc * close[i] * (1 - slip) - f
                    btc = 0.0; invested = False
                    fees[i] += f; sw[i] = 1
        eq[i] = cash + btc * close[i]
        exp[i] = 1.0 if invested else 0.0
    return Run(name, eq, exp, sw, fees)


def regime_module(df: pd.DataFrame, capital: float, fee: float, slip: float,
                  method: str, params: dict, name: str) -> Run:
    """Drive a long/flat BTC equity curve from the LIVE `src.regime` gate, evaluated
    bar-by-bar on a trailing window (no lookahead). This exercises the exact
    `get_regime_state` used by main_loop, so the research metrics reflect the
    shipped module rather than a re-implementation. Risk-on -> hold BTC; risk-off
    (size_factor 0) -> cash."""
    close = df["close"].to_numpy()
    n = len(close)
    # Trailing window big enough for the longest indicator the gate needs.
    win = int(params.get("ma_period", 100)) + int(params.get("slope_lookback", 20)) \
        + int(params.get("vol_period", 20)) + 5
    risk_on = np.zeros(n)
    for i in range(n):
        sub = df.iloc[max(0, i - win):i + 1]
        risk_on[i] = 1.0 if get_regime_state(sub, method=method, params=params).risk_on else 0.0

    eq = np.empty(n); cash = capital; btc = 0.0; invested = False
    sw = np.zeros(n); fees = np.zeros(n); exp = np.zeros(n)
    for i in range(n):
        want = risk_on[i] > 0
        if want and not invested and cash > 0:
            f = cash * fee
            btc = (cash - f) / (close[i] * (1 + slip)); cash = 0.0; invested = True
            fees[i] += f; sw[i] = 1
        elif not want and invested:
            f = btc * close[i] * (1 - slip) * fee
            cash = btc * close[i] * (1 - slip) - f; btc = 0.0; invested = False
            fees[i] += f; sw[i] = 1
        eq[i] = cash + btc * close[i]
        exp[i] = 1.0 if invested else 0.0
    return Run(name, eq, exp, sw, fees)


# --------------------------------------------------------------------------- #
# Metrics                                                                      #
# --------------------------------------------------------------------------- #
def metrics(run: Run, mask: np.ndarray, bh_close: np.ndarray) -> dict[str, Any]:
    eq = run.equity[mask]
    if len(eq) < 5:
        return {}
    eq = eq / eq[0]                       # rebase to the window start
    rets = np.diff(eq) / eq[:-1]
    total = (eq[-1] - 1) * 100
    n = len(eq)
    cagr = ((eq[-1]) ** (365.0 / n) - 1) * 100
    peak = np.maximum.accumulate(eq)
    maxdd = float(((eq - peak) / peak).min() * 100)
    sharpe = (rets.mean() / rets.std() * np.sqrt(365)) if rets.std() > 0 else 0.0
    mar = (cagr / abs(maxdd)) if maxdd < 0 else float("inf")
    # up/down capture vs buy & hold daily returns (approximate)
    bh = bh_close[mask]
    bh_ret = np.diff(bh) / bh[:-1]
    up = bh_ret > 0; dn = bh_ret < 0
    up_cap = (rets[up].sum() / bh_ret[up].sum() * 100) if bh_ret[up].sum() > 0 else 0.0
    dn_cap = (rets[dn].sum() / bh_ret[dn].sum() * 100) if bh_ret[dn].sum() < 0 else 0.0
    return {
        "total_return_pct": round(total, 1),
        "cagr_pct": round(cagr, 1),
        "max_dd_pct": round(maxdd, 1),
        "mar": round(mar, 2) if mar != float("inf") else "inf",
        "sharpe": round(float(sharpe), 2),
        "pct_in_market": round(float(run.exposure[mask].mean() * 100), 1),
        "switches": int(run.switch[mask].sum()),
        "fee_drag_pct": round(float(run.fees[mask].sum() / run.equity[mask][0] * 100), 2),
        "up_capture_pct": round(float(up_cap), 0),
        "down_capture_pct": round(float(dn_cap), 0),
    }


def print_block(title: str, runs: list[Run], mask: np.ndarray, bh_close: np.ndarray) -> str:
    cols = ["total_return_pct", "cagr_pct", "max_dd_pct", "mar", "sharpe",
            "pct_in_market", "switches", "up_capture_pct", "down_capture_pct"]
    hdr = ["Return%", "CAGR%", "MaxDD%", "MAR", "Sharpe", "InMkt%", "Sw", "Up%", "Dn%"]
    lines = ["", "=" * 104, f"  {title}", "=" * 104,
             f"  {'Strategy':<26}" + "".join(f"{h:>9}" for h in hdr)]
    for r in runs:
        m = metrics(r, mask, bh_close)
        if not m:
            continue
        lines.append(f"  {r.name:<26}" + "".join(f"{str(m[c]):>9}" for c in cols))
    lines.append("=" * 104)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Regime/defensive research backtester.")
    ap.add_argument("--years", type=float, default=8.0)
    ap.add_argument("--split", type=str, default="2024-06-01", help="In-sample/OOS boundary (UTC).")
    ap.add_argument("--exchange", type=str, default="auto")
    ap.add_argument("--symbol", type=str, default=None)
    ap.add_argument("--csv", type=str, default=None)
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")

    cfg = load_config()
    fee = cfg["execution"]["taker_fee_pct"]
    slip = cfg["execution"]["paper_slippage_pct"]
    capital = cfg["risk"]["default_capital_usd"]

    df = get_data(args)
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    # Normalize timestamps to naive UTC datetime64 so numpy comparisons work.
    ts = pd.DatetimeIndex(df["timestamp"]).tz_convert("UTC").tz_localize(None).to_numpy()
    atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14).to_numpy()

    split = np.datetime64(pd.Timestamp(args.split))
    is_mask = ts <= split
    oos_mask = ts > split
    full_mask = np.ones(len(ts), dtype=bool)
    logger.info("Data {:%Y-%m-%d} -> {:%Y-%m-%d}  ({} days). Split at {}.",
                df.iloc[0]["timestamp"], df.iloc[-1]["timestamp"], len(df), args.split)
    logger.info("Fees {:.2%}/side, slippage {:.2%}, capital ${:.0f}.", fee, slip, capital)

    bh = buy_hold(close, capital, fee, slip)

    # --- tune the regime/MA params on IN-SAMPLE only (tiny grid) -------------
    best = None
    for ma_p in (150, 200):
        ma = df["close"].rolling(ma_p).mean().to_numpy()
        slope_ok = np.full(len(close), False)
        slope_ok[ma_p + 20:] = ma[ma_p + 20:] > ma[ma_p:-20]
        for buf in (0.02, 0.03, 0.05):
            r = regime(close, high, ma, atr, slope_ok, capital, fee, slip,
                       buf, False, 0.0, f"MA{ma_p} buf{int(buf*100)}%")
            m = metrics(r, is_mask, close)
            score = m.get("mar", 0)
            score = -999 if score == "inf" else score
            if best is None or score > best[0]:
                best = (score, ma_p, buf, ma, slope_ok)
    _, ma_p, buf, ma, slope_ok = best
    logger.info("In-sample best: MA={} buffer={:.0%} (by MAR).", ma_p, buf)

    # --- build the four contenders with the chosen params -------------------
    ma_filter = regime(close, high, ma, atr, slope_ok, capital, fee, slip,
                       buf, False, 0.0, f"MA{ma_p} filter (buf {int(buf*100)}%)")
    regime_chand = regime(close, high, ma, atr, slope_ok, capital, fee, slip,
                          buf, False, 3.0, "Regime + chandelier")
    regime_slope = regime(close, high, ma, atr, slope_ok, capital, fee, slip,
                          buf, True, 3.0, "Regime + slope + chand")
    dca_run = dca(close, ts, capital, fee, slip, weeks=12)

    # Matured regime gate from the LIVE src/regime.py module (ma_slope + composite).
    mod_params = {"ma_period": ma_p, "slope_lookback": 20, "vol_period": 20,
                  "vol_ceiling": 0.05, "score_threshold": 0.5}
    mod_slope = regime_module(df, capital, fee, slip, "ma_slope", mod_params,
                              "Regime module (ma_slope)")
    mod_comp = regime_module(df, capital, fee, slip, "composite", mod_params,
                             "Regime module (composite)")

    runs = [bh, dca_run, ma_filter, regime_chand, regime_slope, mod_slope, mod_comp]

    out = []
    out.append(print_block("IN-SAMPLE  (tuning window)", runs, is_mask, close))
    out.append(print_block("OUT-OF-SAMPLE  (the honest test - judge here)", runs, oos_mask, close))
    out.append(print_block("FULL PERIOD", runs, full_mask, close))
    report = "\n".join(out)
    print(report)

    os.makedirs(BACKTEST_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(BACKTEST_DIR, f"regime_compare_{stamp}.txt"), "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    pd.DataFrame({"timestamp": df["timestamp"], **{r.name: r.equity for r in runs}}).to_csv(
        os.path.join(BACKTEST_DIR, f"regime_equity_{stamp}.csv"), index=False)
    logger.info("Saved regime_compare / regime_equity to {} (suffix {}).", BACKTEST_DIR, stamp)


if __name__ == "__main__":
    main()
