/**
 * TanStack Query hooks — one per endpoint. SSE pushes most updates, but every
 * query also keeps a `refetchInterval` as the polling fallback so data is never
 * stale even if the stream drops (graceful degradation).
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, type TradeQuery } from "@/lib/api";
import { qk } from "@/lib/queryKeys";

const FAST = 15_000; // summary / positions
const MED = 30_000; // risk / health
const SLOW = 60_000; // performance

export const useSummary = () =>
  useQuery({ queryKey: qk.summary, queryFn: api.summary, refetchInterval: FAST });

export const usePositions = () =>
  useQuery({ queryKey: qk.positions, queryFn: api.positions, refetchInterval: FAST });

export const useRisk = () =>
  useQuery({ queryKey: qk.risk, queryFn: api.risk, refetchInterval: MED });

export const useHealth = () =>
  useQuery({ queryKey: qk.health, queryFn: api.health, refetchInterval: MED });

export const useConfig = () =>
  useQuery({ queryKey: qk.config, queryFn: api.config, staleTime: 5 * 60_000 });

export const useCapitalLimits = () =>
  useQuery({ queryKey: qk.capitalLimits, queryFn: api.capitalLimits, refetchInterval: SLOW });

export const useCapitalSchema = () =>
  useQuery({ queryKey: qk.capitalSchema, queryFn: api.capitalSchema, staleTime: Infinity });

export const useTrades = (params: TradeQuery) =>
  useQuery({
    queryKey: qk.trades(params),
    queryFn: () => api.trades(params),
    placeholderData: keepPreviousData,
  });

export const useTradeAggregates = (params: Pick<TradeQuery, "symbol" | "date_from" | "date_to">) =>
  useQuery({ queryKey: qk.tradeAggregates(params), queryFn: () => api.tradeAggregates(params) });

export const useTradeDetail = (id: number | null) =>
  useQuery({
    queryKey: qk.tradeDetail(id ?? -1),
    queryFn: () => api.tradeDetail(id as number),
    enabled: id !== null,
  });

export const useDecisions = (params: { limit?: number; cursor?: number | null; symbol?: string; action?: string }) =>
  useQuery({
    queryKey: qk.decisions(params),
    queryFn: () => api.decisions(params),
    placeholderData: keepPreviousData,
    refetchInterval: FAST,
  });

export const useEquity = (range: string) =>
  useQuery({ queryKey: qk.equity(range), queryFn: () => api.equity(range), refetchInterval: SLOW });

export const usePerfStats = () =>
  useQuery({ queryKey: qk.perfStats, queryFn: api.perfStats, refetchInterval: SLOW });

export const useAttribution = () =>
  useQuery({ queryKey: qk.attribution, queryFn: api.attribution, refetchInterval: SLOW });

export const useRegimeSplit = () =>
  useQuery({ queryKey: qk.regime, queryFn: api.regime, refetchInterval: SLOW });
