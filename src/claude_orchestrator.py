"""
Claude orchestrator (used sparingly).

Two narrow jobs:
  1. confirm_long()  - a final yes/no on a BORDERLINE high-conviction long setup
                       that already passed every gate and trigger check.
  2. daily_summary() - a short plain-English recap for the logs.

Claude is a conservative second opinion, never a per-candle dependency. Default
model is the cheap `claude-haiku-4-5` (override with CLAUDE_MODEL).
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd
from loguru import logger

try:
    import anthropic
except Exception:  # pragma: no cover
    anthropic = None  # type: ignore


LONG_SYSTEM_PROMPT = """\
You are a conservative crypto risk reviewer giving a final yes/no on a single \
proposed SPOT BUY of Bitcoin (BTC/USDT) on Binance.US. This is a LONG-ONLY \
system: the only question is whether to BUY now, or wait.

A multi-timeframe rule engine has ALREADY confirmed an established uptrend (1h \
and 15m) and a cluster of bullish entry triggers on the 5m candle. Your role is \
a last sanity check on a BORDERLINE setup.

Your mandate is CAPITAL PRESERVATION. Approve the buy ONLY if the setup looks \
like a genuinely healthy pullback/continuation in a real uptrend. REJECT if the \
move looks overextended or climactic, if price is being chased after a vertical \
run, if volatility looks dangerous, or if the picture is ambiguous. When in \
doubt, REJECT - missing a trade costs nothing, a bad buy costs real money.

Respond ONLY via the required structured JSON: an `approve` boolean and a short \
`reason` (one sentence)."""

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "approve": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["approve", "reason"],
}


class ClaudeOrchestrator:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.model = cfg["claude"]["model"]
        self.max_tokens = cfg["claude"]["max_tokens"]
        key = cfg["runtime"]["anthropic_api_key"]
        self.enabled = bool(key) and anthropic is not None
        self.client = anthropic.Anthropic(api_key=key) if self.enabled else None
        if not self.enabled:
            logger.warning("Claude disabled (no key/SDK). Borderline setups proceed on the "
                           "rule engine alone.")

    # ------------------------------------------------------------------ #
    def confirm_long(self, frames: dict[str, pd.DataFrame], fired: list[str],
                     count: int, need: int) -> tuple[bool, str]:
        # If Claude is unavailable we DON'T block: the gates+triggers already passed.
        if not self.enabled:
            return True, "Claude disabled; proceeding on rule engine."
        try:
            snapshot = self._snapshot(frames, fired, count, need)
            resp = self.client.messages.create(  # type: ignore[union-attr]
                model=self.model, max_tokens=self.max_tokens,
                system=LONG_SYSTEM_PROMPT,
                output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
                messages=[{"role": "user", "content": snapshot}],
            )
            text = next((b.text for b in resp.content if b.type == "text"), "{}")
            data = json.loads(text)
            return bool(data.get("approve", True)), str(data.get("reason", ""))
        except Exception as exc:
            logger.warning("Claude confirm_long failed ({}); proceeding on rules.", exc)
            return True, f"Claude error: {exc}"

    def daily_summary(self, stats: dict[str, Any]) -> str:
        if not self.enabled:
            return "(Claude disabled - no daily summary.)"
        try:
            resp = self.client.messages.create(  # type: ignore[union-attr]
                model=self.model, max_tokens=self.max_tokens,
                system="You are a careful, concise trading operations assistant.",
                messages=[{"role": "user", "content":
                           "Write a short, calm, plain-English daily summary (<=120 words) for a "
                           "non-technical user running a conservative long-only BTC spot bot on "
                           "Binance.US. Be factual, no hype. Data:\n\n"
                           + json.dumps(stats, indent=2, default=str)}],
            )
            return next((b.text for b in resp.content if b.type == "text"), "")
        except Exception as exc:
            logger.warning("Daily summary failed: {}", exc)
            return f"(Daily summary unavailable: {exc})"

    # ------------------------------------------------------------------ #
    @staticmethod
    def _snapshot(frames: dict[str, pd.DataFrame], fired: list[str],
                  count: int, need: int) -> str:
        def row(tf: str) -> dict[str, Any]:
            r = frames[tf].iloc[-1]
            def g(k):
                v = r.get(k)
                return None if v is None or pd.isna(v) else round(float(v), 4)
            return {"close": g("close"), "ema_50": g("ema_50"), "ema_200": g("ema_200"),
                    "rsi": g("rsi"), "macd_diff": g("macd_diff"), "adx": g("adx"),
                    "atr": g("atr")}
        snap = {
            "triggers_fired": fired,
            "trigger_count": count,
            "triggers_required": need,
            "frames": {tf: row(tf) for tf in frames},
        }
        return ("A borderline long setup passed all gates. Decide approve/reject.\n"
                + json.dumps(snap, indent=2))
