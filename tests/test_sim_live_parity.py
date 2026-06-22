"""Golden-master parity: the RESEARCH scale-out engine
(profit_taking_research.scaleout_asset) must honour the SAME breakeven floor that
the LIVE path enforces (main_loop._manage via risk_manager.profit_taking_plan).

This pins the two independent implementations to ONE behavioural spec. It is the
regression guard for the divergence found in the equivalence audit: the research
engine originally had NO breakeven floor, so the validated OOS result described
exit logic the live bot did not actually run. The live side of the same spec is
covered by tests/test_main_loop_staged_exit.py (a +1.6 ATR move arms a breakeven
stop at entry+0.5 ATR, and a dip to 104 exits the remainder); this file proves the
research engine now does the same thing - and that WITHOUT the floor it would not.
"""
from __future__ import annotations

import pandas as pd

from src.profit_taking_research import scaleout_asset

# Same schedule as config/trading_config.yaml strategy.profit_taking + the live tests.
TIERS_ATR = [1.5, 3.0]
TIERS_PCT = [0.33, 0.33]
RATCHET = [3.0, 2.5, 2.0]


def _breakout_then_shallow_pullback() -> pd.DataFrame:
    """50 flat warmup bars (daily range 10 -> ATR ~10), a Donchian breakout at 106,
    a run to 126 (~+2 ATR from entry -> fires tier 1 at +1.5 ATR and arms the
    breakeven floor at ~entry+0.5 ATR), then a pullback to 108 that is BELOW the
    breakeven floor (~110.9) but well ABOVE the loosened chandelier (~93)."""
    highs, lows, closes = [], [], []
    for _ in range(50):                       # flat warmup -> ATR converges to 10
        highs.append(105.0); lows.append(95.0); closes.append(100.0)
    highs.append(106.0); lows.append(100.0); closes.append(106.0)   # breakout (entry)
    highs.append(126.0); lows.append(106.0); closes.append(126.0)   # run-up: tier 1 + arm BE
    highs.append(108.0); lows.append(108.0); closes.append(108.0)   # shallow pullback
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


def _exposure(df: pd.DataFrame, **kw):
    run = scaleout_asset(df, entry=40, regime_on=None, capital=1000.0, fee=0.001,
                         slip=0.0007, scale_pcts=TIERS_PCT, scale_atr=TIERS_ATR,
                         ratchet=RATCHET, **kw)
    return run.exposure


def test_research_engine_honours_live_breakeven_floor():
    """WITH the live floor (arm after tier 1): the shallow pullback breaches the
    breakeven floor and the remainder is exited - identical to live _manage."""
    expo = _exposure(_breakout_then_shallow_pullback(),
                     breakeven_after_tier=1, breakeven_buffer_atr=0.5)
    assert expo[-2] > 0.0    # still long after the tier-1 scale-out (run-up bar)
    assert expo[-1] == 0.0   # exited on the pullback because the floor was breached


def test_without_floor_the_same_pullback_does_not_exit():
    """WITHOUT the floor (the OLD research behaviour) the chandelier sits far below,
    so the SAME pullback does not exit. This is precisely the divergence that made
    the validated OOS number describe different logic than the bot runs - if this
    ever fails identically to the test above, the two engines have drifted."""
    expo = _exposure(_breakout_then_shallow_pullback(), breakeven_after_tier=0)
    assert expo[-1] > 0.0    # still long -> the floor, not the trail, drives the live exit
