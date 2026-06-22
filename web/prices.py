"""
Public market-data client (no API keys, read-only).

The dashboard needs current spot prices to mark open positions to market. It must
NOT reuse the bot's authenticated `DataPipeline` (that carries exchange keys and
an order surface). Instead it calls a PUBLIC ticker endpoint that requires no
account:

  * Binance.US public REST: GET /api/v3/ticker/price?symbols=[...]  (USDT/USD pairs)

This mirrors the *semantics* of `src.main_loop.TradingBot._all_prices()` - a
latest price per BASE asset - without importing any trading code.

Design notes:
  * Synchronous httpx client. FastAPI runs sync endpoints in a threadpool, and the
    SSE loop calls this via `run_in_threadpool`, so a blocking client is simplest
    and avoids event-loop coupling.
  * A small TTL cache (default 10s) collapses fan-out when many viewers / SSE ticks
    hit the same symbols, and bounds load on the public endpoint.
  * Graceful degradation: on any failure the last good price is returned and marked
    stale; callers fall back to the position's entry price (never blocking the UI).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import httpx
from loguru import logger

# Binance.US lists USD and USDT pairs; the bot quotes in USDT (Binance.US) or USD
# (Alpaca). For display MTM either quote is close enough, so we try the configured
# quote first and fall back to USDT.
_BINANCE_US_BASE = "https://api.binance.us"
_DEFAULT_TTL = 10.0
_HTTP_TIMEOUT = 6.0


@dataclass
class PriceQuote:
    base: str
    price: float
    fetched_at: float
    stale: bool


class MarketDataClient:
    """Caches the latest public price per base asset."""

    def __init__(self, quote_ccy: str = "USDT", ttl_seconds: float = _DEFAULT_TTL) -> None:
        self._quote = quote_ccy.upper()
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._cache: dict[str, PriceQuote] = {}
        # Binance.US uses USDT symbols; map a USD quote onto USDT for the public feed.
        self._feed_quote = "USDT" if self._quote in ("USD", "USDT") else self._quote
        self._client = httpx.Client(
            base_url=_BINANCE_US_BASE,
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "bitcon-trads-dashboard/1.0"},
        )

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # pragma: no cover - best effort on shutdown
            pass

    def _symbol(self, base: str) -> str:
        return f"{base.upper()}{self._feed_quote}"

    def _is_fresh(self, q: PriceQuote, now: float) -> bool:
        return (now - q.fetched_at) < self._ttl and not q.stale

    # ------------------------------------------------------------------ #
    def get_prices(self, bases: Iterable[str]) -> dict[str, PriceQuote]:
        """Return a quote per base asset. Fresh cached entries are reused; the rest
        are fetched in ONE batched request. Failures keep the last good value and
        flag it stale."""
        wanted = sorted({b.upper() for b in bases if b})
        now = time.monotonic()
        out: dict[str, PriceQuote] = {}
        to_fetch: list[str] = []

        with self._lock:
            for b in wanted:
                cached = self._cache.get(b)
                if cached and self._is_fresh(cached, now):
                    out[b] = cached
                else:
                    to_fetch.append(b)

        if to_fetch:
            fetched = self._fetch_batch(to_fetch)
            with self._lock:
                for b in to_fetch:
                    if b in fetched:
                        q = PriceQuote(base=b, price=fetched[b], fetched_at=time.monotonic(), stale=False)
                    else:
                        prev = self._cache.get(b)
                        q = PriceQuote(
                            base=b,
                            price=prev.price if prev else 0.0,
                            fetched_at=prev.fetched_at if prev else time.monotonic(),
                            stale=True,
                        )
                    self._cache[b] = q
                    out[b] = q
        return out

    def get_price(self, base: str) -> Optional[PriceQuote]:
        return self.get_prices([base]).get(base.upper())

    def price_map(self, bases: Iterable[str]) -> dict[str, float]:
        """Convenience: {base: price} for metric math (drops stale-zero entries)."""
        return {b: q.price for b, q in self.get_prices(bases).items() if q.price > 0}

    def max_age_seconds(self, bases: Iterable[str]) -> float:
        """Wall-clock age of the OLDEST relevant quote (for a staleness badge)."""
        quotes = self.get_prices(bases)
        if not quotes:
            return 0.0
        now = time.monotonic()
        return max((now - q.fetched_at) for q in quotes.values())

    # ------------------------------------------------------------------ #
    def _fetch_batch(self, bases: list[str]) -> dict[str, float]:
        """One public request for many symbols. Returns {base: price} for whatever
        resolved; missing/failed symbols are simply absent."""
        symbols = [self._symbol(b) for b in bases]
        sym_to_base = {self._symbol(b): b for b in bases}
        # Binance.US accepts a JSON array string: symbols=["BTCUSDT","ETHUSDT"]
        params = {"symbols": "[" + ",".join(f'"{s}"' for s in symbols) + "]"}
        try:
            resp = self._client.get("/api/v3/ticker/price", params=params)
            resp.raise_for_status()
            data = resp.json()
            out: dict[str, float] = {}
            for row in data:
                sym = row.get("symbol")
                base = sym_to_base.get(sym)
                if base is not None:
                    try:
                        out[base] = float(row["price"])
                    except (TypeError, ValueError, KeyError):
                        continue
            return out
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Public price fetch failed for {} ({}); using last-known.", bases, exc)
            return {}
