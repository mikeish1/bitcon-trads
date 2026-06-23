"""
Pure simulation + metrics for the ETF validation harness (Stage 4).

`simulate` mirrors the LIVE loop's behaviour exactly so the backtest is honest:
  * decisions are made on the CLOSED bar (signal at close[t]),
  * orders execute at the NEXT session's OPEN (open[t+1]) with slippage — which is
    what models OVERNIGHT GAP RISK (a close-signalled order fills at the gapped open,
    not the close),
  * rotation is WHOLE-POSITION (enter new leaders, exit drop-outs, leave held weights
    alone) — not a re-weight-to-equal every period — matching src/etf/main.py,
  * commission-free (Alpaca), with a configurable per-side slippage/spread in bps.

Everything here is pure (operates on injected panels) and unit-tested offline.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

import pandas as pd


# --------------------------------------------------------------------------- #
# Cost model                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class CostModel:
    """Per-side market-order cost at the open. slippage_bps folds bid/ask spread +
    impact; commission_bps is 0 for Alpaca equities. Conservative defaults."""
    slippage_bps: float = 5.0
    commission_bps: float = 0.0

    def buy_px(self, px: float) -> float:
        return px * (1.0 + self.slippage_bps / 1e4)

    def sell_px(self, px: float) -> float:
        return px * (1.0 - self.slippage_bps / 1e4)

    def commission(self, notional: float) -> float:
        return abs(notional) * self.commission_bps / 1e4


# --------------------------------------------------------------------------- #
# Trade ledger (for turnover + tax)                                           #
# --------------------------------------------------------------------------- #
@dataclass
class Lot:
    qty: float
    entry_date: date
    entry_px: float          # cost basis per share (incl. buy slippage/commission)


@dataclass
class Realized:
    symbol: str
    qty: float
    entry_date: date
    exit_date: date
    proceeds: float          # net of sell cost
    cost: float              # basis
    @property
    def gain(self) -> float:
        return self.proceeds - self.cost
    @property
    def held_days(self) -> int:
        return (self.exit_date - self.entry_date).days
    @property
    def long_term(self) -> bool:
        return self.held_days > 365


@dataclass
class SimResult:
    curve: pd.Series                       # daily equity, indexed by timestamp
    realized: list[Realized] = field(default_factory=list)
    turnover_notional: float = 0.0
    days: int = 0
    days_in_risk: int = 0                  # days holding a non-cash-proxy asset
    initial: float = 1.0
    cash_symbol: str = "BIL"


def _iso(t: Any) -> str:
    return pd.Timestamp(t).date().isoformat()


def simulate(panel: dict[str, pd.DataFrame], selector: Any, *, tf: str = "1d",
             warmup: int, cost: Optional[CostModel] = None, initial: float = 10_000.0,
             top_k: Optional[int] = None, cash_symbol: str = "BIL",
             start: Optional[str] = None, end: Optional[str] = None) -> SimResult:
    """Walk the daily grid; rebalance via `selector` (whole-position), execute at the
    next open with `cost`. Returns the equity curve + realized trades + turnover."""
    cost = cost or CostModel()
    k = int(top_k if top_k is not None else getattr(selector, "top_k", 1))

    closes = {s: df.set_index("timestamp")["close"] for s, df in panel.items()}
    opens = {s: df.set_index("timestamp")["open"] for s, df in panel.items()}
    grid = sorted(set().union(*[set(df["timestamp"]) for df in panel.values()]))
    if start:
        grid = [t for t in grid if _iso(t) >= start]
    if end:
        grid = [t for t in grid if _iso(t) <= end]

    cash = float(initial)
    lots: dict[str, list[Lot]] = {}                 # symbol -> FIFO lots
    realized: list[Realized] = []
    pending: Optional[set[str]] = None
    last_rebal: Optional[str] = None
    turnover = 0.0
    curve_idx: list[Any] = []
    curve_val: list[float] = []
    days_in_risk = 0

    def price(series: pd.Series, t: Any) -> Optional[float]:
        try:
            v = series.get(t)
        except Exception:
            v = None
        if v is None or v != v or v <= 0:
            return None
        return float(v)

    def held_qty(sym: str) -> float:
        return sum(lot.qty for lot in lots.get(sym, []))

    def sell_all(sym: str, px: float, d: date) -> None:
        nonlocal cash, turnover
        qty = held_qty(sym)
        if qty <= 0:
            return
        sp = cost.sell_px(px)
        proceeds = qty * sp - cost.commission(qty * sp)
        basis = sum(lot.qty * lot.entry_px for lot in lots[sym])
        cash += proceeds
        turnover += qty * px
        realized.append(Realized(sym, qty, lots[sym][0].entry_date, d, proceeds, basis))
        lots[sym] = []

    def buy(sym: str, spend: float, px: float, d: date) -> None:
        nonlocal cash, turnover
        if spend <= 0:
            return
        bp = cost.buy_px(px)
        comm = cost.commission(spend)
        qty = (spend - comm) / bp
        if qty <= 0:
            return
        cash -= spend
        turnover += qty * px
        lots.setdefault(sym, []).append(Lot(qty, d, bp))

    for i, t in enumerate(grid):
        d = pd.Timestamp(t).date()
        # 1) execute the rebalance decided on the prior close, at THIS open.
        if pending is not None:
            held = {s for s in lots if held_qty(s) > 0}
            exits = held - pending
            enters = pending - held
            for s in exits:
                px = price(opens[s], t)
                if px:
                    sell_all(s, px, d)
            equity_open = cash + sum(held_qty(s) * (price(opens[s], t) or 0.0)
                                     for s in lots if held_qty(s) > 0)
            for s in sorted(enters):
                px = price(opens[s], t)
                if px is None:
                    continue
                spend = min(equity_open / max(k, 1), cash)
                if spend > 0:
                    buy(s, spend, px, d)
            pending = None

        # 2) mark equity at the close.
        eq = cash
        risk_held = False
        for s in list(lots):
            q = held_qty(s)
            if q <= 0:
                continue
            px = price(closes[s], t)
            if px is None:                      # carry at last basis if close missing
                px = lots[s][-1].entry_px
            eq += q * px
            if s != cash_symbol:
                risk_held = True
        curve_idx.append(t)
        curve_val.append(eq)
        if risk_held:
            days_in_risk += 1

        # 3) decide on this close if a rebalance is due.
        if i >= warmup and selector.is_due(last_rebal, _iso(t)):
            frames = {s: {tf: df[df["timestamp"] <= t]} for s, df in panel.items()}
            plan = selector.plan(frames, [s for s in lots if held_qty(s) > 0])
            pending = set(plan["target"])
            last_rebal = _iso(t)

    curve = pd.Series(curve_val, index=pd.to_datetime(curve_idx))
    return SimResult(curve=curve, realized=realized, turnover_notional=turnover,
                     days=len(curve), days_in_risk=days_in_risk, initial=initial,
                     cash_symbol=cash_symbol)


# --------------------------------------------------------------------------- #
# Metrics                                                                     #
# --------------------------------------------------------------------------- #
def _ann_factor() -> int:
    return 252


def metrics(curve: pd.Series, *, ann: int = 252) -> dict[str, float]:
    """Risk/return metrics from a daily equity curve."""
    if curve is None or len(curve) < 2:
        return {}
    rets = curve.pct_change().dropna()
    n = len(curve)
    years = n / ann
    total = float(curve.iloc[-1] / curve.iloc[0] - 1.0)
    cagr = float((curve.iloc[-1] / curve.iloc[0]) ** (1.0 / years) - 1.0) if years > 0 else 0.0

    peak, maxdd = -1e18, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            maxdd = max(maxdd, (peak - v) / peak)

    mean = float(rets.mean()) if len(rets) else 0.0
    sd = float(rets.std(ddof=0)) if len(rets) > 1 else 0.0
    downside = rets[rets < 0]
    dsd = float(downside.std(ddof=0)) if len(downside) > 1 else 0.0
    sharpe = (mean / sd * math.sqrt(ann)) if sd > 0 else 0.0
    sortino = (mean / dsd * math.sqrt(ann)) if dsd > 0 else 0.0
    calmar = (cagr / maxdd) if maxdd > 0 else 0.0
    return {
        "total_return": round(total, 4), "cagr": round(cagr, 4),
        "max_drawdown": round(maxdd, 4), "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2), "calmar": round(calmar, 2),
        "ann_vol": round(sd * math.sqrt(ann), 4), "days": n,
    }


def trade_stats(res: SimResult, *, ann: int = 252) -> dict[str, float]:
    """Win rate, payoff, annualised turnover, and a rough after-tax drag estimate."""
    r = res.realized
    wins = [x.gain for x in r if x.gain > 0]
    losses = [x.gain for x in r if x.gain < 0]
    win_rate = len(wins) / len(r) if r else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else 0.0
    years = res.days / ann if res.days else 1.0
    ann_turnover = (res.turnover_notional / res.initial / years) if years > 0 else 0.0
    return {
        "trades": len(r), "win_rate": round(win_rate, 3),
        "payoff_ratio": round(payoff, 2),
        "ann_turnover_x": round(ann_turnover, 2),
        "time_in_risk": round(res.days_in_risk / res.days, 3) if res.days else 0.0,
    }


def tax_drag_estimate(res: SimResult, *, st_rate: float = 0.24, lt_rate: float = 0.15,
                      wash_window_days: int = 30) -> dict[str, float]:
    """Rough TAXABLE-account drag: tax owed on NET realized gains, split short/long
    term, with a wash-sale flag (a loss sale followed by a re-buy of the same symbol
    within `wash_window_days` has its loss DISALLOWED for the estimate). Informational
    — not tax advice; assumes gains are taxed in-year and losses offset same-term."""
    by_symbol_buys: dict[str, list[date]] = {}
    for x in res.realized:
        by_symbol_buys.setdefault(x.symbol, [])
    # collect buy dates from realized entry dates + any still-open lots ignored
    for x in res.realized:
        by_symbol_buys[x.symbol].append(x.entry_date)

    st_gain = lt_gain = 0.0
    disallowed_loss = 0.0
    for x in res.realized:
        g = x.gain
        if g < 0:
            # wash-sale: was the same symbol re-bought within +/- window of the sale?
            rebought = any(abs((bd - x.exit_date).days) <= wash_window_days and bd != x.entry_date
                           for bd in by_symbol_buys.get(x.symbol, []))
            if rebought:
                disallowed_loss += -g
                continue
        if x.long_term:
            lt_gain += g
        else:
            st_gain += g
    tax = max(st_gain, 0.0) * st_rate + max(lt_gain, 0.0) * lt_rate
    return {
        "st_gain": round(st_gain, 2), "lt_gain": round(lt_gain, 2),
        "wash_disallowed_loss": round(disallowed_loss, 2),
        "est_tax": round(tax, 2),
        "est_tax_drag_pct_of_initial": round(tax / res.initial, 4) if res.initial else 0.0,
    }


# --------------------------------------------------------------------------- #
# Benchmarks                                                                  #
# --------------------------------------------------------------------------- #
def buy_hold_curve(df: pd.DataFrame, *, initial: float = 10_000.0,
                   start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """Buy-and-hold a single asset (e.g. SPY) on adjusted closes."""
    s = df.set_index("timestamp")["close"].astype(float)
    if start:
        s = s[s.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        s = s[s.index <= pd.Timestamp(end, tz="UTC")]
    s = s.dropna()
    return initial * s / s.iloc[0] if len(s) else pd.Series(dtype=float)


def sixty_forty_curve(eq_df: pd.DataFrame, bond_df: pd.DataFrame, *, initial: float = 10_000.0,
                      w_eq: float = 0.6, rebalance: str = "ME",
                      start: Optional[str] = None, end: Optional[str] = None) -> pd.Series:
    """60/40 (equity/bond) with periodic rebalancing (default month-end)."""
    e = eq_df.set_index("timestamp")["close"].astype(float)
    b = bond_df.set_index("timestamp")["close"].astype(float)
    idx = e.index.intersection(b.index)
    if start:
        idx = idx[idx >= pd.Timestamp(start, tz="UTC")]
    if end:
        idx = idx[idx <= pd.Timestamp(end, tz="UTC")]
    e, b = e.reindex(idx).dropna(), b.reindex(idx).dropna()
    idx = e.index.intersection(b.index)
    e, b = e.reindex(idx), b.reindex(idx)
    if len(idx) < 2:
        return pd.Series(dtype=float)
    er = e.pct_change().fillna(0.0)
    br = b.pct_change().fillna(0.0)
    w = w_eq
    val = initial
    out = []
    marks = set(pd.Series(idx).groupby([idx.year, idx.month]).last().values)
    for t in idx:
        port_ret = w * er[t] + (1 - w) * br[t]
        val *= (1 + port_ret)
        # weight drifts intra-period; reset at each rebalance mark
        if t in marks:
            w = w_eq
        out.append(val)
    return pd.Series(out, index=idx)


# --------------------------------------------------------------------------- #
# Monte-Carlo / bootstrap                                                     #
# --------------------------------------------------------------------------- #
def block_bootstrap(curve: pd.Series, *, n: int = 1000, block: int = 20,
                    seed: int = 7, ann: int = 252) -> dict[str, Any]:
    """Stationary block-bootstrap the daily returns to get a DISTRIBUTION of CAGR /
    maxDD / Sharpe (not one lucky path). Returns percentiles."""
    import random
    rets = list(curve.pct_change().dropna())
    if len(rets) < block + 1:
        return {}
    rng = random.Random(seed)
    m = len(rets)
    cagrs, dds, sharpes = [], [], []
    for _ in range(n):
        seq: list[float] = []
        while len(seq) < m:
            start = rng.randrange(m)
            seq.extend(rets[start:start + block])
        seq = seq[:m]
        # rebuild a curve
        v = 1.0
        peak, maxdd = 1.0, 0.0
        for x in seq:
            v *= (1 + x)
            peak = max(peak, v)
            maxdd = max(maxdd, (peak - v) / peak)
        years = m / ann
        cagrs.append(v ** (1 / years) - 1 if years > 0 else 0.0)
        dds.append(maxdd)
        mean = sum(seq) / len(seq)
        sd = statistics.pstdev(seq)
        sharpes.append(mean / sd * math.sqrt(ann) if sd > 0 else 0.0)

    def pct(xs: list[float], p: float) -> float:
        xs = sorted(xs)
        return round(xs[min(len(xs) - 1, max(0, int(p / 100 * len(xs))))], 4)

    return {
        "n": n, "block": block,
        "cagr_p5": pct(cagrs, 5), "cagr_p50": pct(cagrs, 50), "cagr_p95": pct(cagrs, 95),
        "maxdd_p50": pct(dds, 50), "maxdd_p95": pct(dds, 95),
        "sharpe_p5": pct(sharpes, 5), "sharpe_p50": pct(sharpes, 50),
    }
