"""
Carry executor — opens/closes a delta-neutral pair (long spot + short perp).

Two tiers, gated by the carry tripwire (config_carry.py):
  * sim  - synthesise fills against the live price with fee + slippage, send
           nothing. Needs no exchange object, so unit tests run offline.
  * live - real orders on both venues, real money (three-key tripwire).

Leg safety is the whole job here:
  * OPEN sequences spot-buy then perp-short; if the short fails, it ROLLS BACK the
    spot it just bought so you are never left accidentally long-only.
  * CLOSE in live mode closes the perp (the scarier leg) first, then spot; if a
    leg fails it does NOT mark the pair closed and raises a CRITICAL alert for
    operator intervention (atomic cross-venue unwind is genuinely hard — we fail
    loud rather than silently leave a half-open hedge).
"""
from __future__ import annotations

from typing import Any, Optional

import ccxt
from loguru import logger

from .types import Fill, PairFill


class CarryExecutionError(RuntimeError):
    """Raised on a non-recoverable live leg failure (e.g. half-closed pair)."""


class CarryExecutor:
    def __init__(self, cfg: dict[str, Any], spot: Optional[ccxt.Exchange] = None,
                 perp: Optional[ccxt.Exchange] = None):
        self.cfg = cfg
        self.spot = spot
        self.perp = perp
        rt = cfg["carry_runtime"]
        self.place = rt["place_orders"]
        self.real_money = rt["real_money"]
        ex = cfg["carry"]["execution"]
        self.fee_pct = float(ex["taker_fee_pct"])
        self.slip = float(ex["paper_slippage_pct"])
        self.tag = "[CARRY-LIVE]" if self.real_money else "[CARRY-SIM]"
        self._seq = 0

    # ------------------------------------------------------------------ #
    def _amt(self, exchange: Optional[ccxt.Exchange], symbol: str, qty: float) -> float:
        if exchange is None:
            return qty
        try:
            return float(exchange.amount_to_precision(symbol, qty))
        except Exception:
            return float(f"{qty:.8f}")

    def set_leverage(self, perp_symbol: str, leverage: float) -> None:
        if not self.place or self.perp is None:
            return
        try:
            self.perp.set_leverage(leverage, perp_symbol)
        except Exception as exc:
            logger.warning("{} set_leverage({}) failed (continuing): {}", self.tag, leverage, exc)

    # --- simulated fills ---------------------------------------------- #
    def _sim_fill(self, leg: str, side: str, symbol: str, qty: float, price: float) -> Fill:
        # buy pays up, sell receives less (slippage always against us).
        fill_px = price * (1 + self.slip) if side == "buy" else price * (1 - self.slip)
        notional = qty * fill_px
        self._seq += 1
        return Fill(leg=leg, side=side, qty=qty, price=fill_px, notional=notional,
                    fee=notional * self.fee_pct, order_id=f"sim-{leg}-{self._seq}")

    def _live_market(self, exchange: ccxt.Exchange, leg: str, side: str, symbol: str,
                     qty: float, price_hint: float, *, reduce_only: bool = False) -> Fill:
        params: dict[str, Any] = {"reduceOnly": True} if reduce_only else {}
        order = exchange.create_order(symbol, "market", side, qty, None, params)
        filled = float(order.get("filled") or qty)
        cost = float(order.get("cost") or (filled * price_hint))
        avg = float(order.get("average") or (cost / filled if filled else price_hint))
        fee = 0.0
        f = order.get("fee") or {}
        if isinstance(f, dict) and f.get("cost") is not None:
            try:
                fee = float(f["cost"])
            except (TypeError, ValueError):
                fee = 0.0
        return Fill(leg=leg, side=side, qty=filled, price=avg, notional=cost, fee=fee,
                    order_id=str(order.get("id") or ""))

    # ------------------------------------------------------------------ #
    def open_pair(self, asset: str, spot_symbol: str, perp_symbol: str,
                  notional: float, spot_price: float, perp_price: float) -> Optional[PairFill]:
        qty_s = self._amt(self.spot, spot_symbol, notional / spot_price)
        qty_p = self._amt(self.perp, perp_symbol, notional / perp_price)
        if qty_s <= 0 or qty_p <= 0:
            return None

        if not self.place:
            spot_fill = self._sim_fill("spot", "buy", spot_symbol, qty_s, spot_price)
            perp_fill = self._sim_fill("perp", "sell", perp_symbol, qty_p, perp_price)
            logger.info("{} OPEN {} ~${:.2f}: spot buy {:.6f} / perp short {:.6f}",
                        self.tag, asset, notional, qty_s, qty_p)
            return PairFill(asset, spot_fill, perp_fill, notional)

        # LIVE: spot first, then the short. Roll back spot if the short fails.
        try:
            spot_fill = self._live_market(self.spot, "spot", "buy", spot_symbol, qty_s, spot_price)
        except Exception as exc:
            logger.error("{} {} spot buy failed (no position taken): {}", self.tag, asset, exc)
            return None
        try:
            perp_fill = self._live_market(self.perp, "perp", "sell", perp_symbol, qty_p, perp_price)
        except Exception as exc:
            logger.error("{} {} perp short FAILED after spot buy - rolling back spot: {}",
                         self.tag, asset, exc)
            try:
                self._live_market(self.spot, "spot", "sell", spot_symbol, spot_fill.qty, spot_price)
                logger.warning("{} {} spot rollback complete - flat again.", self.tag, asset)
            except Exception as exc2:
                raise CarryExecutionError(
                    f"{asset}: short failed AND spot rollback failed - NAKED LONG {spot_fill.qty}. "
                    f"Manual intervention required. ({exc2})") from exc2
            return None
        return PairFill(asset, spot_fill, perp_fill, notional)

    # --- single-leg closes (the loop drives these for a resumable unwind) ----- #
    def cover_perp(self, asset: str, perp_symbol: str, qty: float,
                   price_hint: float) -> Optional[Fill]:
        """Buy-to-close the short perp. Returns the fill, or None on a live failure
        (the loop persists progress and retries the remaining leg next poll)."""
        qty = self._amt(self.perp, perp_symbol, qty)
        if qty <= 0:
            return None
        if not self.place:
            return self._sim_fill("perp", "buy", perp_symbol, qty, price_hint)
        try:
            return self._live_market(self.perp, "perp", "buy", perp_symbol, qty,
                                     price_hint, reduce_only=True)
        except Exception as exc:
            logger.error("{} {} perp cover failed (will retry): {}", self.tag, asset, exc)
            return None

    def sell_spot(self, asset: str, spot_symbol: str, qty: float,
                  price_hint: float) -> Optional[Fill]:
        """Sell the long spot. Returns the fill, or None on a live failure."""
        qty = self._amt(self.spot, spot_symbol, qty)
        if qty <= 0:
            return None
        if not self.place:
            return self._sim_fill("spot", "sell", spot_symbol, qty, price_hint)
        try:
            return self._live_market(self.spot, "spot", "sell", spot_symbol, qty, price_hint)
        except Exception as exc:
            logger.error("{} {} spot sell failed (will retry): {}", self.tag, asset, exc)
            return None

    def close_pair(self, asset: str, spot_symbol: str, perp_symbol: str, spot_qty: float,
                   perp_qty: float, spot_price: float, perp_price: float) -> Optional[PairFill]:
        """Atomic-ish close (cover perp first, then sell spot) composed from the
        single-leg methods. Returns None if either leg fails. Used in sim/tests;
        the live loop prefers the resumable cover_perp/sell_spot path directly."""
        perp_fill = self.cover_perp(asset, perp_symbol, perp_qty, perp_price)
        if perp_fill is None:
            return None
        spot_fill = self.sell_spot(asset, spot_symbol, spot_qty, spot_price)
        if spot_fill is None:
            return None
        logger.info("{} CLOSE {}: perp cover {:.6f} / spot sell {:.6f}",
                    self.tag, asset, perp_qty, spot_qty)
        return PairFill(asset, spot_fill, perp_fill, spot_qty * spot_price)
