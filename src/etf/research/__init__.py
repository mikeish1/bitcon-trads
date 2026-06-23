"""
ETF validation research harness (Stage 4) — RESEARCH ONLY, never trades.

Realistic-cost, gap-aware, walk-forward validation of the ETF selectors against
SPY buy-and-hold and 60/40. Uses yfinance (research-only dependency, see
requirements-research.txt) for long split+dividend-adjusted history; the live bot
stays on Alpaca. Pure simulation/metrics live in `harness.py`; data fetch+cache in
`feed.py`; the CLI orchestrator + report in `validate.py`.
"""
