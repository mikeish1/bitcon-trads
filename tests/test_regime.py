"""Feature B: the matured higher-timeframe regime gate (src/regime.py).

Checks each detection method, the fail-open behaviour on thin history, the
size_factor semantics (risk-off shrinks instead of only on/off), and that
`regime_from_config` falls back to the legacy btc_regime when the new block is off."""
from __future__ import annotations

from tests.conftest import make_bars

from src.regime import get_regime_state, regime_from_config


def _rising(n=80):
    return make_bars([100.0 + i for i in range(n)])


def _falling(n=80):
    return make_bars([200.0 - i for i in range(n)])


def test_ma_uptrend_is_risk_on():
    st = get_regime_state(_rising(), method="ma", params={"ma_period": 20})
    assert st.risk_on is True
    assert st.size_factor == 1.0


def test_ma_downtrend_is_risk_off_with_size_factor():
    st = get_regime_state(_falling(), method="ma",
                          params={"ma_period": 20, "risk_off_size_factor": 0.2})
    assert st.risk_on is False
    assert st.size_factor == 0.2          # shrink, not necessarily flat


def test_fail_open_on_missing_or_thin_history():
    assert get_regime_state(None, method="ma").risk_on is True
    assert get_regime_state(make_bars([100.0, 101.0]), method="ma",
                            params={"ma_period": 100}).risk_on is True


def test_ma_slope_requires_rising_ma():
    # Uptrend: close>MA and MA rising -> risk-on.
    assert get_regime_state(_rising(), method="ma_slope",
                            params={"ma_period": 20, "slope_lookback": 10}).risk_on is True
    # Downtrend: MA falling -> risk-off.
    assert get_regime_state(_falling(), method="ma_slope",
                            params={"ma_period": 20, "slope_lookback": 10}).risk_on is False


def test_vol_method_steps_aside_when_vol_exceeds_ceiling():
    calm = get_regime_state(_rising(), method="vol",
                            params={"vol_period": 20, "vol_ceiling": 0.05})
    assert calm.risk_on is True           # a smooth +1/bar ramp is low-vol
    turbulent = get_regime_state(_rising(), method="vol",
                                 params={"vol_period": 20, "vol_ceiling": 0.0001})
    assert turbulent.risk_on is False     # an impossibly tight ceiling forces risk-off


def test_composite_score_in_unit_interval():
    st = get_regime_state(_rising(), method="composite",
                          params={"ma_period": 20, "slope_lookback": 10, "vol_period": 20,
                                  "vol_ceiling": 0.05, "score_threshold": 0.5})
    assert 0.0 <= st.score <= 1.0
    assert st.risk_on is True


def test_regime_from_config_legacy_fallback():
    cfg = {"strategy": {"btc_regime": {"enabled": True, "ma_period": 20},
                        "regime": {"enabled": False}}}
    # New block off -> legacy MA gate is used.
    assert regime_from_config(_rising(), cfg).risk_on is True
    assert regime_from_config(_falling(), cfg).risk_on is False


def test_regime_from_config_uses_new_block_when_enabled():
    cfg = {"strategy": {"btc_regime": {"enabled": True, "ma_period": 20},
                        "regime": {"enabled": True, "method": "ma_slope",
                                   "ma_period": 20, "slope_lookback": 10,
                                   "risk_off_size_factor": 0.0}}}
    st = regime_from_config(_falling(), cfg)
    assert st.risk_on is False
    assert st.method == "ma_slope"
