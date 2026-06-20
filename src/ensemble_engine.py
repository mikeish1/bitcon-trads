"""
Ensemble engine: 31 parallel prediction paths.

- 28 fast, deterministic technical/statistical voters (no API calls).
- 3 Claude "expert" voters, consulted ONLY when the deterministic vote is
  marginal (configurable band, default 26-27 of 28). This keeps the system
  cheap and fast: the LLM is a tie-breaker, not a per-candle dependency.

Each voter returns one of:
    +1  -> LONG
    -1  -> SHORT
     0  -> FLAT / abstain

A trade is only signalled when at least `trade_threshold` (default 28) of the
31 paths agree on the same direction. Anything below `discard_threshold`
(default 26) means we stay flat and do nothing.

A basic user does not need to change anything here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd
from loguru import logger


@dataclass
class EnsembleDecision:
    direction: str                       # "LONG", "SHORT", or "FLAT"
    agreement: int                       # votes agreeing with `direction`
    longs: int
    shorts: int
    flats: int
    total_paths: int
    consulted_claude: bool = False
    deterministic_agreement: int = 0
    detail: dict[str, int] = field(default_factory=dict)
    reasoning: str = ""


# --------------------------------------------------------------------------- #
# Deterministic voters                                                         #
# Each takes the latest indicator row (a pandas Series) and returns -1/0/+1.   #
# --------------------------------------------------------------------------- #
def _sign(value: float, threshold: float = 0.0) -> int:
    if value > threshold:
        return 1
    if value < -threshold:
        return -1
    return 0


def _ema_cross(fast: int, slow: int) -> Callable[[pd.Series], int]:
    def voter(r: pd.Series) -> int:
        f, s = r.get(f"ema_{fast}"), r.get(f"ema_{slow}")
        if pd.isna(f) or pd.isna(s):
            return 0
        return 1 if f > s else -1
    return voter


def _price_vs_ema(span: int) -> Callable[[pd.Series], int]:
    def voter(r: pd.Series) -> int:
        e = r.get(f"ema_{span}")
        if pd.isna(e):
            return 0
        return 1 if r["close"] > e else -1
    return voter


def _rsi_voter(window: int, low: float = 45, high: float = 55) -> Callable[[pd.Series], int]:
    def voter(r: pd.Series) -> int:
        v = r.get(f"rsi_{window}")
        if pd.isna(v):
            return 0
        if v > high:
            return 1
        if v < low:
            return -1
        return 0
    return voter


def _roc_voter(window: int) -> Callable[[pd.Series], int]:
    def voter(r: pd.Series) -> int:
        v = r.get(f"roc_{window}")
        return 0 if pd.isna(v) else _sign(v)
    return voter


def _macd_diff(r: pd.Series) -> int:
    v = r.get("macd_diff")
    return 0 if pd.isna(v) else _sign(v)


def _macd_cross(r: pd.Series) -> int:
    m, s = r.get("macd"), r.get("macd_signal")
    if pd.isna(m) or pd.isna(s):
        return 0
    return 1 if m > s else -1


def _stoch_cross(r: pd.Series) -> int:
    k, d = r.get("stoch_k"), r.get("stoch_d")
    if pd.isna(k) or pd.isna(d):
        return 0
    return 1 if k > d else -1


def _stoch_extreme(r: pd.Series) -> int:
    k = r.get("stoch_k")
    if pd.isna(k):
        return 0
    if k < 20:
        return 1   # oversold -> mean-revert up
    if k > 80:
        return -1  # overbought -> mean-revert down
    return 0


def _bb_mid(r: pd.Series) -> int:
    m = r.get("bb_mid")
    if pd.isna(m):
        return 0
    return 1 if r["close"] > m else -1


def _bb_breakout(r: pd.Series) -> int:
    hi, lo = r.get("bb_high"), r.get("bb_low")
    if pd.isna(hi) or pd.isna(lo):
        return 0
    if r["close"] > hi:
        return 1
    if r["close"] < lo:
        return -1
    return 0


def _adx_directional(r: pd.Series) -> int:
    adx, pos, neg = r.get("adx"), r.get("adx_pos"), r.get("adx_neg")
    if pd.isna(adx) or adx < 20:  # only trust direction in a real trend
        return 0
    return 1 if pos > neg else -1


def _cci_voter(r: pd.Series) -> int:
    v = r.get("cci")
    if pd.isna(v):
        return 0
    if v > 100:
        return 1
    if v < -100:
        return -1
    return 0


def _willr_voter(r: pd.Series) -> int:
    v = r.get("willr")
    if pd.isna(v):
        return 0
    if v > -20:
        return -1  # overbought
    if v < -80:
        return 1   # oversold
    return 0


def _obv_voter(r: pd.Series) -> int:
    o, e = r.get("obv"), r.get("obv_ema")
    if pd.isna(o) or pd.isna(e):
        return 0
    return 1 if o > e else -1


def _vol_momentum(r: pd.Series) -> int:
    vol, vema, roc = r.get("volume"), r.get("vol_ema"), r.get("roc_5")
    if pd.isna(vol) or pd.isna(vema) or pd.isna(roc):
        return 0
    if vol > vema:                 # only act on above-average volume
        return _sign(roc)
    return 0


def _build_deterministic_voters() -> dict[str, Callable[[pd.Series], int]]:
    """Construct exactly 28 deterministic voters with parameter variation."""
    voters: dict[str, Callable[[pd.Series], int]] = {}

    # 6 EMA crossover voters
    for fast, slow in [(5, 21), (8, 34), (13, 55), (21, 89), (34, 100), (55, 200)]:
        voters[f"ema_cross_{fast}_{slow}"] = _ema_cross(fast, slow)

    # 4 price-vs-EMA voters
    for span in (21, 55, 100, 200):
        voters[f"price_vs_ema_{span}"] = _price_vs_ema(span)

    # 3 RSI voters
    for window in (7, 14, 21):
        voters[f"rsi_{window}"] = _rsi_voter(window)

    # 3 ROC voters
    for window in (5, 10, 20):
        voters[f"roc_{window}"] = _roc_voter(window)

    # 2 MACD voters
    voters["macd_diff"] = _macd_diff
    voters["macd_cross"] = _macd_cross

    # 2 Stochastic voters
    voters["stoch_cross"] = _stoch_cross
    voters["stoch_extreme"] = _stoch_extreme

    # 2 Bollinger voters
    voters["bb_mid"] = _bb_mid
    voters["bb_breakout"] = _bb_breakout

    # 6 single-signal voters
    voters["adx_directional"] = _adx_directional
    voters["cci"] = _cci_voter
    voters["williams_r"] = _willr_voter
    voters["obv"] = _obv_voter
    voters["vol_momentum"] = _vol_momentum
    voters["price_vs_ema_13"] = _price_vs_ema(13)

    assert len(voters) == 28, f"expected 28 deterministic voters, got {len(voters)}"
    return voters


class EnsembleEngine:
    def __init__(self, cfg: dict[str, Any], claude_orchestrator: Any | None = None):
        self.cfg = cfg
        self.claude = claude_orchestrator
        self.voters = _build_deterministic_voters()

        ens = cfg["ensemble"]
        self.total_paths = ens["total_paths"]
        self.trade_threshold = ens["trade_threshold"]
        self.discard_threshold = ens["discard_threshold"]
        self.claude_paths = ens["claude_paths"]
        self.consult_min = ens["claude_consult_min"]
        self.consult_max = ens["claude_consult_max"]

    # ------------------------------------------------------------------ #
    def decide(self, df: pd.DataFrame) -> EnsembleDecision:
        """Run all voters on the latest candle and produce a decision."""
        if df is None or len(df) < 60:
            return EnsembleDecision(
                direction="FLAT", agreement=0, longs=0, shorts=0,
                flats=self.total_paths, total_paths=self.total_paths,
                reasoning="Not enough data yet (warming up indicators).",
            )

        row = df.iloc[-1]
        detail: dict[str, int] = {}
        longs = shorts = 0
        for name, voter in self.voters.items():
            try:
                vote = int(voter(row))
            except Exception as exc:  # a single bad voter must never crash the loop
                logger.warning("Voter {} errored: {}", name, exc)
                vote = 0
            detail[name] = vote
            if vote > 0:
                longs += 1
            elif vote < 0:
                shorts += 1

        det_direction = "LONG" if longs >= shorts else "SHORT"
        det_agreement = max(longs, shorts)
        consulted = False
        reasoning = ""

        # Decide whether to consult the 3 Claude experts.
        # Marginal zone: between discard_threshold and trade_threshold, and
        # within the configured Claude band, and only when adding the 3 votes
        # could actually reach the trade threshold (don't waste API calls).
        marginal = self.discard_threshold <= det_agreement < self.trade_threshold
        in_band = self.consult_min <= det_agreement <= self.consult_max
        reachable = det_agreement + self.claude_paths >= self.trade_threshold

        claude_votes = [0, 0, 0]
        if marginal and in_band and reachable and self.claude is not None:
            consulted = True
            logger.info(
                "Marginal consensus ({} {} of 28). Consulting 3 Claude experts...",
                det_agreement, det_direction,
            )
            try:
                claude_votes, reasoning = self.claude.get_expert_votes(
                    df=df, leaning=det_direction, longs=longs, shorts=shorts,
                )
            except Exception as exc:
                logger.warning("Claude consult failed ({}); treating as abstain.", exc)
                claude_votes = [0, 0, 0]
                reasoning = f"Claude unavailable: {exc}"

        for i, v in enumerate(claude_votes):
            detail[f"claude_expert_{i + 1}"] = int(v)
            if v > 0:
                longs += 1
            elif v < 0:
                shorts += 1

        flats = self.total_paths - longs - shorts
        agreement = max(longs, shorts)
        direction = "LONG" if longs > shorts else ("SHORT" if shorts > longs else "FLAT")

        if agreement < self.trade_threshold:
            direction = "FLAT"  # below the strict consensus bar -> stay flat

        if not reasoning:
            reasoning = (
                f"{longs} LONG / {shorts} SHORT / {flats} FLAT across "
                f"{self.total_paths} paths."
            )

        return EnsembleDecision(
            direction=direction,
            agreement=agreement,
            longs=longs,
            shorts=shorts,
            flats=flats,
            total_paths=self.total_paths,
            consulted_claude=consulted,
            deterministic_agreement=det_agreement,
            detail=detail,
            reasoning=reasoning,
        )
