import { lazy, Suspense, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { MetricCard } from "@/components/trading/MetricCard";
import { EmptyState } from "@/components/trading/EmptyState";
import { useEquity, usePerfStats, useAttribution, useRegimeSplit } from "@/hooks/queries";
import { fmtUsd, fmtUsdSigned, fmtPct, fmtNum, fmtDuration } from "@/lib/utils";

const EquityChart = lazy(() => import("@/components/charts/EquityChart"));
const AttributionChart = lazy(() => import("@/components/charts/AttributionChart"));

const RANGES = ["7d", "30d", "90d", "all"] as const;

export default function Performance(): JSX.Element {
  const [range, setRange] = useState<(typeof RANGES)[number]>("30d");
  const equity = useEquity(range);
  const stats = usePerfStats();
  const attribution = useAttribution();
  const regime = useRegimeSplit();
  const s = stats.data;

  return (
    <div className="space-y-4">
      <section className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <MetricCard label="Closed trades" loading={stats.isLoading} value={s?.closed_trades ?? "—"} />
        <MetricCard label="Win rate" term="winRate" loading={stats.isLoading} value={s ? `${s.win_rate_pct}%` : "—"} />
        <MetricCard label="Profit factor" term="profitFactor" loading={stats.isLoading}
          tone={(s?.profit_factor ?? 0) >= 1 ? "pos" : "neg"}
          value={s?.profit_factor != null ? fmtNum(s.profit_factor, 2) : "—"} />
        <MetricCard label="Expectancy" term="expectancy" loading={stats.isLoading}
          tone={(s?.expectancy_usd ?? 0) >= 0 ? "pos" : "neg"} value={fmtUsdSigned(s?.expectancy_usd)} />
        <MetricCard label="Max drawdown" term="maxDrawdown" loading={stats.isLoading} tone="neg"
          value={fmtPct(s?.max_drawdown_pct)} />
        <MetricCard label="Avg hold" loading={stats.isLoading} value={fmtDuration(s?.avg_hold_hours)} />
      </section>

      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle>Equity &amp; drawdown</CardTitle>
          <Tabs value={range} onValueChange={(v) => setRange(v as (typeof RANGES)[number])}>
            <TabsList>
              {RANGES.map((r) => <TabsTrigger key={r} value={r}>{r.toUpperCase()}</TabsTrigger>)}
            </TabsList>
          </Tabs>
        </CardHeader>
        <CardContent>
          {equity.isLoading ? (
            <Skeleton className="h-[300px]" />
          ) : equity.data?.available && equity.data.points.length > 1 ? (
            <>
              <Suspense fallback={<Skeleton className="h-[300px]" />}>
                <EquityChart series={equity.data} />
              </Suspense>
              {equity.data.downsampled && (
                <p className="mt-2 text-right text-[10px] text-muted-foreground">Downsampled for display</p>
              )}
            </>
          ) : (
            <EmptyState
              title="Equity history is still being collected"
              description="The snapshot sampler records equity every minute. The curve appears once a few points exist."
            />
          )}
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Per-coin attribution</CardTitle></CardHeader>
          <CardContent>
            {attribution.isLoading ? (
              <Skeleton className="h-48" />
            ) : attribution.data && attribution.data.length > 0 ? (
              <Suspense fallback={<Skeleton className="h-48" />}>
                <AttributionChart data={attribution.data} />
              </Suspense>
            ) : (
              <EmptyState title="No realized P&L yet" />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              Regime impact <Badge variant="regime">BTC filter</Badge>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {regime.isLoading ? (
              <Skeleton className="h-48" />
            ) : regime.data?.available && regime.data.buckets.length > 0 ? (
              <div className="space-y-3">
                {regime.data.buckets.map((b) => (
                  <div key={b.label} className="rounded-md border border-border p-3">
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium">{b.label}</span>
                      <span className={`tnum text-sm font-semibold ${b.realized_pnl_usd >= 0 ? "text-pos" : "text-neg"}`}>
                        {fmtUsdSigned(b.realized_pnl_usd)}
                      </span>
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground tnum">
                      {b.closed_trades} trades · {b.win_rate_pct}% win rate
                    </p>
                  </div>
                ))}
                <p className="text-xs text-muted-foreground">
                  Compares P&amp;L while BTC was in an uptrend vs. below its moving average — the value the
                  regime filter is designed to capture.
                </p>
              </div>
            ) : (
              <EmptyState title="Regime split unavailable" description="Needs equity snapshots with regime flags plus some closed trades." />
            )}
          </CardContent>
        </Card>
      </div>

      {s && (
        <Card>
          <CardHeader><CardTitle>Detailed statistics</CardTitle></CardHeader>
          <CardContent className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3 lg:grid-cols-4">
            <Stat label="Gross profit" value={fmtUsd(s.gross_profit_usd)} tone="pos" />
            <Stat label="Gross loss" value={fmtUsd(s.gross_loss_usd)} tone="neg" />
            <Stat label="Avg win" value={fmtUsd(s.avg_win_usd)} tone="pos" />
            <Stat label="Avg loss" value={fmtUsd(s.avg_loss_usd)} tone="neg" />
            <Stat label="Best trade" value={fmtUsdSigned(s.best_trade_usd)} tone="pos" />
            <Stat label="Worst trade" value={fmtUsdSigned(s.worst_trade_usd)} tone="neg" />
            <Stat label="Wins / losses" value={`${s.wins} / ${s.losses}`} />
            <Stat label="Closed" value={String(s.closed_trades)} />
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: "pos" | "neg" }): JSX.Element {
  return (
    <div className="flex items-baseline justify-between border-b border-border/40 py-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={`tnum font-medium ${tone === "pos" ? "text-pos" : tone === "neg" ? "text-neg" : ""}`}>{value}</span>
    </div>
  );
}
