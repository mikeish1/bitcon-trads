"""
=============================================================================
 LONG-ONLY SPOT BACKTESTER - validate the high-conviction strategy on history
=============================================================================

WHAT THIS DOES
--------------
Replays the EXACT live decision engine over years of history, bar by bar:

  * Entries  -> src/strategy.py  (the multi-timeframe high-conviction gates +
                6-of-8 trigger ensemble + bearish vetoes - the same code the
                live bot runs).
  * Sizing + ATR stops + trailing -> src/risk_manager.py (same formulas).

It is LONG-ONLY SPOT: it buys BTC with USDT and later sells BTC for USDT,
tracking both balances. It applies realistic fees (0.1%) and slippage, and
reports the metrics you need to judge a rare-trade strategy.

No lookahead: at each 5-minute bar it only uses higher-timeframe (15m/1h)
candles that were already CLOSED at that moment.

NOTE ON CLAUDE: the live system can ask Claude to confirm *borderline* setups.
Replaying that over years would mean thousands of paid API calls, so the
backtest runs WITHOUT Claude. That matches live behaviour when no Claude key is
set: borderline setups simply proceed on the rule engine.

HOW TO RUN (basic user - just do this)
--------------------------------------
From the project folder, with your virtual environment active:

    python src/backtester.py

The first run downloads ~2 years of 5-minute BTC data (public, no API keys) and
caches it to  backtests/BTCUSDT_5m.csv  so future runs are instant. It prints a
summary table and saves full results into the  backtests/  folder.

OPTIONS
-------
    python src/backtester.py --years 3            # more history
    python src/backtester.py --capital 250        # starting USDT
    python src/backtester.py --csv mydata.csv     # use your own CSV/Parquet
    python src/backtester.py --exchange okx        # force a data source
    python src/backtester.py --symbol BTC/USDT     # override symbol

RUN THIS BEFORE DEPLOYING TO RAILWAY. A good backtest is encouraging but NOT a
guarantee - markets change. Keep paper trading after this, too.
=============================================================================
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import ccxt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from loguru import logger  # noqa: E402

from src.config import load_config  # noqa: E402
from src.data_pipeline import DataPipeline  # noqa: E402
from src.risk_manager import RiskManager  # noqa: E402
from src.strategy import Strategy  # noqa: E402

BACKTEST_DIR = os.path.join(_PROJECT_ROOT, "backtests")
DEFAULT_CSV = os.path.join(BACKTEST_DIR, "BTCUSDT_5m.csv")


# --------------------------------------------------------------------------- #
# Data loading (5-minute base data; higher timeframes are resampled from it)  #
# --------------------------------------------------------------------------- #
def _fallback_sources(symbol: str) -> list[tuple[str, str]]:
    btc_usd = "BTC/USD" if symbol.upper().startswith("BTC") else symbol
    return [("binance", symbol), ("binanceus", symbol), ("okx", symbol),
            ("kraken", symbol), ("coinbase", btc_usd)]


def _paginate(exchange, symbol: str, timeframe: str, years: float) -> pd.DataFrame:
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    now_ms = exchange.milliseconds()
    since = now_ms - int(years * 365 * 24 * 60 * 60 * 1000)
    rows: list[list] = []
    while since < now_ms:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        except Exception as exc:
            logger.warning("Fetch hiccup ({}); retry in 3s.", str(exc).splitlines()[0][:80])
            time.sleep(3)
            continue
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1][0] + tf_ms
        if nxt <= since or batch[-1][0] >= now_ms:
            break
        since = nxt
        last_dt = datetime.fromtimestamp(batch[-1][0] / 1000, tz=timezone.utc)
        print(f"  ...{len(rows):>8,} candles (up to {last_dt:%Y-%m-%d})", end="\r")
        time.sleep(exchange.rateLimit / 1000)
    print()
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df


def download_5m(symbol: str, years: float, exchange_id: str) -> pd.DataFrame:
    sources = _fallback_sources(symbol) if exchange_id == "auto" else [(exchange_id, symbol)]
    last_err = None
    for ex_id, sym in sources:
        try:
            ex = getattr(ccxt, ex_id)({"enableRateLimit": True})
            ex.fetch_ohlcv(sym, "5m", limit=5)  # reachability probe
        except Exception as exc:
            last_err = exc
            logger.warning("{} unavailable ({}); trying next.", ex_id, str(exc).splitlines()[0][:60])
            continue
        logger.info("Downloading ~{} years of {} 5m from {}...", years, sym, ex_id)
        df = _paginate(ex, sym, "5m", years)
        if len(df) > 1000:
            logger.info("Got {} 5m candles from {}.", len(df), ex_id)
            return df
    raise RuntimeError(f"Could not download data. Last error: {last_err}")


def load_file(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path) if path.lower().endswith(".parquet") else pd.read_csv(path)
    if pd.api.types.is_numeric_dtype(df["timestamp"]):
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    else:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    return df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)


def get_data(args, cfg) -> pd.DataFrame:
    os.makedirs(BACKTEST_DIR, exist_ok=True)
    symbol = args.symbol or "BTC/USDT"
    if args.csv:
        logger.info("Loading candles from {}", args.csv)
        return load_file(args.csv)
    if os.path.exists(DEFAULT_CSV):
        logger.info("Using cached candles {} (delete to re-download).", DEFAULT_CSV)
        return load_file(DEFAULT_CSV)
    df = download_5m(symbol, args.years, args.exchange)
    df.to_csv(DEFAULT_CSV, index=False)
    logger.info("Cached candles to {}", DEFAULT_CSV)
    return df


def resample_ohlcv(df5: pd.DataFrame, minutes: int) -> pd.DataFrame:
    s = df5.set_index("timestamp")
    agg = s.resample(f"{minutes}min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    return agg.reset_index()


def _tf_minutes(tf: str) -> int:
    return int(tf[:-1]) * (60 if tf.endswith("h") else 1)


# --------------------------------------------------------------------------- #
# Portfolio (tracks USDT + BTC; mirrors RiskManager fees/sizing, bar-time)     #
# --------------------------------------------------------------------------- #
@dataclass
class Trade:
    entry_time: pd.Timestamp
    entry: float
    qty: float
    cost: float
    stop: float
    take: float
    current_stop: float
    entry_fee: float
    exit_time: Optional[pd.Timestamp] = None
    exit: Optional[float] = None
    pnl: float = 0.0
    reason: str = ""


class Portfolio:
    def __init__(self, cfg: dict[str, Any], rm: RiskManager, capital: float):
        self.cfg = cfg
        self.rm = rm
        self.fee = cfg["execution"]["taker_fee_pct"]
        self.slip = cfg["execution"]["paper_slippage_pct"]
        s = cfg["safety"]
        self.daily_loss = s["daily_loss_limit_pct"]
        self.weekly_loss = s["weekly_loss_limit_pct"]
        self.max_consec = s["max_consecutive_losses"]
        self.cooldown_min = s["cooldown_minutes"]
        self.max_trades_day = s["max_trades_per_day"]

        self.usdt = capital
        self.btc = 0.0
        self.position: Optional[Trade] = None
        self.trades: list[Trade] = []
        self.wins = self.losses = self.consec = 0
        self.last_exit: Optional[pd.Timestamp] = None
        self.day = None
        self.day_start_eq = capital
        self.week = None
        self.week_start_eq = capital
        self.trades_today = 0

    def equity(self, price: float) -> float:
        return self.usdt + self.btc * price

    def _roll(self, ts: pd.Timestamp, price: float) -> None:
        d = ts.date().isoformat()
        if self.day != d:
            self.day = d
            self.day_start_eq = self.equity(price)
            self.trades_today = 0
        wk = f"{ts.isocalendar().year}-{ts.isocalendar().week}"
        if self.week != wk:
            self.week = wk
            self.week_start_eq = self.equity(price)

    def can_buy(self, ts: pd.Timestamp, price: float) -> bool:
        self._roll(ts, price)
        if self.position is not None:
            return False
        eq = self.equity(price)
        if self.day_start_eq > 0 and (eq - self.day_start_eq) / self.day_start_eq <= -self.daily_loss:
            return False
        if self.week_start_eq > 0 and (eq - self.week_start_eq) / self.week_start_eq <= -self.weekly_loss:
            return False
        if self.consec >= self.max_consec:
            return False
        if self.trades_today >= self.max_trades_day:
            return False
        if self.last_exit is not None:
            if (ts - self.last_exit).total_seconds() / 60 < self.cooldown_min:
                return False
        return True

    def buy(self, ts: pd.Timestamp, price: float, atr: float) -> None:
        eq = self.equity(price)
        sizing = self.rm.size_buy(eq, self.usdt, price, atr)
        if not sizing["viable"]:
            return
        entry = price * (1 + self.slip)
        cost = min(sizing["spend_usd"], self.usdt * 0.999)  # leave a sliver for fees
        if cost < self.cfg["risk"]["min_notional_usd"]:
            return
        qty = cost / entry
        fee = cost * self.fee
        if cost + fee > self.usdt:
            cost = self.usdt - fee
            qty = cost / entry
        self.usdt -= (cost + fee)
        self.btc += qty
        self.position = Trade(entry_time=ts, entry=entry, qty=qty, cost=cost,
                              stop=sizing["stop_price"], take=sizing["take_price"],
                              current_stop=sizing["stop_price"], entry_fee=fee)
        self.trades_today += 1

    def sell(self, ts: pd.Timestamp, exit_price: float, reason: str) -> None:
        p = self.position
        if p is None:
            return
        execp = exit_price * (1 - self.slip)
        proceeds = p.qty * execp
        fee = proceeds * self.fee
        self.usdt += (proceeds - fee)
        self.btc -= p.qty
        if self.btc < 1e-12:
            self.btc = 0.0
        p.pnl = (proceeds - fee) - (p.cost + p.entry_fee)
        p.exit_time, p.exit, p.reason = ts, execp, reason
        self.trades.append(p)
        self.last_exit = ts
        if p.pnl >= 0:
            self.wins += 1
            self.consec = 0
        else:
            self.losses += 1
            self.consec += 1
        # Feed the evolving win-rate back into the live Kelly sizing.
        self.rm._set("wins", self.wins)
        self.rm._set("losses", self.losses)
        self.position = None


# --------------------------------------------------------------------------- #
# Backtest                                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    trades: list[Trade]
    equity_curve: pd.DataFrame
    eval_bars: int
    in_market_bars: int
    start: pd.Timestamp
    end: pd.Timestamp
    start_eq: float
    final_eq: float
    buy_hold_pct: float
    summary: dict[str, Any] = field(default_factory=dict)


def run_backtest(df5: pd.DataFrame, cfg: dict[str, Any], capital: float) -> Result:
    primary = cfg["market"]["primary_timeframe"]              # "5m"
    confirm = cfg["market"]["confirm_timeframes"]             # ["15m","1h"]
    mtf, htf = confirm[0], confirm[-1]
    mtf_min, htf_min = _tf_minutes(mtf), _tf_minutes(htf)

    logger.info("Computing indicators (5m + resampled {} / {})...", mtf, htf)
    df5 = DataPipeline.add_indicators(df5.copy())
    d_mtf = DataPipeline.add_indicators(resample_ohlcv(df5, mtf_min))
    d_htf = DataPipeline.add_indicators(resample_ohlcv(df5, htf_min))

    strat = Strategy(cfg, claude_orchestrator=None)           # no Claude in backtest
    rm = RiskManager(cfg)                                      # uses a temp DB (set in main)
    port = Portfolio(cfg, rm, capital)

    ts5 = df5["timestamp"].values
    ts_m = d_mtf["timestamp"].values
    ts_h = d_htf["timestamp"].values
    td5 = np.timedelta64(_tf_minutes(primary), "m")
    cut_m = td5 - np.timedelta64(mtf_min, "m")               # close-time offset
    cut_h = td5 - np.timedelta64(htf_min, "m")

    start_i = 210
    eq_t: list[pd.Timestamp] = []
    eq_v: list[float] = []
    in_market = eval_bars = 0

    logger.info("Replaying {} 5m bars (this can take a few minutes)...", len(df5) - start_i)
    for i in range(start_i, len(df5)):
        bar = df5.iloc[i]
        t = bar["timestamp"]          # pandas Timestamp (for portfolio time logic)
        t_np = ts5[i]                 # numpy datetime64 (for searchsorted)
        price = float(bar["close"])
        atr = float(bar["atr"]) if bar["atr"] == bar["atr"] else 0.0

        # No-lookahead higher-TF indices: only candles closed by this bar's close.
        j_m = int(np.searchsorted(ts_m, t_np + cut_m, side="right")) - 1
        j_h = int(np.searchsorted(ts_h, t_np + cut_h, side="right")) - 1

        # 1) Manage an open position first (entered on an earlier bar).
        if port.position is not None:
            p = port.position
            high, low = float(bar["high"]), float(bar["low"])
            if atr > 0:
                new_trail = port.rm.trailing_stop(price, atr)
                if new_trail > p.current_stop:
                    p.current_stop = new_trail
            if low <= p.current_stop:
                port.sell(t, p.current_stop, "trailing/stop")
            elif high >= p.take:
                port.sell(t, p.take, "take-profit")

        # 2) If flat, evaluate a high-conviction entry (needs enough HTF history).
        if port.position is None and j_m >= 60 and j_h >= 60 and atr > 0:
            eval_bars += 1
            frames = {
                primary: df5.iloc[i - 79: i + 1],
                mtf: d_mtf.iloc[max(0, j_m - 79): j_m + 1],
                htf: d_htf.iloc[max(0, j_h - 79): j_h + 1],
            }
            decision = strat.decide(frames)
            if decision.action == "BUY" and port.can_buy(t, price):
                port.buy(t, price, atr)
        else:
            eval_bars += 1

        if port.position is not None:
            in_market += 1
        eq_t.append(bar["timestamp"])
        eq_v.append(port.equity(price))

    # Close anything still open at the final price.
    if port.position is not None:
        last = df5.iloc[-1]
        port.sell(last["timestamp"], float(last["close"]), "end-of-backtest")
        eq_v[-1] = port.equity(float(last["close"]))

    equity_curve = pd.DataFrame({"timestamp": eq_t, "equity": eq_v})
    first_c, last_c = float(df5.iloc[start_i]["close"]), float(df5.iloc[-1]["close"])
    return Result(
        trades=port.trades, equity_curve=equity_curve, eval_bars=eval_bars,
        in_market_bars=in_market, start=df5.iloc[start_i]["timestamp"],
        end=df5.iloc[-1]["timestamp"], start_eq=capital, final_eq=port.equity(last_c),
        buy_hold_pct=(last_c / first_c - 1) * 100,
    )


# --------------------------------------------------------------------------- #
# Metrics + reporting                                                          #
# --------------------------------------------------------------------------- #
def _max_dd(eq: pd.Series) -> float:
    peak = eq.cummax()
    return float(((eq - peak) / peak).min() * 100) if len(eq) else 0.0


def compute_summary(res: Result) -> dict[str, Any]:
    t = res.trades
    n = len(t)
    wins = [x for x in t if x.pnl >= 0]
    losses = [x for x in t if x.pnl < 0]
    gp = sum(x.pnl for x in wins)
    gl = abs(sum(x.pnl for x in losses))
    total_ret = (res.final_eq / res.start_eq - 1) * 100
    days = max((res.end - res.start).total_seconds() / 86400, 1)
    cagr = ((res.final_eq / res.start_eq) ** (365.25 / days) - 1) * 100
    return {
        "period_start": res.start.strftime("%Y-%m-%d %H:%M UTC"),
        "period_end": res.end.strftime("%Y-%m-%d %H:%M UTC"),
        "years_tested": round(days / 365.25, 2),
        "starting_usdt": round(res.start_eq, 2),
        "final_equity": round(res.final_eq, 2),
        "total_return_pct": round(total_ret, 2),
        "cagr_pct": round(cagr, 2),
        "buy_hold_return_pct": round(res.buy_hold_pct, 2),
        "vs_buy_hold_pct": round(total_ret - res.buy_hold_pct, 2),
        "max_drawdown_pct": round(_max_dd(res.equity_curve["equity"]), 2),
        "num_trades": n,
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else 0.0,
        "profit_factor": round(gp / gl, 2) if gl > 0 else ("inf" if gp > 0 else 0.0),
        "avg_win_usd": round(gp / len(wins), 2) if wins else 0.0,
        "avg_loss_usd": round(-gl / len(losses), 2) if losses else 0.0,
        "pct_time_in_market": round(res.in_market_bars / res.eval_bars * 100, 2) if res.eval_bars else 0.0,
    }


def render_table(s: dict[str, Any]) -> str:
    labels = {
        "period_start": "Period start", "period_end": "Period end",
        "years_tested": "Years tested", "starting_usdt": "Starting USDT ($)",
        "final_equity": "Final equity ($)", "total_return_pct": "Total return (%)",
        "cagr_pct": "CAGR (%)", "buy_hold_return_pct": "Buy & hold return (%)",
        "vs_buy_hold_pct": "Strategy minus buy & hold (%)",
        "max_drawdown_pct": "Max drawdown (%)", "num_trades": "Number of trades",
        "win_rate_pct": "Win rate - longs (%)", "profit_factor": "Profit factor",
        "avg_win_usd": "Average win ($)", "avg_loss_usd": "Average loss ($)",
        "pct_time_in_market": "% time in market",
    }
    w = max(len(v) for v in labels.values())
    out = ["", "=" * (w + 20), "  LONG-ONLY BACKTEST SUMMARY", "=" * (w + 20)]
    for k, lab in labels.items():
        out.append(f"  {lab:<{w}} : {s[k]}")
    out.append("=" * (w + 20))
    return "\n".join(out)


def save_results(res: Result, summary: dict[str, Any]) -> None:
    os.makedirs(BACKTEST_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    res.equity_curve.to_csv(os.path.join(BACKTEST_DIR, f"equity_curve_{stamp}.csv"), index=False)
    pd.DataFrame([{
        "entry_time": x.entry_time, "entry": round(x.entry, 2),
        "exit_time": x.exit_time, "exit": round(x.exit, 2) if x.exit else None,
        "qty": round(x.qty, 8), "cost_usd": round(x.cost, 2),
        "pnl_usd": round(x.pnl, 2), "reason": x.reason,
    } for x in res.trades]).to_csv(os.path.join(BACKTEST_DIR, f"trades_{stamp}.csv"), index=False)
    with open(os.path.join(BACKTEST_DIR, f"summary_{stamp}.txt"), "w", encoding="utf-8") as fh:
        fh.write(render_table(summary) + "\n")
    logger.info("Saved equity_curve / trades / summary to {} (suffix {}).", BACKTEST_DIR, stamp)


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Long-only spot backtester for the high-conviction strategy.")
    ap.add_argument("--csv", type=str, default=None, help="CSV/Parquet candle file.")
    ap.add_argument("--years", type=float, default=2.0, help="Years of history to download.")
    ap.add_argument("--capital", type=float, default=None, help="Starting USDT (default 250).")
    ap.add_argument("--exchange", type=str, default="auto", help="Data source: auto, okx, binanceus, ...")
    ap.add_argument("--symbol", type=str, default=None, help="Symbol, default BTC/USDT.")
    ap.add_argument("--min-triggers", type=int, default=None,
                    help="Override strategy.triggers.min_required (sensitivity test).")
    ap.add_argument("--adx-min", type=float, default=None,
                    help="Override strategy.gates.adx_min (sensitivity test).")
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>")

    cfg = load_config()
    # Use a throwaway DB so the backtest never touches your live trading_state.db,
    # and run sizing in simulation mode (internal equity, not a broker account).
    tmp_db = os.path.join(tempfile.gettempdir(), f"bt_{os.getpid()}.db")
    cfg["runtime"]["db_path"] = tmp_db
    cfg["runtime"]["uses_broker"] = False
    cfg["runtime"]["real_money"] = False

    # Sensitivity overrides (don't touch the YAML).
    if args.min_triggers is not None:
        cfg["strategy"]["triggers"]["min_required"] = args.min_triggers
    if args.adx_min is not None:
        cfg["strategy"]["gates"]["adx_min"] = args.adx_min
    logger.info("Config: min_triggers={} adx_min={}",
                cfg["strategy"]["triggers"]["min_required"], cfg["strategy"]["gates"]["adx_min"])

    capital = args.capital if args.capital else cfg["risk"]["default_capital_usd"]

    df5 = get_data(args, cfg)
    if len(df5) < 2000:
        logger.error("Not enough 5m candles ({}). Need a few thousand+.", len(df5))
        return

    res = run_backtest(df5, cfg, capital)
    summary = compute_summary(res)
    print(render_table(summary))
    save_results(res, summary)
    try:
        os.remove(tmp_db)
    except OSError:
        pass

    logger.info("Done. Encouraging backtest != guaranteed profit. Keep paper trading first.")


if __name__ == "__main__":
    main()
