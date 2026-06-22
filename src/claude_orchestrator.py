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
from datetime import datetime, timezone
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

PROPOSE_SYSTEM_PROMPT = """\
You are a conservative quantitative TRADING-OPS reviewer for a long-only daily \
Donchian crypto bot. You are shown a statistical report comparing recent LIVE \
performance to a walk-forward BACKTEST baseline, with degradation flags.

Your job: propose AT MOST {max_n} narrowly-scoped parameter changes that could \
plausibly address the flagged degradation, OR propose nothing. You are advisory \
only - a human approves every change, so favour FEWER, HIGHER-CONFIDENCE, \
capital-preserving suggestions over churn.

HARD RULES:
 - Only propose keys that appear in the provided ALLOWLIST, and keep the proposed \
   value within that key's min/max bounds.
 - NEVER propose changes to risk/safety limits, capital policy, or portfolio caps \
   (they are not on the allowlist - do not invent keys).
 - One small step at a time (e.g. atr_trail_mult 3.0 -> 2.5, not 3.0 -> 2.0).
 - If the data is thin or the flags are weak, return an empty proposals list.

Respond ONLY via the required structured JSON."""

_PROPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "key": {"type": "string"},
                    "current": {"type": "number"},
                    "proposed": {"type": "number"},
                    "rationale": {"type": "string"},
                    "expected_impact": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["key", "current", "proposed", "rationale",
                             "expected_impact", "confidence"],
            },
        },
    },
    "required": ["summary", "proposals"],
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

    def propose_parameter_changes(self, context: dict[str, Any], tunable: dict[str, Any],
                                  max_n: int) -> dict[str, Any]:
        """Ask Claude for AT MOST `max_n` narrowly-scoped parameter proposals to
        address a live-vs-backtest degradation report. Structured JSON only; the
        caller re-validates every proposal against the allowlist + bounds + live
        config before anything is written. Returns {"summary","proposals":[...]}.
        Empty (and safe) when Claude is disabled."""
        if not self.enabled:
            return {"summary": "Claude disabled - no LLM proposals.", "proposals": []}
        try:
            user = ("ALLOWLIST (only these keys may be proposed; respect the bounds):\n"
                    + json.dumps(tunable, indent=2, default=str)
                    + "\n\nDEGRADATION REPORT:\n"
                    + json.dumps(context, indent=2, default=str)
                    + f"\n\nReturn at most {max_n} proposals. If none are clearly "
                      "warranted, return an empty list.")
            resp = self.client.messages.create(  # type: ignore[union-attr]
                model=self.model, max_tokens=self.max_tokens,
                system=PROPOSE_SYSTEM_PROMPT.format(max_n=max_n),
                output_config={"format": {"type": "json_schema", "schema": _PROPOSE_SCHEMA}},
                messages=[{"role": "user", "content": user}],
            )
            text = next((b.text for b in resp.content if b.type == "text"), "{}")
            data = json.loads(text)
            return {"summary": str(data.get("summary", "")),
                    "proposals": list(data.get("proposals", []))[:max_n]}
        except Exception as exc:
            logger.warning("Claude propose_parameter_changes failed ({}); no LLM proposals.", exc)
            return {"summary": f"Claude error: {exc}", "proposals": []}

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


# =========================================================================== #
# Ops agent: a daily/weekly live-vs-backtest feedback loop with a human gate.  #
# =========================================================================== #
# Hard blocklist (belt-and-suspenders alongside the allowlist): the agent can
# NEVER touch these, regardless of config.
DEFAULT_BLOCKED = ["safety", "capital_policy", "portfolio.max_total_exposure_pct",
                   "portfolio.per_asset_alloc_pct", "portfolio.max_concurrent_positions",
                   "risk.max_position_pct", "runtime"]


