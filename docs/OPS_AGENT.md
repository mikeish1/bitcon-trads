# Trading-ops agent (live-vs-backtest feedback loop)

A daily/weekly agent that compares LIVE performance to a walk-forward BACKTEST,
flags statistically significant degradation, and proposes a **few, narrow**
parameter changes — **never applying anything without explicit human approval**.
Default **off**. Lives in `src/claude_orchestrator.py` (`OpsAgent`) with thin
helpers in `src/ops_stats.py`, `src/ops_metrics.py`, `src/ops_proposals.py`.

## What it does
1. **Compare** (read-only): live equity/returns/trade-stats/slippage from the
   shared SQLite vs a backtest reference (equal-weight Donchian on cached candles,
   same params the bot trades), over a recent window.
2. **Flag degradation** with real statistics (no SciPy):
   - Mann-Whitney U + Welch's t on live vs backtest daily returns (p-values).
   - Bootstrap CI on the mean-return difference (flag when the whole CI is adverse).
   - z-score of live window metrics (Calmar/maxDD/return) vs the backtest's
     rolling-window distribution.
   Flags carry severity (low/medium/high) and an "investigate" hint.
3. **Propose** (only on medium/high severity, only with ≥ `min_live_days` of data):
   Claude returns ≤ `max_per_run` structured proposals; each is re-validated in code
   against an **allowlist + bounds + drift guard**. If Claude is unavailable, a
   conservative deterministic fallback proposes a small per-trade-risk reduction.
4. **Gate**: proposals are written to `ops/proposals/<ts>_<mode>_review.yaml` as
   `pending`. Nothing is applied until a human approves.
5. **Apply** (separate human step): each approved+valid proposal is applied as a
   **surgical, comment-preserving** YAML edit (one scalar), then the file is reloaded
   and verified to have changed *exactly that one leaf* — else the backup is restored.
   Every action is appended to `ops/audit.log`.
6. **Research refresh**: re-runs an entry/ATR sweep in-process on recent data and,
   if a setting beats the live config on OOS Calmar by ≥ `min_improvement_pct`,
   proposes promoting it — through the same gate.

## Safety guarantees (enforced in code, not by convention)
- **Allowlist only**: the agent can propose *only* keys in
  `ops_agent.proposals.tunable_keys`, each within its `min/max`. Safety rails
  (`safety.*`, `capital_policy`, `portfolio` caps, `risk.max_position_pct`,
  `runtime`) are on a hard **blocklist** and can never be applied.
- **Drift guard**: a proposal's stated `current` must still match the live config.
- **No auto-apply**: generating runs never write to the config; only `--mode apply`
  with an `--approver` does, and only for `approved` proposals.
- **Auditable**: timestamped JSONL audit log + per-edit config backup.

## Commands
```bash
python -m src.claude_orchestrator --mode daily      # analyze + write pending proposals
python -m src.claude_orchestrator --mode weekly
python -m src.claude_orchestrator --mode research   # sweep recent data -> promotion proposals
python -m src.claude_orchestrator --mode list       # show pending proposals
python -m src.claude_orchestrator --mode approve --file ops/proposals/<f>.yaml --approver YOU
python -m src.claude_orchestrator --mode apply   --file ops/proposals/<f>.yaml --approver YOU
```
Schedule daily/weekly via cron, or set `ops_agent.enabled: true` to also run the
daily check from the bot's heartbeat (`OPS_AGENT_ENABLED=1`). Monthly: cron the
`--mode research` job.

## Example proposal (review file)
```yaml
created_at: 2026-06-22T01:55:00Z
mode: daily
status: pending
proposals:
  - key: risk.risk_budget.risk_per_trade_pct
    current: 0.0075
    proposed: 0.006
    rationale: Live performance is statistically below the backtest baseline; reduce
      per-trade risk ~20% to preserve capital while investigating.
    expected_impact: Lower drawdown and exposure until live tracks backtest again.
    confidence: medium
    source: rule          # or "llm" / "research"
    status: pending       # set to "approved" (or use --mode approve) before apply
```

## Config (`ops_agent`)
See `config/trading_config.yaml`. Key knobs: `comparison.{live_lookback_days,
backtest_window_months, min_live_days}`, `thresholds.{pvalue, dd_z,
slippage_alert_bps}`, `research.{min_improvement_pct, entry_grid, atr_grid}`,
`proposals.{approval_mode, max_per_run, tunable_keys, blocked_keys}`.

## Demonstrated run (synthetic degraded live data, 45 days)
```
SEVERITY: HIGH
 - [high] daily_return_distribution: live mean -0.80%/day < backtest -0.15%/day;
     MWU p=0.000, Welch p=0.000, mean-diff CI [-0.85%,-0.45%] (adverse)
 - [high] max_dd: live -0.298 is -4.1σ below the backtest distribution (n=63)
PROPOSAL: risk.risk_budget.risk_per_trade_pct 0.0075 -> 0.006  [medium/rule]
APPLY (after approval): edited + verified; audit.log records write/approve/apply.
```
