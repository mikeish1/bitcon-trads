/** Centralized TanStack Query keys so SSE handlers can invalidate precisely. */
export const qk = {
  summary: ["summary"] as const,
  positions: ["positions"] as const,
  risk: ["risk"] as const,
  health: ["health"] as const,
  config: ["config"] as const,
  capitalLimits: ["capital-limits"] as const,
  capitalSchema: ["capital-limits", "schema"] as const,
  trades: (params: unknown) => ["trades", params] as const,
  tradeAggregates: (params: unknown) => ["trades", "aggregates", params] as const,
  tradeDetail: (id: number) => ["trades", id] as const,
  decisions: (params: unknown) => ["decisions", params] as const,
  equity: (range: string) => ["performance", "equity", range] as const,
  perfStats: ["performance", "stats"] as const,
  attribution: ["performance", "attribution"] as const,
  regime: ["performance", "regime"] as const,
};