class OpsAgent:
    """Read-only analysis + LLM/rule proposals + gated, audited config changes.

    Never applies a change itself: `run_daily_ops`/`run_weekly_ops`/`run_research_refresh`
    only ever WRITE pending proposals to the approval gate. Applying is a separate,
    human-invoked CLI step (`--mode apply`)."""

    def __init__(self, cfg: dict[str, Any], orchestrator: "ClaudeOrchestrator | None" = None):
        from src.ops_proposals import ApprovalGate, sanitize_allowlist
        self.cfg = cfg
        self.oa = cfg.get("ops_agent", {}) or {}
        self.db_path = cfg["runtime"]["db_path"]
        self.claude = orchestrator or ClaudeOrchestrator(cfg)
        prop = self.oa.get("proposals", {}) or {}
        self.gate = ApprovalGate(prop.get("proposals_dir", "ops/proposals"),
                                 prop.get("audit_log", "ops/audit.log"))
        self.blocked = list(prop.get("blocked_keys", DEFAULT_BLOCKED))
        # Self-enforce the safety boundary: drop any allowlist key that overlaps the
        # blocklist or doesn't resolve, so a config typo can't make a safety key tunable.
        self.tunable, _warns = sanitize_allowlist(prop.get("tunable_keys", {}) or {},
                                                  self.blocked, cfg)
        for _w in _warns:
            logger.warning("Ops allowlist: {}", _w)
        self.max_proposals = int(prop.get("max_per_run", 3))
        self.approval_mode = str(prop.get("approval_mode", "manual")).lower()
        self.thresholds = self.oa.get("thresholds", {}) or {}
        comp = self.oa.get("comparison", {}) or {}
        self.live_lookback = int(comp.get("live_lookback_days", 60))
        self.window_months = int(comp.get("backtest_window_months", 24))
        self.min_live_days = int(comp.get("min_live_days", 20))

    # ------------------------------------------------------------------ #
    def build_comparison(self) -> dict[str, Any]:
        """Read-only: live metrics, backtest reference, statistical degradation flags."""
        from src.ops_metrics import live_metrics, backtest_reference
        from src.ops_stats import flag_degradation
        live = live_metrics(self.db_path, self.live_lookback, self.thresholds)
        bt = backtest_reference(self.cfg, live["days"] or self.live_lookback, self.window_months,
                                artifacts=self.oa.get("artifacts", {}))
        flags = flag_degradation(live["returns"], bt["returns"], live.get("window_metrics", {}),
                                 bt.get("window_dist", {}), self.thresholds)
        slip = live.get("slippage", {})
        if slip.get("fills") and slip.get("avg_slippage_bps", 0) > float(
                self.thresholds.get("slippage_alert_bps", 1e9)):
            flags["flags"].append({"metric": "slippage", "severity": "medium",
                                   "detail": f"avg slippage {slip['avg_slippage_bps']} bps exceeds "
                                             f"alert {self.thresholds.get('slippage_alert_bps')} bps",
                                   "investigate": "enable limit orders / passive offset / venue"})
            if flags["severity"] == "none":
                flags["severity"] = "medium"
        return {
            "generated_at": _utcnow(),
            "live": {"days": live["days"], "window_metrics": live.get("window_metrics", {}),
                     "trades": live.get("trades", {}), "slippage": slip},
            "backtest": {"days": bt["days"], "from_cache": bt.get("from_cache", False),
                         "artifact_key": bt.get("artifact_key", "")},
            "flags": flags,
            "current_values": {k: _resolve(self.cfg, k) for k in self.tunable},
            "sufficient_data": live["days"] >= self.min_live_days,
        }

    def _format_report(self, cmp: dict[str, Any]) -> str:
        f = cmp["flags"]
        lines = ["", "=" * 78, "  OPS REPORT  (live vs walk-forward backtest)", "=" * 78,
                 f"  generated {cmp['generated_at']}",
                 f"  live days: {cmp['live']['days']} (min {self.min_live_days}) | "
                 f"backtest days: {cmp['backtest']['days']} "
                 f"(artifact {'HIT' if cmp['backtest'].get('from_cache') else 'miss'} "
                 f"{cmp['backtest'].get('artifact_key', '-')})",
                 f"  live window metrics: {cmp['live']['window_metrics'] or 'n/a'}",
                 f"  live trades: {cmp['live']['trades']}",
                 f"  live slippage: {cmp['live']['slippage']}",
                 f"  SEVERITY: {f['severity'].upper()}"]
        if not cmp["sufficient_data"]:
            lines.append(f"  (insufficient live history - need >= {self.min_live_days} days; "
                         "no proposals generated.)")
        for fl in f["flags"]:
            lines.append(f"   - [{fl['severity']}] {fl['metric']}: {fl['detail']}")
            lines.append(f"       investigate: {fl['investigate']}")
        lines.append("=" * 78)
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    def run_ops(self, mode: str = "daily", write: bool = True) -> dict[str, Any]:
        cmp = self.build_comparison()
        report = self._format_report(cmp)
        logger.info(report)
        proposals: list[Any] = []
        degraded = cmp["flags"]["severity"] in ("medium", "high")
        if degraded and cmp["sufficient_data"] and self.approval_mode != "off":
            proposals = self._generate_proposals(cmp)
        path = None
        if proposals and write:
            path = self.gate.write_pending(proposals, {"report": report, "comparison": cmp}, mode)
        elif degraded and not proposals:
            logger.info("Ops: degradation flagged but no valid proposal cleared the allowlist/bounds.")
        return {"comparison": cmp, "report": report, "proposals": [p.to_dict() for p in proposals],
                "review_path": path}

    def run_daily_ops(self, write: bool = True) -> dict[str, Any]:
        return self.run_ops("daily", write)

    def run_weekly_ops(self, write: bool = True) -> dict[str, Any]:
        return self.run_ops("weekly", write)

    # ------------------------------------------------------------------ #
    def _generate_proposals(self, cmp: dict[str, Any]) -> list[Any]:
        from src.ops_proposals import Proposal, validate_proposal
        context = {"severity": cmp["flags"]["severity"], "flags": cmp["flags"]["flags"],
                   "stats": cmp["flags"]["stats"], "live": cmp["live"],
                   "current_values": cmp["current_values"]}
        raw = self.claude.propose_parameter_changes(context, self.tunable, self.max_proposals)
        out: list[Proposal] = []
        for d in raw.get("proposals", []):
            key = str(d.get("key", ""))
            live = _resolve(self.cfg, key)
            p = Proposal(key=key, current=live, proposed=d.get("proposed"),
                         rationale=str(d.get("rationale", "")),
                         expected_impact=str(d.get("expected_impact", "")),
                         confidence=str(d.get("confidence", "low")), source="llm")
            ok, why = validate_proposal(p, self.cfg, self.tunable, self.blocked)
            if ok:
                out.append(p)
            else:
                logger.info("Ops: dropped LLM proposal {} -> {} ({}).", key, d.get("proposed"), why)
        if not out:                                   # deterministic fallback (no API key etc.)
            out = self._rule_based_proposals(cmp)
        return out[:self.max_proposals]

    def _rule_based_proposals(self, cmp: dict[str, Any]) -> list[Any]:
        """Conservative, deterministic fallback when the LLM yields nothing: on
        flagged degradation, step per-trade risk DOWN toward its floor to preserve
        capital while a human investigates. Only ever touches allowlisted keys."""
        from src.ops_proposals import Proposal, validate_proposal
        out: list[Proposal] = []
        if not (cmp.get("flags", {}) or {}).get("flags"):
            return out                          # no degradation -> nothing to propose
        for key in ("risk.risk_budget.risk_per_trade_pct", "risk.risk_per_trade_pct"):
            spec = self.tunable.get(key)
            live = _resolve(self.cfg, key)
            if spec is None or live is None:
                continue
            lo = float(spec.get("min", 0.0))
            proposed = round(max(lo, float(live) * 0.8), 6)
            p = Proposal(key=key, current=live, proposed=proposed,
                         rationale="Live performance is statistically below the backtest baseline; "
                                   "reduce per-trade risk ~20% to preserve capital while investigating.",
                         expected_impact="Lower drawdown and exposure until live tracks backtest again.",
                         confidence="medium", source="rule")
            ok, _ = validate_proposal(p, self.cfg, self.tunable, self.blocked)
            if ok:
                out.append(p)
                break          # one conservative change is enough
        return out

    # ------------------------------------------------------------------ #
    def run_research_refresh(self, write: bool = True) -> dict[str, Any]:
        """Re-run a small entry/ATR sweep (in-process, reproducible) on the recent
        window and, if a setting clearly beats the live config on OOS Calmar (MAR),
        propose promoting it - through the same approval gate."""
        from src.ops_proposals import Proposal, validate_proposal
        res = self._research_sweep()
        if not res.get("ranked"):
            logger.info("Ops research: insufficient data for a sweep.")
            return {"sweep": res, "proposals": [], "review_path": None}
        # Persist the ranked sweep table - the reproducible evidence behind any
        # promotion proposal (auditable after the fact).
        art = self.oa.get("artifacts", {}) or {}
        if art.get("enabled", False):
            try:
                import os as _os
                d = art.get("dir", "ops/artifacts"); _os.makedirs(d, exist_ok=True)
                ap = _os.path.join(d, f"research_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json")
                with open(ap, "w", encoding="utf-8") as fh:
                    json.dump(res, fh, default=str)
                res["artifact_path"] = ap
            except Exception as exc:
                logger.warning("Ops research: could not persist sweep ({}).", exc)
        cur, best = res["current"], res["ranked"][0]
        improve = float((self.oa.get("research", {}) or {}).get("min_improvement_pct", 0.15))
        proposals: list[Proposal] = []
        if best["mar"] > cur["mar"] * (1 + improve) and best["mar"] > 0:
            for key, cur_v, new_v in (
                    ("strategy.donchian.entry_period", cur["entry"], best["entry"]),
                    ("strategy.donchian.atr_trail_mult", cur["atr"], best["atr"])):
                if abs(float(cur_v) - float(new_v)) < 1e-9 or key not in self.tunable:
                    continue
                p = Proposal(key=key, current=_resolve(self.cfg, key), proposed=new_v,
                             rationale=f"Recent {self.window_months}m sweep: ({best['entry']},"
                                       f"{best['atr']}) OOS MAR {best['mar']:.2f} vs current "
                                       f"({cur['entry']},{cur['atr']}) {cur['mar']:.2f} "
                                       f"(+{(best['mar']/cur['mar']-1):.0%}).",
                             expected_impact="Higher risk-adjusted return (Calmar) on recent data.",
                             confidence="medium", source="research")
                ok, why = validate_proposal(p, self.cfg, self.tunable, self.blocked)
                if ok:
                    proposals.append(p)
                else:
                    logger.info("Ops research: dropped {} ({}).", key, why)
        path = None
        if proposals and write:
            path = self.gate.write_pending(proposals, {"sweep": res}, "research")
        logger.info("Ops research: best ({},{}) MAR {:.2f} vs current ({},{}) MAR {:.2f}; {} proposal(s).",
                    best["entry"], best["atr"], best["mar"], cur["entry"], cur["atr"], cur["mar"],
                    len(proposals))
        return {"sweep": res, "proposals": [p.to_dict() for p in proposals], "review_path": path}

    def _research_sweep(self) -> dict[str, Any]:
        """Reproducible in-process entry/ATR sweep over the recent window using the
        validated improve_backtest engine. Returns current + ranked configs by OOS MAR."""
        import numpy as np
        import pandas as pd
        from datetime import datetime, timezone
        from src.improve_backtest import _daily, run_config
        rcfg = self.oa.get("research", {}) or {}
        entry_grid = [int(x) for x in rcfg.get("entry_grid", [30, 40, 55])]
        atr_grid = [float(x) for x in rcfg.get("atr_grid", [2.5, 3.0, 3.5])]
        ex = self.cfg.get("execution", {})
        fee, slip = float(ex.get("taker_fee_pct", 0.001)), float(ex.get("paper_slippage_pct", 0.0007))
        capital = float(self.cfg["risk"]["default_capital_usd"])
        bases = [str(b).upper() for b in self.cfg["universe"]["bases"]]
        if "BTC" not in bases:
            bases = ["BTC"] + bases
        cutoff = pd.Timestamp(datetime.now(timezone.utc)) - pd.DateOffset(months=self.window_months)
        frames: dict[str, pd.DataFrame] = {}
        for b in bases:
            try:
                df = _daily(b, 8.0, "auto")
            except Exception:
                continue
            idx = pd.DatetimeIndex(pd.to_datetime(df["timestamp"], utc=True))
            df = df[idx >= cutoff].reset_index(drop=True)        # both tz-aware UTC
            if len(df) > 60:
                frames[b] = df
        if len(frames) < 1:
            return {"ranked": [], "current": None}
        # OOS = recent ~30% of the window.
        split = np.datetime64(pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)
                              - pd.DateOffset(days=int(self.window_months * 30 * 0.3)))
        regime_ma = (self.cfg["strategy"].get("btc_regime", {}) or {}).get("ma_period", 100) \
            if (self.cfg["strategy"].get("btc_regime", {}) or {}).get("enabled", False) else 0

        def score(entry: int, atr: float) -> dict[str, Any]:
            r = run_config(frames, entry, atr, capital, fee, slip, split,
                           regime_ma=regime_ma, vol_target=False)
            mar = r["oos"].get("mar", 0.0)
            mar = 0.0 if mar in ("inf", None) else float(mar)
            return {"entry": entry, "atr": atr, "mar": mar,
                    "cagr": r["oos"].get("cagr_pct"), "max_dd": r["oos"].get("max_dd_pct")}

        cur_entry = int(self.cfg["strategy"]["donchian"]["entry_period"])
        cur_atr = float(self.cfg["strategy"]["donchian"]["atr_trail_mult"])
        current = score(cur_entry, cur_atr)
        ranked = [score(e, a) for e in entry_grid for a in atr_grid]
        ranked.sort(key=lambda d: d["mar"], reverse=True)
        return {"ranked": ranked, "current": current, "split": str(split)}


