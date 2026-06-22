/**
 * TypeScript mirror of the backend Pydantic v2 models (web/models.py).
 *
 * These are hand-kept in sync with the FastAPI contract. The backend also serves
 * an OpenAPI schema at /api/openapi.json, so these can be regenerated with
 * `openapi-typescript` if preferred; they are written by hand here so the app has
 * zero codegen step and stays readable. Field names + types match exactly.
 */

export type TradingMode = "PAPER" | "PAPER-BROKER" | "LIVE";

export interface ModeBadge {
  mode: TradingMode;
  real_money: boolean;
  place_orders: boolean;
  exchange_id: string;
  quote_ccy: string;
}

export interface KpiSummary {
  mode: ModeBadge;
  equity: number;
  cash: number;
  open_value: number;
  unrealized_pnl_usd: number;
  day_return_pct: number;
  week_return_pct: number;
  pnl_today_usd: number;
  open_positions: number;
  closed_today: number;
  trades_today: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  consecutive_losses: number;
  equity_basis: "paper_ledger" | "approx";
  as_of: string;
  price_age_seconds: number;
}

export interface OpenPosition {
  id: number;
  symbol: string;
  opened_at: string;
  entry_price: number;
  qty: number;
  cost_usd: number;
  initial_stop: number;
  current_stop: number;
  peak_price: number;
  mode: string;
  reason: string;
  last_price: number;
  market_value: number;
  unrealized_pnl_usd: number;
  unrealized_pnl_pct: number;
  distance_to_stop_pct: number;
  r_multiple: number;
  drawdown_from_peak_pct: number;
  pct_of_per_asset_cap: number;
  age_hours: number;
  price_is_stale: boolean;
}

export interface ClosedTrade {
  id: number;
  symbol: string;
  opened_at: string;
  closed_at: string | null;
  entry_price: number;
  exit_price: number | null;
  qty: number;
  cost_usd: number;
  pnl_usd: number | null;
  return_pct: number | null;
  hold_hours: number | null;
  r_multiple: number | null;
  mode: string;
  reason: string;
}

export interface Page<T> {
  items: T[];
  next_cursor: number | null;
  has_more: boolean;
  total_estimate: number;
}

export interface TradeAggregates {
  count: number;
  total_pnl_usd: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
}

export interface TradeDetail {
  trade: ClosedTrade;
  decisions: Decision[];
}

export interface Decision {
  id: number;
  ts: string;
  symbol: string | null;
  action: "BUY" | "SELL" | "HOLD" | string;
  conviction: number;
  consulted_claude: boolean;
  reasoning: string;
}

export interface GaugeValue {
  key: string;
  label: string;
  current: number;
  limit: number;
  pct_of_limit: number;
  breached: boolean;
  tooltip_plain: string;
  tooltip_math: string;
}

export interface RiskGauges {
  daily_loss: GaugeValue;
  weekly_loss: GaugeValue;
  consecutive_losses: GaugeValue;
  trades_today: GaugeValue;
  concurrent_positions: GaugeValue;
  total_exposure: GaugeValue;
  circuit_breaker_tripped: boolean;
  regime_enabled: boolean;
  regime_on: boolean | null;
  as_of: string;
}

export interface EquityPoint {
  ts: string;
  equity: number;
  drawdown_pct: number;
}

export interface EquitySeries {
  points: EquityPoint[];
  start_equity: number | null;
  end_equity: number | null;
  max_drawdown_pct: number;
  downsampled: boolean;
  available: boolean;
}

export interface PerformanceStats {
  closed_trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  gross_profit_usd: number;
  gross_loss_usd: number;
  profit_factor: number | null;
  expectancy_usd: number;
  avg_win_usd: number;
  avg_loss_usd: number;
  avg_hold_hours: number;
  best_trade_usd: number;
  worst_trade_usd: number;
  max_drawdown_pct: number;
}

export interface CoinAttribution {
  base: string;
  closed_trades: number;
  realized_pnl_usd: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
}

export interface RegimeBucket {
  regime_on: boolean | null;
  label: string;
  closed_trades: number;
  realized_pnl_usd: number;
  win_rate_pct: number;
}

export interface RegimeSplit {
  buckets: RegimeBucket[];
  available: boolean;
}

export interface ConfigView {
  mode: ModeBadge;
  universe: string[];
  strategy: Record<string, unknown>;
  risk: Record<string, unknown>;
  exits: Record<string, unknown>;
  safety: Record<string, unknown>;
  portfolio: Record<string, unknown>;
  market: Record<string, unknown>;
  capital_limits: Record<string, SleeveLimit>;
  redacted_keys: string[];
}

export interface SleeveLimit {
  ok: boolean;
  sleeve: string;
  source: string;
  policy?: CapitalPolicy;
  description?: string;
  errors?: PolicyError[];
}

export interface CapitalPolicy {
  label: string;
  max_pct: number | null;
  max_usd: number | null;
  basis: "equity" | "cash";
  precedence: "min" | "max" | "usd" | "pct";
}

export interface PolicyError {
  field: string;
  value: unknown;
  code: string;
  msg: string;
}

export interface CapitalSimulation {
  sleeve: string;
  valid: boolean;
  errors: PolicyError[];
  policy: CapitalPolicy | null;
  description: string | null;
  equity: number;
  available_cash: number;
  committed: number;
  deployable_capital: number;
  remaining_capacity: number;
  current_exposure_pct: number | null;
}

export interface CapitalSchema {
  fields: Record<string, { type: string; min?: number; max?: number; required?: boolean; help: string; choices?: string[]; default?: string }>;
  constraints: string[];
}

export type HealthState = "healthy" | "degraded" | "stale" | "starting";

export interface HealthStatus {
  status: HealthState;
  db_ok: boolean;
  mode: TradingMode;
  open_positions: number;
  last_bot_activity_at: string | null;
  last_bot_activity_age_seconds: number | null;
  last_decision_at: string | null;
  last_trade_opened_at: string | null;
  poll_seconds: number;
  regime_enabled: boolean;
  regime_on: boolean | null;
  circuit_breaker_tripped: boolean;
  snapshot_count: number;
  db_size_bytes: number;
  server_time: string;
}

// ---- SSE event payloads --------------------------------------------------- //
export type SseEventName =
  | "summary_update"
  | "positions_update"
  | "new_trade"
  | "new_decision"
  | "risk_alert"
  | "equity_update"
  | "error";

export interface RiskAlert {
  kind: string;
  severity: "critical" | "warning";
  message: string;
}

export interface EquityUpdate {
  ts: string;
  equity: number;
  drawdown_pct: number;
}
