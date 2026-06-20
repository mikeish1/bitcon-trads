"""
=============================================================================
 BACKTESTER - replay the ensemble strategy over years of historical data
=============================================================================

WHAT THIS DOES
--------------
It takes the SAME 31-path ensemble brain the live bot uses and "rewinds the
tape": it feeds it years of 5-minute BTC/USDT candles, one bar at a time, and
simulates every trade it WOULD have made - tracking equity, win rate, profit
factor, drawdown, and how often it stays flat.

Use this BEFORE going live to get a feel for how the strategy behaves. A good
backtest is reassuring but NOT a promise of future profit - markets change.

HOW TO RUN (basic user - just do this)
--------------------------------------
From the project folder, in your terminal:

    python src/backtester.py

The first run downloads ~2 years of history from Binance (public data, no API
keys needed) and saves it to  backtests/BTCUSDT_5m.csv  so future runs are
instant. When it finishes it prints a summary table and saves the full results
into the  backtests/  folder.

OPTIONAL EXTRAS (only if you want them)
---------------------------------------
    python src/backtester.py --years 3        # download/use 3 years instead of 2
    python src/backtester.py --csv my.csv     # use your own CSV file
    python src/backtester.py --capital 5000   # start the simulation with $5,000

Your CSV (if you supply one) needs these columns:
    timestamp, open, high, low, close, volume
(timestamp may be milliseconds or an ISO date string.)

IMPORTANT - HONEST NOTE ABOUT CLAUDE
------------------------------------
The live bot occasionally asks 3 Claude "experts" to break a borderline tie.
Replaying that over years of candles would mean thousands of paid API calls, so
the backtest treats those 3 votes as ABSTAIN (0). In practice that means the
backtest only opens a trade when ALL 28 technical models agree (28 of 31) - the
strictest, most conservative version of the live behaviour. Real live trading
can occasionally take a 28/31 that includes 1-2 Claude votes, so live is a
slight superset of what you see here.

Nothing in this file touches your live database or places any orders. It is a
pure, read-only simulation.
=============================================================================
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# --- Make "from src.x import ..." work even when run as `python src/backtester.py` ---
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import ccxt  # noqa: E402
import pandas as pd  # noqa: E402
from loguru import logger  # noqa: E402

from src.config import load_config  # noqa: E402
from src.data_pipeline import DataPipeline  # noqa: E402
from src.ensemble_engine import EnsembleEngine  # noqa: E402

BACKTEST_DIR = os.path.join(_PROJECT_ROOT, "backtests")
DEFAULT_CSV = os.path.join(BACKTEST_DIR, "BTCUSDT_5m.csv")


# --------------------------------------------------------------------------- #
# Data loading                                                                #
# --------------------------------------------------------------------------- #
# Reachable data sources, tried in order when --exchange is "auto".
# binance.com is geo-blocked in some regions (e.g. the US -> HTTP 451), so we
# fall back to mirrors that carry the same 5-minute history.
def _fallback_sources(symbol: str) -> list[tuple[str, str]]:
    btc_usd = "BTC/USD" if symbol.upper().startswith("BTC") else symbol
    return [
        ("binance", symbol),     # best history; often blocked in the US
        ("binanceus", symbol),   # US-friendly mirror, same BTC/USDT symbol
        ("okx", symbol),
        ("kraken", symbol),
        ("coinbase", btc_usd),   # BTC/USD (USDT not listed) - close enough
    ]


def _paginate(exchange, symbol: str, timeframe: str, years: float) -> pd.DataFrame:
    """Page forward `years` of candles in chunks. Pure fetch; caches nothing."""
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    now_ms = exchange.milliseconds()
    since = now_ms - int(years * 365 * 24 * 60 * 60 * 1000)

    rows: list[list] = []
    while since < now_ms:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        except Exception as exc:
            logger.warning("Fetch hiccup ({}); retrying in 3s...", str(exc).splitlines()[0][:80])
            time.sleep(3)
            continue
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1][0] + tf_ms
        if nxt <= since:           # no forward progress -> stop (avoid infinite loop)
            break
        since = nxt
        last_dt = datetime.fromtimestamp(batch[-1][0] / 1000, tz=timezone.utc)
        print(f"  ...{len(rows):>8,} candles (up to {last_dt:%Y-%m-%d})", end="\r")
        time.sleep(exchange.rateLimit / 1000)
    print()  # newline after the progress line

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df


def download_history(symbol: str, timeframe: str, years: float,
                     exchange_id: str = "auto") -> pd.DataFrame:
    """
    Download `years` of OHLCV candles (public data, no API keys).

    If `exchange_id` is "auto", try a list of reachable exchanges and use the
    first that responds (handy because binance.com is blocked in some regions,
    e.g. the US). Otherwise use exactly the exchange you name. Can take a couple
    of minutes the first time - that's normal. Cached to CSV by the caller.
    """
    sources = _fallback_sources(symbol) if exchange_id == "auto" else [(exchange_id, symbol)]
    last_err: Exception | None = None
    for ex_id, sym in sources:
        try:
            exchange = getattr(ccxt, ex_id)({"enableRateLimit": True})
            exchange.fetch_ohlcv(sym, timeframe, limit=5)  # quick reachability probe
        except Exception as exc:
            last_err = exc
            logger.warning("{} unavailable ({}); trying next source...",
                           ex_id, str(exc).splitlines()[0][:70])
            continue
        logger.info("Downloading ~{} years of {} {} from {}...", years, sym, timeframe, ex_id)
        df = _paginate(exchange, sym, timeframe, years)
        if len(df) > 100:
            logger.info("Downloaded {} candles from {}.", len(df), ex_id)
            return df
    raise RuntimeError(f"Could not download data from any source. Last error: {last_err}")


def load_csv(path: str) -> pd.DataFrame:
    """Load a user-supplied or cached CSV of candles."""
    df = pd.read_csv(path)
    # Accept either a millisecond integer timestamp or an ISO date string.
    if pd.api.types.is_numeric_dtype(df["timestamp"]):
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    else:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    return df


def get_data(args, cfg) -> pd.DataFrame:
    """Decide where candles come from: explicit CSV, cached CSV, or fresh download."""
    os.makedirs(BACKTEST_DIR, exist_ok=True)
    symbol = args.symbol or cfg["market"]["symbol"]
    timeframe = cfg["market"]["timeframe"]

    if args.csv:
        logger.info("Loading candles from {}", args.csv)
        return load_csv(args.csv)

    if os.path.exists(DEFAULT_CSV):
        logger.info("Using cached candles at {} (delete it to re-download).", DEFAULT_CSV)
        return load_csv(DEFAULT_CSV)

    df = download_history(symbol, timeframe, args.years, args.exchange)
    df.to_csv(DEFAULT_CSV, index=False)
    logger.info("Cached candles to {}", DEFAULT_CSV)
    return df


# --------------------------------------------------------------------------- #
# Simulated portfolio                                                         #
# Mirrors src/risk_manager.py formulas exactly, but uses BAR time (not the     #
# wall clock) so cooldowns and daily/weekly limits replay correctly.          #
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    side: str
    entry_time: pd.Timestamp
    entry: float
    qty: float
    notional: float
    stop: float
    take: float
    exit_time: Optional[pd.Timestamp] = None
    exit: Optional[float] = None
    pnl: float = 0.0
    reason: str = ""


class SimPortfolio:
    def __init__(self, cfg: dict[str, Any], starting_capital: float):
        risk, safety, ex = cfg["risk"], cfg["safety"], cfg["execution"]
        self.max_risk_per_trade = risk["max_risk_per_trade"]
        self.kelly_fraction = risk["kelly_fraction"]
        self.kelly_payoff = risk["kelly_assumed_payoff"]
        self.min_trade_usd = risk["min_trade_usd"]
        self.max_position_pct = risk["max_position_pct"]
        self.stop_loss_pct = risk["stop_loss_pct"]
        self.take_profit_pct = risk["take_profit_pct"]

        self.daily_loss_limit = safety["daily_loss_limit_pct"]
        self.weekly_loss_limit = safety["weekly_loss_limit_pct"]
        self.max_consec_losses = safety["max_consecutive_losses"]
        self.cooldown_minutes = safety["cooldown_minutes"]

        self.fee_pct = ex["taker_fee_pct"]
        self.slippage_pct = ex["paper_slippage_pct"]

        self.equity = starting_capital
        self.starting_equity = starting_capital
        self.day_start_equity = starting_capital
        self.week_start_equity = starting_capital
        self.day_date: Optional[str] = None
        self.week_id: Optional[str] = None
        self.consecutive_losses = 0
        self.wins = 0
        self.losses = 0
        self.last_close_ts: Optional[pd.Timestamp] = None

        self.position: Optional[Trade] = None
        self.closed_trades: list[Trade] = []

    # --- Kelly sizing (identical formula to RiskManager.compute_position) ---
    def _dynamic_win_rate(self) -> float:
        total = self.wins + self.losses
        return 0.5 if total < 10 else self.wins / total

    def compute_position(self, price: float) -> dict[str, Any]:
        win = self._dynamic_win_rate()
        kelly_star = max(win - (1.0 - win) / self.kelly_payoff, 0.0)
        risk_fraction = min(self.kelly_fraction * kelly_star, self.max_risk_per_trade)
        risk_fraction = max(risk_fraction, self.max_risk_per_trade * 0.25)
        notional = self.equity * risk_fraction / max(self.stop_loss_pct, 1e-6)
        notional = min(notional, self.equity * self.max_position_pct)
        qty = notional / price if price > 0 else 0.0
        return {"risk_fraction": risk_fraction, "notional": notional, "qty": qty,
                "viable": notional >= self.min_trade_usd}

    def stop_and_target(self, side: str, entry: float) -> tuple[float, float]:
        if side == "LONG":
            return entry * (1 - self.stop_loss_pct), entry * (1 + self.take_profit_pct)
        return entry * (1 + self.stop_loss_pct), entry * (1 - self.take_profit_pct)

    # --- Safety rails (same logic as RiskManager, bar-time driven) ---
    def _roll_periods(self, ts: pd.Timestamp) -> None:
        d = ts.date().isoformat()
        if self.day_date != d:
            self.day_date = d
            self.day_start_equity = self.equity
        wk = f"{ts.isocalendar().year}-{ts.isocalendar().week}"
        if self.week_id != wk:
            self.week_id = wk
            self.week_start_equity = self.equity

    def can_open_trade(self, ts: pd.Timestamp) -> tuple[bool, str]:
        self._roll_periods(ts)
        if self.day_start_equity > 0 and \
                (self.equity - self.day_start_equity) / self.day_start_equity <= -self.daily_loss_limit:
            return False, "daily loss limit"
        if self.week_start_equity > 0 and \
                (self.equity - self.week_start_equity) / self.week_start_equity <= -self.weekly_loss_limit:
            return False, "weekly loss limit"
        if self.consecutive_losses >= self.max_consec_losses:
            return False, "circuit breaker"
        if self.last_close_ts is not None:
            mins = (ts - self.last_close_ts).total_seconds() / 60
            if mins < self.cooldown_minutes:
                return False, "cooldown"
        return True, "ok"

    # --- Open / close ---
    def open(self, side: str, ts: pd.Timestamp, close_price: float) -> None:
        sizing = self.compute_position(close_price)
        if not sizing["viable"]:
            return
        entry = close_price * (1 + self.slippage_pct) if side == "LONG" \
            else close_price * (1 - self.slippage_pct)
        stop, take = self.stop_and_target(side, entry)
        self.position = Trade(
            side=side, entry_time=ts, entry=entry, qty=sizing["qty"],
            notional=sizing["notional"], stop=stop, take=take,
        )

    def close(self, ts: pd.Timestamp, exit_price: float, reason: str) -> None:
        p = self.position
        if p is None:
            return
        gross = (exit_price - p.entry) * p.qty if p.side == "LONG" else (p.entry - exit_price) * p.qty
        fees = (p.entry + exit_price) * p.qty * self.fee_pct
        p.pnl = gross - fees
        p.exit_time, p.exit, p.reason = ts, exit_price, reason
        self.equity += p.pnl
        self.last_close_ts = ts
        if p.pnl >= 0:
            self.wins += 1
            self.consecutive_losses = 0
        else:
            self.losses += 1
            self.consecutive_losses += 1
        self.closed_trades.append(p)
        self.position = None


# --------------------------------------------------------------------------- #
# The backtest loop                                                           #
# --------------------------------------------------------------------------- #
@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: pd.DataFrame
    signal_bars: int
    eval_bars: int
    in_market_bars: int
    start: pd.Timestamp
    end: pd.Timestamp
    starting_equity: float
    final_equity: float
    buy_hold_return_pct: float
    summary: dict[str, Any] = field(default_factory=dict)


def run_backtest(df: pd.DataFrame, cfg: dict[str, Any], starting_capital: float) -> BacktestResult:
    logger.info("Computing indicators on {} candles...", len(df))
    df = DataPipeline.add_indicators(df.copy())

    # Engine WITHOUT Claude -> the 3 expert votes abstain (see header note).
    engine = EnsembleEngine(cfg, claude_orchestrator=None)
    trade_threshold = cfg["ensemble"]["trade_threshold"]

    port = SimPortfolio(cfg, starting_capital)

    warmup = 210            # let the slowest indicator (EMA-200) settle
    window = 64             # rows handed to the engine (>= its 60-row minimum)
    start_i = max(warmup, window)

    equity_times: list[pd.Timestamp] = []
    equity_vals: list[float] = []
    signal_bars = in_market_bars = eval_bars = 0

    logger.info("Replaying {} bars (this can take a few minutes)...", len(df) - start_i)
    for i in range(start_i, len(df)):
        bar = df.iloc[i]
        ts = bar["timestamp"]
        eval_bars += 1

        sub = df.iloc[i - window: i + 1]
        decision = engine.decide(sub)
        if decision.direction in ("LONG", "SHORT") and decision.agreement >= trade_threshold:
            signal_bars += 1

        # 1) Manage an existing position FIRST (entered on an earlier bar).
        if port.position is not None:
            p = port.position
            high, low = float(bar["high"]), float(bar["low"])
            if p.side == "LONG":
                if low <= p.stop:
                    port.close(ts, p.stop, "stop-loss")
                elif high >= p.take:
                    port.close(ts, p.take, "take-profit")
                elif decision.direction == "SHORT":
                    port.close(ts, float(bar["close"]), "ensemble reversal")
            else:  # SHORT
                if high >= p.stop:
                    port.close(ts, p.stop, "stop-loss")
                elif low <= p.take:
                    port.close(ts, p.take, "take-profit")
                elif decision.direction == "LONG":
                    port.close(ts, float(bar["close"]), "ensemble reversal")

        # 2) If flat, consider a new entry on this bar's close.
        if port.position is None and decision.direction in ("LONG", "SHORT"):
            allowed, _ = port.can_open_trade(ts)
            if allowed:
                port.open(decision.direction, ts, float(bar["close"]))

        if port.position is not None:
            in_market_bars += 1
        equity_times.append(ts)
        equity_vals.append(port.equity)

    # Close any position still open at the very end, at the last close price.
    if port.position is not None:
        last = df.iloc[-1]
        port.close(last["timestamp"], float(last["close"]), "end-of-backtest")
        equity_vals[-1] = port.equity

    equity_curve = pd.DataFrame({"timestamp": equity_times, "equity": equity_vals})
    first_close, last_close = float(df.iloc[start_i]["close"]), float(df.iloc[-1]["close"])
    buy_hold = (last_close / first_close - 1) * 100

    return BacktestResult(
        trades=port.closed_trades,
        equity_curve=equity_curve,
        signal_bars=signal_bars,
        eval_bars=eval_bars,
        in_market_bars=in_market_bars,
        start=df.iloc[start_i]["timestamp"],
        end=df.iloc[-1]["timestamp"],
        starting_equity=starting_capital,
        final_equity=port.equity,
        buy_hold_return_pct=buy_hold,
    )


# --------------------------------------------------------------------------- #
# Metrics + reporting                                                         #
# --------------------------------------------------------------------------- #
def _max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = (equity - peak) / peak
    return float(dd.min() * 100) if len(dd) else 0.0


def compute_summary(res: BacktestResult) -> dict[str, Any]:
    trades = res.trades
    n = len(trades)
    wins = [t for t in trades if t.pnl >= 0]
    losses = [t for t in trades if t.pnl < 0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    win_rate = (len(wins) / n * 100) if n else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    total_return = (res.final_equity / res.starting_equity - 1) * 100
    days = max((res.end - res.start).total_seconds() / 86400, 1)
    years = days / 365.25
    cagr = ((res.final_equity / res.starting_equity) ** (1 / years) - 1) * 100 if years > 0 else 0.0

    flat_pct = (1 - res.in_market_bars / res.eval_bars) * 100 if res.eval_bars else 100.0
    signal_freq = (res.signal_bars / res.eval_bars * 100) if res.eval_bars else 0.0
    no_signal_flat = (1 - res.signal_bars / res.eval_bars) * 100 if res.eval_bars else 100.0

    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0
    expectancy = (sum(t.pnl for t in trades) / n) if n else 0.0

    return {
        "period_start": res.start.strftime("%Y-%m-%d %H:%M UTC"),
        "period_end": res.end.strftime("%Y-%m-%d %H:%M UTC"),
        "years_tested": round(years, 2),
        "bars_evaluated": res.eval_bars,
        "starting_equity": round(res.starting_equity, 2),
        "final_equity": round(res.final_equity, 2),
        "total_return_pct": round(total_return, 2),
        "cagr_pct": round(cagr, 2),
        "buy_hold_return_pct": round(res.buy_hold_return_pct, 2),
        "max_drawdown_pct": round(_max_drawdown(res.equity_curve["equity"]), 2),
        "total_trades": n,
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
        "avg_win_usd": round(avg_win, 2),
        "avg_loss_usd": round(avg_loss, 2),
        "expectancy_per_trade_usd": round(expectancy, 2),
        "high_consensus_signal_bars": res.signal_bars,
        "signal_frequency_pct": round(signal_freq, 2),
        "pct_time_flat_no_signal": round(no_signal_flat, 2),
        "pct_time_flat_no_position": round(flat_pct, 2),
    }


def render_table(summary: dict[str, Any]) -> str:
    labels = {
        "period_start": "Period start",
        "period_end": "Period end",
        "years_tested": "Years tested",
        "bars_evaluated": "Bars evaluated",
        "starting_equity": "Starting equity ($)",
        "final_equity": "Final equity ($)",
        "total_return_pct": "Total return (%)",
        "cagr_pct": "CAGR (%)",
        "buy_hold_return_pct": "Buy & hold return (%)",
        "max_drawdown_pct": "Max drawdown (%)",
        "total_trades": "Total trades",
        "win_rate_pct": "Win rate (%)",
        "profit_factor": "Profit factor",
        "avg_win_usd": "Avg win ($)",
        "avg_loss_usd": "Avg loss ($)",
        "expectancy_per_trade_usd": "Expectancy / trade ($)",
        "high_consensus_signal_bars": "High-consensus (>=28/31) signal bars",
        "signal_frequency_pct": "Signal frequency (% of bars)",
        "pct_time_flat_no_signal": "% time flat (no high-consensus signal)",
        "pct_time_flat_no_position": "% time flat (no open position)",
    }
    width = max(len(v) for v in labels.values())
    lines = ["", "=" * (width + 22), "  BACKTEST SUMMARY", "=" * (width + 22)]
    for key, label in labels.items():
        lines.append(f"  {label:<{width}} : {summary[key]}")
    lines.append("=" * (width + 22))
    return "\n".join(lines)


def save_results(res: BacktestResult, summary: dict[str, Any]) -> None:
    os.makedirs(BACKTEST_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    eq_path = os.path.join(BACKTEST_DIR, f"equity_curve_{stamp}.csv")
    res.equity_curve.to_csv(eq_path, index=False)

    tr_path = os.path.join(BACKTEST_DIR, f"trades_{stamp}.csv")
    pd.DataFrame([{
        "side": t.side, "entry_time": t.entry_time, "entry": round(t.entry, 2),
        "exit_time": t.exit_time, "exit": round(t.exit, 2) if t.exit else None,
        "qty": round(t.qty, 6), "notional_usd": round(t.notional, 2),
        "pnl_usd": round(t.pnl, 2), "reason": t.reason,
    } for t in res.trades]).to_csv(tr_path, index=False)

    sum_path = os.path.join(BACKTEST_DIR, f"summary_{stamp}.txt")
    with open(sum_path, "w", encoding="utf-8") as fh:
        fh.write(render_table(summary) + "\n")

    logger.info("Saved equity curve -> {}", eq_path)
    logger.info("Saved trades       -> {}", tr_path)
    logger.info("Saved summary      -> {}", sum_path)


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the BTC ensemble strategy.")
    parser.add_argument("--csv", type=str, default=None, help="Path to a candle CSV to use.")
    parser.add_argument("--years", type=float, default=2.0, help="Years of history to download.")
    parser.add_argument("--capital", type=float, default=None, help="Starting capital (USD).")
    parser.add_argument("--exchange", type=str, default="auto",
                        help="Data source: 'auto' (try several), or e.g. binanceus, okx, coinbase.")
    parser.add_argument("--symbol", type=str, default=None,
                        help="Market symbol, e.g. BTC/USDT (default from config).")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")

    cfg = load_config()
    starting_capital = args.capital if args.capital else cfg["risk"]["starting_capital_usd"]

    df = get_data(args, cfg)
    if len(df) < 300:
        logger.error("Not enough candles to backtest ({}). Need a few hundred+.", len(df))
        return

    res = run_backtest(df, cfg, starting_capital)
    summary = compute_summary(res)
    print(render_table(summary))
    save_results(res, summary)

    logger.info("Done. Remember: a good backtest is encouraging, not a guarantee. "
                "Keep PAPER_TRADING=true until you're confident.")


if __name__ == "__main__":
    main()