def _resolve(cfg: dict[str, Any], dotted: str) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
def main() -> None:
    import argparse
    import sys

    from src.config import _CONFIG_PATH, load_config

    ap = argparse.ArgumentParser(description="Trading-ops agent (live vs backtest, gated proposals).")
    ap.add_argument("--mode", required=True,
                    choices=["daily", "weekly", "research", "list", "approve", "apply"])
    ap.add_argument("--file", type=str, default=None, help="Review file for approve/apply.")
    ap.add_argument("--approver", type=str, default="", help="Your name (required to approve/apply).")
    ap.add_argument("--indices", type=str, default=None, help="Comma proposal indices to approve (default all).")
    ap.add_argument("--no-write", action="store_true", help="Analyze only; don't write proposals.")
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | {message}")
    cfg = load_config()
    agent = OpsAgent(cfg)

    if args.mode in ("daily", "weekly"):
        agent.run_ops(args.mode, write=not args.no_write)
    elif args.mode == "research":
        agent.run_research_refresh(write=not args.no_write)
    elif args.mode == "list":
        pend = agent.gate.list_pending()
        if not pend:
            print("No proposal files in", agent.gate.dir)
        for path, doc in pend:
            print(f"\n{path}  [{doc.get('status')}]  ({doc.get('mode')}, {doc.get('created_at')})")
            for i, pr in enumerate(doc.get("proposals", [])):
                print(f"  [{i}] {pr['status']:<8} {pr['key']}: {pr['current']} -> {pr['proposed']} "
                      f"({pr.get('confidence')}, {pr.get('source')})  {pr.get('rationale','')}")
    elif args.mode == "approve":
        if not args.file or not args.approver:
            print("approve needs --file and --approver"); return
        idx = [int(x) for x in args.indices.split(",")] if args.indices else None
        n = agent.gate.approve(args.file, args.approver, idx)
        print(f"Approved {n} proposal(s) in {args.file}.")
    elif args.mode == "apply":
        if not args.file or not args.approver:
            print("apply needs --file and --approver"); return
        res = agent.gate.apply_approved(args.file, str(_CONFIG_PATH), cfg,
                                        agent.tunable, agent.blocked, args.approver)
        print("Applied:", res["applied"])
        print("Skipped:", res["skipped"])


if __name__ == "__main__":
    import os
    import sys
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    main()
