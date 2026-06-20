"""
Claude orchestrator.

Wraps the Anthropic API for two narrow, infrequent jobs:

  1. get_expert_votes()  - 3 conservative "expert" votes used ONLY to break a
                           marginal ensemble tie (borderline 26-27 of 28 cases).
  2. daily_summary()     - a short plain-English recap of the day's activity.

Claude is deliberately used SPARINGLY: a tie-breaker and a reporter, never a
per-candle dependency. The full conservative system prompt is embedded below.

Default model is the cheap `claude-haiku-4-5`; override with the CLAUDE_MODEL
env var (e.g. `claude-opus-4-8` for maximum quality at higher cost).
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


# --------------------------------------------------------------------------- #
# The exact conservative system prompt used for borderline trade validation.   #
# --------------------------------------------------------------------------- #
EXPERT_SYSTEM_PROMPT = """\
You are a panel of three independent, highly conservative quantitative trading \
experts evaluating a single potential Bitcoin (BTC/USDT) trade on the 5-minute \
timeframe. A fast 28-model technical ensemble has already run and is MARGINAL \
(it slightly leans one way but did not reach the strict consensus required to \
trade). Your three votes are the tie-breakers.

Your overriding mandate is CAPITAL PRESERVATION. When in genuine doubt, vote to \
stay FLAT. A missed trade costs nothing; a bad trade costs real money. It is \
completely acceptable - and usually correct - for all three experts to vote 0.

Each of the three experts must independently return exactly one vote:
   1  = LONG  (open or favour a long position)
  -1  = SHORT (open or favour a short position)
   0  = FLAT  (no trade / abstain)

Decision guidance for every expert:
- Only vote with the trade direction if the evidence is genuinely strong and \
the trend, momentum, and volatility context all reasonably align.
- If signals conflict, the move looks extended/exhausted, volatility is \
abnormally high, or the setup is ambiguous, vote 0.
- Do NOT try to be clever or contrarian. Do NOT invent a thesis the data does \
not support. Prefer 0 over a low-conviction directional vote.
- The three experts should reason independently; it is fine for them to disagree.

You will be given a compact snapshot of current indicator readings. Base your \
votes only on that snapshot and sound risk management. Respond ONLY via the \
required structured JSON output."""


# JSON schema that constrains Claude's structured output.
_VOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "experts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "vote": {"type": "integer", "enum": [-1, 0, 1]},
                    "rationale": {"type": "string"},
                },
                "required": ["vote", "rationale"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["experts", "summary"],
}


class ClaudeOrchestrator:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.model = cfg["claude"]["model"]
        self.max_tokens = cfg["claude"]["max_tokens"]
        api_key = cfg["runtime"]["anthropic_api_key"]

        self.enabled = bool(api_key) and anthropic is not None
        self.client = anthropic.Anthropic(api_key=api_key) if self.enabled else None
        if not self.enabled:
            logger.warning(
                "Claude disabled (no ANTHROPIC_API_KEY or SDK missing). "
                "Marginal cases will resolve to FLAT (the safe default)."
            )

    # ------------------------------------------------------------------ #
    def get_expert_votes(
        self, df: pd.DataFrame, leaning: str, longs: int, shorts: int
    ) -> tuple[list[int], str]:
        """
        Ask the 3 Claude experts to vote on a marginal setup.
        Returns (votes, reasoning). On any failure, returns 3 abstentions.
        """
        if not self.enabled:
            return [0, 0, 0], "Claude disabled - defaulting marginal case to FLAT."

        snapshot = self._market_snapshot(df, leaning, longs, shorts)
        try:
            resp = self.client.messages.create(  # type: ignore[union-attr]
                model=self.model,
                max_tokens=self.max_tokens,
                system=EXPERT_SYSTEM_PROMPT,
                output_config={"format": {"type": "json_schema", "schema": _VOTE_SCHEMA}},
                messages=[{"role": "user", "content": snapshot}],
            )
            text = next((b.text for b in resp.content if b.type == "text"), "{}")
            data = json.loads(text)
            experts = data.get("experts", [])
            votes = [int(e.get("vote", 0)) for e in experts][: self.cfg["ensemble"]["claude_paths"]]
            # Pad/trim to exactly the configured number of Claude paths.
            while len(votes) < self.cfg["ensemble"]["claude_paths"]:
                votes.append(0)
            reasoning = data.get("summary", "")
            logger.info("Claude experts voted {} - {}", votes, reasoning)
            return votes, reasoning
        except Exception as exc:
            logger.warning("Claude expert call failed: {}", exc)
            return [0, 0, 0], f"Claude error: {exc}"

    # ------------------------------------------------------------------ #
    def daily_summary(self, stats: dict[str, Any]) -> str:
        """Generate a short plain-English daily recap. Best-effort."""
        if not self.enabled:
            return "(Claude disabled - no daily summary generated.)"

        prompt = (
            "Write a short, calm, plain-English daily summary (max ~120 words) for a "
            "non-technical user running an automated, conservative Bitcoin paper/live "
            "trading bot. Be factual, avoid hype, and gently flag anything that needs "
            "attention. Here is today's data as JSON:\n\n"
            + json.dumps(stats, indent=2, default=str)
        )
        try:
            resp = self.client.messages.create(  # type: ignore[union-attr]
                model=self.model,
                max_tokens=self.max_tokens,
                system="You are a careful, concise trading operations assistant.",
                messages=[{"role": "user", "content": prompt}],
            )
            return next((b.text for b in resp.content if b.type == "text"), "")
        except Exception as exc:
            logger.warning("Daily summary failed: {}", exc)
            return f"(Daily summary unavailable: {exc})"

    # ------------------------------------------------------------------ #
    @staticmethod
    def _market_snapshot(df: pd.DataFrame, leaning: str, longs: int, shorts: int) -> str:
        """Build a compact, token-cheap snapshot of the current market state."""
        r = df.iloc[-1]

        def g(key: str) -> Any:
            v = r.get(key)
            return None if v is None or pd.isna(v) else round(float(v), 4)

        snap = {
            "ensemble_leaning": leaning,
            "deterministic_longs": longs,
            "deterministic_shorts": shorts,
            "price": g("close"),
            "ema_21": g("ema_21"),
            "ema_55": g("ema_55"),
            "ema_200": g("ema_200"),
            "rsi_14": g("rsi_14"),
            "macd_diff": g("macd_diff"),
            "stoch_k": g("stoch_k"),
            "adx": g("adx"),
            "adx_pos": g("adx_pos"),
            "adx_neg": g("adx_neg"),
            "bb_high": g("bb_high"),
            "bb_low": g("bb_low"),
            "atr": g("atr"),
            "roc_10": g("roc_10"),
        }
        return (
            "Current BTC/USDT 5m indicator snapshot (vote independently, prefer FLAT "
            "when uncertain):\n" + json.dumps(snap, indent=2)
        )
