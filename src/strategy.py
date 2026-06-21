"""
High-conviction LONG-ONLY strategy for Binance.US spot.

Conviction is built in layers, not a single vote count:

  LAYER 1 - GATES (all must pass, else stay flat):
      * 1h established uptrend: close above EMA-50 and EMA-200, ADX > threshold,
        and +DI > -DI.
      * 15m alignment: close above EMA-50.
      * Market structure: higher highs AND higher lows on 15m.

  LAYER 2 - ENTRY-TRIGGER ENSEMBLE (need >= min_required of these on the 5m):
      RSI pullback-in-uptrend, RSI rising, pullback to a rising EMA, MACD turning
      up, volume confirmation, bullish candle, stochastic turning up, positive
      short-term momentum.

  LAYER 3 - BEARISH VETOES (any one cancels an otherwise-valid setup):
      overbought RSI (don't chase), negative 1h MACD histogram.

  LAYER 4 - Optional Claude yes/no on borderline conviction.

The result is BUY only on genuinely strong setups; otherwise FLAT. The bot is
expected to stay flat the large majority of the time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from loguru import logger


@dataclass
class StrategyDecision:
    action: str                       # "BUY" or "FLAT"
    conviction: int                   # number of entry triggers that fired
    triggers_required: int
    gates_passed: bool
    veto_hit: str = ""
    consulted_claude: bool = False
    reasons: list[str] = field(default_factory=list)
    reasoning: str = ""


def _v(row: pd.Series, key: str, default=float("nan")):
    val = row.get(key, default)
    return default if val is None or (isinstance(val, float) and pd.isna(val)) else val


class Strategy:
    def __init__(self, cfg: dict[str, Any], claude_orchestrator: Any | None = None):
        self.cfg = cfg
        self.claude = claude_orchestrator
        self.s = cfg["strategy"]
        self.primary_tf = cfg["market"]["primary_timeframe"]
        self.confirm_tfs = cfg["market"]["confirm_timeframes"]
        self.htf = self.confirm_tfs[-1]      # "1h"
        self.mtf = self.confirm_tfs[0]       # "15m"

    # ------------------------------------------------------------------ #
    def decide(self, frames: dict[str, pd.DataFrame]) -> StrategyDecision:
        for tf in (self.primary_tf, self.mtf, self.htf):
            if tf not in frames or len(frames[tf]) < 60 or "ema_200" not in frames[tf].columns:
                return StrategyDecision("FLAT", 0, self.s["triggers"]["min_required"],
                                        False, reasoning="Warming up / insufficient data.")

        p = frames[self.primary_tf].iloc[-1]
        p_prev = frames[self.primary_tf].iloc[-2]
        m = frames[self.mtf]
        h = frames[self.htf].iloc[-1]
        reasons: list[str] = []

        # ---------------- LAYER 1: GATES ----------------
        g = self.s["gates"]
        htf_up = (
            _v(h, "close") > _v(h, "ema_200")
            and _v(h, "close") > _v(h, "ema_50")
            and _v(h, "adx") >= g["adx_min"]
            and _v(h, "adx_pos") > _v(h, "adx_neg")
        )
        mtf_up = m.iloc[-1]["close"] > _v(m.iloc[-1], "ema_50")
        structure_ok = self._higher_highs_lows(m, g["structure_lookback"])

        if not (htf_up and mtf_up and structure_ok):
            why = []
            if not htf_up:
                why.append("1h not in a confirmed uptrend")
            if not mtf_up:
                why.append("15m below EMA-50")
            if not structure_ok:
                why.append("no higher-highs/higher-lows on 15m")
            return StrategyDecision("FLAT", 0, self.s["triggers"]["min_required"],
                                    gates_passed=False, reasons=why,
                                    reasoning="Gate(s) failed: " + "; ".join(why))

        reasons.append("Gates passed: 1h uptrend + 15m alignment + market structure.")

        # ---------------- LAYER 3: VETOES (checked early) ----------------
        veto = self._check_vetoes(p, h)
        if veto:
            return StrategyDecision("FLAT", 0, self.s["triggers"]["min_required"],
                                    gates_passed=True, veto_hit=veto,
                                    reasons=reasons, reasoning=f"Vetoed: {veto}.")

        # ---------------- LAYER 2: ENTRY-TRIGGER ENSEMBLE ----------------
        triggers = self._entry_triggers(p, p_prev, frames[self.primary_tf])
        fired = [name for name, ok in triggers.items() if ok]
        count = len(fired)
        need = self.s["triggers"]["min_required"]
        reasons.append(f"Triggers {count}/{len(triggers)} fired: {', '.join(fired) or 'none'}.")

        if count < need:
            return StrategyDecision("FLAT", count, need, gates_passed=True,
                                    reasons=reasons,
                                    reasoning=f"Only {count} triggers (<{need}) - staying flat.")

        # ---------------- LAYER 4: optional Claude confirmation ----------------
        consulted = False
        band = self.s.get("claude_consult_band", 1)
        borderline = count <= need + band
        if borderline and self.claude is not None:
            consulted = True
            ok, note = self.claude.confirm_long(frames, fired, count, need)
            reasons.append(f"Claude: {'APPROVE' if ok else 'REJECT'} - {note}")
            if not ok:
                return StrategyDecision("FLAT", count, need, gates_passed=True,
                                        consulted_claude=True, reasons=reasons,
                                        reasoning="Claude vetoed a borderline setup.")

        return StrategyDecision("BUY", count, need, gates_passed=True,
                                consulted_claude=consulted, reasons=reasons,
                                reasoning="High-conviction long confirmed.")

    # ------------------------------------------------------------------ #
    def _higher_highs_lows(self, df: pd.DataFrame, lookback: int) -> bool:
        win = df.tail(lookback)
        if len(win) < lookback:
            return False
        half = lookback // 2
        first, second = win.iloc[:half], win.iloc[half:]
        return (second["high"].max() > first["high"].max()
                and second["low"].min() > first["low"].min())

    def _check_vetoes(self, p: pd.Series, h: pd.Series) -> str:
        v = self.s["vetoes"]
        if _v(p, "rsi") > v["rsi_overbought"]:
            return f"5m RSI overbought ({_v(p, 'rsi'):.0f} > {v['rsi_overbought']}) - not chasing"
        if v.get("htf_macd_must_be_positive", True) and _v(h, "macd_diff") < 0:
            return "1h MACD histogram negative"
        return ""

    def _entry_triggers(self, p: pd.Series, p_prev: pd.Series, df: pd.DataFrame) -> dict[str, bool]:
        t = self.s["triggers"]
        ema = _v(p, f"ema_{t['pullback_ema']}")
        ema_prev3 = df.iloc[-4].get(f"ema_{t['pullback_ema']}") if len(df) >= 4 else None
        ema_rising = ema_prev3 is not None and not pd.isna(ema_prev3) and ema > ema_prev3
        near_ema = ema and abs(_v(p, "close") - ema) / _v(p, "close") <= t["pullback_distance_pct"]

        return {
            "rsi_pullback": t["rsi_pullback_low"] <= _v(p, "rsi") <= t["rsi_pullback_high"],
            "rsi_rising": _v(p, "rsi") > _v(p_prev, "rsi"),
            "pullback_to_rising_ema": bool(ema_rising and near_ema),
            "macd_turning_up": _v(p, "macd_diff") > _v(p_prev, "macd_diff"),
            "volume_confirm": _v(p, "volume") > _v(p, "vol_ema") * t["volume_factor"],
            "bullish_candle": _v(p, "close") > _v(p, "open"),
            "stoch_turning_up": _v(p, "stoch_k") > _v(p, "stoch_d") and _v(p, "stoch_k") < 80,
            "momentum_positive": _v(p, "roc_5") > 0,
        }


class DonchianStrategy:
    """
    Validated daily breakout trend-follower.

    Entry  : daily close breaks above the highest high of the prior `entry_period`
             days (a fresh N-day high = momentum/strength).
    Exit   : handled by the risk manager's ATR chandelier trail (exit when close
             falls atr_trail_mult x ATR below the highest close held). No fixed
             target - winners are allowed to run.

    This strategy only signals ENTRIES; the loop + risk manager handle the trail.
    """
    def __init__(self, cfg: dict[str, Any], claude_orchestrator: Any | None = None):
        self.cfg = cfg
        self.primary_tf = cfg["market"]["primary_timeframe"]
        d = cfg["strategy"]["donchian"]
        self.entry_period = d["entry_period"]
        self.min_history = d.get("min_history", 60)

    def decide(self, frames: dict[str, pd.DataFrame]) -> StrategyDecision:
        df = frames.get(self.primary_tf)
        if df is None or len(df) < max(self.min_history, self.entry_period + 5) \
                or "atr" not in df.columns:
            return StrategyDecision("FLAT", 0, 0, False,
                                    reasoning="Warming up / insufficient daily history.")
        # Prior N-day high EXCLUDING today (shift(1)) -> no lookahead.
        prior_high = df["high"].rolling(self.entry_period).max().shift(1).iloc[-1]
        close = float(df.iloc[-1]["close"])
        if pd.isna(prior_high):
            return StrategyDecision("FLAT", 0, 0, True, reasoning="Donchian window not ready.")
        if close > prior_high:
            return StrategyDecision(
                "BUY", 1, 1, True,
                reasons=[f"Close {close:,.0f} broke {self.entry_period}-day high {prior_high:,.0f}"],
                reasoning=f"Breakout: close {close:,.0f} > {self.entry_period}-day high {prior_high:,.0f}.")
        return StrategyDecision(
            "FLAT", 0, 1, True,
            reasoning=f"No breakout (close {close:,.0f} <= {self.entry_period}-day high {prior_high:,.0f}).")
