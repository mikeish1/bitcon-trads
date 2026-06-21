"""
Delta-neutral funding-rate carry — a sibling strategy to the Donchian spot bot.

Long spot + short perpetual on the same coin to harvest funding (positive funding
= shorts get paid). Delta-neutral: P&L is funding income minus fees/basis, not
price direction. USA-legal via CFTC-regulated perps (e.g. Kraken Futures).

Runs in its OWN process (`python -m src.carry.main`) and never touches the
validated trend-follower. SIM (paper) by default. See docs/CARRY_ARBITRAGE.md.
"""
