import { formatDistanceToNow, parseISO } from "date-fns";
import { Activity, Database, Clock, CircleCheck, CircleAlert, CircleX } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { MetricCard } from "@/components/trading/MetricCard";
import { ModeBadge, RegimeBadge, CircuitBreakerBadge } from "@/components/trading/StatusBadges";
import { useHealth } from "@/hooks/queries";
import { fmtBytes, fmtDuration } from "@/lib/utils";
import type { HealthState } from "@/types/api";

const STATE: Record<HealthState, { icon: typeof CircleCheck; color: string; label: string }> = {
  healthy: { icon: CircleCheck, color: "text-pos", label: "Healthy" },
  degraded: { icon: CircleAlert, color: "text-warn", label: "Degraded" },
  stale: { icon: CircleX, color: "text-neg", label: "Stale" },
  starting: { icon: Clock, color: "text-muted-foreground", label: "Starting" },
};

export default function Health(): JSX.Element {
  const { data: h, isLoading } = useHealth();

  if (isLoading || !h) {
    return <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-24" />)}</div>;
  }

  const st = STATE[h.status];
  const StIcon = st.icon;
  const ageHours = h.last_bot_activity_age_seconds != null ? h.last_bot_activity_age_seconds / 3600 : null;

  return (
    <div className="space-y-4">
      <Card className={h.status === "stale" ? "border-neg/50" : h.status === "degraded" ? "border-warn/50" : undefined}>
        <CardContent className="flex flex-wrap items-center justify-between gap-3 p-4">
          <div className="flex items-center gap-3">
            <div className={`rounded-full bg-muted p-2 ${st.color}`}>
              <StIcon className="size-6" />
            </div>
            <div>
              <p className="text-lg font-semibold">{st.label}</p>
              <p className="text-xs text-muted-foreground">
                {h.last_bot_activity_at
                  ? `Last bot DB activity ${formatDistanceToNow(parseISO(h.last_bot_activity_at), { addSuffix: true })}`
                  : "Awaiting first bot write"}
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <ModeBadge mode={h.mode} />
            <RegimeBadge enabled={h.regime_enabled} on={h.regime_on} />
            <CircuitBreakerBadge tripped={h.circuit_breaker_tripped} />
          </div>
        </CardContent>
      </Card>

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <MetricCard label="Open positions" value={h.open_positions} />
        <MetricCard label="Poll interval" value={fmtDuration(h.poll_seconds / 3600)} sub="bot cycle cadence" />
        <MetricCard label="Last activity" value={ageHours != null ? fmtDuration(ageHours) : "—"} sub="ago" />
        <MetricCard label="Snapshots" value={h.snapshot_count} sub="equity samples" />
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2"><Database className="size-4" /> Database</CardTitle></CardHeader>
          <CardContent className="space-y-1 text-sm">
            <Row label="Connection"><Badge variant={h.db_ok ? "pos" : "neg"}>{h.db_ok ? "OK (read-only)" : "Error"}</Badge></Row>
            <Row label="Size">{fmtBytes(h.db_size_bytes)}</Row>
            <Row label="Equity snapshots">{h.snapshot_count}</Row>
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2"><Activity className="size-4" /> Bot activity</CardTitle></CardHeader>
          <CardContent className="space-y-1 text-sm">
            <Row label="Last decision">
              {h.last_decision_at ? formatDistanceToNow(parseISO(h.last_decision_at), { addSuffix: true }) : "—"}
            </Row>
            <Row label="Last trade">
              {h.last_trade_opened_at ? formatDistanceToNow(parseISO(h.last_trade_opened_at), { addSuffix: true }) : "—"}
            </Row>
            <Row label="Server time">{new Date(h.server_time).toLocaleString()}</Row>
          </CardContent>
        </Card>
      </div>

      <p className="text-xs text-muted-foreground">
        Liveness is inferred from the age of the newest database write. The bot trades daily candles
        on a {fmtDuration(h.poll_seconds / 3600)} poll and only logs on new candles, so sparse activity
        is normal — thresholds are generous (configurable server-side).
      </p>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }): JSX.Element {
  return (
    <div className="flex items-center justify-between border-b border-border/30 py-1.5">
      <span className="text-muted-foreground">{label}</span>
      <span className="tnum font-medium">{children}</span>
    </div>
  );
}
