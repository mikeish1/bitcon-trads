/**
 * Typed fetch client for the dashboard API.
 *
 * - Same-origin `/api` (dev proxies to FastAPI; prod serves the SPA from FastAPI).
 * - Optional token auth: if the user supplies a token (stored in memory only, never
 *   localStorage), it is sent as `X-API-Key`. The backend is open when no
 *   DASHBOARD_TOKEN is configured server-side.
 * - Normalizes the backend's uniform error envelope `{ error: { code, message } }`
 *   into a thrown `ApiError`.
 */
import type {
  CapitalSchema,
  CapitalSimulation,
  ClosedTrade,
  ConfigView,
  Decision,
  EquitySeries,
  HealthStatus,
  KpiSummary,
  OpenPosition,
  Page,
  PerformanceStats,
  CoinAttribution,
  RegimeSplit,
  RiskGauges,
  SleeveLimit,
  TradeAggregates,
  TradeDetail,
} from "@/types/api";

export class ApiError extends Error {
  constructor(public status: number, message: string, public detail?: unknown) {
    super(message);
    this.name = "ApiError";
  }
}

let authToken: string | null = null;
/** Set/clear the in-memory API token (used by the optional auth prompt). */
export function setAuthToken(token: string | null): void {
  authToken = token && token.trim() ? token.trim() : null;
}
export function getAuthToken(): string | null {
  return authToken;
}

export function authHeaders(): Record<string, string> {
  return authToken ? { "X-API-Key": authToken } : {};
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...authHeaders(),
      ...(init?.headers ?? {}),
    },
  });

  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    let detail: unknown;
    try {
      const body = await res.json();
      if (body?.error?.message) message = body.error.message;
      detail = body?.detail ?? body?.error;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, message, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export interface TradeQuery {
  limit?: number;
  cursor?: number | null;
  symbol?: string;
  status?: "OPEN" | "CLOSED";
  date_from?: string;
  date_to?: string;
  sort?: string;
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

/** The full API surface, one method per backend endpoint. */
export const api = {
  summary: () => request<KpiSummary>("/summary"),
  positions: () => request<OpenPosition[]>("/positions"),
  trades: (q: TradeQuery = {}) =>
    request<Page<ClosedTrade>>(`/trades${qs({ ...q, cursor: q.cursor ?? undefined })}`),
  tradeAggregates: (q: Pick<TradeQuery, "symbol" | "date_from" | "date_to"> = {}) =>
    request<TradeAggregates>(`/trades/aggregates${qs(q)}`),
  tradeDetail: (id: number) => request<TradeDetail>(`/trades/${id}`),
  decisions: (q: { limit?: number; cursor?: number | null; symbol?: string; action?: string } = {}) =>
    request<Page<Decision>>(`/decisions${qs({ ...q, cursor: q.cursor ?? undefined })}`),
  equity: (range: string, maxPoints = 600) =>
    request<EquitySeries>(`/performance/equity${qs({ range, max_points: maxPoints })}`),
  perfStats: () => request<PerformanceStats>("/performance/stats"),
  attribution: () => request<CoinAttribution[]>("/performance/attribution"),
  regime: () => request<RegimeSplit>("/performance/regime"),
  risk: () => request<RiskGauges>("/risk"),
  config: () => request<ConfigView>("/config"),
  health: () => request<HealthStatus>("/health"),
  capitalLimits: () => request<Record<string, SleeveLimit>>("/capital-limits"),
  capitalSchema: () => request<CapitalSchema>("/capital-limits/schema"),
  capitalSimulate: (sleeve: string, body: Record<string, unknown>) =>
    request<CapitalSimulation>(`/capital-limits/${sleeve}/simulate`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  capitalUpdate: (sleeve: string, body: Record<string, unknown>) =>
    request<SleeveLimit & { saved?: unknown; shadowed_by_env?: boolean }>(
      `/capital-limits/${sleeve}`,
      { method: "PUT", body: JSON.stringify(body) },
    ),
};
