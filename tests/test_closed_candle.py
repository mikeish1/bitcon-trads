"""Confirmed-closed-candle discipline (signal integrity).

DataPipeline.closed_frames must drop a still-FORMING trailing candle so signals
decide on settled bars only - matching the close-based backtest and making a
mid-candle restart deterministic - while leaving an already-closed frame untouched.
A candle opened at t is closed once t + timeframe <= now.
"""
from __future__ import annotations

import pandas as pd

from src.data_pipeline import DataPipeline


class _StubEx:
    """Minimal ccxt-shaped stub: timeframe parsing + a fixed clock, no network."""
    def __init__(self, now: pd.Timestamp):
        self._now_ms = int(now.timestamp() * 1000)

    def parse_timeframe(self, tf: str) -> int:        # seconds, like ccxt
        return {"1d": 86400, "1h": 3600}.get(tf, 86400)

    def milliseconds(self) -> int:
        return self._now_ms


def _cfg():
    return {"market": {"primary_timeframe": "1d", "confirm_timeframes": [],
                       "backfill_candles": 400}, "quote_ccy": "USDT"}


def _daily_frame(last_day: str, n: int = 5) -> pd.DataFrame:
    days = pd.date_range(end=last_day, periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"timestamp": days, "high": range(n), "low": range(n),
                         "close": range(n)})


def _pipe(now: pd.Timestamp) -> DataPipeline:
    return DataPipeline(_cfg(), _StubEx(now))


def test_in_progress_daily_candle_is_dropped():
    # 'Today' = 2026-06-21; now is mid-day, so the 06-21 candle is still forming.
    pipe = _pipe(pd.Timestamp("2026-06-21 12:00", tz="UTC"))
    frames = {"1d": _daily_frame("2026-06-21")}
    out = pipe.closed_frames(frames)["1d"]
    assert len(out) == len(frames["1d"]) - 1                     # exactly one dropped
    assert out.iloc[-1]["timestamp"] == pd.Timestamp("2026-06-20", tz="UTC")


def test_just_after_midnight_still_drops_todays_candle():
    # Even seconds into the new UTC day, today's candle is in-progress and dropped;
    # the decision falls on yesterday's CONFIRMED close (restart-deterministic).
    pipe = _pipe(pd.Timestamp("2026-06-21 00:05", tz="UTC"))
    out = pipe.closed_frames({"1d": _daily_frame("2026-06-21")})["1d"]
    assert out.iloc[-1]["timestamp"] == pd.Timestamp("2026-06-20", tz="UTC")


def test_already_closed_frame_is_untouched():
    # Venue already excluded the in-progress candle (last bar = yesterday, closed).
    # Nothing should be dropped - we must not act a day late.
    pipe = _pipe(pd.Timestamp("2026-06-21 12:00", tz="UTC"))
    frames = {"1d": _daily_frame("2026-06-20")}
    out = pipe.closed_frames(frames)["1d"]
    assert len(out) == len(frames["1d"])
    assert out.iloc[-1]["timestamp"] == pd.Timestamp("2026-06-20", tz="UTC")
