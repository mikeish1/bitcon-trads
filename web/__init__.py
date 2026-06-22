"""
Read-only monitoring & operations dashboard backend.

This package serves a FastAPI app that READS the bot's SQLite state
(`trading_state.db`: tables `state`, `trades`, `decisions`) plus public market
prices, and exposes them to a frontend over REST + SSE.

Hard safety boundary (see docs/DASHBOARD_ARCHITECTURE.md):
  * The DB is opened in READ-ONLY URI mode (`mode=ro` + `PRAGMA query_only`), so
    the web process physically cannot write the trading tables.
  * The ONLY mutation is the deployable-capital limit, delegated verbatim to the
    pre-existing, audited `src.settings_service.CapitalSettingsService` (which the
    running bot already hot-reloads via `RiskManager.maybe_reload_policy()`).
  * This package never imports `SpotExecutor`, `DataPipeline`, broker modules, or
    constructs a `RiskManager` write path. It imports only pure read helpers:
    `src.config.load_config`, `src.settings_service`, `src.capital_policy`.

The one new table, `equity_snapshots`, is written exclusively by this package's
own sampler (web/snapshots.py) and is never read or written by the bot.
"""
from __future__ import annotations

__all__ = ["__version__"]
__version__ = "1.0.0"
