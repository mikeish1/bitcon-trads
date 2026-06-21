"""
ETF cross-sectional momentum — a sibling strategy to the crypto trend-follower.

It REUSES the same validated engine, just pointed at a US ETF universe:
  * trend filter      = src.strategy.DonchianStrategy.active_state
  * cross-sectional   = src.momentum_allocator.MomentumRotation (top-K by momentum)
  * indicators        = src.data_pipeline.DataPipeline.add_indicators

Long-only, low-frequency (rebalances every N days), so it fits the synchronous
loop model. USA-legal and commission-free via Alpaca. Runs in its OWN process
(`python -m src.etf.main`), SIM by default. See docs/ETF_MOMENTUM.md.
"""
