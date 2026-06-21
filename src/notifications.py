"""
Optional Telegram notifications.

Sends short alerts to your phone via a Telegram bot for:
  * new high-conviction LONG entry  (price, size, reason)
  * sell / exit executed            (price, PnL, reason)
  * daily / weekly performance summary
  * critical errors / circuit-breaker trips

Completely OPTIONAL and fail-safe:
  * If Telegram isn't configured (no token/chat id, or TELEGRAM_ENABLED=false),
    every method is a silent no-op - the core system runs exactly as before.
  * Sending uses only the Python standard library (no extra dependency) and
    never raises: a Telegram outage can't crash or block the trading loop.

See the README ("Telegram notifications") for how to create a bot and get your
token + chat id.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from loguru import logger


class Notifier:
    def __init__(self, cfg: dict[str, Any]):
        rt = cfg["runtime"]
        self.token = rt.get("telegram_token", "")
        self.chat_id = rt.get("telegram_chat_id", "")
        self.enabled = bool(rt.get("telegram_enabled", True) and self.token and self.chat_id)
        if self.enabled:
            logger.info("Telegram notifications: ON")
        else:
            logger.info("Telegram notifications: OFF (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to enable)")

    # ------------------------------------------------------------------ #
    def _send(self, text: str) -> None:
        """Best-effort send. Never raises; logs a warning on failure."""
        if not self.enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            }).encode()
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
                body = json.loads(r.read().decode())
            if not body.get("ok"):
                logger.warning("Telegram send not ok: {}", body.get("description"))
        except Exception as exc:
            logger.warning("Telegram send failed ({}); continuing.", exc)

    # ------------------------------------------------------------------ #
    # Public, intention-revealing helpers                                #
    # ------------------------------------------------------------------ #
    def startup(self, venue: str, symbol: str, mode: str) -> None:
        self._send(f"🤖 Trading bot started\nVenue: {venue}\nSymbol: {symbol}\nMode: {mode}")

    def entry(self, price: float, qty: float, spend_usd: float,
              stop: float, take: float, reason: str) -> None:
        self._send(
            "🟢 BUY (long entry)\n"
            f"Price: {price:,.2f}\n"
            f"Size: {qty:.6f} BTC (~${spend_usd:,.2f})\n"
            f"Stop: {stop:,.2f}  |  Target: {take:,.2f}\n"
            f"Why: {reason}"
        )

    def exit(self, price: float, pnl: float, reason: str) -> None:
        emoji = "✅" if pnl >= 0 else "🔻"
        self._send(
            f"{emoji} SELL (exit)\n"
            f"Price: {price:,.2f}\n"
            f"PnL: ${pnl:,.2f}\n"
            f"Reason: {reason}"
        )

    def summary(self, stats: dict[str, Any], note: str = "") -> None:
        lines = [
            f"📊 Daily summary ({stats.get('date_utc', '')})",
            f"Mode: {stats.get('mode', '')}",
            f"Equity: ${stats.get('equity', 0):,.2f}",
            f"Day: {stats.get('day_return_pct', 0)}%  |  Week: {stats.get('week_return_pct', 0)}%",
            f"Trades today: {stats.get('trades_today', 0)}  |  Win rate: {stats.get('win_rate_pct', 0)}%",
        ]
        if note:
            lines.append("")
            lines.append(note.strip())
        self._send("\n".join(lines))

    def error(self, message: str) -> None:
        self._send(f"⚠️ ALERT\n{message}")

    def message(self, text: str) -> None:
        """Generic passthrough for sibling strategies (e.g. funding carry) that
        compose their own alert text. Still a silent no-op when Telegram is off."""
        self._send(text)
