/**
 * SSE connection manager + live-data context.
 *
 * Opens an EventSource to /api/stream and pushes payloads straight into the
 * TanStack Query cache (setQueryData) so components re-render instantly without a
 * refetch. EventSource auto-reconnects; we track connection state and expose it as
 * a "live | polling | offline" indicator. If SSE never connects, the per-query
 * `refetchInterval` (see queries.ts) keeps everything fresh — graceful degradation.
 *
 * Note: native EventSource cannot send custom headers, so token auth for the stream
 * is passed as a `?token=` query param when present (the backend accepts it via the
 * same auth dependency in dev/prod behind the proxy). When the dashboard is open
 * (no token configured) nothing is appended.
 */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { qk } from "@/lib/queryKeys";
import { getAuthToken } from "@/lib/api";
import type {
  Decision,
  EquityUpdate,
  KpiSummary,
  OpenPosition,
  RiskAlert,
} from "@/types/api";

export type ConnState = "connecting" | "live" | "polling" | "offline";

interface StreamContextValue {
  state: ConnState;
  lastEventAt: number | null;
  lastEquity: EquityUpdate | null;
}

const StreamContext = createContext<StreamContextValue>({
  state: "connecting",
  lastEventAt: null,
  lastEquity: null,
});

export function useStreamStatus(): StreamContextValue {
  return useContext(StreamContext);
}

const MAX_RETRIES_BEFORE_POLLING = 3;

export function StreamProvider({ children }: { children: ReactNode }): JSX.Element {
  const qc = useQueryClient();
  const [state, setState] = useState<ConnState>("connecting");
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);
  const [lastEquity, setLastEquity] = useState<EquityUpdate | null>(null);
  const retriesRef = useRef(0);

  useEffect(() => {
    let es: EventSource | null = null;
    let disposed = false;

    const connect = () => {
      if (disposed) return;
      const token = getAuthToken();
      const url = token ? `/api/stream?token=${encodeURIComponent(token)}` : "/api/stream";
      es = new EventSource(url);

      es.onopen = () => {
        retriesRef.current = 0;
        setState("live");
      };

      const touch = () => setLastEventAt(Date.now());

      es.addEventListener("summary_update", (e) => {
        touch();
        qc.setQueryData<KpiSummary>(qk.summary, JSON.parse((e as MessageEvent).data));
      });
      es.addEventListener("positions_update", (e) => {
        touch();
        qc.setQueryData<OpenPosition[]>(qk.positions, JSON.parse((e as MessageEvent).data));
      });
      es.addEventListener("new_trade", (e) => {
        touch();
        const t = JSON.parse((e as MessageEvent).data) as { symbol: string; status?: string };
        qc.invalidateQueries({ queryKey: ["trades"] });
        qc.invalidateQueries({ queryKey: qk.perfStats });
        qc.invalidateQueries({ queryKey: qk.attribution });
        toast.info(`Trade update: ${t.symbol}`);
      });
      es.addEventListener("new_decision", (e) => {
        touch();
        const d = JSON.parse((e as MessageEvent).data) as Decision;
        qc.invalidateQueries({ queryKey: ["decisions"] });
        if (d.action === "BUY" || d.action === "SELL") {
          toast.message(`${d.action} · ${d.symbol ?? ""}`, { description: d.reasoning });
        }
      });
      es.addEventListener("risk_alert", (e) => {
        touch();
        const a = JSON.parse((e as MessageEvent).data) as RiskAlert;
        qc.invalidateQueries({ queryKey: qk.risk });
        if (a.severity === "critical") toast.error(a.message);
        else toast.warning(a.message);
      });
      es.addEventListener("equity_update", (e) => {
        touch();
        const u = JSON.parse((e as MessageEvent).data) as EquityUpdate;
        setLastEquity(u);
        qc.invalidateQueries({ queryKey: ["performance", "equity"] });
      });

      es.onerror = () => {
        es?.close();
        retriesRef.current += 1;
        if (retriesRef.current >= MAX_RETRIES_BEFORE_POLLING) {
          // Give up on SSE for now; the query refetchIntervals keep data fresh.
          setState("polling");
        } else {
          setState("connecting");
        }
        // EventSource normally self-reconnects, but we closed it to control backoff.
        if (!disposed) setTimeout(connect, Math.min(1000 * retriesRef.current, 5000));
      };
    };

    connect();
    const onOffline = () => setState("offline");
    const onOnline = () => {
      retriesRef.current = 0;
      connect();
    };
    window.addEventListener("offline", onOffline);
    window.addEventListener("online", onOnline);

    return () => {
      disposed = true;
      es?.close();
      window.removeEventListener("offline", onOffline);
      window.removeEventListener("online", onOnline);
    };
  }, [qc]);

  const value = useMemo(
    () => ({ state, lastEventAt, lastEquity }),
    [state, lastEventAt, lastEquity],
  );
  return <StreamContext.Provider value={value}>{children}</StreamContext.Provider>;
}
