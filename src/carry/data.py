"""
Carry data layer.

Pulls everything the signal needs for one asset: a smoothed, annualised funding
rate, spot + perp prices, the basis, and how stale the freshest input is. It also
resolves the venue-specific symbols dynamically (so we don't hard-code Kraken's
contract strings) and exposes a startup sanity check for the known
emulated-`fetchFundingRate` quirk on krakenfutures.

Network calls are wrapped so a transient failure raises a clear error the loop can
turn into a staleness/circuit-breaker event rather than crashing.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import ccxt
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from .signal import annualize_funding
from .types import FundingQuote

_PREFERRED_QUOTES = ("USD", "USDT", "USDC")


class CarryData:
    def __init__(self, cfg: dict[str, Any], spot: ccxt.Exchange, perp: ccxt.Exchange):
        self.cfg = cfg
        self.spot = spot
        self.perp = perp
        self.interval_hours = float(cfg["carry"]["funding_interval_hours"])
        self.lookback = int(cfg["carry"]["signal"]["funding_lookback"])
        self._symbols: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------ #
    # Symbol resolution                                                  #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pick(markets: dict[str, Any], asset: str, *, swap: bool) -> Optional[str]:
        cands = []
        for m in markets.values():
            if (m.get("base") == asset and m.get("active", True)
                    and bool(m.get("swap")) == swap
                    and (not swap or m.get("linear"))
                    and (swap or m.get("spot"))
                    and m.get("quote") in _PREFERRED_QUOTES):
                cands.append(m)
        if not cands:
            return None
        cands.sort(key=lambda m: _PREFERRED_QUOTES.index(m["quote"]))
        return cands[0]["symbol"]

    def resolve(self, asset: str) -> tuple[str, str]:
        """(spot_symbol, perp_symbol) for an asset, cached. Raises if unavailable."""
        if asset in self._symbols:
            return self._symbols[asset]
        spot_sym = self._pick(self.spot.markets or self.spot.load_markets(), asset, swap=False)
        perp_sym = self._pick(self.perp.markets or self.perp.load_markets(), asset, swap=True)
        if not spot_sym:
            raise ValueError(f"No spot market for {asset} on {self.cfg['carry_runtime']['spot_id']}")
        if not perp_sym:
            raise ValueError(f"No linear perp for {asset} on {self.cfg['carry_runtime']['perp_id']}")
        self._symbols[asset] = (spot_sym, perp_sym)
        logger.info("{} resolved: spot={} perp={}", asset, spot_sym, perp_sym)
        return self._symbols[asset]

    # ------------------------------------------------------------------ #
    # Funding + prices                                                   #
    # ------------------------------------------------------------------ #
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    def _funding_history(self, perp_symbol: str) -> list[dict[str, Any]]:
        # Native on krakenfutures; avoids the emulated fetch_funding_rate quirk.
        return self.perp.fetch_funding_rate_history(perp_symbol, limit=max(self.lookback, 1))

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
    def _last(self, exchange: ccxt.Exchange, symbol: str) -> tuple[float, float]:
        """Return (price, age_seconds) from a ticker; prefers mark for perps."""
        t = exchange.fetch_ticker(symbol)
        price = t.get("last") or t.get("close") or t.get("mark")
        info = t.get("info") or {}
        price = float(price if price is not None else info.get("markPrice") or 0.0)
        ts = t.get("timestamp")
        age = (exchange.milliseconds() - ts) / 1000.0 if ts else 0.0
        return price, max(age, 0.0)

    def funding_quote(self, asset: str) -> FundingQuote:
        """Assemble a FundingQuote for one asset (raises on hard data failure)."""
        spot_sym, perp_sym = self.resolve(asset)
        hist = self._funding_history(perp_sym)
        if not hist:
            raise ValueError(f"No funding history for {perp_sym}")
        recent = hist[-self.lookback:]
        rates = [float(h["fundingRate"]) for h in recent if h.get("fundingRate") is not None]
        if not rates:
            raise ValueError(f"Funding history for {perp_sym} had no usable rates")
        smoothed = sum(rates) / len(rates)
        latest = float(recent[-1]["fundingRate"])
        last_ts = recent[-1].get("timestamp") or 0
        funding_age = (self.perp.milliseconds() - last_ts) / 1000.0 if last_ts else 0.0

        spot_px, spot_age = self._last(self.spot, spot_sym)
        perp_px, perp_age = self._last(self.perp, perp_sym)
        basis_bps = ((perp_px - spot_px) / spot_px * 1e4) if spot_px else 0.0
        # Staleness = the freshest-input view: funding history is intentionally slow
        # (one point per interval), so only the live prices gate the breaker.
        age = max(spot_age, perp_age)

        return FundingQuote(
            asset=asset,
            funding_rate=latest,
            funding_apr=annualize_funding(smoothed, self.interval_hours),
            spot=spot_px,
            perp=perp_px,
            basis_bps=basis_bps,
            age_seconds=age,
        )

    # ------------------------------------------------------------------ #
    # Startup validation + live account                                  #
    # ------------------------------------------------------------------ #
    def validate(self, assets: list[str]) -> None:
        """Best-effort startup sanity check; warns loudly on absurd funding."""
        for asset in assets:
            try:
                q = self.funding_quote(asset)
            except Exception as exc:
                logger.warning("Validation: {} quote failed ({}).", asset, exc)
                continue
            if abs(q.funding_apr) > 3.0:  # > 300%/yr almost certainly a unit error
                logger.warning("Validation: {} funding APR looks absurd ({:.1%}). "
                               "Check funding_interval_hours / venue units before live.",
                               asset, q.funding_apr)
            else:
                logger.info("Validation: {} funding {:.2%}/yr, basis {:.0f}bps (age {:.0f}s).",
                            asset, q.funding_apr, q.basis_bps, q.age_seconds)

    def perp_margin_ratio(self, perp_symbol: str) -> Optional[float]:
        """Live margin ratio for the short leg, or None if unavailable (sim)."""
        try:
            positions = self.perp.fetch_positions([perp_symbol])
        except Exception as exc:
            logger.debug("Margin read failed for {}: {}", perp_symbol, exc)
            return None
        for p in positions:
            mm = p.get("marginRatio") or p.get("maintenanceMarginPercentage")
            if mm is not None:
                try:
                    return float(mm)
                except (TypeError, ValueError):
                    return None
        return None

    def spot_quote_free(self, quote_ccy: str = "USD") -> float:
        try:
            bal = self.spot.fetch_balance()
            return float((bal.get("free", {}) or {}).get(quote_ccy, 0.0) or 0.0)
        except Exception as exc:
            logger.debug("Spot balance read failed: {}", exc)
            return 0.0
