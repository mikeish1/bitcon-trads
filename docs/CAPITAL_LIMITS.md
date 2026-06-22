# Deployable-capital limit (the master cap)

The **deployable-capital limit** is the single, centralized ceiling on how much
capital the system may have committed to trades at any one time. It is a
first-class, user-adjustable parameter — not an implicit "spend up to 100%"
default — and it is fully backend-driven today and ready for a frontend.

It caps the **total envelope only**. It does *not* change how capital is ranked
and allocated to the best opportunities inside that envelope: the Donchian entry
signal, momentum rotation, per-asset cap, and available-cash limit all behave
exactly as before. The limit just lowers the lid.

---

## Concepts (precise terms)

| Term | Meaning |
|------|---------|
| **equity** | Total account value = free quote cash + marked-to-market holdings. |
| **available cash** | Free quote-currency balance not yet committed. |
| **committed capital** | Capital already tied up in open positions. |
| **deployable capital** | The envelope this policy permits, as a function of equity/cash. |
| **remaining capacity** | `deployable − committed`, floored at 0 (what a new trade may use). |

## The policy schema

One value object, [`DeployableCapitalPolicy`](../src/capital_policy.py), governs
every sleeve (spot / carry / etf):

| Field | Type | Rule | Meaning |
|-------|------|------|---------|
| `max_pct` | number or null | `(0, 1]` | Fraction of the `basis` deployable (`0.90` = 90%). |
| `max_usd` | number or null | `>= 0` | Absolute USD ceiling on deployed capital. |
| `basis` | enum | `equity` \| `cash` | What `max_pct` is a percentage of. |
| `precedence` | enum | `min` \| `max` \| `usd` \| `pct` | How to combine the two caps when **both** are set. |

**Invariant:** at least one of `max_pct` / `max_usd` must be set — an unbounded
limit is rejected at load time. All math is done in `Decimal` (exact on money).
When both caps are set, `precedence: min` (the default) deploys **no more than
the smaller** — the safe choice.

### YAML (config/trading_config.yaml)
```yaml
capital_policy:
  spot:
    max_pct: 0.90          # <= 90% of equity (the legacy default)
    # max_usd: 1000.0      # also cap deployed capital at $1,000 (binds via `min`)
    basis: "equity"
    precedence: "min"
  # carry: {max_usd: 1000.0}              # optional per-sleeve overrides
  # etf:   {max_pct: 0.95, basis: equity}
```
A flat block (`capital_policy: {max_pct: ...}`) applies to the **spot** sleeve.

### Environment variables (spot sleeve; "flip-it-fast", survive restarts)
```bash
MAX_DEPLOYED_CAPITAL_USD=1000        # hard USD ceiling
MAX_DEPLOYED_CAPITAL_PCT=0.90        # fraction of the basis
MAX_DEPLOYED_CAPITAL_BASIS=equity    # equity | cash
MAX_DEPLOYED_CAPITAL_PRECEDENCE=min  # min | max | usd | pct
```
Setting only `MAX_DEPLOYED_CAPITAL_USD` keeps the YAML percentage cap; the two
then combine via `precedence`.

### JSON example (the persisted override file, `config/capital_limits.json`)
```json
{ "spot": { "max_pct": 0.90, "max_usd": 1000.0, "basis": "equity", "precedence": "min" } }
```

## Resolution precedence (highest wins)

1. **Environment variables** (`MAX_DEPLOYED_CAPITAL_*`) — operator override.
2. **`config/capital_limits.json`** — written by the settings service / frontend;
   **hot-reloaded without a restart**.
3. **`capital_policy:`** block in `config/trading_config.yaml`.
4. **Legacy** values (`portfolio.max_total_exposure_pct`, sleeve `sleeve_usd`).

Each lower layer supplies defaults the higher layers merge over. The defaults
reproduce the **pre-refactor behavior exactly** (spot = 90% of equity, carry =
the fixed USD sleeve, ETF = 95% of equity), so upgrading changes nothing until
you opt in.

## Changing the limit at runtime

- **Edit a value** → write it through the settings service (or, manually, edit
  `config/capital_limits.json`). The running spot bot checks the file's mtime each
  cycle and **reloads the limit without a restart**; a bad value is rejected and
  the last-known-good limit is kept. Carry/ETF pick up changes on restart.
- **Persistence:** the JSON file and env vars both survive restarts. On ephemeral
  hosts (e.g. Railway), prefer the `MAX_DEPLOYED_CAPITAL_*` env vars.
- **Audit:** every change is appended to `config/capital_limits_audit.log`
  (`ts`, `sleeve`, `actor`, `before`, `after`).

## Frontend / REST

[`src/settings_service.py`](../src/settings_service.py) is the backend surface:
`get(sleeve)`, `get_all()`, and `update(payload, sleeve=, actor=)` return
structured, machine-readable results (validation errors carry
`{field, value, code, msg}`). An optional FastAPI adapter
([`src/api/capital_settings_api.py`](../src/api/capital_settings_api.py)) exposes:

| Method & path | Purpose |
|---------------|---------|
| `GET /capital-limits` | Effective policy for every sleeve. |
| `GET /capital-limits/schema` | Field metadata to render a form. |
| `GET /capital-limits/{sleeve}` | Effective policy for one sleeve. |
| `PUT /capital-limits/{sleeve}` | Validate + persist (422 + field errors on failure). |

FastAPI is **not** a base requirement (`pip install fastapi uvicorn` only if you
want the HTTP layer); the bots stay dependency-light and never import it.

## Enforcement points

The envelope is enforced wherever a new position is sized:

- spot: [`RiskManager.size_for_asset`](../src/risk_manager.py) and `size_rotation`
- carry: [`CarryRiskManager.can_open` / `size`](../src/carry/risk.py)
- etf: [`EtfRiskManager.size`](../src/etf/risk.py)

Each computes `remaining_capacity(equity, available_cash, committed)` from the
policy and clamps the spend to it — alongside the unchanged per-asset and
available-cash caps — so the system can never deploy more than the configured
limit or more than it actually holds.
